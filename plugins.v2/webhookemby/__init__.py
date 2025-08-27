from typing import Any, List, Dict, Tuple, Optional, Union, Annotated
from pathlib import Path
import traceback

from app import schemas
from app.core.config import settings
from app.core.event import eventmanager
from app.core.security import verify_apikey
from app.log import logger
from app.plugins import _PluginBase
from app.chain.transfer import TransferChain
from app.chain.media import MediaChain
from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfoPath
from app.schemas import FileItem, TransferTask, TransferInfo, Notification
from app.schemas.types import MediaType, EventType, NotificationType


class WebhookEmby(_PluginBase):
    # 插件名称
    plugin_name = "Emby入库Webhook"
    # 插件描述
    plugin_desc = "通过Webhook接收文件路径并触发刮削和入库通知，适用于文件已在正确位置的场景。"
    # 插件图标
    plugin_icon = "Emby_A.png"
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "leGO9"
    # 作者主页
    author_url = "https://github.com/leG09"
    # 插件配置项ID前缀
    plugin_config_prefix = "webhookemby_"
    # 加载顺序
    plugin_order = 99
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _api_token = None
    _send_notification = True

    def init_plugin(self, config: dict = None):
        """
        初始化插件
        """
        if config:
            self._enabled = config.get("enabled", False)
            self._api_token = config.get("api_token", "")
            self._send_notification = config.get("send_notification", True)

    def get_state(self) -> bool:
        """
        获取插件状态
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        """
        return [{
            "path": "/emby_webhook",
            "endpoint": self.emby_webhook,
            "methods": ["POST"],
            "summary": "Emby入库Webhook",
            "description": "接收文件路径触发Emby入库整理",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'send_notification',
                                            'label': '发送入库通知',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'api_token',
                                            'label': 'API Token（可选，为空则使用系统Token）',
                                            'placeholder': '留空使用系统默认Token'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'text': 'Webhook地址：/api/v1/plugin/WebhookEmby/emby_webhook'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'text': '请求格式：POST {"file_path": "/path/to/your/file", "apikey": "your_api_key"}，apikey参数可选'
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
            "send_notification": True
        }

    def get_page(self) -> List[dict]:
        """
        插件页面
        """
        pass

    def emby_webhook(self, file_path: str, apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        Emby入库Webhook接口
        :param file_path: 文件路径
        :param apikey: API密钥（由MoviePilot系统验证）
        :return: 处理结果
        """
        try:
            # 检查插件是否启用
            if not self._enabled:
                return {
                    "success": False,
                    "message": "插件未启用"
                }

            # 检查文件路径
            if not file_path:
                return {
                    "success": False,
                    "message": "文件路径不能为空"
                }

            # 转换为Path对象并检查文件是否存在
            path = Path(file_path)
            if not path.exists():
                return {
                    "success": False,
                    "message": f"文件或目录不存在：{file_path}"
                }

            logger.info(f"接收到Emby入库请求：{file_path}")

            # 创建FileItem对象
            fileitem = FileItem(
                storage="local",
                path=str(path),
                type="dir" if path.is_dir() else "file",
                name=path.name,
                size=path.stat().st_size if path.is_file() else 0,
                extension=path.suffix.lstrip('.') if path.is_file() else '',
            )

            # 执行刮削和通知处理
            result = self._process_file(fileitem)
            
            if result["success"]:
                logger.info(f"文件 {file_path} 入库成功")
                return result
            else:
                logger.error(f"文件 {file_path} 入库失败：{result['message']}")
                return result

        except Exception as e:
            error_msg = f"处理Webhook请求时发生错误：{str(e)}\n{traceback.format_exc()}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg
            }
            
    def _process_file(self, fileitem: FileItem) -> Dict[str, Any]:
        """
        处理文件入库（刮削和通知）
        :param fileitem: 文件项
        :return: 处理结果
        """
        try:
            # 获取TransferChain实例
            transfer_chain = TransferChain()
            
            # 首先识别媒体信息
            file_path = Path(fileitem.path)
            
            # 解析文件元数据
            meta = MetaInfoPath(file_path)
            
            # 使用MediaChain识别媒体信息
            mediainfo = MediaChain().recognize_by_meta(meta)
            
            if not mediainfo:
                # 尝试按路径识别
                mediainfo = MediaChain().recognize_by_path(str(file_path))
                
            if not mediainfo:
                return {
                    "success": False,
                    "message": f"无法识别媒体信息：{fileitem.name}"
                }
            
            # 更新媒体图片
            transfer_chain.obtain_images(mediainfo=mediainfo)
            
            logger.info(f"文件 {fileitem.name} 识别为：{mediainfo.title_year}")
            
            # 发送刮削事件 - 让Emby等媒体服务器刮削元数据
            try:
                eventmanager.send_event(EventType.MetadataScrape, {
                    'meta': meta,
                    'mediainfo': mediainfo,
                    'fileitem': fileitem,
                    'file_list': [fileitem.name],
                    'overwrite': False
                })
                logger.info(f"已发送刮削事件：{fileitem.name}")
            except Exception as e:
                logger.warning(f"发送刮削事件失败：{str(e)}")
            
            # 发送入库通知（如果启用）
            if self._send_notification:
                try:
                    se_str = None
                    if mediainfo.type == MediaType.TV and meta:
                        if meta.season and meta.episode_list:
                            se_str = f"{meta.season} E{','.join(map(str, meta.episode_list))}"
                        elif meta.season_episode:
                            se_str = meta.season_episode
                    
                    # 创建通知消息
                    notification = Notification(
                        mtype=NotificationType.Organize,
                        title=f"{mediainfo.title_year} 入库成功！",
                        text=f"文件：{fileitem.name}\n位置：{fileitem.path}",
                        image=mediainfo.get_message_image(),
                        link=settings.MP_DOMAIN('#/history')
                    )
                    
                    # 发送通知
                    transfer_chain.post_message(notification)
                    logger.info(f"已发送入库通知：{fileitem.name}")
                    
                except Exception as e:
                    logger.warning(f"发送入库通知失败：{str(e)}")
            else:
                logger.info("入库通知已禁用，跳过发送通知")
            
            return {
                "success": True,
                "message": f"文件 {fileitem.name} 入库处理完成（已发送刮削事件和通知）",
                "media_info": {
                    "title": mediainfo.title,
                    "year": mediainfo.year,
                    "type": mediainfo.type.value if mediainfo.type else None,
                    "tmdb_id": mediainfo.tmdb_id,
                    "season_episode": se_str
                }
            }
                
        except Exception as e:
            error_msg = f"处理文件入库时发生错误：{str(e)}\n{traceback.format_exc()}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg
            }

    def stop_service(self):
        """
        退出插件
        """
        pass
