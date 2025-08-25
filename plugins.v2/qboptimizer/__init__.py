import threading
import time
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.context import MediaInfo, TorrentInfo, Context
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.helper.downloader import DownloaderHelper
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo
from app.schemas.message import Notification
from app.schemas.types import EventType, MediaType
from app.utils.string import StringUtils

lock = threading.Lock()


class QbOptimizer(_PluginBase):
    # 插件名称
    plugin_name = "QB种子优化"
    # 插件描述
    plugin_desc = "优化qBittorrent种子管理：清理进行中无人做种任务、清理预计下载时长超过N小时且已下载N小时的慢速任务、智能综合评分优先级（下载速度+做种数+进度）"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.1"
    # 插件作者
    plugin_author = "leG09"
    # 作者主页
    author_url = "https://github.com/leG09"
    # 插件配置项ID前缀
    plugin_config_prefix = "qboptimizer_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _event = threading.Event()
    _scheduler = None
    _enabled = False
    _onlyonce = False
    _notify = False
    _downloaders = []
    _cron = None
    
    # 功能开关
    _enable_zero_seed_clean = False  # 启用进行中无人做种任务清理
    _enable_timeout_clean = False    # 启用慢速下载任务清理
    _enable_auto_redownload = True   # 启用自动重新下载
    
    # 配置参数
    _zero_seed_threshold = 0         # 做种数阈值

    _timeout_hours = 24              # 慢速下载时长阈值（小时）
    _max_download_slots = 5          # 最大下载槽位
    _max_zero_seed_process = 50      # 每次处理的最大无人做种种子数量
    _max_timeout_process = 50        # 每次处理的最大慢速下载种子数量
    _max_priority_process = 100      # 每次处理的最大优先级优化种子数量
    # 移除优先级提升倍数，改为按做种数排序
    _search_sites = []               # 重新下载时用于搜索的站点ID列表
    _seeder_metric = 'num_complete'  # 做种数度量方式：固定使用num_complete字段
    
    # 综合评分配置
    _enable_score_priority = True    # 启用综合评分优先级
    _speed_weight = 1.0             # 下载速度权重
    _seeder_weight = 0.5            # 做种数权重
    _min_score_threshold = 1.0      # 最低综合评分阈值
    
    # 磁盘空间和I/O监控配置
    _enable_disk_monitor = False     # 启用磁盘空间和I/O监控
    _disk_space_threshold = 10       # 磁盘空间不足阈值（GB）
    _io_cache_threshold = 70         # 写入缓存阈值（百分比）
    _io_queue_threshold = 8000       # 队列I/O任务阈值
    _speed_limit_mbps = 1            # 限制下载速度（MB/s）
    _is_speed_limited = False        # 当前是否已限速

    def init_plugin(self, config: dict = None):
        logger.info("【QB种子优化】开始初始化插件...")
        logger.info(f"【QB种子优化】收到配置: {config}")
        
        if config:
            logger.info(f"【QB种子优化】收到配置: {config}")
            
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._downloaders = config.get("downloaders") or []
            if not self._downloaders:
                logger.error("【QB种子优化】错误：未配置下载器，请先在插件配置中选择下载器")
                return
            self._cron = config.get("cron")
            
            # 功能开关
            self._enable_zero_seed_clean = config.get("enable_zero_seed_clean")
            self._enable_timeout_clean = config.get("enable_timeout_clean")
            self._enable_auto_redownload = config.get("enable_auto_redownload", True)
            self._search_sites = config.get("search_sites") or []
            self._seeder_metric = 'num_complete'  # 固定使用num_complete字段
            
            # 配置参数
            self._zero_seed_threshold = int(config.get("zero_seed_threshold") or 0)

            self._timeout_hours = int(config.get("timeout_hours") or 24)
            self._max_download_slots = int(config.get("max_download_slots") or 5)
            self._max_zero_seed_process = int(config.get("max_zero_seed_process") or 50)
            self._max_timeout_process = int(config.get("max_timeout_process") or 50)
            self._max_priority_process = int(config.get("max_priority_process") or 100)
            # 移除优先级提升倍数配置
            
            # 综合评分配置
            self._enable_score_priority = config.get("enable_score_priority", True)
            logger.info(f"【QB种子优化】配置加载 - enable_score_priority: {self._enable_score_priority} (原始配置值: {config.get('enable_score_priority', 'NOT_FOUND')})")
            self._speed_weight = float(config.get("speed_weight", 1.0))
            self._seeder_weight = float(config.get("seeder_weight", 0.5))
            self._min_score_threshold = float(config.get("min_score_threshold", 1.0))
            
            # 磁盘空间和I/O监控配置
            self._enable_disk_monitor = config.get("enable_disk_monitor", False)
            self._disk_space_threshold = float(config.get("disk_space_threshold", 10))
            self._io_cache_threshold = float(config.get("io_cache_threshold", 70))
            self._io_queue_threshold = int(config.get("io_queue_threshold", 8000))
            self._speed_limit_mbps = float(config.get("speed_limit_mbps", 1))
            self._is_speed_limited = False  # 重置限速状态
            
            logger.info(f"【QB种子优化】配置加载完成:")
            logger.info(f"  - 启用状态: {self._enabled}")
            logger.info(f"  - 立即运行: {self._onlyonce}")
            logger.info(f"  - 发送通知: {self._notify}")
            logger.info(f"  - 下载器: {self._downloaders}")
            logger.info(f"  - 执行周期: {self._cron}")
            logger.info(f"  - 搜索站点: {self._search_sites}")
            logger.info(f"  - 做种数度量: {self._seeder_metric}")
            logger.info(f"  - 清理做种数为0: {self._enable_zero_seed_clean}")
            logger.info(f"  - 清理超时: {self._enable_timeout_clean}")
            logger.info(f"  - 做种数阈值: {self._zero_seed_threshold}")
            logger.info(f"  - 慢速下载时长阈值: {self._timeout_hours}小时")
            logger.info(f"  - 最大无人做种处理数: {self._max_zero_seed_process}")
            logger.info(f"  - 最大慢速下载处理数: {self._max_timeout_process}")
            logger.info(f"  - 最大优先级优化处理数: {self._max_priority_process}")
            logger.info(f"  - 优先级策略: 综合评分排序")
            logger.info(f"  - 综合评分开关: {self._enable_score_priority}")
            logger.info(f"  - 速度权重: {self._speed_weight}")
            logger.info(f"  - 做种数权重: {self._seeder_weight}")
            logger.info(f"  - 最低评分阈值: {self._min_score_threshold}")
            logger.info(f"  - 磁盘监控开关: {self._enable_disk_monitor}")
            logger.info(f"  - 磁盘空间阈值: {self._disk_space_threshold}GB")
            logger.info(f"  - I/O缓存阈值: {self._io_cache_threshold}%")
            logger.info(f"  - I/O队列阈值: {self._io_queue_threshold}")
            logger.info(f"  - 速度限制: {self._speed_limit_mbps}MB/s")
        else:
            logger.warning("【QB种子优化】未收到配置，使用默认值")

        self.stop_service()

        if self.get_state() or self._onlyonce:
            logger.info(f"【QB种子优化】插件状态检查: get_state()={self.get_state()}, onlyonce={self._onlyonce}")
            
            if self._onlyonce:
                logger.info("【QB种子优化】准备立即运行一次...")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"【QB种子优化】创建调度器，时区: {settings.TZ}")
                
                run_time = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3)
                logger.info(f"【QB种子优化】设置运行时间: {run_time}")
                
                self._scheduler.add_job(func=self.optimize_torrents, trigger='date',
                                        run_date=run_time)
                
                # 关闭一次性开关
                self._onlyonce = False
                logger.info("【QB种子优化】已关闭立即运行开关")
                
                # 保存设置
                config_to_save = {
                    "enabled": self._enabled,
                    "notify": self._notify,
                    "onlyonce": self._onlyonce,
                    "cron": self._cron,
                    "downloaders": self._downloaders,
                    "enable_zero_seed_clean": self._enable_zero_seed_clean,
                    "enable_timeout_clean": self._enable_timeout_clean,
                    "enable_auto_redownload": self._enable_auto_redownload,
                    "search_sites": self._search_sites,
                    "zero_seed_threshold": self._zero_seed_threshold,

                    "timeout_hours": self._timeout_hours,
                    "max_download_slots": self._max_download_slots,
                    "max_zero_seed_process": self._max_zero_seed_process,
                    "max_timeout_process": self._max_timeout_process,
                    "max_priority_process": self._max_priority_process,
                    # 综合评分配置
                    "enable_score_priority": self._enable_score_priority,
                    "speed_weight": self._speed_weight,
                    "seeder_weight": self._seeder_weight,
                    "min_score_threshold": self._min_score_threshold,
                    # 磁盘空间和I/O监控配置
                    "enable_disk_monitor": self._enable_disk_monitor,
                    "disk_space_threshold": self._disk_space_threshold,
                    "io_cache_threshold": self._io_cache_threshold,
                    "io_queue_threshold": self._io_queue_threshold,
                    "speed_limit_mbps": self._speed_limit_mbps,
                }
                logger.info(f"【QB种子优化】保存配置: {config_to_save}")
                self.update_config(config_to_save)
                
                if self._scheduler.get_jobs():
                    logger.info("【QB种子优化】调度器任务列表:")
                    self._scheduler.print_jobs()
                    logger.info("【QB种子优化】启动调度器...")
                    self._scheduler.start()
                    logger.info("【QB种子优化】调度器启动成功")
                else:
                    logger.warning("【QB种子优化】调度器没有任务")
            else:
                logger.info("【QB种子优化】插件已启用，等待定时任务执行")

    def get_state(self) -> bool:
        state = True if self._enabled and self._cron and self._downloaders else False
        logger.debug(f"【QB种子优化】插件状态: enabled={self._enabled}, cron={self._cron}, downloaders={self._downloaders}, state={state}")
        return state

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        """
        return [
            {
                "cmd": "/qb_optimize",
                "event": EventType.PluginAction,
                "desc": "执行QB种子优化",
                "category": "QB优化",
                "data": {"action": "qb_optimize"},
            },
            {
                "cmd": "/qb_status",
                "event": EventType.PluginAction,
                "desc": "查看QB优化状态",
                "category": "QB优化",
                "data": {"action": "qb_status"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self.get_state():
            return [{
                "id": "QbOptimizer",
                "name": "QB种子优化服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.optimize_torrents,
                "kwargs": {}
            }]
        return []

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        """
        服务信息
        """
        logger.debug(f"【QB种子优化】获取服务信息，配置的下载器: {self._downloaders}")
        
        if not self._downloaders:
            logger.warning("【QB种子优化】尚未配置下载器，请检查配置")
            return None

        logger.debug("【QB种子优化】开始获取下载器服务...")
        services = DownloaderHelper().get_services(name_filters=self._downloaders)
        logger.debug(f"【QB种子优化】获取到的服务: {list(services.keys()) if services else 'None'}")
        
        if not services:
            logger.warning("【QB种子优化】获取下载器实例失败，请检查配置")
            return None

        active_services = {}
        for service_name, service_info in services.items():
            logger.debug(f"【QB种子优化】检查下载器: {service_name}")
            
            if service_info.instance.is_inactive():
                logger.warning(f"【QB种子优化】下载器 {service_name} 未连接，请检查配置")
            elif not self.check_is_qb(service_info):
                logger.warning(f"【QB种子优化】不支持的下载器类型 {service_name}，仅支持QB，请检查配置")
            else:
                logger.info(f"【QB种子优化】下载器 {service_name} 连接正常")
                active_services[service_name] = service_info

        logger.info(f"【QB种子优化】活跃下载器数量: {len(active_services)}")
        if not active_services:
            logger.warning("【QB种子优化】没有已连接的下载器，请检查配置")
            return None

        return active_services

    @staticmethod
    def check_is_qb(service_info) -> bool:
        """
        检查下载器类型是否为 qbittorrent
        """
        is_qb = DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info)
        logger.debug(f"【QB种子优化】检查下载器类型: {service_info.name if service_info else 'None'} -> {'qbittorrent' if is_qb else '其他'}")
        return is_qb

    def __get_downloader(self, name: str):
        """
        根据名称返回下载器实例
        """
        logger.debug(f"【QB种子优化】获取下载器实例: {name}")
        service_info = self.service_infos.get(name)
        if service_info:
            logger.debug(f"【QB种子优化】找到下载器: {name}")
            return service_info.instance
        else:
            logger.error(f"【QB种子优化】未找到下载器: {name}")
            return None

    @eventmanager.register(EventType.PluginAction)
    def handle_qb_optimize(self, event: Event):
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "qb_optimize":
                return
        self.optimize_torrents()

    @eventmanager.register(EventType.PluginAction)
    def handle_qb_status(self, event: Event):
        if not self._enabled:
            return
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "qb_status":
                return
        self.show_status()

    def optimize_torrents(self):
        """
        执行种子优化
        """
        logger.info("【QB种子优化】开始执行种子优化任务...")
        logger.info(f"【QB种子优化】配置的下载器: {self._downloaders}")
        
        if not self._downloaders:
            logger.error("【QB种子优化】错误：未配置下载器，无法执行优化任务")
            return
        
        total_zero_seed_removed = 0
        total_priority_boosted = 0
        total_timeout_removed = 0
        all_failed_torrents = []  # 收集所有重新下载失败的种子
        
        for downloader_name in self._downloaders:
            logger.info(f"【QB种子优化】开始处理下载器: {downloader_name}")
            
            try:
                with lock:
                    logger.debug(f"【QB种子优化】获取下载器实例: {downloader_name}")
                    downloader_obj = self.__get_downloader(downloader_name)
                    if not downloader_obj:
                        logger.error(f"【QB种子优化】获取下载器失败: {downloader_name}")
                        continue

                    logger.info(f"【QB种子优化】开始获取种子列表: {downloader_name}")
                    # 获取所有种子
                    all_torrents, error = downloader_obj.get_torrents()
                    if error:
                        logger.error(f"【QB种子优化】获取下载器:{downloader_name}种子失败: {error}")
                        continue

                    if not all_torrents:
                        logger.info(f"【QB种子优化】下载器:{downloader_name}没有种子")
                        continue

                    logger.info(f"【QB种子优化】下载器:{downloader_name}种子总数: {len(all_torrents)}")
                    
                    # 统计种子状态
                    downloading_count = len([t for t in all_torrents if t.state_enum.is_downloading])
                    uploading_count = len([t for t in all_torrents if t.state_enum.is_uploading])
                    logger.info(f"【QB种子优化】下载中: {downloading_count}, 做种中: {uploading_count}")
                    
                    # 添加状态分布统计
                    state_distribution = {}
                    for torrent in all_torrents:
                        state = getattr(torrent, 'state', 'unknown')
                        state_distribution[state] = state_distribution.get(state, 0) + 1
                    
                    logger.info(f"【QB种子优化】状态分布: {state_distribution}")
                    
                    # 显示前几个下载中种子的详细信息
                    downloading_torrents = [t for t in all_torrents if t.state_enum.is_downloading]
                    logger.info(f"【QB种子优化】前5个下载中种子详情:")
                    for i, torrent in enumerate(downloading_torrents[:5], 1):
                        seeder_count = self._get_seeder_count(torrent)
                        logger.info(f"  {i}. {torrent.name}")
                        logger.info(f"     - 状态: {getattr(torrent, 'state', 'unknown')}")
                        logger.info(f"     - 做种数: {seeder_count}")
                        logger.info(f"     - 原始字段: num_complete={getattr(torrent, 'num_complete', 'N/A')}, num_seeds={getattr(torrent, 'num_seeds', 'N/A')}")
                    
                    # 执行各项优化
                    logger.info(f"【QB种子优化】开始执行优化操作...")
                    zero_seed_removed, zero_seed_failed = self._clean_zero_seed_torrents(downloader_obj, all_torrents, downloader_name)
                    priority_boosted = self._boost_priority_torrents(downloader_obj, all_torrents, downloader_name)
                    timeout_removed, timeout_failed = self._clean_timeout_torrents(downloader_obj, all_torrents, downloader_name)
                    
                    # 执行磁盘空间和I/O监控
                    disk_monitor_result = self._monitor_disk_and_io(downloader_obj, downloader_name)
                    
                    total_zero_seed_removed += zero_seed_removed
                    total_priority_boosted += priority_boosted
                    total_timeout_removed += timeout_removed
                    
                    # 收集重新下载失败信息
                    all_failed_torrents.extend(zero_seed_failed)
                    all_failed_torrents.extend(timeout_failed)
                    
                    logger.info(f"【QB种子优化】{downloader_name} 优化完成:")
                    logger.info(f"  - 清理下载中无人做种: {zero_seed_removed}个")
                    logger.info(f"  - 综合评分优化: {priority_boosted}个")
                    logger.info(f"  - 清理慢速下载: {timeout_removed}个")

            except Exception as e:
                logger.error(f"【QB种子优化】处理下载器 {downloader_name} 时发生异常：{str(e)}")
                import traceback
                logger.error(f"【QB种子优化】异常详情: {traceback.format_exc()}")
        
        # 输出总结信息到日志
        logger.info(f"【QB种子优化】所有下载器处理完成，总计:")
        logger.info(f"  - 清理下载中无人做种: {total_zero_seed_removed}个")
        logger.info(f"  - 综合评分优化: {total_priority_boosted}个")
        logger.info(f"  - 清理慢速下载: {total_timeout_removed}个")
        logger.info(f"  - 重新下载失败: {len(all_failed_torrents)}个")
        
        # 输出详细总结信息
        if total_zero_seed_removed > 0 or total_timeout_removed > 0:
            logger.info(f"【QB种子优化】本次清理总结:")
            logger.info(f"  清理种子总数: {total_zero_seed_removed + total_timeout_removed}个")
            logger.info(f"  - 清理下载中无人做种: {total_zero_seed_removed}个")
            logger.info(f"  - 清理慢速下载: {total_timeout_removed}个")
            
            if all_failed_torrents:
                logger.warning(f"【QB种子优化】重新下载失败总结: 共{len(all_failed_torrents)}个种子未找到资源")
                for i, failed_torrent in enumerate(all_failed_torrents, 1):
                    logger.warning(f"  {i}. {failed_torrent['name']}")
                    logger.warning(f"     - HASH: {failed_torrent['hash']}")
                    logger.warning(f"     - 大小: {failed_torrent['size']}")
                    logger.warning(f"     - 原因: {failed_torrent['reason']}")
                
                # 发送通知
                if self._notify:
                    summary_message = f"【QB种子优化完成】\n"
                    summary_message += f"本次清理种子: {total_zero_seed_removed + total_timeout_removed}个\n"
                    summary_message += f"  - 清理下载中无人做种: {total_zero_seed_removed}个\n"
                    summary_message += f"  - 清理慢速下载: {total_timeout_removed}个\n"
                    summary_message += f"重新下载失败: {len(all_failed_torrents)}个\n\n"
                    summary_message += "失败种子列表:\n"
                    for i, failed_torrent in enumerate(all_failed_torrents, 1):
                        summary_message += f"{i}. {failed_torrent['name']}\n"
                        summary_message += f"   HASH: {failed_torrent['hash']}\n"
                        summary_message += f"   大小: {failed_torrent['size']}\n"
                        summary_message += f"   原因: {failed_torrent['reason']}\n\n"
                    
                    logger.info(f"【QB种子优化】发送总结通知: 清理{total_zero_seed_removed + total_timeout_removed}个，失败{len(all_failed_torrents)}个")
                    self.post_message(
                        mtype=NotificationType.Manual,
                        title=f"【QB种子优化】清理完成 - {len(all_failed_torrents)}个种子未找到资源",
                        text=summary_message
                    )
            else:
                logger.info(f"【QB种子优化】重新下载成功总结: 清理的{total_zero_seed_removed + total_timeout_removed}个种子全部重新下载成功")
                if self._notify:
                    logger.info(f"【QB种子优化】清理{total_zero_seed_removed + total_timeout_removed}个种子，全部重新下载成功，无需发送通知")
        else:
            logger.info(f"【QB种子优化】本次无清理操作，无需发送通知")

    def _clean_zero_seed_torrents(self, downloader_obj, torrents, downloader_name):
        """
        清理下载中无人做种的任务并重新下载
        """
        logger.info(f"【QB种子优化】开始清理下载中无人做种任务，功能开关: {self._enable_zero_seed_clean}")
        logger.info("【QB种子优化】清理条件: 仅处理状态为 downloading 或 stalledDL 的任务")
        
        if not self._enable_zero_seed_clean:
            logger.info("【QB种子优化】清理下载中无人做种功能已禁用，跳过")
            return 0, []
            
        # 记录重新下载失败的种子
        redownload_failed_torrents = []
            
        logger.info(f"【功能1-无人做种】检查条件: 做种数阈值={self._zero_seed_threshold}, 目标状态=downloading/stalledDL")
        
        zero_seed_torrents = []
        checked_count = 0
        
        for torrent in torrents:
            checked_count += 1
            logger.debug(f"【QB种子优化】检查种子 {checked_count}/{len(torrents)}: {torrent.name}")
            
            # 仅处理 downloading 或 stalledDL
            state_name = getattr(torrent, 'state', '') or ''
            state_lower = state_name.lower()
            is_target_state = state_lower in ('downloading', 'stalleddl')
            seeder_count = self._get_seeder_count(torrent)
            
            # 添加详细的调试日志
            if checked_count <= 10:  # 只显示前10个种子的详细信息
                logger.info(f"【功能1-无人做种】种子检查详情 {checked_count}: {torrent.name}")
                logger.info(f"  - 状态: {state_name} (state_lower={state_lower})")
                logger.info(f"  - 是否目标状态(downloading/stalledDL): {is_target_state}")
                logger.info(f"  - 做种数: {seeder_count} (阈值={self._zero_seed_threshold})")
                logger.info(f"  - 原始字段: num_complete={getattr(torrent, 'num_complete', 'N/A')}, num_seeds={getattr(torrent, 'num_seeds', 'N/A')}, seeds={getattr(torrent, 'seeds', 'N/A')}")
            
            if is_target_state and seeder_count <= self._zero_seed_threshold:
                
                logger.debug(f"【功能1-无人做种】种子符合条件: {torrent.name}")
                logger.debug(f"  - 状态: {state_name}")
                logger.debug(f"  - 做种数: {seeder_count}")
                logger.debug(f"  - 下载进度: {torrent.progress * 100:.1f}%")
                
                logger.info(f"【功能1-无人做种】添加待清理种子: {torrent.name}")
                zero_seed_torrents.append(torrent)
        
        logger.info(f"【功能1-无人做种】检查完成，发现{len(zero_seed_torrents)}个下载中无人做种的任务")
        
        # 根据配置限制处理数量
        if len(zero_seed_torrents) > self._max_zero_seed_process:
            logger.info(f"【功能1-无人做种】种子数量({len(zero_seed_torrents)})超过配置限制({self._max_zero_seed_process})，只处理前{self._max_zero_seed_process}个")
            zero_seed_torrents = zero_seed_torrents[:self._max_zero_seed_process]
        
        if zero_seed_torrents:
            logger.info(f"【功能1-无人做种】开始删除下载中无人做种的任务并重新下载...")
            for i, torrent in enumerate(zero_seed_torrents, 1):
                logger.info(f"【功能1-无人做种】删除审计-开始 ({i}/{len(zero_seed_torrents)}): {torrent.name}")
                logger.info(f"  - HASH: {getattr(torrent, 'hash', 'N/A')}")
                logger.info(f"  - 做种数: {self._get_seeder_count(torrent)}")
                logger.info(f"  - 下载进度: {torrent.progress * 100:.1f}%")
                logger.info(f"  - 大小: {StringUtils.str_filesize(torrent.size)}")
                logger.info(f"  - 状态: {getattr(torrent, 'state', 'unknown')}")
                logger.info(f"  - 分类: {getattr(torrent, 'category', 'N/A')}")
                logger.info(f"  - 标签: {getattr(torrent, 'tags', 'N/A')}")
                logger.info(f"  - Tracker: {getattr(torrent, 'tracker', 'N/A')}")
                
                try:
                    # 先记录种子信息用于重新下载（包含下载目录）
                    torrent_info = {
                        'name': torrent.name,
                        'size': torrent.size,
                        'hash': torrent.hash,
                        'tracker': getattr(torrent, 'tracker', ''),
                        'category': getattr(torrent, 'category', ''),
                        'tags': getattr(torrent, 'tags', ''),
                        'save_path': getattr(torrent, 'save_path', '') or getattr(torrent, 'content_path', '')
                    }
                    
                    # 删除种子（保留文件）
                    result = downloader_obj.delete_torrents(delete_file=False, ids=[torrent.hash])
                    if result:
                        logger.info(f"【功能1-无人做种】删除审计-成功: {torrent.name} (HASH={getattr(torrent, 'hash', 'N/A')})，已保留数据文件")
                        # 重新下载逻辑
                        if self._enable_auto_redownload and self._search_sites:
                            logger.info(f"【功能1-无人做种】开始搜索同名种子文件，做种数最多的作为下载任务")
                            redownload_success = self._re_download_torrent(torrent_info)
                            if not redownload_success:
                                redownload_failed_torrents.append({
                                    'name': torrent.name,
                                    'hash': getattr(torrent, 'hash', 'N/A'),
                                    'size': StringUtils.str_filesize(torrent.size),
                                    'category': getattr(torrent, 'category', 'N/A'),
                                    'tags': getattr(torrent, 'tags', 'N/A'),
                                    'tracker': getattr(torrent, 'tracker', 'N/A'),
                                    'reason': '无人做种'
                                })
                        elif not self._search_sites:
                            logger.info(f"【功能1-无人做种】未配置搜索站点，跳过重新下载: {torrent.name}")
                        else:
                            logger.info(f"【功能1-无人做种】自动重新下载已禁用，跳过重新下载: {torrent.name}")
                    else:
                        logger.error(f"【功能1-无人做种】删除审计-失败: {torrent.name} (HASH={getattr(torrent, 'hash', 'N/A')})")
                    
                except Exception as e:
                    logger.error(f"【功能1-无人做种】删除审计-异常: {torrent.name} (HASH={getattr(torrent, 'hash', 'N/A')}), 错误: {str(e)}")
        
        # 输出重新下载失败总结
        if redownload_failed_torrents:
            logger.warning(f"【功能1-无人做种】重新下载失败总结: 共{len(redownload_failed_torrents)}个种子")
            for i, failed_torrent in enumerate(redownload_failed_torrents, 1):
                logger.warning(f"  {i}. {failed_torrent['name']}")
                logger.warning(f"     - HASH: {failed_torrent['hash']}")
                logger.warning(f"     - 大小: {failed_torrent['size']}")
                logger.warning(f"     - 分类: {failed_torrent['category']}")
                logger.warning(f"     - 标签: {failed_torrent['tags']}")
                logger.warning(f"     - Tracker: {failed_torrent['tracker']}")
                logger.warning(f"     - 原因: {failed_torrent['reason']}")
        
        return len(zero_seed_torrents), redownload_failed_torrents

    def _boost_priority_torrents(self, downloader_obj, torrents, downloader_name):
        """
        提升做种数多的种子下载优先级
        """
        logger.info(f"【功能3-综合评分】开始综合评分优先级优化，功能开关: {self._enable_score_priority}")
        
        if not self._enable_score_priority:
            logger.info("【功能3-综合评分】综合评分优先级优化功能已禁用，跳过")
            return 0
            
        logger.info(f"【功能3-综合评分】检查条件: 最低综合评分阈值={self._min_score_threshold}")

        high_seed_torrents = []
        checked_count = 0

        # 统计信息用于调试
        state_counter = {}
        downloading_like_counter = 0
        completed_counter = 0
        queued_counter = 0
        seeds_ge_1 = 0
        seeds_ge_5 = 0
        seeds_ge_10 = 0

        # 分类种子
        downloading_torrents = []  # 正在下载的种子
        queued_torrents = []       # 排队中的种子

        for torrent in torrents:
            checked_count += 1
            state_name = getattr(torrent, 'state', '') or ''
            state_counter[state_name] = state_counter.get(state_name, 0) + 1

            # 检查是否为已完成状态
            is_completed = False
            try:
                progress = getattr(torrent, 'progress', 0)
                is_completed = progress >= 1.0  # 进度100%视为已完成
            except Exception:
                is_completed = False
            
            if is_completed:
                completed_counter += 1
                continue  # 跳过已完成的种子

            # 分类种子状态
            state_lower = (state_name or '').lower()
            is_downloading = 'downloading' in state_lower or 'metadl' in state_lower
            is_queued = 'queueddl' in state_lower or state_lower == 'queued'
            
            # 调试日志：查看前几个种子的状态分类
            if checked_count <= 10:
                logger.debug(f"【QB种子优化】状态分类调试: {torrent.name} - 原始状态: '{state_name}' - 小写: '{state_lower}' - 下载中: {is_downloading} - 排队中: {is_queued}")
            
            if is_downloading:
                downloading_like_counter += 1
                downloading_torrents.append(torrent)
            elif is_queued:
                queued_counter += 1
                queued_torrents.append(torrent)
                
        # 只保留真正排队中的种子（排除等待和进行中的）
        true_queued_torrents = []
        for torrent in queued_torrents:
            state_name = getattr(torrent, 'state', '') or ''
            state_lower = state_name.lower()
            
            # 只处理真正排队中的种子，排除其他状态
            if 'queueddl' in state_lower or state_lower == 'queued':
                true_queued_torrents.append(torrent)
            else:
                logger.debug(f"【QB种子优化】跳过非排队状态种子: {torrent.name} (状态: {state_name})")
        
        queued_torrents = true_queued_torrents
        logger.info(f"【QB种子优化】真正排队中的种子数量: {len(queued_torrents)}个")
        # 按队列顺序排序，确保优先处理排在最前的任务（例如 101、102...）
        try:
            queued_torrents.sort(key=lambda t: self._get_queue_position(t))
            logger.info(f"【QB种子优化】按队列位置排序完成，展示前20个队列位置:")
            for idx, t in enumerate(queued_torrents[:20], 1):
                queue_pos = self._get_queue_position(t)
                logger.info(f"  {idx:2d}. 队列位置: {queue_pos:3d} | {getattr(t, 'name', '')}")
        except Exception as e:
            logger.warning(f"【QB种子优化】按队列顺序排序失败，将按默认顺序处理: {str(e)}")

        # 处理正在下载的种子
        logger.info(f"【QB种子优化】处理正在下载的种子: {len(downloading_torrents)}个")
        for torrent in downloading_torrents:
            seeder_count = self._get_seeder_count(torrent)
            dlspeed = getattr(torrent, 'dlspeed', 0)
            progress = getattr(torrent, 'progress', 0) * 100
            
            # 计算综合评分：使用配置的权重
            speed_score = (dlspeed / (1024 * 1024)) * self._speed_weight  # 转换为MB/s并应用权重
            seeder_score = seeder_count * self._seeder_weight  # 做种数权重
            
            total_score = speed_score + seeder_score
            
            logger.debug(f"【QB种子优化】种子评分: {torrent.name}")
            logger.debug(f"  - 做种数: {seeder_count}, 下载速度: {StringUtils.str_filesize(dlspeed)}/s, 进度: {progress:.1f}%")
            logger.debug(f"  - 速度评分: {speed_score:.2f}, 做种评分: {seeder_score:.2f}")
            logger.debug(f"  - 综合评分: {total_score:.2f}")
            
            # 使用综合评分判断是否加入优化
            if total_score >= self._min_score_threshold:
                logger.debug(f"【QB种子优化】下载中种子符合条件: {torrent.name} (综合评分: {total_score:.2f})")
                high_seed_torrents.append({
                    'torrent': torrent,
                    'score': total_score,
                    'seeder_count': seeder_count,
                    'dlspeed': dlspeed,
                    'progress': progress
                })
            
            # 统计做种数分布
            if seeder_count >= 1:
                seeds_ge_1 += 1
            if seeder_count >= 5:
                seeds_ge_5 += 1
            if seeder_count >= 10:
                seeds_ge_10 += 1

        # 处理排队中的种子（分批启动获取做种数）
        if queued_torrents:
            logger.info(f"【QB种子优化】发现排队中的种子: {len(queued_torrents)}个")
            
            # 只处理队列最前面的任务，避免重复处理不合格的任务
            # 限制处理数量，避免负载过高
            max_process_count = min(self._max_priority_process, len(queued_torrents))
            queued_torrents = queued_torrents[:max_process_count]
            
            logger.info(f"【QB种子优化】准备处理队列最前面的{len(queued_torrents)}个种子:")
            for idx, torrent in enumerate(queued_torrents, 1):
                queue_pos = self._get_queue_position(torrent)
                logger.info(f"  处理第{idx}个: 队列位置 {queue_pos} | {torrent.name}")
            self._process_queued_torrents(downloader_obj, queued_torrents, high_seed_torrents)

        # 输出统计日志帮助定位为何为0
        logger.info(f"【QB种子优化】已完成种子数量: {completed_counter}")
        logger.info(f"【QB种子优化】正在下载的种子数量: {downloading_like_counter}")
        logger.info(f"【QB种子优化】排队中的种子数量: {queued_counter}")
        logger.info(f"【QB种子优化】做种数分布统计(仅下载中): ≥1:{seeds_ge_1} ≥5:{seeds_ge_5} ≥10:{seeds_ge_10}")
        top_states = sorted(state_counter.items(), key=lambda x: x[1], reverse=True)[:8]
        logger.info(f"【QB种子优化】状态分布Top: {top_states}")
        logger.info(f"【QB种子优化】处理策略: 直接处理下载中种子，分批启动排队种子获取做种数")
        
        logger.info(f"【QB种子优化】检查完成，发现{len(high_seed_torrents)}个高做种数的种子")
        
        # 根据配置限制处理数量
        if len(high_seed_torrents) > self._max_priority_process:
            logger.info(f"【功能3-综合评分】种子数量({len(high_seed_torrents)})超过配置限制({self._max_priority_process})，只处理前{self._max_priority_process}个")
            high_seed_torrents = high_seed_torrents[:self._max_priority_process]
        
        if high_seed_torrents:
            # 按照综合评分排序，评分最高的排在最前面
            high_seed_torrents.sort(key=lambda x: x['score'], reverse=True)
            
            logger.info(f"【QB种子优化】按综合评分排序后的种子列表:")
            for i, torrent_info in enumerate(high_seed_torrents, 1):
                torrent = torrent_info['torrent']
                score = torrent_info['score']
                seeder_count = torrent_info['seeder_count']
                dlspeed = torrent_info['dlspeed']
                progress = torrent_info['progress']
                logger.info(f"  {i}. {torrent.name}")
                logger.info(f"     综合评分: {score:.2f}, 做种数: {seeder_count}, 下载速度: {StringUtils.str_filesize(dlspeed)}/s, 进度: {progress:.1f}%")
            
            logger.info(f"【QB种子优化】开始设置种子下载优先级...")
            logger.info(f"【QB种子优化】策略: 彻底重置优先级，按做种数重新排序")
            
            total_count = len(high_seed_torrents)
            success_count = 0
            
            # 第一步：先将所有种子移到底部，清除历史优先级
            logger.info(f"【QB种子优化】第一步：将所有{total_count}个种子移到底部，清除历史优先级")
            for i, torrent_info in enumerate(high_seed_torrents, 1):
                torrent = torrent_info['torrent']
                score = torrent_info['score']
                logger.info(f"【QB种子优化】移到底部 {i}/{total_count}: {torrent.name} (综合评分: {score:.2f})")
                
                try:
                    # 先移到底部，清除历史优先级
                    bottom_result = self._set_qb_torrent_priority(downloader_obj, torrent.hash, 999)
                    
                    if bottom_result:
                        success_count += 1
                        logger.debug(f"【QB种子优化】移到底部成功: {torrent.name}")
                    else:
                        logger.error(f"【QB种子优化】移到底部失败: {torrent.name}")
                        
                except Exception as e:
                    logger.error(f"【QB种子优化】移到底部时发生异常: {torrent.name}, 错误: {str(e)}")
            
            logger.info(f"【QB种子优化】第一步完成：成功移到底部 {success_count}/{total_count} 个种子")
            
            # 等待一下让qBittorrent处理完
            logger.info(f"【QB种子优化】等待2秒让qBittorrent处理优先级变更...")
            time.sleep(2)
            
            # 第二步：从最后一名开始置顶，确保第1名最终在最前面
            logger.info(f"【QB种子优化】第二步：从最后一名开始置顶，第1名最后置顶")
            # 倒序处理：从最后一名到第一名
            for i in range(total_count, 0, -1):
                torrent_info = high_seed_torrents[i - 1]  # 索引从0开始
                torrent = torrent_info['torrent']
                score = torrent_info['score']
                logger.info(f"【QB种子优化】置顶第{i}名/{total_count}: {torrent.name} (综合评分: {score:.2f})")
                
                try:
                    # 置顶，建立新的排序
                    top_result = self._set_qb_torrent_priority(downloader_obj, torrent.hash, 1)
                    
                    if top_result:
                        logger.info(f"【QB种子优化】置顶成功: {torrent.name} (第{i}名)")
                    else:
                        logger.error(f"【QB种子优化】置顶失败: {torrent.name}")
                    
                    # 每次置顶后稍等一下，确保顺序正确
                    if i > 1:  # 不是最后一次（第1名）
                        time.sleep(0.5)
                        
                except Exception as e:
                    logger.error(f"【QB种子优化】置顶时发生异常: {torrent.name}, 错误: {str(e)}")
            
            # 第三步：验证最终排序
            logger.info(f"【QB种子优化】第三步：验证最终排序结果")
            logger.info(f"【QB种子优化】期望的最终排序（按综合评分从高到低）:")
            for i, torrent_info in enumerate(high_seed_torrents, 1):
                torrent = torrent_info['torrent']
                score = torrent_info['score']
                seeder_count = torrent_info['seeder_count']
                dlspeed = torrent_info['dlspeed']
                progress = torrent_info['progress']
                logger.info(f"  第{i}名: {torrent.name}")
                logger.info(f"    综合评分: {score:.2f}, 做种数: {seeder_count}, 下载速度: {StringUtils.str_filesize(dlspeed)}/s")
            
            logger.info(f"【QB种子优化】优先级设置完成，请检查qBittorrent队列中的实际排序")
        else:
            # 无匹配时输出采样日志
            sample = torrents[:10]
            logger.info("【QB种子优化】未匹配到高做种种子，采样前10条做种信息：")
            for t in sample:
                logger.info(f"  - {getattr(t, 'name', '')} | state={getattr(t, 'state','')} | seeds={self._get_seeder_count(t)} | raw=[num_seeds={getattr(t,'num_seeds',None)}, seeds={getattr(t,'seeds',None)}, seeders={getattr(t,'seeders',None)}, connected_seeds={getattr(t,'connected_seeds',None)}, num_complete={getattr(t,'num_complete',None)}]")
        
        return len(high_seed_torrents)

    def _process_queued_torrents(self, downloader_obj, queued_torrents, high_seed_torrents):
        """
        分批处理排队中的种子，启动获取做种数后决定是否继续下载
        不合格的任务会被移到队列最后，避免重复处理
        """
        if not queued_torrents:
            return
            
        # 简化批次处理，每次只处理少量种子
        batch_size = 5  # 固定批次大小，避免负载过高
        
        total_batches = (len(queued_torrents) + batch_size - 1) // batch_size
        
        logger.info(f"【QB种子优化】分批处理排队种子: 总数{len(queued_torrents)}个，分{total_batches}批，每批{batch_size}个")
        logger.info(f"【QB种子优化】批次处理详情:")
        for batch_num in range(total_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(queued_torrents))
            batch_torrents = queued_torrents[start_idx:end_idx]
            logger.info(f"  第{batch_num + 1}批: 处理 {start_idx + 1}-{end_idx} 号种子")
            for idx, torrent in enumerate(batch_torrents, start_idx + 1):
                queue_pos = self._get_queue_position(torrent)
                logger.info(f"    {idx}. 队列位置 {queue_pos} | {torrent.name}")
        
        for batch_num in range(total_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(queued_torrents))
            batch_torrents = queued_torrents[start_idx:end_idx]
            
            logger.info(f"【QB种子优化】处理第{batch_num + 1}/{total_batches}批: {len(batch_torrents)}个种子")
            
            # 启动并置顶这一批种子
            started_hashes = []
            for torrent in batch_torrents:
                try:
                    queue_pos = self._get_queue_position(torrent)
                    logger.info(f"【QB种子优化】启动并置顶种子获取做种数: 队列位置 {queue_pos} | {torrent.name}")
                    # 先启动种子
                    start_result = downloader_obj.start_torrents(ids=[torrent.hash])
                    if start_result:
                        # 再置顶种子，确保有下载速度
                        priority_result = self._set_qb_torrent_priority(downloader_obj, torrent.hash, 1)
                        if priority_result:
                            started_hashes.append(torrent.hash)
                            logger.debug(f"【QB种子优化】启动并置顶成功: {torrent.name}")
                        else:
                            logger.warning(f"【QB种子优化】置顶失败: {torrent.name}")
                    else:
                        logger.warning(f"【QB种子优化】启动失败: {torrent.name}")
                except Exception as e:
                    logger.error(f"【QB种子优化】启动种子异常: {torrent.name}, 错误: {str(e)}")
            
            if not started_hashes:
                logger.warning(f"【QB种子优化】第{batch_num + 1}批没有成功启动的种子，跳过")
                continue
            
            # 等待更长时间让种子获取到真实的做种数信息
            logger.info(f"【QB种子优化】等待10秒让种子获取真实做种数信息...")
            time.sleep(10)
            
            # 重新获取种子信息
            try:
                updated_torrents, error = downloader_obj.get_torrents(ids=started_hashes)
                if error or not updated_torrents:
                    logger.warning(f"【QB种子优化】无法获取更新后的种子信息")
                    continue
                
                # 检查综合评分并决定是否继续下载
                # 为了保持和队列顺序一致，按队列位置排序后再处理
                try:
                    updated_torrents.sort(key=lambda t: self._get_queue_position(t))
                except Exception:
                    pass
                for torrent in updated_torrents:
                    seeder_count = self._get_seeder_count(torrent)
                    # 获取更多详细信息用于调试
                    progress = getattr(torrent, 'progress', 0) * 100
                    dlspeed = getattr(torrent, 'dlspeed', 0)
                    state = getattr(torrent, 'state', 'unknown')
                    
                    # 计算综合评分：使用配置的权重
                    speed_score = (dlspeed / (1024 * 1024)) * self._speed_weight  # 转换为MB/s并应用权重
                    seeder_score = seeder_count * self._seeder_weight  # 做种数权重
                    total_score = speed_score + seeder_score
                    
                    logger.info(f"【QB种子优化】种子综合评分检查: {torrent.name}")
                    logger.info(f"  - 做种数: {seeder_count}")
                    logger.info(f"  - 下载进度: {progress:.1f}%")
                    logger.info(f"  - 下载速度: {StringUtils.str_filesize(dlspeed)}/s")
                    logger.info(f"  - 状态: {state}")
                    logger.info(f"  - 综合评分: {total_score:.2f} (速度:{speed_score:.2f} + 做种:{seeder_score:.2f})")
                    
                    if total_score >= self._min_score_threshold:
                        logger.info(f"【QB种子优化】排队种子符合条件，加入优先级优化: {torrent.name} (综合评分: {total_score:.2f})")
                        high_seed_torrents.append({
                            'torrent': torrent,
                            'score': total_score,
                            'seeder_count': seeder_count,
                            'dlspeed': dlspeed,
                            'progress': progress
                        })
                    else:
                        logger.info(f"【QB种子优化】排队种子综合评分不足，移到队列最后: {torrent.name} (综合评分: {total_score:.2f})")
                        # 将不合格的种子移到队列最后，避免下次重复处理
                        try:
                            bottom_result = self._set_qb_torrent_priority(downloader_obj, torrent.hash, 999)  # 999表示底部
                            if bottom_result:
                                logger.info(f"【QB种子优化】已移到队列最后: {torrent.name}")
                            else:
                                logger.warning(f"【QB种子优化】移到队列最后失败: {torrent.name}")
                        except Exception as e:
                            logger.error(f"【QB种子优化】移到队列最后异常: {torrent.name}, 错误: {str(e)}")
                
            except Exception as e:
                logger.error(f"【QB种子优化】处理第{batch_num + 1}批种子时发生异常: {str(e)}")
            
            # 批次间等待，避免负载过高
            if batch_num < total_batches - 1:  # 不是最后一批
                logger.info(f"【QB种子优化】等待3秒后处理下一批...")
                time.sleep(3)
        
        logger.info(f"【QB种子优化】排队种子分批处理完成")

    def _get_seeder_count(self, torrent) -> int:
        """获取做种数，支持多种度量方式：num_complete/connected/availability/auto"""
        metric = (self._seeder_metric or 'num_complete').lower()
        # 优先使用显式度量
        if metric == 'num_complete':
            # 直接使用 num_complete 字段作为做种数
            return self._first_int_attr(torrent, ['num_complete'])
        if metric == 'connected':
            # connected peers as seeders fallback (有的客户端会提供connected种子数)
            return self._first_int_attr(torrent, ['connected_seeds', 'num_complete', 'seeds'])
        if metric == 'availability':
            # 可用性近似：可用性*10 作为粗略整数（避免浮点），仅用于排序
            avail = getattr(torrent, 'availability', None)
            try:
                if isinstance(avail, (int, float)):
                    return int(max(avail, 0) * 10)
            except Exception:
                pass
            return self._first_int_attr(torrent, ['num_seeds', 'seeds', 'seeders'])
        # auto：按照常见字段顺序尝试（包含总计字段）
        return self._first_int_attr(torrent, [
            'num_complete', 'num_seeds', 'seeds', 'seeders', 'connected_seeds'
        ])

    @staticmethod
    def _get_queue_position(torrent) -> int:
        """
        获取种子的队列位置（用于排序）。不同版本的对象可能字段名不同，做兼容。
        优先顺序：priority -> queue_position -> queue_pos -> qpos -> index。
        取不到时返回一个较大的数，避免被优先处理。
        """
        fallback = 10 ** 9
        for name in ['priority', 'queue_position', 'queue_pos', 'qpos', 'index']:
            try:
                val = getattr(torrent, name, None)
                if isinstance(val, (int, float)):
                    return int(val)
                # 字符串数字也尝试转为int
                if isinstance(val, str) and val.isdigit():
                    return int(val)
            except Exception:
                continue
        return fallback

    @staticmethod
    def _first_int_attr(obj, names: list) -> int:
        for name in names:
            try:
                val = getattr(obj, name, None)
                if isinstance(val, (int, float)) and val is not None:
                    return int(val)
            except Exception:
                continue
        return 0

    def _set_qb_torrent_priority(self, downloader_obj, torrent_hash: str, priority_rank: int) -> bool:
        """
        使用qBittorrent原生HTTP API设置种子优先级
        :param downloader_obj: 下载器对象
        :param torrent_hash: 种子哈希
        :param priority_rank: 优先级排名（1为最高）
        :return: 是否设置成功
        """
        try:
            logger.debug(f"【QB种子优化】开始设置种子优先级: hash={torrent_hash}, rank={priority_rank}")
            
            # 获取qBittorrent客户端实例
            qb_client = downloader_obj.qbc
            if not qb_client:
                logger.error("【QB种子优化】无法获取qBittorrent客户端")
                return False
            
            # 使用qBittorrentAPI客户端的_request方法直接调用API
            try:
                # 根据优先级排名选择API端点和调用策略
                if priority_rank == 1:
                    # 最高优先级：设置为顶部
                    logger.debug(f"【QB种子优化】设置种子为最高优先级: topPrio")
                    response = qb_client._request(
                        http_method='POST',
                        api_namespace='torrents',
                        api_method='topPrio',
                        data={'hashes': torrent_hash}
                    )
                    
                elif priority_rank == 2:
                    # 下移优先级
                    logger.debug(f"【QB种子优化】设置种子为下移优先级: decreasePrio")
                    response = qb_client._request(
                        http_method='POST',
                        api_namespace='torrents',
                        api_method='decreasePrio',
                        data={'hashes': torrent_hash}
                    )
                    
                elif priority_rank <= 5:
                    # 高优先级：多次上移（rank=3调用1次，rank=4调用2次，rank=5调用3次）
                    logger.debug(f"【QB种子优化】设置种子为高优先级: increasePrio, 调用次数: {priority_rank - 2}")
                    
                    # 多次调用increasePrio
                    for i in range(priority_rank - 2):
                        response = qb_client._request(
                            http_method='POST',
                            api_namespace='torrents',
                            api_method='increasePrio',
                            data={'hashes': torrent_hash}
                        )
                        logger.debug(f"【QB种子优化】第{i+1}次上移成功")
                    
                elif priority_rank == 999:
                    # 最低优先级：设置为底部
                    logger.debug(f"【QB种子优化】设置种子为最低优先级: bottomPrio")
                    response = qb_client._request(
                        http_method='POST',
                        api_namespace='torrents',
                        api_method='bottomPrio',
                        data={'hashes': torrent_hash}
                    )
                    
                else:
                    # 普通优先级：设置为底部
                    logger.debug(f"【QB种子优化】设置种子为普通优先级: bottomPrio")
                    response = qb_client._request(
                        http_method='POST',
                        api_namespace='torrents',
                        api_method='bottomPrio',
                        data={'hashes': torrent_hash}
                    )
                
                logger.info(f"【QB种子优化】优先级设置成功: {torrent_hash}")
                return True
                
            except Exception as api_error:
                logger.error(f"【QB种子优化】qBittorrent API调用失败: {str(api_error)}")
                return False
                
        except Exception as e:
            logger.error(f"【QB种子优化】设置优先级时发生异常: {str(e)}")
            import traceback
            logger.error(f"【QB种子优化】设置优先级异常详情: {traceback.format_exc()}")
            return False

    def _clean_timeout_torrents(self, downloader_obj, torrents, downloader_name):
        """
        清理预计下载时长超过N小时且已下载N小时的慢速任务
        """
        logger.info(f"【功能2-慢速下载】开始清理慢速下载任务，功能开关: {self._enable_timeout_clean}")
        
        if not self._enable_timeout_clean:
            logger.info("【功能2-慢速下载】清理慢速下载功能已禁用，跳过")
            return 0, []
            
        # 记录重新下载失败的种子
        redownload_failed_torrents = []
            
        current_time = int(time.time())
        timeout_seconds = self._timeout_hours * 3600
        logger.info(f"【功能2-慢速下载】检查条件: 预计下载时长阈值={self._timeout_hours}小时, 超时秒数={timeout_seconds}秒, 目标状态=downloading/stalledDL")
        
        timeout_torrents = []
        checked_count = 0
        
        for torrent in torrents:
            checked_count += 1
            logger.debug(f"【QB种子优化】检查种子 {checked_count}/{len(torrents)}: {torrent.name}")
            
            # 检查是否为下载中状态且有种子
            state_name = getattr(torrent, 'state', '') or ''
            # 只处理 downloading 和 stalledDL 状态的任务
            is_target_state = state_name in ['downloading', 'stalledDL']
            
            if is_target_state:
                # 检查是否有种子
                seeder_count = self._get_seeder_count(torrent)
                if seeder_count > 0:  # 有种子
                    # 计算已下载时长
                    added_time = torrent.added_on
                    downloaded_duration = current_time - added_time
                    downloaded_hours = downloaded_duration / 3600
                    
                    # 计算预计下载时长
                    # 基于当前下载速度和剩余大小计算
                    progress = getattr(torrent, 'progress', 0)
                    size = getattr(torrent, 'size', 0)
                    dlspeed = getattr(torrent, 'dlspeed', 0)
                    
                    if progress > 0 and dlspeed > 0:
                        # 计算剩余大小
                        remaining_size = size * (1 - progress)
                        # 计算预计剩余时间（秒）
                        estimated_remaining_seconds = remaining_size / dlspeed
                        # 计算总预计下载时长
                        estimated_total_hours = (downloaded_duration + estimated_remaining_seconds) / 3600
                    else:
                        # 如果无法计算，使用已下载时长作为预计时长
                        estimated_total_hours = downloaded_hours
                    
                    logger.debug(f"【功能2-慢速下载】种子慢速下载检查: {torrent.name}")
                    logger.debug(f"  - 做种数: {seeder_count}")
                    logger.debug(f"  - 已下载时长: {downloaded_hours:.1f}小时")
                    logger.debug(f"  - 下载进度: {progress * 100:.1f}%")
                    logger.debug(f"  - 下载速度: {StringUtils.str_filesize(dlspeed)}/s")
                    logger.debug(f"  - 预计总时长: {estimated_total_hours:.1f}小时")
                    logger.debug(f"  - 超时阈值: {self._timeout_hours}小时")
                    
                    # 只有当预计下载时长超过阈值且已下载时长也超过阈值时才清理
                    if estimated_total_hours > self._timeout_hours and downloaded_hours > self._timeout_hours:
                        logger.debug(f"【功能2-慢速下载】种子慢速下载超时: {torrent.name}")
                        
                        logger.info(f"【功能2-慢速下载】添加待清理慢速下载种子: {torrent.name}")
                        timeout_torrents.append(torrent)
        
        logger.info(f"【功能2-慢速下载】检查完成，发现{len(timeout_torrents)}个慢速下载的种子")
        
        # 根据配置限制处理数量
        if len(timeout_torrents) > self._max_timeout_process:
            logger.info(f"【功能2-慢速下载】种子数量({len(timeout_torrents)})超过配置限制({self._max_timeout_process})，只处理前{self._max_timeout_process}个")
            timeout_torrents = timeout_torrents[:self._max_timeout_process]
        
        if timeout_torrents:
            logger.info(f"【功能2-慢速下载】开始删除慢速下载的种子...")
            for i, torrent in enumerate(timeout_torrents, 1):
                # 重新计算信息用于日志显示
                added_time = torrent.added_on
                downloaded_hours = (current_time - added_time) / 3600
                progress = getattr(torrent, 'progress', 0)
                dlspeed = getattr(torrent, 'dlspeed', 0)
                size = getattr(torrent, 'size', 0)
                
                if progress > 0 and dlspeed > 0:
                    remaining_size = size * (1 - progress)
                    estimated_remaining_seconds = remaining_size / dlspeed
                    estimated_total_hours = (downloaded_hours * 3600 + estimated_remaining_seconds) / 3600
                else:
                    estimated_total_hours = downloaded_hours
                
                logger.info(f"【功能2-慢速下载】删除审计-开始 ({i}/{len(timeout_torrents)}): {torrent.name}")
                logger.info(f"  - HASH: {getattr(torrent, 'hash', 'N/A')}")
                logger.info(f"  - 已下载时长: {downloaded_hours:.1f}小时")
                logger.info(f"  - 预计总时长: {estimated_total_hours:.1f}小时")
                logger.info(f"  - 下载进度: {progress * 100:.1f}%")
                logger.info(f"  - 下载速度: {StringUtils.str_filesize(dlspeed)}/s")
                logger.info(f"  - 做种数: {self._get_seeder_count(torrent)}")
                logger.info(f"  - 大小: {StringUtils.str_filesize(torrent.size)}")
                logger.info(f"  - 状态: {getattr(torrent, 'state', 'unknown')}")
                logger.info(f"  - 分类: {getattr(torrent, 'category', 'N/A')}")
                logger.info(f"  - 标签: {getattr(torrent, 'tags', 'N/A')}")
                logger.info(f"  - Tracker: {getattr(torrent, 'tracker', 'N/A')}")
                
                try:
                    # 删除种子和文件
                    result = downloader_obj.delete_torrents(delete_file=True, ids=[torrent.hash])
                    if result:
                        logger.info(f"【功能2-慢速下载】删除审计-成功: {torrent.name} (HASH={getattr(torrent, 'hash', 'N/A')})，已删除种子与数据文件")
                        # 重新下载逻辑
                        if self._enable_auto_redownload and self._search_sites:
                            logger.info(f"【功能2-慢速下载】开始搜索同名种子文件，做种数最多的作为下载任务")
                            torrent_info = {
                                'name': torrent.name,
                                'size': torrent.size,
                                'hash': torrent.hash,
                                'tracker': getattr(torrent, 'tracker', ''),
                                'category': getattr(torrent, 'category', ''),
                                'tags': getattr(torrent, 'tags', ''),
                                'save_path': getattr(torrent, 'save_path', '') or getattr(torrent, 'content_path', '')
                            }
                            redownload_success = self._re_download_torrent(torrent_info)
                            if not redownload_success:
                                redownload_failed_torrents.append({
                                    'name': torrent.name,
                                    'hash': getattr(torrent, 'hash', 'N/A'),
                                    'size': StringUtils.str_filesize(torrent.size),
                                    'category': getattr(torrent, 'category', 'N/A'),
                                    'tags': getattr(torrent, 'tags', 'N/A'),
                                    'tracker': getattr(torrent, 'tracker', 'N/A'),
                                    'reason': f'慢速下载{downloaded_hours:.1f}小时，预计{estimated_total_hours:.1f}小时'
                                })
                        elif not self._search_sites:
                            logger.info(f"【功能2-慢速下载】未配置搜索站点，跳过重新下载: {torrent.name}")
                        else:
                            logger.info(f"【功能2-慢速下载】自动重新下载已禁用，跳过重新下载: {torrent.name}")
                    else:
                        logger.error(f"【功能2-慢速下载】删除审计-失败: {torrent.name} (HASH={getattr(torrent, 'hash', 'N/A')})")
                        
                except Exception as e:
                    logger.error(f"【功能2-慢速下载】删除审计-异常: {torrent.name} (HASH={getattr(torrent, 'hash', 'N/A')}), 错误: {str(e)}")
        
        # 输出重新下载失败总结
        if redownload_failed_torrents:
            logger.warning(f"【功能2-慢速下载】重新下载失败总结: 共{len(redownload_failed_torrents)}个种子")
            for i, failed_torrent in enumerate(redownload_failed_torrents, 1):
                logger.warning(f"  {i}. {failed_torrent['name']}")
                logger.warning(f"     - HASH: {failed_torrent['hash']}")
                logger.warning(f"     - 大小: {failed_torrent['size']}")
                logger.warning(f"     - 分类: {failed_torrent['category']}")
                logger.warning(f"     - 标签: {failed_torrent['tags']}")
                logger.warning(f"     - Tracker: {failed_torrent['tracker']}")
                logger.warning(f"     - 原因: {failed_torrent['reason']}")
        
        return len(timeout_torrents), redownload_failed_torrents

    def _monitor_disk_and_io(self, downloader_obj, downloader_name):
        """
        监控磁盘空间和I/O状态，在条件满足时限制下载速度
        """
        logger.info(f"【功能4-磁盘监控】开始磁盘空间和I/O监控，功能开关: {self._enable_disk_monitor}")
        
        if not self._enable_disk_monitor:
            logger.info("【功能4-磁盘监控】磁盘空间和I/O监控功能已禁用，跳过")
            return False
            
        try:
            # 获取qBittorrent客户端实例
            qb_client = downloader_obj.qbc
            if not qb_client:
                logger.error("【功能4-磁盘监控】无法获取qBittorrent客户端")
                return False
            
            # 调用qBittorrent API获取服务器状态
            try:
                # 使用POST方法，包含rid参数
                response = qb_client._request(
                    http_method='POST',
                    api_namespace='sync',
                    api_method='maindata',
                    data={'rid': 0}
                )
                
                logger.debug(f"【功能4-磁盘监控】API响应类型: {type(response)}")
                logger.debug(f"【功能4-磁盘监控】API响应: {response}")
                
                if not response:
                    logger.error("【功能4-磁盘监控】API响应为空")
                    return False
                
                # 如果响应是HTTP响应对象，需要获取JSON内容
                if hasattr(response, 'json'):
                    try:
                        response_data = response.json()
                        logger.debug(f"【功能4-磁盘监控】解析后的JSON数据: {response_data}")
                    except Exception as json_error:
                        logger.error(f"【功能4-磁盘监控】JSON解析失败: {str(json_error)}")
                        return False
                elif hasattr(response, 'text'):
                    # 如果是HTTP响应对象但没有json方法，尝试解析文本
                    try:
                        import json
                        response_data = json.loads(response.text)
                        logger.debug(f"【功能4-磁盘监控】从文本解析的JSON数据: {response_data}")
                    except Exception as json_error:
                        logger.error(f"【功能4-磁盘监控】文本JSON解析失败: {str(json_error)}, 响应文本: {response.text}")
                        return False
                else:
                    # 如果已经是字典，直接使用
                    response_data = response
                
                if not response_data or 'server_state' not in response_data:
                    logger.error(f"【功能4-磁盘监控】API响应中缺少server_state字段，响应内容: {response_data}")
                    return False
                
                server_state = response_data['server_state']
                
                # 获取各项指标
                free_space_gb = server_state.get('free_space_on_disk', 0) / (1024**3)  # 转换为GB
                write_cache_overload = float(server_state.get('write_cache_overload', 0))  # 百分比，确保是数值
                queued_io_jobs = int(server_state.get('queued_io_jobs', 0))  # I/O任务数，确保是整数
                
                logger.info(f"【功能4-磁盘监控】服务器状态:")
                logger.info(f"  - 磁盘剩余空间: {free_space_gb:.2f}GB (阈值: {self._disk_space_threshold}GB)")
                logger.info(f"  - 写入缓存过载: {write_cache_overload:.1f}% (阈值: {self._io_cache_threshold}%)")
                logger.info(f"  - 队列I/O任务: {queued_io_jobs} (阈值: {self._io_queue_threshold})")
                
                # 判断是否需要限制速度
                disk_space_insufficient = free_space_gb < self._disk_space_threshold
                io_cache_high = write_cache_overload > self._io_cache_threshold
                io_queue_high = queued_io_jobs > self._io_queue_threshold
                
                logger.info(f"【功能4-磁盘监控】检查结果:")
                logger.info(f"  - 磁盘空间不足: {disk_space_insufficient} ({free_space_gb:.2f}GB < {self._disk_space_threshold}GB)")
                logger.info(f"  - I/O缓存过高: {io_cache_high} ({write_cache_overload:.1f}% > {self._io_cache_threshold}%)")
                logger.info(f"  - I/O队列过高: {io_queue_high} ({queued_io_jobs} > {self._io_queue_threshold})")
                
                # 判断是否需要限制或恢复速度
                should_limit = disk_space_insufficient or (io_cache_high and io_queue_high)
                
                logger.info(f"【功能4-磁盘监控】状态检查:")
                logger.info(f"  - 是否需要限速: {should_limit}")
                logger.info(f"  - 当前是否已限速: {self._is_speed_limited}")
                logger.info(f"  - 磁盘空间不足: {disk_space_insufficient}")
                logger.info(f"  - I/O缓存过高: {io_cache_high}")
                logger.info(f"  - I/O队列过高: {io_queue_high}")
                
                if should_limit and not self._is_speed_limited:
                    # 需要限速且当前未限速
                    logger.warning(f"【功能4-磁盘监控】检测到系统资源不足，开始限制下载速度")
                    
                    # 限制下载速度
                    speed_limit_bytes = int(self._speed_limit_mbps * 1024 * 1024)  # 转换为字节/秒
                    success = self._set_download_speed_limit(downloader_obj, speed_limit_bytes)
                    
                    if success:
                        self._is_speed_limited = True  # 标记为已限速
                        logger.info(f"【功能4-磁盘监控】下载速度限制成功: {self._speed_limit_mbps}MB/s")
                        
                        # 发送通知
                        if self._notify:
                            notification_title = "【QB种子优化】系统资源不足，已限制下载速度"
                            notification_text = f"检测到以下问题:\n"
                            if disk_space_insufficient:
                                notification_text += f"• 磁盘空间不足: {free_space_gb:.2f}GB (阈值: {self._disk_space_threshold}GB)\n"
                            if io_cache_high:
                                notification_text += f"• I/O缓存使用率过高: {write_cache_overload:.1f}% (阈值: {self._io_cache_threshold}%)\n"
                            if io_queue_high:
                                notification_text += f"• 队列I/O任务过多: {queued_io_jobs} (阈值: {self._io_queue_threshold})\n"
                            notification_text += f"\n已自动限制下载速度为: {self._speed_limit_mbps}MB/s"
                            
                            self.post_message(
                                mtype=NotificationType.Manual,
                                title=notification_title,
                                text=notification_text
                            )
                            logger.info(f"【功能4-磁盘监控】已发送资源不足通知")
                        
                        return True
                    else:
                        logger.error(f"【功能4-磁盘监控】下载速度限制失败")
                        return False
                        
                elif not should_limit and self._is_speed_limited:
                    # 不需要限速但当前已限速，需要恢复
                    logger.info(f"【功能4-磁盘监控】系统资源已恢复正常，开始恢复下载速度")
                    
                    # 恢复下载速度（设置为0表示无限制）
                    success = self._set_download_speed_limit(downloader_obj, 0)
                    
                    if success:
                        self._is_speed_limited = False  # 标记为未限速
                        logger.info(f"【功能4-磁盘监控】下载速度恢复成功，已取消限制")
                        
                        # 发送通知
                        if self._notify:
                            notification_title = "【QB种子优化】系统资源已恢复，已取消下载速度限制"
                            notification_text = f"系统资源已恢复正常:\n"
                            notification_text += f"• 磁盘剩余空间: {free_space_gb:.2f}GB (阈值: {self._disk_space_threshold}GB) ✓\n"
                            notification_text += f"• I/O缓存使用率: {write_cache_overload:.1f}% (阈值: {self._io_cache_threshold}%) ✓\n"
                            notification_text += f"• 队列I/O任务: {queued_io_jobs} (阈值: {self._io_queue_threshold}) ✓\n"
                            notification_text += f"\n已自动取消下载速度限制"
                            
                            self.post_message(
                                mtype=NotificationType.Manual,
                                title=notification_title,
                                text=notification_text
                            )
                            logger.info(f"【功能4-磁盘监控】已发送资源恢复通知")
                        
                        return True
                    else:
                        logger.error(f"【功能4-磁盘监控】下载速度恢复失败")
                        return False
                        
                elif should_limit and self._is_speed_limited:
                    # 需要限速且当前已限速，无需操作
                    logger.info(f"【功能4-磁盘监控】系统资源仍不足，已处于限速状态，无需操作")
                    return True
                    
                else:
                    # 不需要限速且当前未限速，无需操作
                    logger.info(f"【功能4-磁盘监控】系统资源正常，无需限制下载速度")
                    return False
                    
            except Exception as api_error:
                logger.error(f"【功能4-磁盘监控】qBittorrent API调用失败: {str(api_error)}")
                return False
                
        except Exception as e:
            logger.error(f"【功能4-磁盘监控】监控过程中发生异常: {str(e)}")
            import traceback
            logger.error(f"【功能4-磁盘监控】异常详情: {traceback.format_exc()}")
            return False

    def _set_download_speed_limit(self, downloader_obj, speed_limit_bytes):
        """
        设置下载器速度限制
        """
        try:
            speed_limit_kbps = int(speed_limit_bytes / 1024)  # 转换为KB/s
            logger.info(f"【功能4-磁盘监控】设置下载速度限制: {speed_limit_bytes} bytes/s ({speed_limit_kbps}KB/s)")
            
            # 获取当前的上传速度限制
            try:
                current_download_limit, current_upload_limit = downloader_obj.get_speed_limit()
                logger.debug(f"【功能4-磁盘监控】当前速度限制 - 下载: {current_download_limit}KB/s, 上传: {current_upload_limit}KB/s")
            except Exception as e:
                logger.warning(f"【功能4-磁盘监控】获取当前速度限制失败: {str(e)}")
                current_download_limit, current_upload_limit = 0, 0
            
            # 使用下载器的set_speed_limit方法设置速度限制
            success = downloader_obj.set_speed_limit(
                download_limit=speed_limit_kbps,
                upload_limit=current_upload_limit
            )
            
            if success:
                logger.info(f"【功能4-磁盘监控】下载速度限制设置成功: {speed_limit_kbps}KB/s")
                return True
            else:
                logger.error(f"【功能4-磁盘监控】下载速度限制设置失败")
                return False
                
        except Exception as e:
            logger.error(f"【功能4-磁盘监控】设置下载速度限制失败: {str(e)}")
            import traceback
            logger.error(f"【功能4-磁盘监控】设置速度限制异常详情: {traceback.format_exc()}")
            return False

    def _re_download_torrent(self, torrent_info):
        """
        重新下载种子
        """
        try:
            logger.info(f"【重新下载】准备重新下载: {torrent_info['name']}")
            
            # 提取种子名称，去除文件扩展名
            torrent_name = torrent_info['name']
            search_keyword = self._extract_search_keyword(torrent_name)
            
            # 记录重新下载信息
            logger.info(f"【重新下载】重新下载信息:")
            logger.info(f"  - 种子名称: {torrent_name}")
            logger.info(f"  - 搜索关键词: {search_keyword}")
            logger.info(f"  - 原始种子: {torrent_info['name']}")
            logger.info(f"  - 种子大小: {StringUtils.str_filesize(torrent_info['size'])}")
            logger.info(f"  - 分类: {torrent_info.get('category', 'N/A')}")
            logger.info(f"  - 标签: {torrent_info.get('tags', 'N/A')}")
            logger.info(f"  - Tracker: {torrent_info.get('tracker', 'N/A')}")
            logger.info(f"  - 保存路径: {torrent_info.get('save_path', 'N/A')}")
            
            # 输出搜索站点信息
            try:
                if self._search_sites:
                    from app.db.site_oper import SiteOper as _SiteOper
                    site_map = {site.id: site.name for site in _SiteOper().list_order_by_pri()}
                    pretty_sites = [f"{site_map.get(sid, sid)}({sid})" for sid in self._search_sites]
                    logger.info(f"【重新下载】将使用以下站点进行搜索: {pretty_sites}")
                else:
                    logger.info(f"【重新下载】未指定站点，使用全站搜索")
            except Exception:
                pass

            # 搜索新的种子
            search_results = self._search_torrents(search_keyword)
            if not search_results:
                logger.warning(f"【重新下载】未找到替代种子: {search_keyword}")
                return False
                
            # 选择最佳种子（做种数最多的）
            best_torrent = self._select_best_torrent(search_results, torrent_info)
            if not best_torrent:
                logger.warning(f"【重新下载】未找到合适的替代种子: {search_keyword}")
                return False
                
            # 下载选中的种子
            return self._download_torrent(best_torrent, torrent_info)
            
        except Exception as e:
            logger.error(f"【重新下载】重新下载种子失败: {torrent_info.get('name', 'Unknown')}, 错误: {str(e)}")
            import traceback
            logger.error(f"【重新下载】重新下载异常详情: {traceback.format_exc()}")
            return False

    def _extract_search_keyword(self, torrent_name):
        """
        从种子名称中提取搜索关键词
        """
        try:
            # 去除常见的文件扩展名
            if '.' in torrent_name:
                name_parts = torrent_name.split('.')
                # 保留主要部分，去除质量、编码等信息
                if len(name_parts) > 2:
                    torrent_name = '.'.join(name_parts[:-2])
            
            # 去除常见的发布组标识和质量信息
            remove_patterns = [
                r'\b(1080p|720p|480p|2160p|4K|UHD)\b',
                r'\b(BluRay|BDRip|DVDRip|WEBRip|HDTV)\b',
                r'\b(x264|x265|H\.264|H\.265|HEVC)\b',
                r'\b(AAC|AC3|DTS|FLAC)\b',
                r'\[.*?\]',  # 方括号内容
                r'\(.*?\)',  # 圆括号内容
                r'-\w+$',    # 末尾的发布组
            ]
            
            import re
            for pattern in remove_patterns:
                torrent_name = re.sub(pattern, '', torrent_name, flags=re.IGNORECASE)
            
            # 清理多余的空格和特殊字符
            torrent_name = re.sub(r'[.\-_]+', ' ', torrent_name)
            torrent_name = ' '.join(torrent_name.split())
            
            logger.info(f"【QB种子优化】提取搜索关键词: {torrent_name}")
            return torrent_name.strip()
            
        except Exception as e:
            logger.error(f"【QB种子优化】提取搜索关键词失败: {str(e)}")
            return torrent_name

    def _search_torrents(self, keyword):
        """
        搜索种子
        """
        try:
            logger.info(f"【重新下载】开始搜索种子: {keyword}")
            
            # 使用SearchChain搜索种子
            search_chain = SearchChain()
            search_results = []
            
            # 搜索多页结果，获取更多候选种子（一次性传入站点列表，使用SearchChain并发能力）
            for page in range(3):  # 搜索前3页
                try:
                    if self._search_sites:
                        # 指定站点并发搜索
                        page_results = search_chain.search_by_title(title=keyword, page=page, sites=self._search_sites)
                        added = len(page_results) if page_results else 0
                        if added:
                            search_results.extend(page_results)
                        logger.info(f"【重新下载】第{page + 1}页合计新增{added}个候选（指定站点）")
                    else:
                        # 全站并发搜索
                        page_results = search_chain.search_by_title(title=keyword, page=page)
                        added = len(page_results) if page_results else 0
                        if added:
                            search_results.extend(page_results)
                        logger.info(f"【重新下载】第{page + 1}页新增{added}个候选（全站）")
                    # 如该页没有新增，尝试下一页；连续空页则提前结束
                    if not page_results:
                        logger.info(f"【重新下载】第{page + 1}页无结果，提前结束翻页")
                        break
                except Exception as e:
                    logger.error(f"【重新下载】第{page + 1}页搜索失败: {str(e)}")
                    continue
            
            logger.info(f"【重新下载】总共搜索到{len(search_results)}个候选种子")

            # 展示候选Top10（按做种数降序）
            try:
                def _safe_int(x):
                    try:
                        return int(x)
                    except Exception:
                        return 0
                
                # 使用综合评分算法排序候选种子
                scored_candidates = []
                for r in search_results:
                    ti = getattr(r, 'torrent_info', None)
                    if not ti:
                        continue
                    
                    seeders = _safe_int(getattr(ti, 'seeders', 0))
                    dlspeed = 0  # 搜索结果中的种子还未下载，速度为0
                    speed_score = (dlspeed / (1024 * 1024)) * self._speed_weight
                    seeder_score = seeders * self._seeder_weight
                    total_score = speed_score + seeder_score
                    
                    scored_candidates.append((
                        total_score,
                        seeders,
                        StringUtils.str_filesize(getattr(ti, 'size', 0) or 0),
                        getattr(ti, 'site_name', '未知站点'),
                        getattr(ti, 'title', '未知标题')
                    ))
                
                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                logger.info("【重新下载】候选Top10（按综合评分）:")
                for idx, (score, seeders, size_str, site_name, title) in enumerate(scored_candidates[:10], 1):
                    logger.info(f"  {idx:2d}. 综合评分: {score:6.2f} | 做种: {seeders:4d} | 大小: {size_str:>8} | 站点: {site_name} | 标题: {title}")
            except Exception:
                pass
            return search_results
            
        except Exception as e:
            logger.error(f"【重新下载】搜索种子失败: {str(e)}")
            return []

    def _select_best_torrent(self, search_results, original_torrent_info):
        """
        从搜索结果中选择最佳种子（使用综合评分算法）
        """
        try:
            if not search_results:
                return None
                
            logger.info(f"【重新下载】从{len(search_results)}个种子中选择最佳种子（使用综合评分算法）")
            logger.info(f"【重新下载】综合评分算法: 评分 = 下载速度(MB/s) × {self._speed_weight} + 做种数 × {self._seeder_weight}")
            logger.info(f"【重新下载】最低综合评分阈值: {self._min_score_threshold}")
            
            # 使用综合评分算法选择最佳种子
            scored_torrents = []
            original_size = original_torrent_info.get('size', 0)
            
            for result in search_results:
                torrent_info = result.torrent_info
                if not torrent_info:
                    continue
                    
                # 获取种子信息
                seeders = getattr(torrent_info, 'seeders', 0) or 0
                torrent_size = getattr(torrent_info, 'size', 0) or 0
                
                # 计算综合评分（与优先级优化使用相同的算法）
                # 注意：搜索结果的种子还没有下载，所以下载速度为0，主要依赖做种数
                dlspeed = 0  # 搜索结果中的种子还未下载，速度为0
                speed_score = (dlspeed / (1024 * 1024)) * self._speed_weight  # 转换为MB/s并应用权重
                seeder_score = seeders * self._seeder_weight  # 做种数权重
                
                final_score = speed_score + seeder_score
                
                # 避免0做种数的种子
                if seeders == 0:
                    final_score = 0
                
                scored_torrents.append({
                    'result': result,
                    'torrent_info': torrent_info,
                    'score': final_score,
                    'speed_score': speed_score,
                    'seeder_score': seeder_score,
                    'seeders': seeders,
                    'size': torrent_size
                })
                
                logger.debug(f"【重新下载】种子评分: {torrent_info.title}")
                logger.debug(f"  - 做种数: {seeders}, 大小: {StringUtils.str_filesize(torrent_size)}")
                logger.debug(f"  - 速度评分: {speed_score:.2f}, 做种评分: {seeder_score:.2f}")
                logger.debug(f"  - 综合评分: {final_score:.2f}")
            
            # 过滤出符合最低综合评分阈值的种子
            qualified_torrents = [t for t in scored_torrents if t['score'] >= self._min_score_threshold]
            
            logger.info(f"【重新下载】评分统计: 总候选{len(scored_torrents)}个, 符合阈值(≥{self._min_score_threshold}){len(qualified_torrents)}个")
            
            # 按评分排序，选择最佳种子
            if qualified_torrents:
                # 输出评分Top5
                try:
                    preview = sorted(qualified_torrents, key=lambda x: x['score'], reverse=True)[:5]
                    logger.info("【重新下载】符合阈值的综合评分Top5 候选：")
                    for idx, item in enumerate(preview, 1):
                        logger.info(
                            f"  {idx}. 综合评分:{item['score']:.2f} (速度:{item['speed_score']:.2f} + 做种:{item['seeder_score']:.2f})"
                        )
                        logger.info(
                            f"     做种:{item['seeders']} | 大小:{StringUtils.str_filesize(item['size'])} | 标题:{item['torrent_info'].title}"
                        )
                except Exception:
                    pass

                best = max(qualified_torrents, key=lambda x: x['score'])
                logger.info(f"【重新下载】选择最佳种子: {best['torrent_info'].title}")
                logger.info(f"  - 做种数: {best['seeders']}")
                logger.info(f"  - 大小: {StringUtils.str_filesize(best['size'])}")
                logger.info(f"  - 综合评分: {best['score']:.2f} (速度:{best['speed_score']:.2f} + 做种:{best['seeder_score']:.2f})")
                logger.info(f"  - 评分阈值检查: {best['score']:.2f} ≥ {self._min_score_threshold} ✓")
                try:
                    logger.info(f"  - 站点: {getattr(best['torrent_info'], 'site_name', '未知')}")
                except Exception:
                    pass
                return best['result']
            else:
                # 没有符合阈值的种子，输出不符合的候选信息
                logger.warning(f"【重新下载】没有种子符合最低综合评分阈值 {self._min_score_threshold}")
                if scored_torrents:
                    logger.info("【重新下载】不符合阈值的候选Top3：")
                    preview = sorted(scored_torrents, key=lambda x: x['score'], reverse=True)[:3]
                    for idx, item in enumerate(preview, 1):
                        logger.info(
                            f"  {idx}. 综合评分:{item['score']:.2f} < {self._min_score_threshold} ✗ | 做种:{item['seeders']} | 标题:{item['torrent_info'].title}"
                        )
            
            return None
            
        except Exception as e:
            logger.error(f"【重新下载】选择最佳种子失败: {str(e)}")
            return None

    def _download_torrent(self, search_result, original_torrent_info):
        """
        下载选中的种子
        """
        try:
            torrent_info = search_result.torrent_info
            meta_info = search_result.meta_info
            media_info = search_result.media_info
            
            logger.info(f"【重新下载】开始下载种子: {torrent_info.title}")
            logger.info(f"  - 站点: {torrent_info.site_name}")
            logger.info(f"  - 大小: {StringUtils.str_filesize(torrent_info.size)}")
            logger.info(f"  - 做种数: {torrent_info.seeders}")
            
            # 如果没有识别到媒体信息，创建基础媒体信息
            if not media_info:
                media_info = MediaInfo()
                media_info.category = original_torrent_info.get('category', '其它')
                media_info.type = MediaType.UNKNOWN
                media_info.title = meta_info.title if meta_info else torrent_info.title
                logger.info(f"【重新下载】未识别到媒体信息，使用默认配置")
            
            # 创建上下文
            context = Context(
                meta_info=meta_info,
                media_info=media_info,
                torrent_info=torrent_info
            )
            
            # 获取下载目录（使用原始种子的保存路径）
            save_path = original_torrent_info.get('save_path', '')
            if save_path:
                logger.info(f"【重新下载】使用原始种子的下载目录: {save_path}")
            else:
                logger.info(f"【重新下载】未找到原始下载目录，使用默认路径")
            
            # 使用DownloadChain下载
            download_chain = DownloadChain()
            download_id = download_chain.download_single(
                context=context,
                username='admin',  # 使用默认用户
                save_path=save_path if save_path else None  # 使用原始种子的下载路径
            )
            
            if download_id:
                logger.info(f"【重新下载】种子下载成功: {torrent_info.title}, 下载ID: {download_id}")
                return True
            else:
                logger.error(f"【重新下载】种子下载失败: {torrent_info.title}")
                return False
                
        except Exception as e:
            logger.error(f"【重新下载】下载种子异常: {str(e)}")
            import traceback
            logger.error(f"【重新下载】下载异常详情: {traceback.format_exc()}")
            return False

    def _is_important_torrent(self, torrent):
        """
        判断是否为重要种子（避免误删）
        """
        try:
            logger.debug(f"【QB种子优化】检查种子重要性: {torrent.name}")
            
            # 检查种子标签
            if hasattr(torrent, 'tags') and torrent.tags:
                important_tags = ['important', 'keep', 'preserve', 'moviepilot']
                logger.debug(f"【QB种子优化】种子标签: {torrent.tags}")
                for tag in important_tags:
                    if tag.lower() in torrent.tags.lower():
                        logger.info(f"【QB种子优化】种子包含重要标签 '{tag}': {torrent.name}")
                        return True
            
            # 检查种子名称关键词
            important_keywords = ['moviepilot', 'important', 'keep']
            torrent_name_lower = torrent.name.lower()
            logger.debug(f"【QB种子优化】种子名称: {torrent.name}")
            for keyword in important_keywords:
                if keyword in torrent_name_lower:
                    logger.info(f"【QB种子优化】种子名称包含重要关键词 '{keyword}': {torrent.name}")
                    return True
            
            # 检查分享率（高分享率的种子可能是重要的）
            if hasattr(torrent, 'ratio') and torrent.ratio > 10:
                logger.info(f"【QB种子优化】种子分享率较高 ({torrent.ratio}): {torrent.name}")
                return True
            
            logger.debug(f"【QB种子优化】种子不是重要种子: {torrent.name}")
            return False
            
        except Exception as e:
            logger.warning(f"【QB种子优化】检查种子重要性时出错: {str(e)}")
            return False  # 出错时保守处理，不删除

    def show_status(self):
        """
        显示优化状态
        """
        logger.info("【QB种子优化】开始显示状态...")
        
        for downloader_name in self._downloaders:
            logger.info(f"【QB种子优化】检查下载器状态: {downloader_name}")
            
            try:
                downloader_obj = self.__get_downloader(downloader_name)
                if not downloader_obj:
                    logger.error(f"【QB种子优化】无法获取下载器: {downloader_name}")
                    continue

                logger.info(f"【QB种子优化】获取种子列表: {downloader_name}")
                all_torrents, error = downloader_obj.get_torrents()
                if error:
                    logger.error(f"【QB种子优化】获取种子列表失败: {downloader_name}, 错误: {error}")
                    continue

                logger.info(f"【QB种子优化】种子列表获取成功: {downloader_name}, 总数: {len(all_torrents)}")

                # 统计各种状态的种子
                downloading = [t for t in all_torrents if t.state_enum.is_downloading]
                uploading = [t for t in all_torrents if t.state_enum.is_uploading]
                zero_seed = [t for t in uploading if t.num_seeds <= self._zero_seed_threshold]
                # high_seed = [t for t in downloading if t.num_seeds >= 10]  # 已改为综合评分判断
                
                # 详细统计
                logger.info(f"【QB种子优化】{downloader_name} 详细统计:")
                logger.info(f"  - 总种子数: {len(all_torrents)}")
                logger.info(f"  - 下载中: {len(downloading)}")
                logger.info(f"  - 做种中: {len(uploading)}")
                logger.info(f"  - 做种数为0 (阈值≤{self._zero_seed_threshold}): {len(zero_seed)}")
                # logger.info(f"  - 高做种数: 已改为综合评分判断")
                
                # 显示做种数为0的种子详情
                if zero_seed:
                    logger.info(f"【QB种子优化】做种数为0的种子详情:")
                    for i, torrent in enumerate(zero_seed[:5], 1):  # 只显示前5个
                        logger.info(f"  {i}. {torrent.name} (做种数: {torrent.num_seeds}, 分享率: {torrent.ratio})")
                    if len(zero_seed) > 5:
                        logger.info(f"  ... 还有 {len(zero_seed) - 5} 个")
                
                # 显示高做种数的种子详情（已改为综合评分判断）
                # if high_seed:
                #     logger.info(f"【QB种子优化】高做种数的种子详情:")
                #     for i, torrent in enumerate(high_seed[:5], 1):  # 只显示前5个
                #         logger.info(f"  {i}. {torrent.name} (做种数: {torrent.num_seeds})")
                #     if len(high_seed) > 5:
                #         logger.info(f"  ... 还有 {len(high_seed) - 5} 个")
                
                status_msg = f"【{downloader_name}状态】\n"
                status_msg += f"总种子数: {len(all_torrents)}\n"
                status_msg += f"下载中: {len(downloading)}\n"
                status_msg += f"做种中: {len(uploading)}\n"
                status_msg += f"做种数为0: {len(zero_seed)}\n"
                # status_msg += f"高做种数: 已改为综合评分判断\n"
                
                logger.info(f"【QB种子优化】状态消息: {status_msg}")
                if self._notify:
                    logger.info(f"【QB种子优化】发送状态通知")
                    self.post_message(
                        mtype=NotificationType.Download,
                        title=f"【QB优化状态】",
                        text=status_msg
                    )

            except Exception as e:
                logger.error(f"【QB种子优化】获取状态异常：{str(e)}")
                import traceback
                logger.error(f"【QB种子优化】状态异常详情: {traceback.format_exc()}")
        
        logger.info("【QB种子优化】状态显示完成")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    # 基础配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '基础配置'
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
                            },
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
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'downloaders',
                                            'label': '下载器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in DownloaderHelper().get_configs().values()]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 */6 * * *'
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 无人做种任务配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '清理下载中无人做种任务'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_zero_seed_clean',
                                            'label': '启用无人做种清理',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'zero_seed_threshold',
                                            'label': '做种数阈值',
                                            'placeholder': '0',
                                            'type': 'number'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_zero_seed_process',
                                            'label': '每次处理数量',
                                            'placeholder': '50',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 慢速下载任务配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '清理慢速下载任务'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_timeout_clean',
                                            'label': '启用慢速下载清理',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'timeout_hours',
                                            'label': '预计时长&&已下载时长超出（小时）',
                                            'placeholder': '24',
                                            'type': 'number'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_timeout_process',
                                            'label': '每次处理数量',
                                            'placeholder': '50',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 综合评分优先级配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '优先级优化'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_score_priority',
                                            'label': '启用优先级优化',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_score_threshold',
                                            'label': '最低综合评分阈值',
                                            'placeholder': '1.0',
                                            'type': 'number',
                                            'step': 0.1
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_priority_process',
                                            'label': '每次处理数量',
                                            'placeholder': '100',
                                            'type': 'number'
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'speed_weight',
                                            'label': '下载速度权重',
                                            'placeholder': '1.0',
                                            'type': 'number',
                                            'step': 0.1
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seeder_weight',
                                            'label': '做种数权重',
                                            'placeholder': '0.5',
                                            'type': 'number',
                                            'step': 0.1
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 磁盘空间和I/O监控配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '磁盘空间和I/O监控'
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_disk_monitor',
                                            'label': '启用磁盘监控',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'disk_space_threshold',
                                            'label': '磁盘空间阈值（GB）',
                                            'placeholder': '10',
                                            'type': 'number'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'speed_limit_mbps',
                                            'label': '限制下载速度（MB/s）',
                                            'placeholder': '1',
                                            'type': 'number',
                                            'step': 0.1
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'io_cache_threshold',
                                            'label': 'I/O缓存阈值（%）',
                                            'placeholder': '70',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'io_queue_threshold',
                                            'label': 'I/O队列阈值',
                                            'placeholder': '8000',
                                            'type': 'number'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 其他配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '其他配置'
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_auto_redownload',
                                            'label': '启用自动重新下载',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    


                    
                    # 搜索站点配置组
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
                                        'component': 'VDivider',
                                        'props': {
                                            'text': '搜索站点配置'
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
                                            'model': 'search_sites',
                                            'label': '重新下载时用于搜索的站点',
                                            'items': [{"title": site.name, "value": site.id}
                                                      for site in SiteOper().list_order_by_pri()]
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    
                    # 功能说明
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
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '📊 综合评分算法：评分 = 下载速度(MB/s) × 速度权重 + 做种数 × 做种数权重'
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
            "notify": True,
            "onlyonce": False,
            "cron": "0 */6 * * *",
            "downloaders": [],
            "search_sites": [],
            "enable_zero_seed_clean": True,
            "enable_priority_boost": True,
            "enable_timeout_clean": True,
            "enable_auto_redownload": True,
            "zero_seed_threshold": 0,
            "timeout_hours": 24,
            "max_download_slots": 5,
            "max_zero_seed_process": 50,
            "max_timeout_process": 50,
            "max_priority_process": 100,
            # 综合评分配置
            "enable_score_priority": True,
            "speed_weight": 1.0,
            "seeder_weight": 0.5,
            "min_score_threshold": 1.0,
            # 磁盘空间和I/O监控配置
            "enable_disk_monitor": False,
            "disk_space_threshold": 10,
            "io_cache_threshold": 70,
            "io_queue_threshold": 8000,
            "speed_limit_mbps": 1,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        logger.info("【QB种子优化】开始停止服务...")
        try:
            if self._scheduler:
                logger.info("【QB种子优化】移除所有调度任务")
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    logger.info("【QB种子优化】停止调度器")
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
                logger.info("【QB种子优化】调度器已停止")
            else:
                logger.info("【QB种子优化】没有运行中的调度器")
        except Exception as e:
            logger.error(f"【QB种子优化】退出插件失败：{str(e)}")
            import traceback
            logger.error(f"【QB种子优化】退出异常详情: {traceback.format_exc()}")
        logger.info("【QB种子优化】服务停止完成")
