import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict

from app.plugins import _PluginBase
from app.core.config import settings
from app.core.security import verify_apikey
from app.core.event import eventmanager, Event
from app.helper.service import ServiceConfigHelper
from app.chain.download import DownloadChain
from app.schemas import DownloaderConf, ResourceDownloadEventData
from app.schemas.types import MediaType, ChainEventType, EventType
from app.log import logger

try:
    from typing_extensions import Annotated
except ImportError:
    from typing import Annotated


class DownloaderBalancer(_PluginBase):
    """下载器负载均衡插件"""
    
    # 插件信息
    plugin_name = "DownloaderBalancer"
    plugin_desc = "下载器负载均衡插件，支持轮询、Hash、电影规则分类等多种选择策略"
    plugin_icon = "Qbittorrent_A.png"
    plugin_color = "#00BFFF"
    plugin_version = "1.0.0"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "downloaderbalancer"
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self._enabled = config.get("enabled", False) if config else False
        self._strategy = config.get("strategy", "round_robin") if config else "round_robin"
        self._selected_downloaders = config.get("selected_downloaders", []) if config else []
        self._movie_rules = config.get("movie_rules", "") if config else ""
        self._enable_failover = config.get("enable_failover", True) if config else True
        self._health_check_interval = config.get("health_check_interval", 300) if config else 300
        self._max_retries = config.get("max_retries", 3) if config else 3
        self._override_downloader = config.get("override_downloader", True) if config else True
        
        # 内部状态
        self._round_robin_index = 0
        self._downloader_stats = defaultdict(lambda: {
            'success_count': 0,
            'fail_count': 0,
            'last_used': 0,
            'is_healthy': True,
            'last_health_check': 0
        })
        self._lock = threading.Lock()
        self._health_check_thread = None
        self._stop_health_check = False
        
        # 启动健康检查线程
        if self._enabled and self._enable_failover:
            self._start_health_check()
        
        # 动态注册作为兜底
        if self._enabled and self._override_downloader:
            try:
                # 同步事件
                eventmanager.register(ChainEventType.ResourceDownload, self._handle_resource_download)
                # 兼容订阅完成/添加钩子（用于验证日志或后续扩展）
                eventmanager.register(EventType.ResourceDownload, self._handle_resource_download)
            except Exception as _:
                pass

        # 对 DownloadChain 进行猴子补丁，强制覆盖 downloader 参数
        if self._enabled and self._override_downloader:
            try:
                self._monkey_patch_download_chain()
            except Exception as e:
                logger.warning(f"DownloadChain 补丁失败: {e}")
        
        logger.info(f"下载器负载均衡插件初始化完成，启用状态: {self._enabled}, 策略: {self._strategy}, 覆盖下载器: {self._override_downloader}")

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
                "path": "/select_downloader",
                "endpoint": self.select_downloader,
                "methods": ["POST"],
                "summary": "选择下载器",
                "description": "根据配置的策略选择最适合的下载器"
            },
            {
                "path": "/get_stats",
                "endpoint": self.get_stats,
                "methods": ["GET"],
                "summary": "获取下载器统计信息",
                "description": "获取各下载器的使用统计和健康状态"
            },
            {
                "path": "/reset_stats",
                "endpoint": self.reset_stats,
                "methods": ["POST"],
                "summary": "重置统计信息",
                "description": "重置所有下载器的统计信息"
            },
            {
                "path": "/test_event",
                "endpoint": self.test_event,
                "methods": ["POST"],
                "summary": "测试事件监听",
                "description": "测试插件是否正确监听了下载事件"
            }
        ]

    def get_form(self) -> tuple:
        """获取配置表单"""
        # 获取可用的下载器列表
        available_downloaders = self._get_available_downloaders()
        downloader_options = [
            {"title": f"{dl['name']} ({dl['type']})", "value": dl['name']}
            for dl in available_downloaders
        ]
        
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
                                            "model": "enable_failover",
                                            "label": "启用故障转移",
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
                                            "model": "override_downloader",
                                            "label": "覆盖下载器选择",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "strategy",
                                            "label": "选择策略",
                                            "items": [
                                                {"title": "轮询 (Round Robin)", "value": "round_robin"},
                                                {"title": "Hash 分配", "value": "hash"},
                                                {"title": "电影规则分类", "value": "movie_rules"},
                                                {"title": "最少使用", "value": "least_used"},
                                                {"title": "随机选择", "value": "random"}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "health_check_interval",
                                            "label": "健康检查间隔(秒)",
                                            "placeholder": "300"
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
                                        "component": "VSelect",
                                        "props": {
                                            "model": "selected_downloaders",
                                            "label": "选择下载器",
                                            "multiple": True,
                                            "chips": True,
                                            "items": downloader_options,
                                            "hint": "选择要参与负载均衡的下载器"
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
                                            "model": "movie_rules",
                                            "label": "电影规则配置",
                                            "placeholder": "每行一个规则，格式：条件:下载器名称\n例如：\nIMAX:qbittorrent1\n4K:qbittorrent2\n其他:transmission1",
                                            "rows": 5,
                                            "hint": "仅在策略为'电影规则分类'时生效"
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_retries",
                                            "label": "最大重试次数",
                                            "placeholder": "3"
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
                                            "text": "轮询：按顺序轮流选择下载器\nHash：根据内容Hash值固定分配到特定下载器\n电影规则分类：根据电影特征（分辨率、格式等）选择下载器\n最少使用：选择使用次数最少的下载器\n随机选择：随机选择一个下载器"
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
            "strategy": "round_robin",
            "selected_downloaders": [],
            "movie_rules": "",
            "enable_failover": True,
            "health_check_interval": 300,
            "max_retries": 3,
            "override_downloader": True
        }

    def get_page(self) -> list:
        """获取插件页面"""
        return []

    def stop_service(self):
        """停止插件"""
        self._stop_health_check = True
        if self._health_check_thread and self._health_check_thread.is_alive():
            self._health_check_thread.join(timeout=5)
        # 还原猴子补丁
        try:
            if hasattr(self, "_orig_download_single") and self._orig_download_single:
                DownloadChain.download_single = self._orig_download_single
        except Exception:
            pass
        try:
            if hasattr(self, "_orig_download") and self._orig_download:
                DownloadChain.download = self._orig_download
        except Exception:
            pass

    def _monkey_patch_download_chain(self) -> None:
        """对 DownloadChain.download_single 打补丁，强制使用插件选择的下载器"""
        if hasattr(self, "_orig_download_single") and self._orig_download_single:
            return
        self._orig_download_single = DownloadChain.download_single
        self._orig_download = DownloadChain.download

        plugin = self

        def _wrapper(dc_self, *args, **kwargs):
            logger.info("DownloaderBalancer: 进入 download_single 补丁包装器")
            try:
                context = kwargs.get("context")
                if not context and len(args) >= 1:
                    context = args[0]
                # 记录在实例上，供最终 download 包装器兜底使用
                try:
                    setattr(dc_self, "_last_context", context)
                except Exception:
                    pass
                selected = None
                if context and getattr(context, "torrent_info", None) and getattr(context, "media_info", None):
                    ctx = {
                        "filename": context.torrent_info.title,
                        "title": context.media_info.title,
                        "torrent_hash": getattr(context.torrent_info, 'hash', ''),
                        "url": context.torrent_info.enclosure,
                        "media_type": context.media_info.type.value if context.media_info and context.media_info.type else None,
                        "category": getattr(context.media_info, 'category', None),
                    }
                    selected = plugin._select_downloader_by_strategy(ctx)
                    if not selected and plugin._selected_downloaders:
                        selected = plugin._selected_downloaders[0]
                # 覆盖 downloader 与 site_downloader
                if selected:
                    try:
                        if context and getattr(context, "torrent_info", None):
                            setattr(context.torrent_info, "site_downloader", selected)
                    except Exception:
                        pass
                    kwargs["downloader"] = selected
                    logger.info(f"下载器负载均衡（补丁）选择: {selected} 用于下载: {context.torrent_info.title if context else ''}")
            except Exception as e:
                logger.info(f"下载器选择补丁失败: {e}")
            return plugin._orig_download_single(dc_self, *args, **kwargs)

        DownloadChain.download_single = _wrapper

        def _download_wrapper(dc_self, *args, **kwargs):
            # 强制在最终下载入口也校正 downloader
            try:
                # 参数位置: content,cookie,episodes,download_dir,category,label,downloader
                # 优先使用显式传入的 downloader；若空，则再次根据最近的 context 猜测
                current_downloader = kwargs.get("downloader")
                if not current_downloader:
                    # 尝试从调用方属性拿到最近 context（下载链上常见字段）
                    context = getattr(dc_self, "_last_context", None)
                    selected = None
                    if context and getattr(context, "torrent_info", None) and getattr(context, "media_info", None):
                        ctx = {
                            "filename": context.torrent_info.title,
                            "title": context.media_info.title,
                            "torrent_hash": getattr(context.torrent_info, 'hash', ''),
                            "url": context.torrent_info.enclosure,
                            "media_type": context.media_info.type.value if context.media_info and context.media_info.type else None,
                            "category": getattr(context.media_info, 'category', None),
                        }
                        selected = plugin._select_downloader_by_strategy(ctx)
                        if not selected and plugin._selected_downloaders:
                            selected = plugin._selected_downloaders[0]
                    if selected:
                        kwargs["downloader"] = selected
                        logger.info(f"下载器负载均衡（最终）选择: {selected}")
            except Exception as e:
                logger.info(f"下载器最终选择补丁失败: {e}")
            return plugin._orig_download(dc_self, *args, **kwargs)

        DownloadChain.download = _download_wrapper

    def _get_available_downloaders(self) -> List[Dict[str, Any]]:
        """获取可用的下载器列表"""
        try:
            configs = ServiceConfigHelper.get_downloader_configs()
            return [
                {
                    "name": conf.name,
                    "type": conf.type,
                    "enabled": conf.enabled,
                    "default": conf.default
                }
                for conf in configs if conf.enabled
            ]
        except Exception as e:
            logger.error(f"获取下载器配置失败: {e}")
            return []

    def _get_healthy_downloaders(self) -> List[str]:
        """获取健康的下载器列表"""
        if not self._enable_failover:
            return self._selected_downloaders
        
        healthy_downloaders = []
        for downloader in self._selected_downloaders:
            if self._downloader_stats[downloader]['is_healthy']:
                healthy_downloaders.append(downloader)
        
        # 如果没有健康的下载器，返回所有选中的下载器
        return healthy_downloaders if healthy_downloaders else self._selected_downloaders

    def _select_downloader_round_robin(self, context: Dict[str, Any] = None) -> Optional[str]:
        """轮询策略选择下载器"""
        healthy_downloaders = self._get_healthy_downloaders()
        if not healthy_downloaders:
            return None
        
        with self._lock:
            selected = healthy_downloaders[self._round_robin_index % len(healthy_downloaders)]
            self._round_robin_index = (self._round_robin_index + 1) % len(healthy_downloaders)
            return selected

    def _select_downloader_hash(self, context: Dict[str, Any] = None) -> Optional[str]:
        """Hash策略选择下载器"""
        healthy_downloaders = self._get_healthy_downloaders()
        if not healthy_downloaders:
            return None
        
        # 使用内容Hash值选择下载器
        hash_key = ""
        if context:
            # 优先使用种子Hash
            if "torrent_hash" in context:
                hash_key = context["torrent_hash"]
            # 其次使用文件名
            elif "filename" in context:
                hash_key = context["filename"]
            # 最后使用URL
            elif "url" in context:
                hash_key = context["url"]
        
        if not hash_key:
            # 如果没有Hash键，回退到轮询
            return self._select_downloader_round_robin(context)
        
        # 计算Hash值并选择下载器
        hash_value = int(hashlib.md5(hash_key.encode()).hexdigest(), 16)
        index = hash_value % len(healthy_downloaders)
        return healthy_downloaders[index]

    def _select_downloader_movie_rules(self, context: Dict[str, Any] = None) -> Optional[str]:
        """电影规则分类策略选择下载器"""
        healthy_downloaders = self._get_healthy_downloaders()
        if not healthy_downloaders:
            return None
        
        if not self._movie_rules or not context:
            # 如果没有配置规则或上下文，回退到轮询
            return self._select_downloader_round_robin(context)
        
        # 解析电影规则
        rules = {}
        for line in self._movie_rules.strip().split('\n'):
            line = line.strip()
            if not line or ':' not in line:
                continue
            try:
                condition, downloader = line.split(':', 1)
                rules[condition.strip()] = downloader.strip()
            except Exception as e:
                logger.warning(f"解析电影规则失败: {line}, 错误: {e}")
        
        # 根据上下文匹配规则
        filename = context.get("filename", "").upper()
        title = context.get("title", "").upper()
        
        for condition, downloader in rules.items():
            condition_upper = condition.upper()
            if (condition_upper in filename or condition_upper in title) and downloader in healthy_downloaders:
                logger.info(f"电影规则匹配: {condition} -> {downloader}")
                return downloader
        
        # 如果没有匹配的规则，使用默认规则或回退到轮询
        default_downloader = rules.get("其他") or rules.get("默认")
        if default_downloader and default_downloader in healthy_downloaders:
            return default_downloader
        
        return self._select_downloader_round_robin(context)

    def _select_downloader_least_used(self, context: Dict[str, Any] = None) -> Optional[str]:
        """最少使用策略选择下载器"""
        healthy_downloaders = self._get_healthy_downloaders()
        if not healthy_downloaders:
            return None
        
        # 选择使用次数最少的下载器
        least_used_downloader = None
        min_usage = float('inf')
        
        for downloader in healthy_downloaders:
            stats = self._downloader_stats[downloader]
            total_usage = stats['success_count'] + stats['fail_count']
            if total_usage < min_usage:
                min_usage = total_usage
                least_used_downloader = downloader
        
        return least_used_downloader

    def _select_downloader_random(self, context: Dict[str, Any] = None) -> Optional[str]:
        """随机策略选择下载器"""
        import random
        healthy_downloaders = self._get_healthy_downloaders()
        if not healthy_downloaders:
            return None
        
        return random.choice(healthy_downloaders)

    def select_downloader(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        选择下载器API
        
        Args:
            request_data: 请求数据，包含选择上下文信息
            apikey: API密钥
            
        Returns:
            Dict: 选择结果
        """
        try:
            if not self._enabled:
                return {"success": False, "message": "插件未启用"}
            
            if not self._selected_downloaders:
                return {"success": False, "message": "未配置下载器"}
            
            # 获取选择上下文
            context = request_data.get("context", {})
            
            # 根据策略选择下载器
            selected_downloader = None
            strategy_methods = {
                "round_robin": self._select_downloader_round_robin,
                "hash": self._select_downloader_hash,
                "movie_rules": self._select_downloader_movie_rules,
                "least_used": self._select_downloader_least_used,
                "random": self._select_downloader_random
            }
            
            method = strategy_methods.get(self._strategy)
            if method:
                selected_downloader = method(context)
            
            if not selected_downloader:
                return {"success": False, "message": "无法选择下载器"}
            
            # 更新统计信息
            with self._lock:
                self._downloader_stats[selected_downloader]['last_used'] = time.time()
            
            logger.info(f"选择下载器: {selected_downloader} (策略: {self._strategy})")
            
            return {
                "success": True,
                "downloader": selected_downloader,
                "strategy": self._strategy,
                "context": context
            }
            
        except Exception as e:
            logger.error(f"选择下载器时发生错误: {e}")
            return {"success": False, "message": f"选择下载器失败: {str(e)}"}

    def get_stats(self, apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        获取下载器统计信息
        
        Args:
            apikey: API密钥
            
        Returns:
            Dict: 统计信息
        """
        try:
            stats = {}
            for downloader in self._selected_downloaders:
                downloader_stats = self._downloader_stats[downloader]
                stats[downloader] = {
                    "success_count": downloader_stats['success_count'],
                    "fail_count": downloader_stats['fail_count'],
                    "total_count": downloader_stats['success_count'] + downloader_stats['fail_count'],
                    "success_rate": (
                        downloader_stats['success_count'] / 
                        (downloader_stats['success_count'] + downloader_stats['fail_count'])
                        if (downloader_stats['success_count'] + downloader_stats['fail_count']) > 0
                        else 0
                    ),
                    "is_healthy": downloader_stats['is_healthy'],
                    "last_used": downloader_stats['last_used'],
                    "last_health_check": downloader_stats['last_health_check']
                }
            
            return {
                "success": True,
                "strategy": self._strategy,
                "selected_downloaders": self._selected_downloaders,
                "stats": stats
            }
            
        except Exception as e:
            logger.error(f"获取统计信息时发生错误: {e}")
            return {"success": False, "message": f"获取统计信息失败: {str(e)}"}

    def reset_stats(self, apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        重置统计信息
        
        Args:
            apikey: API密钥
            
        Returns:
            Dict: 操作结果
        """
        try:
            with self._lock:
                for downloader in self._selected_downloaders:
                    self._downloader_stats[downloader] = {
                        'success_count': 0,
                        'fail_count': 0,
                        'last_used': 0,
                        'is_healthy': True,
                        'last_health_check': 0
                    }
                self._round_robin_index = 0
            
            logger.info("统计信息已重置")
            return {"success": True, "message": "统计信息已重置"}
            
        except Exception as e:
            logger.error(f"重置统计信息时发生错误: {e}")
            return {"success": False, "message": f"重置统计信息失败: {str(e)}"}

    def test_event(self, apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        测试事件监听
        
        Args:
            apikey: API密钥
            
        Returns:
            Dict: 测试结果
        """
        try:
            # 模拟一个下载事件来测试插件是否正常工作
            from app.core.context import MediaInfo, TorrentInfo, Context
            from app.core.metainfo import MetaInfo
            from app.schemas import ResourceDownloadEventData
            
            # 创建测试数据
            meta = MetaInfo(title="测试电影.2023.1080p.BluRay.x264")
            media = MediaInfo()
            media.title = "测试电影"
            media.year = 2023
            media.type = MediaType.MOVIE
            
            torrent = TorrentInfo()
            torrent.title = "测试电影.2023.1080p.BluRay.x264"
            torrent.enclosure = "magnet:?xt=urn:btih:test"
            
            context = Context(
                meta_info=meta,
                media_info=media,
                torrent_info=torrent
            )
            
            # 创建事件数据
            event_data = ResourceDownloadEventData(
                context=context,
                episodes=[],
                channel=None,
                origin="test",
                downloader="test_downloader",
                options={}
            )
            
            # 模拟事件处理
            context_for_selection = {
                "filename": torrent.title,
                "title": media.title,
                "torrent_hash": "test_hash",
                "url": torrent.enclosure,
                "media_type": media.type.value,
                "category": media.category
            }
            
            selected_downloader = self._select_downloader_by_strategy(context_for_selection)
            
            return {
                "success": True,
                "message": "事件监听测试完成",
                "test_data": {
                    "original_downloader": "test_downloader",
                    "selected_downloader": selected_downloader,
                    "strategy": self._strategy,
                    "available_downloaders": self._selected_downloaders,
                    "plugin_enabled": self._enabled,
                    "override_enabled": self._override_downloader
                }
            }
            
        except Exception as e:
            logger.error(f"测试事件监听时发生错误: {e}")
            return {"success": False, "message": f"测试失败: {str(e)}"}

    def _start_health_check(self):
        """启动健康检查线程"""
        if self._health_check_thread and self._health_check_thread.is_alive():
            return
        
        self._stop_health_check = False
        self._health_check_thread = threading.Thread(target=self._health_check_worker, daemon=True)
        self._health_check_thread.start()
        logger.info("健康检查线程已启动")

    def _health_check_worker(self):
        """健康检查工作线程"""
        while not self._stop_health_check:
            try:
                current_time = time.time()
                
                for downloader in self._selected_downloaders:
                    try:
                        # 这里可以添加实际的健康检查逻辑
                        # 例如：尝试连接下载器、检查API响应等
                        is_healthy = self._check_downloader_health(downloader)
                        
                        with self._lock:
                            self._downloader_stats[downloader]['is_healthy'] = is_healthy
                            self._downloader_stats[downloader]['last_health_check'] = current_time
                        
                        if not is_healthy:
                            logger.warning(f"下载器 {downloader} 健康检查失败")
                        
                    except Exception as e:
                        logger.error(f"检查下载器 {downloader} 健康状态时发生错误: {e}")
                        with self._lock:
                            self._downloader_stats[downloader]['is_healthy'] = False
                            self._downloader_stats[downloader]['last_health_check'] = current_time
                
                # 等待下次检查
                time.sleep(self._health_check_interval)
                
            except Exception as e:
                logger.error(f"健康检查线程发生错误: {e}")
                time.sleep(60)  # 发生错误时等待1分钟

    def _check_downloader_health(self, downloader_name: str) -> bool:
        """
        检查下载器健康状态
        
        Args:
            downloader_name: 下载器名称
            
        Returns:
            bool: 是否健康
        """
        try:
            # 这里应该实现实际的健康检查逻辑
            # 例如：尝试连接下载器API、检查响应时间等
            # 目前返回True作为占位符
            return True
            
        except Exception as e:
            logger.error(f"检查下载器 {downloader_name} 健康状态失败: {e}")
            return False

    def _handle_resource_download(self, event: Event):
        """
        处理资源下载事件，自动选择下载器
        
        Args:
            event: 资源下载事件
        """
        try:
            logger.info("DownloaderBalancer: 收到 ResourceDownload 事件")
            if not self._enabled or not self._override_downloader:
                return
            
            if not event or not event.event_data:
                return
            
            event_data: ResourceDownloadEventData = event.event_data
            
            # 构建选择上下文
            context = {
                "filename": event_data.context.torrent_info.title,
                "title": event_data.context.media_info.title,
                "torrent_hash": getattr(event_data.context.torrent_info, 'hash', ''),
                "url": event_data.context.torrent_info.enclosure,
                "media_type": event_data.context.media_info.type.value,
                "category": event_data.context.media_info.category
            }
            
            # 选择下载器（若调用方已显式指定 downloader，则仍然覆盖以确保策略生效）
            selected_downloader = self._select_downloader_by_strategy(context)
            if not selected_downloader and self._selected_downloaders:
                # 兜底：未选出则使用第一个配置的下载器
                selected_downloader = self._selected_downloaders[0]
            
            if selected_downloader:
                # 记录原始下载器
                original_downloader = event_data.downloader
                
                # 更新事件数据中的下载器
                event_data.downloader = selected_downloader
                # 关键：直接覆盖 context 内的 site_downloader，供 download_single 使用
                try:
                    if (
                        hasattr(event_data, "context") and event_data.context and
                        hasattr(event_data.context, "torrent_info") and event_data.context.torrent_info
                    ):
                        setattr(event_data.context.torrent_info, "site_downloader", selected_downloader)
                        # 同步覆盖 DownloadChain.call 参数场景：设置 options 里的 downloder/save_path
                        if not event_data.options:
                            event_data.options = {}
                        event_data.options["downloader"] = selected_downloader
                except Exception as _:
                    pass
                
                # 更新统计信息
                with self._lock:
                    self._downloader_stats[selected_downloader]['last_used'] = time.time()
                
                logger.info(f"下载器负载均衡选择: {selected_downloader} (策略: {self._strategy}) "
                           f"用于下载: {event_data.context.torrent_info.title} "
                           f"(原下载器: {original_downloader or '未指定'})")
            
        except Exception as e:
            logger.error(f"处理资源下载事件时发生错误: {e}")

    # 使用装饰器确保事件注册一定生效
    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event):
        try:
            return self._handle_resource_download(event)
        except Exception as e:
            logger.error(f"on_resource_download 处理失败: {e}")

    def _select_downloader_by_strategy(self, context: Dict[str, Any] = None) -> Optional[str]:
        """
        根据策略选择下载器
        
        Args:
            context: 选择上下文
            
        Returns:
            str: 选择的下载器名称
        """
        try:
            if not self._selected_downloaders:
                return None
            
            # 根据策略选择下载器
            strategy_methods = {
                "round_robin": self._select_downloader_round_robin,
                "hash": self._select_downloader_hash,
                "movie_rules": self._select_downloader_movie_rules,
                "least_used": self._select_downloader_least_used,
                "random": self._select_downloader_random
            }
            
            method = strategy_methods.get(self._strategy)
            if method:
                return method(context)
            
            return None
            
        except Exception as e:
            logger.error(f"选择下载器时发生错误: {e}")
            return None

    def report_download_result(self, downloader_name: str, success: bool):
        """
        报告下载结果（供其他模块调用）
        
        Args:
            downloader_name: 下载器名称
            success: 是否成功
        """
        try:
            with self._lock:
                if success:
                    self._downloader_stats[downloader_name]['success_count'] += 1
                else:
                    self._downloader_stats[downloader_name]['fail_count'] += 1
                
                # 如果失败次数过多，标记为不健康
                stats = self._downloader_stats[downloader_name]
                total_count = stats['success_count'] + stats['fail_count']
                if total_count > 10 and stats['fail_count'] / total_count > 0.5:
                    stats['is_healthy'] = False
                    logger.warning(f"下载器 {downloader_name} 失败率过高，标记为不健康")
            
        except Exception as e:
            logger.error(f"报告下载结果时发生错误: {e}")
