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
from app.schemas.transfer import TransferInfo
from app.schemas.file import FileItem
from app.chain.transfer import TransferChain
from app.chain.media import MediaChain
from app.core.metainfo import MetaInfoPath
from app.schemas.types import MediaType
from app.log import logger


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
        self._check_file_exists = config.get("check_file_exists", True) if config else True
        self._server_addr = config.get("server_addr", "") if config else ""
        self._username = config.get("username", "") if config else ""
        self._password = config.get("password", "") if config else ""
        self._use_ssl = config.get("use_ssl", False) if config else False
        self._path_mappings = config.get("path_mappings", "") if config else ""
        
        # gRPC相关
        self._channel = None
        self._stub = None
        self._token = None
        self._token_time = 0
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
                "description": "接收JSON格式的文件列表，通过gRPC直接刷新文件所在的上一级目录。支持批量处理多个文件路径。"
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
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "check_file_exists",
                                            "label": "检查文件是否存在",
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "text": "检查文件是否存在：启用后会在发送通知前通过gRPC检查文件是否真实存在于CloudDrive中。如果文件不存在，将跳过通知发送。"
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
            "check_file_exists": True,
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
            self._token = None
            self._token_time = 0

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
                
                # 合并重合的目录
                merged_directories = self._merge_overlapping_directories(directories_to_refresh, directory_files)
                
                # 执行刷新
                for directory in merged_directories:
                    try:
                        logger.info(f"执行合并刷新: {directory}")
                        # 先尝试直接刷新
                        result = self._refresh_directory_via_grpc(directory)
                        # 若失败，先刷新父级目录链再重试一次
                        if not result.get("success"):
                            logger.warning(f"直接刷新失败，尝试先刷新父级链后重试: {directory}")
                            self._refresh_parent_chain(directory)
                            result = self._refresh_directory_via_grpc(directory)
                        if result["success"]:
                            logger.info(f"目录 {directory} 刷新成功")
                            
                            # 刷新成功后，检查该目录下的所有文件并发送通知
                            if self._send_notification and directory in directory_files:
                                logger.info(f"目录刷新成功，准备发送 {len(directory_files[directory])} 个文件的通知")
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

    def _merge_overlapping_directories(self, directories: list, directory_files: dict) -> list:
        """
        合并重合的目录，只保留最顶层的目录
        
        Args:
            directories: 要刷新的目录列表
            directory_files: 每个目录对应的文件信息
            
        Returns:
            list: 合并后的目录列表
        """
        if not directories:
            return []
        
        logger.info(f"开始合并重合目录，原始目录列表: {directories}")
        
        # 按路径长度排序，确保父目录在前
        sorted_directories = sorted(directories, key=lambda x: len(x.split('/')))
        
        merged_directories = []
        merged_files = {}
        
        for directory in sorted_directories:
            # 检查当前目录是否已经被其他目录包含
            is_contained = False
            for existing_dir in merged_directories:
                if directory.startswith(existing_dir + '/') or directory == existing_dir:
                    is_contained = True
                    # 将当前目录的文件信息合并到父目录
                    if directory in directory_files:
                        if existing_dir not in merged_files:
                            merged_files[existing_dir] = []
                        merged_files[existing_dir].extend(directory_files[directory])
                        logger.info(f"目录 {directory} 被 {existing_dir} 包含，文件信息已合并")
                    break
            
            if not is_contained:
                merged_directories.append(directory)
                if directory in directory_files:
                    merged_files[directory] = directory_files[directory]
                logger.info(f"添加独立目录: {directory}")
        
        # 更新directory_files为合并后的结果
        directory_files.clear()
        directory_files.update(merged_files)
        
        logger.info(f"目录合并完成，最终目录列表: {merged_directories}")
        logger.info(f"合并后的文件信息: {list(directory_files.keys())}")
        
        return merged_directories

    def _map_file_path(self, file_path: str) -> tuple[str, bool]:
        """
        映射文件路径
        
        Args:
            file_path: 原始文件路径
            
        Returns:
            tuple: (映射后的文件路径, 是否成功映射)
        """
        if not self._path_mappings:
            logger.info(f"未配置路径映射，使用原始路径: {file_path}")
            return file_path, True
        
        mapped_path = file_path
        logger.info(f"开始路径映射，原始路径: {file_path}")
        logger.info(f"当前路径映射配置: {self._path_mappings}")
        
        # 解析路径映射配置
        mapping_applied = False
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
                    mapping_applied = True
                    break
                else:
                    logger.debug(f"路径不匹配: {file_path} 不以 {source_path} 开头")
            except Exception as e:
                logger.warning(f"解析路径映射失败: {line}, 错误: {str(e)}")
        
        if not mapping_applied:
            logger.warning(f"路径映射失败，跳过处理: {file_path}")
            return file_path, False
        else:
            logger.info(f"最终映射路径: {mapped_path}")
            return mapped_path, True

    def clouddrive_webhook(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        CloudDrive目录刷新Webhook
        
        Args:
            request_data: 请求数据，包含data数组，每个元素有source_file字段；支持path_type参数："mapping"(默认)/"source"
            apikey: API密钥（通过query参数或X-API-KEY头传递）
            
        Returns:
            Dict: 处理结果
        """
        try:
            logger.info(f"接收到CloudDrive刷新请求：{request_data}")
            
            # 验证请求数据
            if not request_data or not isinstance(request_data, dict):
                return {"success": False, "message": "请求数据格式错误"}
            
            # 获取data数组
            data = request_data.get("data", [])
            if not data or not isinstance(data, list):
                return {"success": False, "message": "data字段为空或格式错误"}
            
            # 刷新模式：mapping(默认) 或 source
            path_type = str(request_data.get("path_type", "mapping")).strip().lower()
            if path_type not in ("mapping", "source"):
                path_type = "mapping"
            logger.info(f"刷新模式 path_type: {path_type}")

            # 如果是 source 模式：基于源路径层级刷新（按映射前缀起点逐级到父目录），不发送通知
            if path_type == "source":
                # 收集需要刷新的(映射后的)目录集合
                target_directories: set[str] = set()
                failed_files = []

                for i, item in enumerate(data):
                    try:
                        source_file = item.get("source_file", "")
                        if not source_file:
                            logger.warning(f"第 {i+1} 个文件缺少source_file字段: {item}")
                            failed_files.append({"index": i, "reason": "missing_source_file"})
                            continue

                        # 仅到父目录为止
                        file_parent = str(Path(source_file).parent)

                        # 找到源路径映射前缀
                        source_prefix = self._get_source_path_prefix(source_file)
                        if not source_prefix:
                            logger.warning(f"未找到源路径映射前缀，跳过: {source_file}")
                            failed_files.append({"index": i, "source_file": source_file, "reason": "no_source_prefix"})
                            continue

                        # 基于源前缀构建逐级目录列表（源路径）
                        source_levels = self._build_hierarchy_from_prefix(file_parent, source_prefix)

                        # 将每个源目录映射为目标目录以适配CloudDrive gRPC
                        for src_dir in source_levels:
                            mapped_dir, ok = self._map_file_path(src_dir)
                            if ok:
                                target_directories.add(mapped_dir.rstrip('/'))
                                logger.info(f"加入刷新(源→映射): {src_dir} → {mapped_dir}")
                            else:
                                logger.warning(f"目录映射失败，跳过: {src_dir}")
                                failed_files.append({"index": i, "source_dir": src_dir, "reason": "map_dir_failed"})
                    except Exception as e:
                        logger.error(f"处理源路径目录时发生错误: {str(e)}")
                        failed_files.append({"index": i, "reason": "processing_error", "error": str(e)})

                # 合并重合目录并刷新（不发送通知）
                merged_dirs = self._merge_overlapping_directories(sorted(list(target_directories)), {})
                for directory in merged_dirs:
                    try:
                        logger.info(f"执行源模式合并刷新: {directory}")
                        _ = self._refresh_directory_via_grpc(directory)
                    except Exception as e:
                        logger.error(f"刷新目录 {directory} 时发生错误: {str(e)}")

                return {
                    "success": True,
                    "message": f"源模式刷新完成，目录数: {len(merged_dirs)}，失败: {len(failed_files)}",
                    "path_type": path_type,
                    "refreshed_directories": merged_dirs,
                    "failed_files": failed_files
                }

            logger.info(f"接收到 {len(data)} 个文件路径")
            
            # 处理每个文件路径
            processed_files = []
            skipped_files = []
            failed_files = []
            
            for i, item in enumerate(data):
                try:
                    # 获取source_file字段
                    source_file = item.get("source_file", "")
                    if not source_file:
                        logger.warning(f"第 {i+1} 个文件缺少source_file字段: {item}")
                        failed_files.append({"index": i, "reason": "missing_source_file"})
                        continue
                    
                    logger.info(f"处理第 {i+1} 个文件: {source_file}")
                    
                    # 映射文件路径
                    mapped_file_path, mapping_success = self._map_file_path(source_file.strip())
                    
                    # 如果路径映射失败，则跳过
                    if not mapping_success:
                        logger.info(f"路径映射失败，跳过处理: {source_file}")
                        skipped_files.append({
                            "index": i,
                            "source_file": source_file,
                            "reason": "path_mapping_failed"
                        })
                        continue
                    
                    # 获取文件路径对象
                    file_path_obj = Path(mapped_file_path)
                    
                    # 获取上一级目录
                    parent_dir = file_path_obj.parent
                    if not parent_dir or str(parent_dir) == ".":
                        logger.warning(f"无法获取有效的上级目录: {mapped_file_path}")
                        failed_files.append({
                            "index": i,
                            "source_file": source_file,
                            "reason": "invalid_parent_directory"
                        })
                        continue
                    
                    parent_dir_str = str(parent_dir)
                    
                    # 添加到刷新队列
                    with self._refresh_lock:
                        self._refresh_queue[parent_dir_str].append({
                            'timestamp': time.time(),
                            'original_path': source_file,
                            'mapped_path': mapped_file_path
                        })
                    
                    processed_files.append({
                        "index": i,
                        "source_file": source_file,
                        "mapped_file": mapped_file_path,
                        "parent_directory": parent_dir_str
                    })
                    
                    logger.info(f"文件 {source_file} 已加入刷新队列，父目录: {parent_dir_str}")
                    
                except Exception as e:
                    logger.error(f"处理第 {i+1} 个文件时发生错误: {str(e)}")
                    failed_files.append({
                        "index": i,
                        "source_file": item.get("source_file", ""),
                        "reason": "processing_error",
                        "error": str(e)
                    })
            
            # 统计结果
            total_files = len(data)
            success_count = len(processed_files)
            skipped_count = len(skipped_files)
            failed_count = len(failed_files)
            
            logger.info(f"处理完成 - 总计: {total_files}, 成功: {success_count}, 跳过: {skipped_count}, 失败: {failed_count}")
            
            # 注意：通知将在目录刷新完成后发送
            
            return {
                "success": True,
                "message": f"批量处理完成 - 总计: {total_files}, 成功: {success_count}, 跳过: {skipped_count}, 失败: {failed_count}",
                "summary": {
                    "total_files": total_files,
                    "success_count": success_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count
                },
                "processed_files": processed_files,
                "skipped_files": skipped_files,
                "failed_files": failed_files,
                "path_type": path_type
            }
                
        except Exception as e:
            import traceback
            error_msg = f"处理CloudDrive刷新请求时发生错误：{str(e)}\n{traceback.format_exc()}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def _get_grpc_stub(self, force_recreate=False):
        """获取gRPC stub"""
        try:
            # 如果强制重建或者通道无效，则重新创建
            need_new_channel = force_recreate
            
            if not need_new_channel:
                # 检查通道是否需要重新创建
                if self._channel is None or self._stub is None:
                    need_new_channel = True
                    logger.info("gRPC通道或stub为空，需要重新创建")
                else:
                    # 简单检查：如果通道和stub都存在，就认为是可用的
                    # 如果实际调用时遇到错误，会在错误处理中重新创建
                    logger.debug("gRPC通道和stub都存在，跳过重新创建")
            
            if need_new_channel:
                logger.info("开始重新创建gRPC连接")
                
                # 关闭旧通道
                if self._channel:
                    try:
                        logger.debug("关闭旧的gRPC通道")
                        self._channel.close()
                    except Exception as e:
                        logger.warning(f"关闭旧通道时发生错误: {str(e)}")
                
                # 重置状态
                self._channel = None
                self._stub = None
                
                # 验证服务器地址
                if not self._server_addr:
                    logger.error("服务器地址未配置")
                    return None
                
                # 创建新的gRPC通道
                logger.info(f"创建新的gRPC通道: {self._server_addr}")
                try:
                    if self._use_ssl:
                        self._channel = grpc.secure_channel(self._server_addr, grpc.ssl_channel_credentials())
                    else:
                        self._channel = grpc.insecure_channel(self._server_addr)
                    
                    # 等待通道准备就绪（最多等待5秒）
                    grpc.channel_ready_future(self._channel).result(timeout=5)
                    logger.info("gRPC通道创建成功并已就绪")
                    
                except Exception as e:
                    logger.error(f"创建gRPC通道失败: {str(e)}")
                    self._channel = None
                    return None
                
                # 导入protobuf模块并创建stub
                try:
                    import sys
                    import os
                    
                    # 添加当前插件目录到Python路径
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    if current_dir not in sys.path:
                        sys.path.insert(0, current_dir)
                    
                    import clouddrive.CloudDrive_pb2_grpc as CloudDrive_grpc_pb2_grpc
                    self._stub = CloudDrive_grpc_pb2_grpc.CloudDriveFileSrvStub(self._channel)
                    logger.info(f"成功创建gRPC stub: {self._server_addr}")
                    
                except ImportError as e:
                    logger.error(f"未找到CloudDrive protobuf模块: {str(e)}")
                    self._channel = None
                    self._stub = None
                    return None
            
            return self._stub
            
        except Exception as e:
            logger.error(f"创建gRPC stub失败: {str(e)}")
            # 重置状态
            self._channel = None
            self._stub = None
            return None



    def _login_to_clouddrive(self):
        """登录到CloudDrive（基于token的认证）"""
        try:
            current_time = time.time()
            
            # 检查现有token是否仍然有效（30分钟有效期）
            if (self._token and self._token_time > 0 and 
                current_time - self._token_time < 1800):  # 30分钟
                logger.info("使用现有token")
                return True, "使用现有token"
            
            logger.info("token过期或不存在，需要重新获取token")
            
            with self._login_lock:
                # 双重检查，避免重复登录
                if (self._token and self._token_time > 0 and 
                    current_time - self._token_time < 1800):
                    return True, "使用现有token"
                
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
                    import clouddrive.CloudDrive_pb2 as CloudDrive_pb2
                except ImportError as e:
                    logger.error(f"导入CloudDrive protobuf模块失败: {str(e)}")
                    return False, "未找到CloudDrive protobuf模块"
                
                # 创建token请求
                token_request = CloudDrive_pb2.GetTokenRequest(
                    userName=self._username,
                    password=self._password
                )
                
                logger.info(f"正在获取token，用户: {self._username}")
                
                # 调用GetToken API
                response = stub.GetToken(token_request)
                
                logger.info(f"Token响应: success={response.success}")
                
                if response.success:
                    self._token = response.token
                    self._token_time = current_time
                    logger.info("CloudDrive token获取成功")
                    return True, "token获取成功"
                else:
                    error_message = getattr(response, 'errorMessage', 'Unknown error')
                    logger.error(f"CloudDrive token获取失败: {error_message}")
                    return False, error_message
                    
        except grpc.RpcError as e:
            if "Cannot invoke RPC on closed channel" in str(e):
                logger.warning("gRPC通道已关闭，尝试重新创建连接")
                # 强制重新创建连接
                stub = self._get_grpc_stub(force_recreate=True)
                if stub:
                    logger.info("gRPC连接重新创建成功，重试登录")
                    # 重新尝试登录（仅一次，避免无限递归）
                    try:
                        # 导入protobuf消息
                        import clouddrive.CloudDrive_pb2 as CloudDrive_pb2
                        
                        # 创建登录请求
                        login_request = CloudDrive_pb2.UserLoginRequest(
                            userName=self._username,
                            password=self._password,
                            synDataToCloud=False
                        )
                        
                        # 调用登录API
                        response = stub.Login(login_request)
                        
                        if response.success:
                            self._session_active = True
                            self._session_time = time.time()
                            logger.info("CloudDrive重新登录成功")
                            return True, "重新登录成功"
                        else:
                            error_message = getattr(response, 'errorMessage', 'Unknown error')
                            if "already login" in error_message.lower():
                                self._session_active = True
                                self._session_time = time.time()
                                return True, "已经登录"
                            else:
                                return False, error_message
                    except Exception as retry_e:
                        logger.error(f"重试登录失败: {str(retry_e)}")
                        return False, f"重试登录失败: {str(retry_e)}"
                else:
                    logger.error("重新创建gRPC连接失败")
                    return False, "重新创建gRPC连接失败"
            else:
                logger.error(f"登录CloudDrive时发生gRPC错误: {str(e)}")
                return False, str(e)
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
            
            # 创建带token的metadata
            metadata = [('authorization', f'Bearer {self._token}')]
            
            # 调用GetSubFiles API进行刷新
            response_stream = stub.GetSubFiles(refresh_request, metadata=metadata)
            
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
            if "Cannot invoke RPC on closed channel" in str(e):
                logger.warning("gRPC通道已关闭，尝试重新创建连接并重试")
                # 强制重新创建连接
                stub = self._get_grpc_stub(force_recreate=True)
                if stub:
                    # 重新尝试登录
                    login_success, login_message = self._login_to_clouddrive()
                    if login_success:
                        # 重新尝试刷新目录（仅一次）
                        try:
                            import clouddrive.CloudDrive_pb2 as CloudDrive_pb2
                            refresh_request = CloudDrive_pb2.ListSubFileRequest(
                                path=directory_path,
                                forceRefresh=True,
                                checkExpires=True
                            )
                            response_stream = stub.GetSubFiles(refresh_request)
                            file_count = 0
                            for response in response_stream:
                                if hasattr(response, 'subFiles'):
                                    file_count += len(response.subFiles)
                            logger.info(f"重试刷新成功，发现 {file_count} 个文件")
                            return {
                                "success": True,
                                "message": f"重试刷新成功，目录 {directory_path} 发现 {file_count} 个文件",
                                "file_count": file_count
                            }
                        except Exception as retry_e:
                            logger.error(f"重试刷新失败: {str(retry_e)}")
                            return {"success": False, "message": f"重试刷新失败: {str(retry_e)}"}
                    else:
                        return {"success": False, "message": f"重新登录失败: {login_message}"}
                else:
                    return {"success": False, "message": "重新创建gRPC连接失败"}
            
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

    def _check_server_login_status(self) -> bool:
        """
        检查服务器端登录状态（使用token）
        
        Returns:
            bool: 是否已登录
        """
        try:
            if not self._token:
                return False
            
            stub = self._get_grpc_stub()
            if not stub:
                return False
            
            # 尝试调用一个需要登录的API来检查状态
            try:
                from google.protobuf import empty_pb2
                
                # 创建带token的metadata
                metadata = [('authorization', f'Bearer {self._token}')]
                
                # 尝试获取账户状态
                response = stub.GetAccountStatus(empty_pb2.Empty(), metadata=metadata)
                logger.info("服务器端登录状态检查成功")
                return True
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                    logger.info("token无效或已过期")
                    return False
                else:
                    logger.warning(f"检查登录状态时发生错误: {e.code()} - {e.details()}")
                    return False
            except Exception as e:
                logger.warning(f"检查登录状态时发生异常: {str(e)}")
                return False
                
        except Exception as e:
            logger.warning(f"检查服务器登录状态失败: {str(e)}")
            return False

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
                original_file_path = file_info.get('original_path', '')
                
                # 优先使用映射路径，如果不存在则使用原始路径
                # 注意：这里mapped_file_path已经确保是有效的，因为映射失败的文件不会进入队列
                file_path_to_check = mapped_file_path if mapped_file_path else original_file_path
                
                if file_path_to_check:
                    # 根据配置决定是否检查文件存在
                    if self._check_file_exists:
                        # 先刷新多个层级目录，然后检查文件是否存在，最多重试3次
                        file_exists = False
                        for retry_count in range(3):
                            # 刷新多个层级目录
                            refresh_success = self._refresh_multiple_directories(file_path_to_check)
                            
                            if refresh_success:
                                logger.info(f"目录刷新成功，开始检查文件: {file_path_to_check}")
                                file_exists = self._check_file_exists_via_grpc(file_path_to_check)
                            else:
                                logger.warning(f"目录刷新失败")
                                file_exists = False
                            
                            if file_exists:
                                logger.info(f"文件存在，发送通知: {file_path_to_check}")
                                self._send_refresh_notification(file_path_to_check)
                                break
                            else:
                                if retry_count < 2:  # 不是最后一次重试
                                    logger.info(f"文件不存在，等待5秒后重试 ({retry_count + 1}/3): {file_path_to_check}")
                                    time.sleep(5)  # 等待5秒后重试
                                else:
                                    logger.warning(f"文件不存在，跳过通知 (已重试3次): {file_path_to_check}")
                    else:
                        # 不检查文件存在，直接发送通知
                        logger.info(f"跳过文件存在检查，直接发送通知: {file_path_to_check}")
                        self._send_refresh_notification(file_path_to_check)
                else:
                    logger.warning(f"文件信息中缺少路径信息: {file_info}")
                    
        except Exception as e:
            logger.error(f"发送文件通知时发生错误: {str(e)}")

    def _refresh_multiple_directories(self, file_path: str) -> bool:
        """
        刷新多个层级目录
        
        Args:
            file_path: 文件路径
            
        Returns:
            bool: 是否成功刷新所有目录
        """
        try:
            logger.info(f"=== 开始刷新多个层级目录 ===")
            logger.info(f"文件路径: {file_path}")
            
            # 从路径映射配置中获取目标路径前缀
            target_path_prefix = self._get_target_path_prefix(file_path)
            if not target_path_prefix:
                logger.warning(f"无法从路径映射配置中获取目标路径前缀")
                return False
            
            logger.info(f"从路径映射获取的目标路径前缀: {target_path_prefix}")
            
            # 解析路径，获取需要刷新的目录层级
            path_parts = Path(file_path).parts
            
            # 找到目标路径前缀的位置
            prefix_parts = Path(target_path_prefix).parts
            prefix_index = -1
            
            # 在文件路径中查找目标路径前缀
            for i in range(len(path_parts) - len(prefix_parts) + 1):
                if path_parts[i:i+len(prefix_parts)] == prefix_parts:
                    prefix_index = i
                    break
            
            if prefix_index == -1:
                logger.warning(f"无法在文件路径中找到目标路径前缀: {target_path_prefix}")
                return False
            
            logger.info(f"找到目标路径前缀位置: {prefix_index}")
            
            # 构建需要刷新的目录列表
            directories_to_refresh = []
            
            # 从目标路径前缀开始，逐级构建目录
            current_path = target_path_prefix
            directories_to_refresh.append(current_path)
            
            # 逐级添加子目录
            for i in range(len(prefix_parts), len(path_parts) - 1):  # -1 是因为最后一个是文件名
                current_path = f"{current_path}/{path_parts[i]}"
                directories_to_refresh.append(current_path)
            
            logger.info(f"需要刷新的目录列表: {directories_to_refresh}")
            
            # 逐个刷新目录
            all_success = True
            for directory in directories_to_refresh:
                logger.info(f"刷新目录: {directory}")
                refresh_result = self._refresh_directory_via_grpc(directory)
                
                if refresh_result["success"]:
                    logger.info(f"✓ 目录刷新成功: {directory}")
                else:
                    logger.warning(f"✗ 目录刷新失败: {directory} - {refresh_result['message']}")
                    all_success = False
            
            logger.info(f"多层级目录刷新完成，总体结果: {'成功' if all_success else '失败'}")
            return all_success
            
        except Exception as e:
            logger.error(f"刷新多层级目录时发生错误: {str(e)}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
            return False

    def _refresh_parent_chain(self, directory_path: str) -> None:
        """
        逐级刷新父级目录链，从映射前缀一路刷新到指定目录的上级目录。
        仅做尽力而为的容错，不抛异常。
        """
        try:
            if not directory_path:
                return
            # 找到映射前缀（目标前缀）
            target_prefix = self._get_target_path_prefix(directory_path)
            if not target_prefix:
                logger.warning(f"无法确定目标路径前缀，跳过父级链刷新: {directory_path}")
                return
            dir_parent = str(Path(directory_path).parent)
            if not dir_parent or dir_parent == ".":
                return
            # 构建从前缀到父目录的层级
            try:
                parts = Path(dir_parent).parts
                prefix_parts = Path(target_prefix).parts
                start = None
                for i in range(len(parts) - len(prefix_parts) + 1):
                    if parts[i:i+len(prefix_parts)] == prefix_parts:
                        start = i
                        break
                if start is None:
                    logger.warning(f"父级链中未找到前缀: {target_prefix} in {dir_parent}")
                    return
                # 逐级刷新
                current = target_prefix
                self._refresh_directory_via_grpc(current)
                for j in range(start + len(prefix_parts), len(parts)):
                    current = f"{current}/{parts[j]}"
                    self._refresh_directory_via_grpc(current)
            except Exception as e:
                logger.warning(f"构建/刷新父级链失败: {str(e)}")
        except Exception:
            pass

    def _get_source_path_prefix(self, source_path: str) -> str:
        """
        从路径映射配置中获取与源路径匹配的源前缀（左侧前缀）
        
        Args:
            source_path: 源文件或目录路径
        Returns:
            str: 源路径前缀，未找到返回空字符串
        """
        try:
            if not self._path_mappings:
                logger.warning("未配置路径映射")
                return ""
            # 选择与source_path匹配且最长的源前缀
            best_match = ""
            for line in self._path_mappings.strip().split('\n'):
                line = line.strip()
                if not line or '=>' not in line:
                    continue
                try:
                    src_prefix, _ = line.split('=>', 1)
                    src_prefix = src_prefix.strip()
                    if source_path.startswith(src_prefix) and len(src_prefix) > len(best_match):
                        best_match = src_prefix
                except Exception as e:
                    logger.warning(f"解析路径映射失败: {line}, 错误: {str(e)}")
            if best_match:
                logger.info(f"找到源路径前缀: {best_match}")
            else:
                logger.warning(f"未匹配到源路径前缀: {source_path}")
            return best_match
        except Exception as e:
            logger.error(f"获取源路径前缀时发生错误: {str(e)}")
            return ""

    def _build_hierarchy_from_prefix(self, full_path: str, prefix: str) -> list[str]:
        """
        从给定前缀开始，逐级构建到full_path的所有层级目录列表（包含prefix与full_path）
        Args:
            full_path: 终点目录（通常为文件父目录）
            prefix: 起始前缀目录
        Returns:
            list[str]: 逐级目录列表
        """
        try:
            # 规范化，移除末尾斜杠
            full_path = full_path.rstrip('/')
            prefix = prefix.rstrip('/')
            if not full_path.startswith(prefix):
                # 若不包含，直接返回prefix与full_path的最小公共起点（退化为prefix）
                return [prefix]
            prefix_parts = Path(prefix).parts
            full_parts = Path(full_path).parts
            # 找到prefix在full中的起始索引
            start = None
            for i in range(len(full_parts) - len(prefix_parts) + 1):
                if full_parts[i:i+len(prefix_parts)] == prefix_parts:
                    start = i
                    break
            if start is None:
                return [prefix]
            # 从prefix到full逐级拼接
            result = [prefix]
            current = prefix
            for j in range(start + len(prefix_parts), len(full_parts)):
                current = f"{current}/{full_parts[j]}"
                result.append(current)
            return result
        except Exception as e:
            logger.error(f"构建目录层级时发生错误: {str(e)}")
            return [prefix]

    def _get_target_path_prefix(self, file_path: str) -> str:
        """
        从路径映射配置中获取目标路径前缀
        
        Args:
            file_path: 文件路径，用于确定使用哪个映射规则
            
        Returns:
            str: 目标路径前缀，如果获取失败则返回空字符串
        """
        try:
            if not self._path_mappings:
                logger.warning(f"未配置路径映射")
                return ""
            
            logger.info(f"解析路径映射配置: {self._path_mappings}")
            logger.info(f"查找文件路径对应的目标路径前缀: {file_path}")
            
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
                    
                    # 检查文件路径是否匹配这个目标路径
                    if file_path.startswith(target_path):
                        logger.info(f"找到匹配的目标路径: {target_path}")
                        return target_path
                        
                except Exception as e:
                    logger.warning(f"解析路径映射失败: {line}, 错误: {str(e)}")
            
            logger.warning(f"未找到匹配文件路径 {file_path} 的目标路径")
            return ""
            
        except Exception as e:
            logger.error(f"获取目标路径前缀时发生错误: {str(e)}")
            return ""

    def _check_file_exists_via_grpc(self, file_path: str) -> bool:
        """
        通过gRPC检查文件是否存在
        
        Args:
            file_path: 文件路径
            
        Returns:
            bool: 文件是否存在
        """
        try:
            logger.info(f"=== 开始gRPC文件存在检查 ===")
            logger.info(f"检查文件路径: {file_path}")
            
            # 首先登录
            login_success, login_message = self._login_to_clouddrive()
            if not login_success:
                logger.warning(f"登录失败，无法检查文件: {login_message}")
                return False
            
            logger.info(f"登录状态: {login_success}")
            
            stub = self._get_grpc_stub()
            if not stub:
                logger.warning("无法创建gRPC连接，无法检查文件")
                return False
            
            logger.info(f"gRPC stub创建成功")
            
            # 导入protobuf消息
            try:
                import clouddrive.CloudDrive_pb2 as CloudDrive_pb2
                logger.info(f"protobuf模块导入成功")
            except ImportError as e:
                logger.error(f"导入CloudDrive protobuf模块失败: {str(e)}")
                return False
            
            # 获取文件路径对象
            file_path_obj = Path(file_path)
            parent_path = str(file_path_obj.parent)
            file_name = file_path_obj.name
            
            logger.info(f"解析路径信息:")
            logger.info(f"  完整路径: {file_path}")
            logger.info(f"  父目录: {parent_path}")
            logger.info(f"  文件名: {file_name}")
            
            # 创建FindFileByPath请求
            find_request = CloudDrive_pb2.FindFileByPathRequest(
                parentPath=parent_path,
                path=file_name
            )
            
            logger.info(f"创建FindFileByPath请求:")
            logger.info(f"  parentPath: {find_request.parentPath}")
            logger.info(f"  path: {find_request.path}")
            
            # 创建带token的metadata
            metadata = [('authorization', f'Bearer {self._token}')]
            logger.info(f"创建metadata: {metadata}")
            
            # 调用FindFileByPath API
            logger.info(f"开始调用FindFileByPath API...")
            response = stub.FindFileByPath(find_request, metadata=metadata)
            
            logger.info(f"API调用完成，检查响应...")
            
            # 检查响应
            if response:
                logger.info(f"响应对象存在，类型: {type(response)}")
                logger.info(f"响应对象属性: {dir(response)}")
                
                if hasattr(response, 'fullPathName'):
                    logger.info(f"fullPathName属性存在: {response.fullPathName}")
                    if response.fullPathName:
                        logger.info(f"✓ 文件存在: {response.fullPathName}")
                        return True
                    else:
                        logger.warning(f"✗ fullPathName为空")
                else:
                    logger.warning(f"✗ 响应对象没有fullPathName属性")
                
                # 打印更多响应信息
                for attr in dir(response):
                    if not attr.startswith('_'):
                        try:
                            value = getattr(response, attr)
                            logger.info(f"响应属性 {attr}: {value}")
                        except Exception as e:
                            logger.info(f"响应属性 {attr}: 无法获取 ({str(e)})")
            else:
                logger.warning(f"✗ API返回空响应")
            
            logger.warning(f"✗ 文件不存在: {file_path}")
            return False
                
        except grpc.RpcError as e:
            logger.error(f"=== gRPC错误详情 ===")
            logger.error(f"错误代码: {e.code()}")
            logger.error(f"错误详情: {e.details()}")
            logger.error(f"错误消息: {str(e)}")
            
            if e.code() == grpc.StatusCode.NOT_FOUND:
                logger.info(f"✓ 文件不存在 (NOT_FOUND): {file_path}")
                return False
            elif e.code() == grpc.StatusCode.UNAUTHENTICATED:
                logger.warning(f"✗ 认证失败 (UNAUTHENTICATED): {e.details()}")
                return False
            elif e.code() == grpc.StatusCode.PERMISSION_DENIED:
                logger.warning(f"✗ 权限不足 (PERMISSION_DENIED): {e.details()}")
                return False
            elif e.code() == grpc.StatusCode.INVALID_ARGUMENT:
                logger.warning(f"✗ 参数无效 (INVALID_ARGUMENT): {e.details()}")
                return False
            else:
                logger.warning(f"✗ 其他gRPC错误 ({e.code()}): {e.details()}")
                return False
        except Exception as e:
            logger.error(f"=== 其他错误详情 ===")
            logger.error(f"错误类型: {type(e)}")
            logger.error(f"错误消息: {str(e)}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
            return False

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
                logger.info(f"识别到媒体信息：{mediainfo.title_year}")
                
                # 创建TransferInfo对象
                
                # 创建FileItem对象
                file_item = FileItem(
                    path=str(file_path_obj),
                    size=file_path_obj.stat().st_size if file_path_obj.exists() else 0
                )
                
                # 创建TransferInfo对象
                transfer_info = TransferInfo(
                    success=True,
                    fileitem=file_item,
                    file_count=1,
                    total_size=file_item.size,
                    transfer_type="move"
                )
                
                # 获取季集信息
                season_episode = None
                if mediainfo.type == MediaType.TV and meta:
                    if meta.season and meta.episode_list:
                        season_episode = f"{meta.season} E{','.join(map(str, meta.episode_list))}"
                    elif meta.season_episode:
                        season_episode = meta.season_episode
                
                # 使用TransferChain的send_transfer_message方法发送通知
                transfer_chain.send_transfer_message(
                    meta=meta,
                    mediainfo=mediainfo,
                    transferinfo=transfer_info,
                    season_episode=season_episode
                )
                
                logger.info(f"已发送入库通知：{mediainfo.title_year}")
            else:
                logger.warning(f"无法识别媒体信息，跳过通知：{file_path}")
            
        except Exception as e:
            logger.warning(f"发送入库通知失败：{str(e)}")
