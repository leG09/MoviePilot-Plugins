import logging
import grpc
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional
from collections import defaultdict
try:
    from typing_extensions import Annotated
except ImportError:
    # 兼容性处理：如果typing_extensions不可用，使用typing
    from typing import Annotated

from app.plugins import _PluginBase
from app.core.config import settings
from app.core.security import verify_apikey
from app.schemas import Notification, NotificationType, ContentType
from app.chain.transfer import TransferChain
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfoPath
from app.schemas.types import MediaType

logger = logging.getLogger(__name__)


class CloudDriveWebhook(_PluginBase):
    """CloudDrive目录刷新Webhook插件 - 纯Python gRPC版本"""
    
    # 插件信息
    plugin_name = "CloudDriveWebhook"
    plugin_desc = "接收文件路径，通过gRPC直接调用CloudDrive API刷新上一级目录"
    plugin_icon = "clouddrive.png"
    plugin_color = "#00BFFF"
    plugin_version = "2.1"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "clouddrivewebhook"
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self._enabled = config.get("enabled", False) if config else False
        self._api_token = config.get("api_token", "") if config else ""
        self._send_notification = config.get("send_notification", True) if config else True
        self._server_addr = config.get("server_addr", "") if config else ""
        self._username = config.get("username", "") if config else ""
        self._password = config.get("password", "") if config else ""
        self._use_ssl = config.get("use_ssl", False) if config else False
        self._path_mappings = config.get("path_mappings", "") if config else ""
        
        # gRPC相关
        self._channel = None
        self._stub = None
        self._token = None
        self._login_time = 0
        self._login_lock = threading.Lock()
        
        # 请求合并相关
        self._refresh_queue = defaultdict(list)
        self._refresh_lock = threading.Lock()
        self._refresh_thread = None
        self._stop_refresh_thread = False
        
        # 启动刷新线程
        self._start_refresh_thread()
        
        logger.info(f"CloudDrive Webhook插件初始化完成，启用状态: {self._enabled}")

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> list:
        """获取插件命令"""
        return []

    def get_api(self) -> list:
        """获取API接口"""
        return [
            {
                "path": "/clouddrive_webhook",
                "endpoint": self.clouddrive_webhook,
                "methods": ["POST"],
                "summary": "CloudDrive目录刷新Webhook",
                "description": "接收文件路径，通过gRPC直接刷新该文件所在的上一级目录"
            },
            {
                "path": "/test_connection",
                "endpoint": self.test_connection,
                "methods": ["GET"],
                "summary": "测试CloudDrive连接",
                "description": "测试CloudDrive服务器连接和登录"
            }
        ]

    def get_form(self) -> tuple:
        """获取配置表单"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                            "color": "primary"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "send_notification",
                                            "label": "发送入库通知",
                                            "color": "primary"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "api_token",
                                            "label": "API Token",
                                            "placeholder": "留空使用系统默认Token",
                                            "type": "password",
                                            "show_password_toggle": True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "server_addr",
                                            "label": "CloudDrive服务器地址",
                                            "placeholder": "host:port，例如: 2.56.98.127:19798"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "use_ssl",
                                            "label": "使用SSL/TLS",
                                            "color": "primary"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "username",
                                            "label": "用户名",
                                            "placeholder": "CloudDrive用户名"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "password",
                                            "label": "密码",
                                            "placeholder": "CloudDrive密码",
                                            "type": "password",
                                            "show_password_toggle": True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射配置",
                                            "placeholder": "每行一个映射，格式：源路径 => 目标路径\n例如：\n/gd5/media/ => /GoogleDrive2/media/\n/gd18/media/ => /GoogleDrive18/media/",
                                            "rows": 5,
                                            "hint": "用于将接收到的文件路径映射到实际的CloudDrive路径"
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "api_token": "",
            "send_notification": True,
            "server_addr": "",
            "username": "",
            "password": "",
            "use_ssl": False,
            "path_mappings": ""
        }

    def get_page(self) -> list:
        """获取插件页面"""
        return []

    def stop_service(self):
        """停止插件"""
        # 停止刷新线程
        self._stop_refresh_thread = True
        if self._refresh_thread and self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=5)
        
        # 关闭gRPC连接
        if self._channel:
            self._channel.close()
            self._channel = None
            self._stub = None

    def _start_refresh_thread(self):
        """启动刷新线程"""
        if self._refresh_thread is None or not self._refresh_thread.is_alive():
            self._refresh_thread = threading.Thread(target=self._refresh_worker, daemon=True)
            self._refresh_thread.start()
            logger.info("刷新工作线程已启动")

    def _refresh_worker(self):
        """刷新工作线程"""
        while not self._stop_refresh_thread:
            try:
                # 处理队列中的刷新请求
                with self._refresh_lock:
                    current_time = time.time()
                    directories_to_refresh = []
                    directory_files = {}  # 保存每个目录的文件信息
                    
                    for directory, requests in self._refresh_queue.items():
                        if requests:  # 如果有请求
                            # 取最新的请求时间
                            latest_request_time = max(req['timestamp'] for req in requests)
                            # 如果距离最新请求超过2秒，则执行刷新
                            if current_time - latest_request_time >= 2.0:
                                directories_to_refresh.append(directory)
                                # 保存该目录的文件信息
                                directory_files[directory] = requests.copy()
                                # 清空该目录的请求队列
                                self._refresh_queue[directory].clear()
                
                # 执行刷新
                for directory in directories_to_refresh:
                    try:
                        logger.info(f"执行合并刷新: {directory}")
                        result = self._refresh_directory_via_grpc(directory)
                        if result["success"]:
                            logger.info(f"目录 {directory} 刷新成功")
                            
                            # 刷新成功后，检查该目录下的所有文件并发送通知
                            if self._send_notification and directory in directory_files:
                                self._send_notifications_for_files(directory_files[directory])
                        else:
                            logger.error(f"目录 {directory} 刷新失败: {result['message']}")
                    except Exception as e:
                        logger.error(f"刷新目录 {directory} 时发生错误: {str(e)}")
                
                # 休眠1秒
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"刷新工作线程发生错误: {str(e)}")
                time.sleep(5)  # 发生错误时等待更长时间

    def _map_file_path(self, file_path: str) -> str:
        """
        映射文件路径
        
        Args:
            file_path: 原始文件路径
            
        Returns:
            str: 映射后的文件路径
        """
        if not self._path_mappings:
            logger.info(f"未配置路径映射，使用原始路径: {file_path}")
            return file_path
        
        mapped_path = file_path
        logger.info(f"开始路径映射，原始路径: {file_path}")
        logger.info(f"当前路径映射配置: {self._path_mappings}")
        
        # 解析路径映射配置
        for line in self._path_mappings.strip().split('\n'):
            line = line.strip()
            if not line or '=>' not in line:
                continue
            
            try:
                source_path, target_path = line.split('=>', 1)
                source_path = source_path.strip()
                target_path = target_path.strip()
                
                logger.info(f"检查映射规则: {source_path} => {target_path}")
                
                if file_path.startswith(source_path):
                    mapped_path = file_path.replace(source_path, target_path, 1)
                    logger.info(f"路径映射成功: {file_path} => {mapped_path}")
                    break
                else:
                    logger.info(f"路径不匹配: {file_path} 不以 {source_path} 开头")
            except Exception as e:
                logger.warning(f"解析路径映射失败: {line}, 错误: {str(e)}")
        
        if mapped_path == file_path:
            logger.warning(f"路径映射失败，使用原始路径: {file_path}")
        else:
            logger.info(f"最终映射路径: {mapped_path}")
        
        return mapped_path

    def clouddrive_webhook(self, file_path: str, apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        CloudDrive目录刷新Webhook
        
        Args:
            file_path: 文件路径
            apikey: API密钥（通过query参数或X-API-KEY头传递）
            
        Returns:
            Dict: 处理结果
        """
        try:
            logger.info(f"接收到CloudDrive刷新请求：{file_path}")
            
            # 验证文件路径
            if not file_path or not file_path.strip():
                return {"success": False, "message": "文件路径不能为空"}
            
            # 映射文件路径
            logger.info(f"原始文件路径: {file_path}")
            mapped_file_path = self._map_file_path(file_path.strip())
            logger.info(f"映射后文件路径: {mapped_file_path}")
            
            # 获取文件路径对象
            file_path_obj = Path(mapped_file_path)
            
            # 获取上一级目录
            parent_dir = file_path_obj.parent
            if not parent_dir or str(parent_dir) == ".":
                return {"success": False, "message": "无法获取有效的上级目录"}
            
            parent_dir_str = str(parent_dir)
            logger.info(f"准备刷新目录：{parent_dir_str}")
            
            # 添加到刷新队列
            logger.info(f"准备将目录 {parent_dir_str} 添加到刷新队列")
            with self._refresh_lock:
                self._refresh_queue[parent_dir_str].append({
                    'timestamp': time.time(),
                    'original_path': file_path,
                    'mapped_path': mapped_file_path
                })
                logger.info(f"目录 {parent_dir_str} 已添加到刷新队列，当前队列长度: {len(self._refresh_queue[parent_dir_str])}")
            
            # 注意：通知将在目录刷新完成后发送
            
            return {
                "success": True,
                "message": f"目录 {parent_dir_str} 已加入刷新队列",
                "queued_directory": parent_dir_str,
                "original_file": file_path,
                "mapped_file": mapped_file_path
            }
                
        except Exception as e:
            import traceback
            error_msg = f"处理CloudDrive刷新请求时发生错误：{str(e)}\n{traceback.format_exc()}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def _get_grpc_stub(self):
        """获取gRPC stub"""
        try:
            # 检查通道是否需要重新创建
            need_new_channel = False
            if self._channel is None:
                need_new_channel = True
            else:
                # 检查通道状态
                try:
                    # 尝试获取通道状态
                    state = self._channel.get_state(try_to_connect=True)
                    if state == grpc.ChannelConnectivity.SHUTDOWN:
                        need_new_channel = True
                except Exception:
                    need_new_channel = True
            
            if need_new_channel:
                # 关闭旧通道
                if self._channel:
                    try:
                        self._channel.close()
                    except Exception:
                        pass
                
                # 创建新的gRPC通道
                if self._use_ssl:
                    self._channel = grpc.secure_channel(self._server_addr, grpc.ssl_channel_credentials())
                else:
                    self._channel = grpc.insecure_channel(self._server_addr)
                
                # 导入protobuf生成的模块
                try:
                    # 尝试导入已生成的protobuf模块
                    import sys
                    import os
                    
                    # 添加当前插件目录到Python路径
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    if current_dir not in sys.path:
                        sys.path.insert(0, current_dir)
                    
                    import clouddrive.CloudDrive_pb2_grpc as CloudDrive_grpc_pb2_grpc
                    self._stub = CloudDrive_grpc_pb2_grpc.CloudDriveFileSrvStub(self._channel)
                    logger.info(f"成功创建gRPC连接: {self._server_addr}")
                except ImportError as e:
                    # 如果protobuf模块不存在，返回错误
                    logger.error(f"未找到CloudDrive protobuf模块: {str(e)}")
                    logger.error("请确保protobuf文件已正确生成")
                    return None
            
            return self._stub
        except Exception as e:
            logger.error(f"创建gRPC stub失败: {str(e)}")
            return None



    def _login_to_clouddrive(self):
        """登录到CloudDrive（带缓存）"""
        try:
            # 检查是否已经登录且token未过期（30分钟有效期）
            current_time = time.time()
            if (self._token and self._login_time > 0 and 
                current_time - self._login_time < 1800):  # 30分钟
                logger.info("使用缓存的登录状态")
                return True, "使用缓存的登录状态"
            
            with self._login_lock:
                # 双重检查，避免重复登录
                if (self._token and self._login_time > 0 and 
                    current_time - self._login_time < 1800):
                    return True, "使用缓存的登录状态"
                
                # 验证配置
                if not self._server_addr:
                    return False, "服务器地址未配置"
                if not self._username or not self._password:
                    return False, "用户名或密码未配置"
                
                logger.info(f"尝试连接到CloudDrive服务器: {self._server_addr}")
                
                stub = self._get_grpc_stub()
                if not stub:
                    return False, "无法创建gRPC连接"
                
                # 导入protobuf消息
                try:
                    import sys
                    import os
                    
                    # 添加当前插件目录到Python路径
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    if current_dir not in sys.path:
                        sys.path.insert(0, current_dir)
                    
                    import clouddrive.CloudDrive_pb2 as CloudDrive_pb2
                except ImportError as e:
                    logger.error(f"导入CloudDrive protobuf模块失败: {str(e)}")
                    return False, "未找到CloudDrive protobuf模块"
                
                # 创建登录请求
                login_request = CloudDrive_pb2.UserLoginRequest(
                    userName=self._username,
                    password=self._password,
                    synDataToCloud=False
                )
                
                logger.info(f"正在登录用户: {self._username}")
                
                # 调用登录API
                response = stub.Login(login_request)
                
                if response.success:
                    self._token = "logged_in"  # 简化token管理
                    self._login_time = current_time
                    logger.info("CloudDrive登录成功")
                    return True, "登录成功"
                else:
                    logger.error(f"CloudDrive登录失败: {response.errorMessage}")
                    return False, response.errorMessage
                    
        except Exception as e:
            logger.error(f"登录CloudDrive时发生错误: {str(e)}")
            return False, str(e)

    def _refresh_directory_via_grpc(self, directory_path: str) -> Dict[str, Any]:
        """
        通过gRPC刷新指定目录
        
        Args:
            directory_path: 要刷新的目录路径
            
        Returns:
            Dict: 执行结果
        """
        try:
            # 首先登录
            login_success, login_message = self._login_to_clouddrive()
            if not login_success:
                return {"success": False, "message": f"登录失败: {login_message}"}
            
            stub = self._get_grpc_stub()
            if not stub:
                return {"success": False, "message": "无法创建gRPC连接"}
            
            # 导入protobuf消息
            try:
                import sys
                import os
                
                # 添加当前插件目录到Python路径
                current_dir = os.path.dirname(os.path.abspath(__file__))
                if current_dir not in sys.path:
                    sys.path.insert(0, current_dir)
                
                import clouddrive.CloudDrive_pb2 as CloudDrive_pb2
            except ImportError as e:
                logger.error(f"导入CloudDrive protobuf模块失败: {str(e)}")
                return {"success": False, "message": "未找到CloudDrive protobuf模块"}
            
            # 创建目录刷新请求（通过GetSubFiles强制刷新）
            refresh_request = CloudDrive_pb2.ListSubFileRequest(
                path=directory_path,
                forceRefresh=True,
                checkExpires=True
            )
            
            logger.info(f"发送gRPC刷新请求: {directory_path}")
            
            # 调用GetSubFiles API进行刷新
            response_stream = stub.GetSubFiles(refresh_request)
            
            # 读取响应流
            file_count = 0
            for response in response_stream:
                if hasattr(response, 'subFiles'):
                    file_count += len(response.subFiles)
            
            logger.info(f"目录刷新完成，发现 {file_count} 个文件")
            
            return {
                "success": True,
                "message": f"目录 {directory_path} 刷新成功，发现 {file_count} 个文件",
                "file_count": file_count
            }
                
        except grpc.RpcError as e:
            error_msg = f"gRPC调用失败: {e.code()} - {e.details()}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
        except Exception as e:
            error_msg = f"刷新目录时发生错误：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def test_connection(self, apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        测试CloudDrive连接
        
        Args:
            apikey: API密钥
            
        Returns:
            Dict: 测试结果
        """
        try:
            logger.info("开始测试CloudDrive连接")
            
            # 检查配置
            config_status = {
                "server_addr": bool(self._server_addr),
                "username": bool(self._username),
                "password": bool(self._password),
                "use_ssl": self._use_ssl,
                "path_mappings": self._path_mappings
            }
            
            if not self._server_addr or not self._username or not self._password:
                return {
                    "success": False,
                    "message": "配置不完整",
                    "config_status": config_status
                }
            
            # 测试gRPC连接
            stub = self._get_grpc_stub()
            if not stub:
                return {
                    "success": False,
                    "message": "无法创建gRPC连接",
                    "config_status": config_status
                }
            
            # 测试登录
            login_success, login_message = self._login_to_clouddrive()
            
            return {
                "success": login_success,
                "message": login_message,
                "config_status": config_status,
                "server_addr": self._server_addr
            }
                
        except Exception as e:
            import traceback
            error_msg = f"测试连接时发生错误：{str(e)}\n{traceback.format_exc()}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def _send_notifications_for_files(self, files_info: list):
        """
        为文件列表发送入库通知
        
        Args:
            files_info: 文件信息列表
        """
        try:
            logger.info(f"准备为 {len(files_info)} 个文件发送通知")
            
            for file_info in files_info:
                mapped_file_path = file_info.get('mapped_path', '')
                if mapped_file_path:
                    # 检查文件是否存在
                    file_path_obj = Path(mapped_file_path)
                    if file_path_obj.exists():
                        logger.info(f"文件存在，发送通知: {mapped_file_path}")
                        self._send_refresh_notification(mapped_file_path)
                    else:
                        logger.warning(f"文件不存在，跳过通知: {mapped_file_path}")
                else:
                    logger.warning(f"文件信息中缺少mapped_path: {file_info}")
                    
        except Exception as e:
            logger.error(f"发送文件通知时发生错误: {str(e)}")

    def _send_refresh_notification(self, file_path: str):
        """
        发送入库通知
        
        Args:
            file_path: 文件路径
        """
        try:
            transfer_chain = TransferChain()
            
            # 解析文件路径获取媒体信息
            file_path_obj = Path(file_path)
            meta = MetaInfoPath(file_path_obj)
            
            # 使用MediaChain识别媒体信息
            mediainfo = MediaChain().recognize_by_meta(meta)
            
            if not mediainfo:
                # 尝试按路径识别
                mediainfo = MediaChain().recognize_by_path(str(file_path_obj))
            
            if mediainfo:
                # 获取媒体标题
                title = f"{mediainfo.title_year} 入库成功！"
                # 获取媒体图片
                image = mediainfo.get_message_image()
                
                # 获取季集信息
                se_str = None
                if mediainfo.type == MediaType.TV and meta:
                    if meta.season and meta.episode_list:
                        se_str = f"{meta.season} E{','.join(map(str, meta.episode_list))}"
                    elif meta.season_episode:
                        se_str = meta.season_episode
                
                # 构建通知文本
                text = f"文件：{file_path_obj.name}\n位置：{file_path}"
                if se_str:
                    text += f"\n季集：{se_str}"
                
                logger.info(f"识别到媒体信息：{mediainfo.title_year}")
            else:
                # 如果无法识别媒体信息，使用文件名
                title = f"CloudDrive入库成功！"
                text = f"文件：{file_path_obj.name}\n位置：{file_path}"
                image = ""
                logger.warning(f"无法识别媒体信息，使用文件名：{file_path_obj.name}")
            
            # 参考transfer.py中的入库通知格式
            notification = Notification(
                mtype=NotificationType.Organize,
                ctype=ContentType.OrganizeSuccess,
                title=title,
                text=text,
                image=image,
                link=settings.MP_DOMAIN('#/history')
            )
            
            transfer_chain.post_message(notification)
            logger.info(f"已发送入库通知：{file_path_obj.name}")
            
        except Exception as e:
            logger.warning(f"发送入库通知失败：{str(e)}")
