import threading
import time
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo, RefreshMediaItem, ServiceInfo
from app.schemas.types import EventType


class Lego9MediaServerRefresh(_PluginBase):
    # 插件名称
    plugin_name = "媒体库服务器刷新"
    # 插件描述
    plugin_desc = "入库后自动刷新Emby/Jellyfin/Plex服务器海报墙。"
    # 插件图标
    plugin_icon = "refresh2.png"
    # 插件版本
    plugin_version = "1.4.0"
    # 插件作者
    plugin_author = "leGO9"
    # 作者主页
    author_url = "https://github.com/leG09"
    # 插件配置项ID前缀
    plugin_config_prefix = "lego9_mediaserverrefresh_"
    # 加载顺序
    plugin_order = 14
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _delay = 0
    _mediaservers = None
    _path_mapping = ""  # 路径映射配置

    # 延迟相关的属性
    _in_delay = False
    _pending_items = []
    _end_time = 0.0
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):

        if config:
            self._enabled = config.get("enabled")
            self._delay = config.get("delay") or 0
            self._mediaservers = config.get("mediaservers") or []
            self._path_mapping = config.get("path_mapping", "")

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        服务信息
        """
        if not self._mediaservers:
            logger.warning("尚未配置媒体服务器，请检查配置")
            return None

        services = MediaServerHelper().get_services(name_filters=self._mediaservers)
        if not services:
            logger.warning("获取媒体服务器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            if service_info.instance.is_inactive():
                logger.warning(f"媒体服务器 {service_name} 未连接，请检查配置")
            else:
                active_services[service_name] = service_info

        if not active_services:
            logger.warning("没有已连接的媒体服务器，请检查配置")
            return None

        return active_services

    def _parse_path_mapping(self) -> Dict[str, str]:
        """
        解析路径映射配置
        格式：/host/path1:/container/path1,/host/path2:/container/path2
        返回：{'/host/path1': '/container/path1', '/host/path2': '/container/path2'}
        """
        mapping = {}
        if not self._path_mapping:
            return mapping
            
        try:
            # 分割每个映射项
            for mapping_item in self._path_mapping.split(','):
                mapping_item = mapping_item.strip()
                if not mapping_item or ':' not in mapping_item:
                    continue
                    
                parts = mapping_item.split(':', 1)
                if len(parts) == 2:
                    source_path = parts[0].strip()
                    target_path = parts[1].strip()
                    mapping[source_path] = target_path
                    
        except Exception as e:
            logger.error(f"解析路径映射配置时出错: {e}")
            
        return mapping

    def _map_path(self, original_path: Path) -> Path:
        """
        根据配置映射路径
        """
        if not self._path_mapping:
            return original_path
            
        path_mapping = self._parse_path_mapping()
        if not path_mapping:
            return original_path
            
        original_str = str(original_path)
        
        # 查找匹配的映射规则
        for source_path, target_path in path_mapping.items():
            if original_str.startswith(source_path):
                # 执行路径替换
                mapped_str = original_str.replace(source_path, target_path, 1)
                mapped_path = Path(mapped_str)
                logger.info(f"路径映射：{original_path} -> {mapped_path}")
                return mapped_path
                
        # 如果没有匹配的规则，返回原路径
        logger.debug(f"未找到匹配的路径映射规则，使用原路径：{original_path}")
        return original_path

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
                                    'md': 6
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in MediaServerHelper().get_configs().values()]
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
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay',
                                            'label': '延迟时间（秒）',
                                            'placeholder': '0'
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
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_mapping',
                                            'label': '路径映射',
                                            'placeholder': '/host/path:/container/path,/host/path2:/container/path2',
                                            'rows': 3
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
            "delay": 0,
            "path_mapping": ""
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.TransferComplete)
    def refresh(self, event: Event):
        """
        发送通知消息
        """
        if not self._enabled:
            return

        event_info: dict = event.event_data
        if not event_info:
            return

        # 刷新媒体库
        if not self.service_infos:
            return

        # 入库数据
        transferinfo: TransferInfo = event_info.get("transferinfo")
        if not transferinfo or not transferinfo.target_diritem or not transferinfo.target_diritem.path:
            return

        def debounce_delay(duration: int):
            """
            延迟防抖优化

            :return: 延迟是否已结束
            """
            with self._lock:
                self._end_time = time.time() + float(duration)
                if self._in_delay:
                    return False
                self._in_delay = True

            def end_time():
                with self._lock:
                    return self._end_time

            while time.time() < end_time():
                time.sleep(1)
            with self._lock:
                self._in_delay = False
            return True

        mediainfo: MediaInfo = event_info.get("mediainfo")
        
        # 应用路径映射
        original_path = Path(transferinfo.target_diritem.path)
        mapped_path = self._map_path(original_path)
        
        item = RefreshMediaItem(
            title=mediainfo.title,
            year=mediainfo.year,
            type=mediainfo.type,
            category=mediainfo.category,
            target_path=mapped_path,
        )

        if self._delay:
            logger.info(f"延迟 {self._delay} 秒后刷新媒体库... ")
            with self._lock:
                self._pending_items.append(item)
            if not debounce_delay(self._delay):
                # 还在延迟中 忽略本次请求
                return
            with self._lock:
                items = self._pending_items
                self._pending_items = []
        else:
            items = [item]

        for name, service in self.service_infos.items():
            if hasattr(service.instance, 'refresh_library_by_items'):
                service.instance.refresh_library_by_items(items)
            elif hasattr(service.instance, 'refresh_root_library'):
                # FIXME Jellyfin未找到刷新单个项目的API
                service.instance.refresh_root_library()
            else:
                logger.warning(f"{name} 不支持刷新")

    def stop_service(self):
        """
        退出插件
        """
        with self._lock:
            # 放弃等待，立即刷新
            self._end_time = 0.0
            # self._pending_items.clear()