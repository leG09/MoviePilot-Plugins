from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
import pytz
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler

from app.plugins import _PluginBase
from app.core.config import settings
from app.core.security import verify_apikey
from app.core.event import eventmanager, Event
from app.schemas import Notification, NotificationType
from app.schemas.types import EventType, MediaType
from app.log import logger
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.db.models.subscribe import Subscribe
from app.helper.mediaserver import MediaServerHelper
from app.chain.media import MediaChain
try:
    from typing_extensions import Annotated
except ImportError:
    from typing import Annotated


class EmbyEpisodeSync(_PluginBase):
    """Emby集数同步插件 - 定时更新Emby已存在的集数到订阅已下载中"""
    
    # 插件信息
    plugin_name = "EmbyEpisodeSync"
    plugin_desc = "定时更新Emby已存在的集数到订阅已下载中，避免已下载中存在但Emby不存在导致缺集"
    plugin_icon = "Emby_A.png"
    plugin_color = "#52C41A"
    plugin_version = "2.2"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "embyepisodesync"
    
    # 调度器
    _scheduler = None
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        # 停止现有任务
        self.stop_service()
        
        self._enabled = config.get("enabled", False) if config else False
        self._cron = config.get("cron", "0 3 * * *") if config else "0 3 * * *"  # 默认每天凌晨3点执行
        self._mediaserver_name = config.get("mediaserver_name", "") if config else ""
        self._send_notification = config.get("send_notification", True) if config else True
        self._onlyonce = config.get("onlyonce", False) if config else False
        self._auto_create_subscribe = config.get("auto_create_subscribe", False) if config else False
        self._test_mode = config.get("test_mode", False) if config else False
        self._test_limit = config.get("test_limit", 10) if config else 10  # 测试模式限制数量
        
        # 初始化帮助类
        self._mediaserver_helper = MediaServerHelper()
        self._media_chain = MediaChain()
        
        logger.info(f"EmbyEpisodeSync插件初始化完成，启用状态: {self._enabled}")
        if self._enabled:
            logger.info(f"定时任务: {self._cron}")
            logger.info(f"媒体服务器: {self._mediaserver_name}")
        
        # 立即执行一次
        if self._onlyonce:
            logger.info("Emby集数同步服务启动，立即运行一次")
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self._execute_sync,
                trigger='date',
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="Emby集数同步",
                kwargs={"test_mode": self._test_mode, "test_limit": self._test_limit}
            )
            
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()
            
            # 关闭一次性开关
            self._onlyonce = False
            # 保存配置
            self.__update_config()
    
    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled
    
    @staticmethod
    def get_command() -> list:
        """获取插件命令"""
        return [
            {
                "cmd": "/embyepisodesync",
                "event": EventType.PluginAction,
                "desc": "手动执行Emby集数同步",
                "category": "同步",
                "data": {
                    "action": "manual_sync"
                }
            }
        ]
    
    def get_api(self) -> list:
        """获取API接口"""
        return [
            {
                "path": "/manual_sync",
                "endpoint": self.manual_sync,
                "methods": ["POST"],
                "summary": "手动执行Emby集数同步",
                "description": "手动触发Emby集数同步任务"
            },
            {
                "path": "/test_sync",
                "endpoint": self.test_sync,
                "methods": ["POST"],
                "summary": "测试运行Emby集数同步",
                "description": "测试运行Emby集数同步任务，只处理少量数据用于测试"
            }
        ]
    
    def get_form(self) -> tuple:
        """获取配置表单"""
        # 获取所有Emby类型的媒体服务器配置
        try:
            # 确保 MediaServerHelper 已初始化
            if not hasattr(self, '_mediaserver_helper') or not self._mediaserver_helper:
                self._mediaserver_helper = MediaServerHelper()
            
            # 获取所有Emby类型的媒体服务器
            emby_services = self._mediaserver_helper.get_services(type_filter="emby")
            mediaserver_options = [
                {"title": name, "value": name}
                for name in emby_services.keys()
            ]
        except Exception as e:
            logger.warning(f"获取媒体服务器配置失败: {e}")
            mediaserver_options = []
        
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
                                            "label": "发送通知",
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
                                            "model": "onlyonce",
                                            "label": "立即执行一次",
                                            "color": "warning"
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
                                            "model": "auto_create_subscribe",
                                            "label": "自动创建缺失订阅",
                                            "color": "success"
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
                                            "model": "test_mode",
                                            "label": "测试模式",
                                            "color": "info"
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
                                            "model": "test_limit",
                                            "label": "测试模式限制数量",
                                            "placeholder": "10",
                                            "type": "number",
                                            "hint": "测试运行时只处理前N个订阅/剧集，用于小规模测试"
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
                                            "model": "cron",
                                            "label": "定时任务表达式",
                                            "placeholder": "0 3 * * * (每天凌晨3点)",
                                            "hint": "使用cron表达式，如：0 3 * * * 表示每天凌晨3点执行"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mediaserver_name",
                                            "label": "媒体服务器",
                                            "items": mediaserver_options,
                                            "hint": "选择要同步的Emby服务器"
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
                                            "text": "此插件会定时查询Emby媒体库中的集数，并与订阅的已下载集数进行同步。如果订阅的已下载集数中包含Emby中不存在的集数，则会被移除，避免出现缺集误判。"
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
            "cron": "0 3 * * *",
            "mediaserver_name": "",
            "send_notification": True,
            "onlyonce": False,
            "auto_create_subscribe": False,
            "test_mode": False,
            "test_limit": 10
        }
    
    def get_page(self) -> List[dict]:
        pass
    
    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [{
                "id": "EmbyEpisodeSync",
                "name": "Emby集数同步服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._execute_sync,
                "kwargs": {}
            }]
        return []
    
    def stop_service(self):
        """停止插件"""
        # 停止调度器
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown()
            self._scheduler = None
        logger.info("EmbyEpisodeSync插件已停止")
    
    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "mediaserver_name": self._mediaserver_name,
            "send_notification": self._send_notification,
            "onlyonce": self._onlyonce,
            "auto_create_subscribe": self._auto_create_subscribe,
            "test_mode": self._test_mode,
            "test_limit": self._test_limit
        })
    
    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        """
        处理插件动作事件
        
        Args:
            event: 事件对象
        """
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "manual_sync":
            logger.info("收到手动同步请求")
            self._execute_sync()
    
    def manual_sync(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        手动执行Emby集数同步任务
        
        Args:
            request_data: 请求数据
            apikey: API密钥
            
        Returns:
            Dict: 同步结果
        """
        try:
            logger.info("开始手动执行Emby集数同步任务")
            
            # 执行同步
            result = self._execute_sync()
            
            return {
                "success": True,
                "message": "手动Emby集数同步任务执行完成",
                "result": result
            }
            
        except Exception as e:
            error_msg = f"手动Emby集数同步任务执行失败：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
    
    def test_sync(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        测试运行Emby集数同步任务（只处理少量数据）
        
        Args:
            request_data: 请求数据，可包含test_limit参数覆盖配置
            apikey: API密钥
            
        Returns:
            Dict: 同步结果
        """
        try:
            # 获取测试限制数量（优先使用请求参数，否则使用配置）
            test_limit = request_data.get("test_limit", self._test_limit) if request_data else self._test_limit
            test_limit = int(test_limit) if test_limit else 10
            
            logger.info(f"开始测试运行Emby集数同步任务（限制处理数量: {test_limit}）")
            
            # 执行同步（测试模式）
            result = self._execute_sync(test_mode=True, test_limit=test_limit)
            
            return {
                "success": True,
                "message": f"测试运行Emby集数同步任务执行完成（限制数量: {test_limit}）",
                "result": result
            }
            
        except Exception as e:
            error_msg = f"测试运行Emby集数同步任务执行失败：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}
    
    def _execute_sync(self, test_mode: bool = False, test_limit: int = 10) -> Dict[str, Any]:
        """
        执行Emby集数同步任务
        
        Args:
            test_mode: 是否为测试模式
            test_limit: 测试模式限制数量
            
        Returns:
            Dict: 同步结果
        """
        try:
            mode_str = "测试模式" if test_mode else "正常模式"
            # 强制转换 test_limit 为整数，避免 int 与 str 比较
            try:
                test_limit = int(test_limit)
            except Exception:
                test_limit = 10
            logger.info(f"=== 开始执行Emby集数同步任务 ({mode_str}) ===")
            if test_mode:
                logger.info(f"测试模式限制数量: {test_limit}")
            
            # 获取媒体服务器服务
            if not self._mediaserver_name:
                logger.warning("未配置媒体服务器名称，跳过同步")
                return {
                    "success": False,
                    "message": "未配置媒体服务器名称",
                    "updated_count": 0,
                    "skipped_count": 0
                }
            
            service_info = self._mediaserver_helper.get_service(name=self._mediaserver_name, type_filter="emby")
            if not service_info:
                logger.warning(f"未找到媒体服务器: {self._mediaserver_name}")
                return {
                    "success": False,
                    "message": f"未找到媒体服务器: {self._mediaserver_name}",
                    "updated_count": 0,
                    "skipped_count": 0
                }
            
            emby = service_info.instance
            if not emby:
                logger.warning(f"媒体服务器 {self._mediaserver_name} 实例不存在")
                return {
                    "success": False,
                    "message": f"媒体服务器 {self._mediaserver_name} 实例不存在",
                    "updated_count": 0,
                    "skipped_count": 0
                }
            
            # 检查服务是否可用，如果不可用则尝试重连
            if emby.is_inactive():
                logger.info(f"媒体服务器 {self._mediaserver_name} 连接断开，尝试重连...")
                emby.reconnect()
            
            if not emby.user:
                logger.warning(f"媒体服务器 {self._mediaserver_name} 连接失败")
                return {
                    "success": False,
                    "message": f"媒体服务器 {self._mediaserver_name} 连接失败",
                    "updated_count": 0,
                    "skipped_count": 0
                }
            
            # 获取所有TV类型的订阅
            subscribe_oper = SubscribeOper()
            subscribes = subscribe_oper.list()
            
            tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
            
            # 测试模式：限制处理数量
            if test_mode and len(tv_subscribes) > test_limit:
                original_count = len(tv_subscribes)
                tv_subscribes = tv_subscribes[:test_limit]
                logger.info(f"测试模式：从 {original_count} 个TV订阅中只处理前 {test_limit} 个")
            
            logger.info(f"找到 {len(tv_subscribes)} 个TV类型订阅")
            
            # 按名称和TMDB ID分组，显示所有订阅的详细信息
            subscribe_groups = {}
            for s in tv_subscribes:
                key = f"{s.name} (TMDB: {s.tmdbid})"
                if key not in subscribe_groups:
                    subscribe_groups[key] = []
                subscribe_groups[key].append(f"S{s.season:02d}")
            
            for name, seasons in subscribe_groups.items():
                logger.debug(f"订阅: {name} - 季: {', '.join(seasons)}")
            
            updated_count = 0
            skipped_count = 0
            error_count = 0
            created_count = 0
            cancelled_count = 0
            updated_subscribes = []
            created_subscribes = []
            cancelled_subscribes = []
            
            for subscribe in tv_subscribes:
                try:
                    logger.info(f"处理订阅: {subscribe.name} S{subscribe.season:02d} (TMDB ID: {subscribe.tmdbid})")
                    
                    # 跳过洗版订阅
                    if subscribe.best_version:
                        logger.info(f"订阅 {subscribe.name} S{subscribe.season:02d} 是洗版订阅，跳过")
                        skipped_count += 1
                        continue
                    
                    # 获取订阅的已下载集数（允许为空，空数组时也需要从Emby恢复）
                    current_episodes = subscribe.note or []
                    
                    # 查询Emby中的集数（不传season参数，获取所有季的数据，确保能获取到所有季）
                    emby_item_id, emby_season_episodes = emby.get_tv_episodes(
                        title=subscribe.name,
                        year=subscribe.year,
                        tmdb_id=subscribe.tmdbid,
                        season=None  # 不传season，获取所有季的数据
                    )
                    
                    if not emby_item_id or not emby_season_episodes:
                        logger.warning(f"订阅 {subscribe.name} S{subscribe.season:02d} 在Emby中未找到 (TMDB ID: {subscribe.tmdbid})")
                        skipped_count += 1
                        continue
                    
                    # 获取对应季的集数列表
                    emby_episodes = emby_season_episodes.get(subscribe.season, [])
                    if not emby_episodes:
                        # 如果指定季没有数据，但Emby中有其他季的数据，记录详细信息以便调试
                        available_seasons = sorted([str(k) for k in emby_season_episodes.keys()]) if emby_season_episodes else []
                        if available_seasons:
                            logger.warning(f"订阅 {subscribe.name} S{subscribe.season:02d} 在Emby中未找到集数。Emby中可用的季: {available_seasons}，订阅的季号: {subscribe.season}，可能订阅的季号配置不正确")
                        else:
                            logger.warning(f"订阅 {subscribe.name} S{subscribe.season:02d} 在Emby中未找到集数")
                        skipped_count += 1
                        continue
                    
                    # 确保集数都是整数类型（Emby可能返回字符串）
                    emby_episodes = [int(ep) if isinstance(ep, str) else int(ep) for ep in emby_episodes]
                    
                    # 去重集数（一集可能有多个版本）
                    emby_episodes_set = set(emby_episodes)
                    unique_episode_count = len(emby_episodes_set)
                    
                    # 计算是否已经全集集齐（使用TMDB总集数）
                    total_episode_count = 0
                    try:
                        mediainfo = self._media_chain.recognize_media(mtype=MediaType.TV, tmdbid=subscribe.tmdbid)
                        if mediainfo and mediainfo.seasons:
                            seas = mediainfo.seasons.get(subscribe.season)
                            if seas:
                                total_episode_count = len(seas)
                    except Exception as _:
                        pass
                    
                    # 确保total_episode是整数类型
                    if subscribe.total_episode:
                        try:
                            subscribe_total = int(subscribe.total_episode) if isinstance(subscribe.total_episode, str) else int(subscribe.total_episode)
                            if subscribe_total > 0:
                                total_episode_count = subscribe_total if total_episode_count == 0 else total_episode_count
                        except (ValueError, TypeError):
                            pass
                    
                    # 使用去重后的集数判断是否全集（避免一集多版本导致误判）
                    if total_episode_count > 0 and unique_episode_count >= total_episode_count:
                        # 已全集且非洗版，若仍为订阅中则自动取消（置为暂停S）
                        if (subscribe.state in ['N', 'R', 'P']) and not subscribe.best_version:
                            logger.info(f"订阅 {subscribe.name} S{subscribe.season:02d} 已全集(去重后{unique_episode_count}/{total_episode_count}，原始{len(emby_episodes)}集)，自动取消订阅")
                            payload = {
                                "state": 'S',
                                "lack_episode": 0,
                                "total_episode": int(total_episode_count)
                            }
                            subscribe_oper.update(subscribe.id, payload)
                            cancelled_count += 1
                            cancelled_subscribes.append({
                                "name": subscribe.name,
                                "season": subscribe.season,
                                "tmdb_id": subscribe.tmdbid,
                                "total": int(total_episode_count)
                            })
                            # 已取消则不再更新note
                            continue
                    
                    # 使用Emby的集数作为新的已下载集数（去重并排序）
                    # 注意：emby_episodes_set 已在前面创建
                    # 确保current_episodes也是整数类型
                    current_episodes = [int(ep) if isinstance(ep, str) else int(ep) for ep in current_episodes] if current_episodes else []
                    current_episodes_set = set(current_episodes)
                    new_episodes = sorted(list(emby_episodes_set))
                    
                    # 安全检查：如果Emby集数为空，不应该更新（避免数据丢失）
                    if not new_episodes:
                        logger.warning(f"订阅 {subscribe.name} S{subscribe.season:02d} Emby集数为空，跳过更新以避免数据丢失")
                        skipped_count += 1
                        continue
                    
                    # 计算移除的集数（在订阅已下载中但Emby中不存在的）
                    removed_episodes = sorted(list(current_episodes_set - emby_episodes_set))
                    # 计算新增的集数（在Emby中但订阅已下载中不存在的）
                    added_episodes = sorted(list(emby_episodes_set - current_episodes_set))
                    
                    # 如果集数有变化，更新订阅
                    if set(new_episodes) != current_episodes_set:
                        
                        logger.info(f"订阅 {subscribe.name} S{subscribe.season:02d} 更新集数：")
                        logger.info(f"  当前集数: {sorted(current_episodes)}")
                        logger.info(f"  Emby集数: {sorted(emby_episodes)}")
                        if removed_episodes:
                            logger.info(f"  移除集数: {removed_episodes}")
                        if added_episodes:
                            logger.info(f"  新增集数: {added_episodes}")
                        logger.info(f"  新集数: {new_episodes}")
                        
                        # 更新订阅的note字段：使用 query().update() 直接执行 UPDATE，避免 ORM 对 JSON 的
                        # dirty 追踪不生效或 detached 对象导致更新未持久化
                        _session = SessionFactory()
                        try:
                            _cnt = _session.query(Subscribe).filter(
                                Subscribe.id == subscribe.id
                            ).update({"note": new_episodes}, synchronize_session=False)
                            _session.commit()
                            if _cnt == 0:
                                logger.warning(
                                    f"订阅 {subscribe.name} S{subscribe.season:02d} 更新 note 未匹配到行 id={subscribe.id}"
                                )
                        except Exception as _e:
                            _session.rollback()
                            logger.error(f"更新订阅 note 失败 (id={subscribe.id}): {_e}")
                            raise
                        finally:
                            _session.close()
                        
                        updated_count += 1
                        updated_subscribes.append({
                            "name": subscribe.name,
                            "season": subscribe.season,
                            "removed_episodes": removed_episodes,
                            "added_episodes": added_episodes,
                            "new_episodes": new_episodes
                        })
                    else:
                        logger.debug(f"订阅 {subscribe.name} S{subscribe.season:02d} 集数无需更新")
                        skipped_count += 1
                        
                except Exception as e:
                    error_msg = f"处理订阅 {subscribe.name} S{subscribe.season:02d} 时发生错误: {str(e)}"
                    logger.error(error_msg)
                    error_count += 1
            
            # 检查并创建缺失的订阅
            if self._auto_create_subscribe:
                logger.info("=== 开始检查并创建缺失的订阅 ===")
                created_result = self._check_and_create_missing_subscribes(
                    emby, subscribe_oper, tv_subscribes, test_mode=test_mode, test_limit=test_limit
                )
                created_count = created_result.get("created_count", 0)
                created_subscribes = created_result.get("created_subscribes", [])
                logger.info(f"创建了 {created_count} 个缺失的订阅")
            
            # 发送通知
            if self._send_notification and (updated_count > 0 or created_count > 0 or cancelled_count > 0):
                self._send_sync_notification(
                    updated_count, skipped_count, error_count, updated_subscribes,
                    created_count, created_subscribes, cancelled_count, cancelled_subscribes
                )
            
            result = {
                "success": error_count == 0,
                "message": f"Emby集数同步完成 - 更新: {updated_count}, 创建: {created_count}, 取消: {cancelled_count}, 跳过: {skipped_count}, 错误: {error_count}",
                "updated_count": updated_count,
                "created_count": created_count,
                "cancelled_count": cancelled_count,
                "skipped_count": skipped_count,
                "error_count": error_count,
                "updated_subscribes": updated_subscribes,
                "created_subscribes": created_subscribes,
                "cancelled_subscribes": cancelled_subscribes
            }
            
            logger.info(f"=== Emby集数同步任务完成 ===")
            logger.info(f"结果: {result['message']}")
            
            return result
            
        except Exception as e:
            error_msg = f"执行Emby集数同步任务时发生错误: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1
            }
    
    def _get_all_emby_series(self, emby, test_mode: bool = False, test_limit: int = 10) -> Dict[str, Dict]:
        """
        从Emby获取所有剧集及其季和集数信息
        
        Args:
            emby: Emby实例
            test_mode: 是否为测试模式
            test_limit: 测试模式限制数量
            
        Returns:
            Dict: {tmdb_id: {"item_id": emby_item_id, "name": name, "year": year, "seasons": {season: [episodes]}}}
        """
        emby_series = {}
        
        try:
            logger.info("=== 开始从Emby获取所有剧集 ===")
            
            # 强制转换 test_limit 为整数
            try:
                test_limit = int(test_limit)
            except Exception:
                test_limit = 10
            
            if not emby._host or not emby._apikey:
                logger.warning("Emby配置不完整，无法获取剧集列表")
                return emby_series
            
            # 获取所有Series类型的项目
            url = f"{emby._host}emby/Items"
            # 正式模式下减少每页数量，避免单次响应过大导致阻塞
            page_limit = 1000 if test_mode else 200
            params = {
                "IncludeItemTypes": "Series",
                "Recursive": "true",
                "Fields": "ProviderIds,ProductionYear",
                "api_key": emby._apikey,
                "Limit": page_limit  # 分页大小：测试模式1000，正式模式200
            }
            
            start_index = 0
            total_count = 0
            
            while True:
                params["StartIndex"] = start_index
                try:
                    from app.utils.http import RequestUtils
                    request_utils = RequestUtils()
                    res = request_utils.get_res(url, params)
                    if not res or res.status_code != 200:
                        logger.warning(f"获取Emby剧集列表失败: {res.status_code if res else 'No response'}")
                        break
                    
                    data = res.json()
                    items = data.get("Items", [])
                    total_record_count = data.get("TotalRecordCount", 0)
                    
                    if not items:
                        break
                    
                    logger.debug(f"获取到 {len(items)} 个剧集 (本页 {page_limit}/ 总计: {total_record_count})，起始索引: {start_index}")
                    
                    for item in items:
                        try:
                            # 获取TMDB ID
                            provider_ids = item.get("ProviderIds", {})
                            tmdb_id_str = provider_ids.get("Tmdb")
                            if not tmdb_id_str:
                                continue
                            
                            tmdb_id = int(tmdb_id_str)
                            item_id = item.get("Id")
                            name = item.get("Name", "")
                            year = item.get("ProductionYear")
                            
                            if not item_id or not name:
                                continue
                            
                            # 获取该剧集的所有季和集数
                            emby_item_id, emby_season_episodes = emby.get_tv_episodes(
                                item_id=item_id,
                                tmdb_id=tmdb_id,
                                season=None  # 获取所有季的数据
                            )
                            
                            if not emby_item_id or not emby_season_episodes:
                                logger.debug(f"剧集 {name} (TMDB: {tmdb_id}) 无法获取集数信息")
                                continue
                            
                            # 只记录有集数的季，并确保集数都是整数类型
                            seasons_with_episodes = {}
                            for season, episodes in emby_season_episodes.items():
                                if episodes:
                                    # 确保集数都是整数类型
                                    int_episodes = [int(ep) if isinstance(ep, str) else int(ep) for ep in episodes]
                                    seasons_with_episodes[season] = int_episodes
                            
                            if not seasons_with_episodes:
                                logger.debug(f"剧集 {name} (TMDB: {tmdb_id}) 没有有集数的季")
                                continue
                            
                            emby_series[tmdb_id] = {
                                "item_id": emby_item_id,
                                "name": name,
                                "year": year,
                                "seasons": seasons_with_episodes
                            }
                            
                            total_count += 1
                            
                            # 测试模式：达到上限后立即停止收集
                            if test_mode and len(emby_series) >= test_limit:
                                logger.info(f"测试模式：已收集 {len(emby_series)} 个剧集，提前结束采集")
                                break
                            
                        except (ValueError, KeyError) as e:
                            logger.debug(f"处理剧集项时出错: {str(e)}")
                            continue
                        except Exception as e:
                            logger.warning(f"处理剧集项时发生错误: {str(e)}")
                            continue
                    
                    # 若测试模式达到上限，结束外层循环
                    if test_mode and len(emby_series) >= test_limit:
                        break
                    
                    # 检查是否还有更多数据
                    start_index += len(items)
                    logger.debug(f"分页进度：已收集 {len(emby_series)}/{total_record_count}（下一页起始索引: {start_index}）")
                    if start_index >= total_record_count:
                        break
                    
                except Exception as e:
                    logger.error(f"获取Emby剧集列表时发生错误: {str(e)}")
                    break
            
            logger.info(f"=== 从Emby获取到 {total_count} 个有集数的剧集 ===")
            
            # 测试模式：限制处理数量
            if test_mode and len(emby_series) > test_limit:
                original_count = len(emby_series)
                # 只保留前test_limit个剧集
                emby_series = dict(list(emby_series.items())[:test_limit])
                logger.info(f"测试模式：从 {original_count} 个剧集中只处理前 {test_limit} 个")
            
        except Exception as e:
            logger.error(f"获取Emby所有剧集时发生错误: {str(e)}")
        
        return emby_series
    
    def _check_and_create_missing_subscribes(self, emby, subscribe_oper: SubscribeOper, 
                                            tv_subscribes: List, test_mode: bool = False, 
                                            test_limit: int = 10) -> Dict[str, Any]:
        """
        检查并创建缺失的订阅
        先从Emby获取所有剧集，然后与MoviePilot订阅对比，找出差异
        
        Args:
            emby: Emby实例
            subscribe_oper: 订阅操作类
            tv_subscribes: MoviePilot中的TV订阅列表
            test_mode: 是否为测试模式
            test_limit: 测试模式限制数量
            
        Returns:
            Dict: 创建结果
        """
        created_count = 0
        created_subscribes = []
        
        try:
            # 第一步：从Emby获取所有剧集及其季和集数
            logger.info("=== 第一步：从Emby获取所有剧集 ===")
            emby_series = self._get_all_emby_series(emby, test_mode=test_mode, test_limit=test_limit)
            
            if not emby_series:
                logger.info("Emby中没有找到剧集，跳过创建订阅")
                return {
                    "created_count": 0,
                    "created_subscribes": []
                }
            
            # 第二步：从MoviePilot获取所有订阅，按TMDB ID和季分组
            logger.info("=== 第二步：从MoviePilot获取所有订阅 ===")
            mp_subscribes_dict = {}  # {tmdb_id: {season: subscribe}}
            
            for subscribe in tv_subscribes:
                if subscribe.best_version:
                    continue  # 跳过洗版订阅
                
                if subscribe.tmdbid not in mp_subscribes_dict:
                    mp_subscribes_dict[subscribe.tmdbid] = {}
                mp_subscribes_dict[subscribe.tmdbid][subscribe.season] = subscribe
            
            logger.info(f"MoviePilot中有 {len(mp_subscribes_dict)} 个剧集的订阅")
            
            # 第三步：对比找出差异
            logger.info("=== 第三步：对比找出差异 ===")
            
            # 找出Emby中有但MoviePilot中没有的剧集
            emby_tmdb_ids = set(emby_series.keys())
            mp_tmdb_ids = set(mp_subscribes_dict.keys())
            
            # 完全缺失的剧集（Emby有但MoviePilot完全没有订阅）
            missing_series = emby_tmdb_ids - mp_tmdb_ids
            logger.info(f"发现 {len(missing_series)} 个完全缺失的剧集")
            
            # 部分缺失的剧集（Emby有但MoviePilot缺少某些季）
            partial_missing_series = emby_tmdb_ids & mp_tmdb_ids
            
            # 处理完全缺失的剧集
            for tmdb_id in missing_series:
                try:
                    emby_info = emby_series[tmdb_id]
                    seasons = emby_info["seasons"]
                    
                    logger.info(f"发现完全缺失的剧集: {emby_info['name']} (TMDB: {tmdb_id})，有 {len(seasons)} 个季")
                    
                    # 通过TMDB ID识别媒体信息
                    mediainfo = self._media_chain.recognize_media(
                        mtype=MediaType.TV,
                        tmdbid=tmdb_id
                    )
                    
                    if not mediainfo:
                        logger.warning(f"无法通过TMDB ID {tmdb_id} 识别媒体信息: {emby_info['name']}")
                        continue
                    
                    # 为每个季检查是否有缺集
                    for season, episodes in seasons.items():
                        try:
                            # 确保集数都是整数类型
                            episodes = [int(ep) if isinstance(ep, str) else int(ep) for ep in episodes]
                            episodes_list = sorted(list(set(episodes)))
                            
                            # 获取该季的总集数
                            total_episodes = mediainfo.seasons.get(season) if mediainfo.seasons else None
                            total_episode_count = len(total_episodes) if total_episodes else 0
                            
                            # 如果无法获取总集数，默认创建订阅（保守策略）
                            if total_episode_count == 0:
                                logger.warning(f"无法获取 {mediainfo.title_year} S{season:02d} 的总集数，默认创建订阅")
                                should_create = True
                            else:
                                # 检查是否有缺集：如果Emby中的集数等于总集数，说明已经下载完了，不需要创建订阅
                                should_create = len(episodes_list) < int(total_episode_count)
                                
                                if not should_create:
                                    logger.info(f"剧集 {mediainfo.title_year} S{season:02d} 所有集数已下载完成 ({len(episodes_list)}/{total_episode_count})，跳过创建订阅")
                                    continue
                            
                            logger.info(f"创建缺失订阅: {mediainfo.title_year} S{season:02d} (TMDB: {tmdb_id})")
                            logger.info(f"  Emby中的集数: {episodes_list} ({len(episodes_list)}/{total_episode_count if total_episode_count > 0 else '未知'})")
                            
                            # 创建订阅
                            subscribe_id, message = subscribe_oper.add(
                                mediainfo=mediainfo,
                                season=season,
                                note=episodes_list,
                                state='N',
                                username="EmbyEpisodeSync"
                            )
                            
                            # 校正总集数与缺失集，避免显示负数
                            try:
                                total_episode_for_save = int(total_episode_count) if total_episode_count else 0
                                if total_episode_for_save < len(episodes_list):
                                    total_episode_for_save = len(episodes_list)
                                lack_episode_for_save = max(total_episode_for_save - len(episodes_list), 0)
                                subscribe_oper.update(subscribe_id, {
                                    "total_episode": total_episode_for_save,
                                    "lack_episode": lack_episode_for_save,
                                    "note": episodes_list
                                })
                            except Exception:
                                pass
                            
                            logger.info(f"订阅创建成功: {mediainfo.title_year} S{season:02d} - {message}")
                            
                            created_count += 1
                            created_subscribes.append({
                                "name": mediainfo.title,
                                "season": season,
                                "tmdb_id": tmdb_id,
                                "episodes": episodes_list
                            })
                            
                        except Exception as e:
                            error_msg = f"创建订阅 {mediainfo.title_year} S{season:02d} 时发生错误: {str(e)}"
                            logger.error(error_msg)
                            continue
                            
                except Exception as e:
                    error_msg = f"处理完全缺失的剧集 {tmdb_id} 时发生错误: {str(e)}"
                    logger.error(error_msg)
                    continue
            
            # 处理部分缺失的剧集（某些季缺失）
            for tmdb_id in partial_missing_series:
                try:
                    emby_info = emby_series[tmdb_id]
                    emby_seasons = set(emby_info["seasons"].keys())
                    mp_seasons = set(mp_subscribes_dict[tmdb_id].keys())
                    
                    # 找出缺失的季
                    missing_seasons = emby_seasons - mp_seasons
                    
                    if not missing_seasons:
                        continue
                    
                    logger.info(f"剧集 {emby_info['name']} (TMDB: {tmdb_id}) 发现缺失的季: {sorted(missing_seasons)}")
                    
                    # 通过TMDB ID识别媒体信息
                    mediainfo = self._media_chain.recognize_media(
                        mtype=MediaType.TV,
                        tmdbid=tmdb_id
                    )
                    
                    if not mediainfo:
                        logger.warning(f"无法通过TMDB ID {tmdb_id} 识别媒体信息: {emby_info['name']}")
                        continue
                    
                    # 为每个缺失的季检查是否有缺集
                    for season in missing_seasons:
                        try:
                            episodes = emby_info["seasons"].get(season, [])
                            if not episodes:
                                continue
                            
                            # 确保集数都是整数类型
                            episodes = [int(ep) if isinstance(ep, str) else int(ep) for ep in episodes]
                            episodes_list = sorted(list(set(episodes)))
                            
                            # 获取该季的总集数
                            total_episodes = mediainfo.seasons.get(season) if mediainfo.seasons else None
                            total_episode_count = len(total_episodes) if total_episodes else 0
                            
                            # 如果无法获取总集数，默认创建订阅（保守策略）
                            if total_episode_count == 0:
                                logger.warning(f"无法获取 {mediainfo.title_year} S{season:02d} 的总集数，默认创建订阅")
                                should_create = True
                            else:
                                # 检查是否有缺集：如果Emby中的集数等于总集数，说明已经下载完了，不需要创建订阅
                                should_create = len(episodes_list) < int(total_episode_count)
                                
                                if not should_create:
                                    logger.info(f"剧集 {mediainfo.title_year} S{season:02d} 所有集数已下载完成 ({len(episodes_list)}/{total_episode_count})，跳过创建订阅")
                                    continue
                            
                            logger.info(f"创建缺失订阅: {mediainfo.title_year} S{season:02d} (TMDB: {tmdb_id})")
                            logger.info(f"  Emby中的集数: {episodes_list} ({len(episodes_list)}/{total_episode_count if total_episode_count > 0 else '未知'})")
                            
                            # 创建订阅
                            subscribe_id, message = subscribe_oper.add(
                                mediainfo=mediainfo,
                                season=season,
                                note=episodes_list,
                                state='N'
                            )
                            
                            # 校正总集数与缺失集，避免显示负数
                            try:
                                total_episode_for_save = int(total_episode_count) if total_episode_count else 0
                                if total_episode_for_save < len(episodes_list):
                                    total_episode_for_save = len(episodes_list)
                                lack_episode_for_save = max(total_episode_for_save - len(episodes_list), 0)
                                subscribe_oper.update(subscribe_id, {
                                    "total_episode": total_episode_for_save,
                                    "lack_episode": lack_episode_for_save,
                                    "note": episodes_list
                                })
                            except Exception:
                                pass
                            
                            logger.info(f"订阅创建成功: {mediainfo.title_year} S{season:02d} - {message}")
                            
                            created_count += 1
                            created_subscribes.append({
                                "name": mediainfo.title,
                                "season": season,
                                "tmdb_id": tmdb_id,
                                "episodes": episodes_list
                            })
                            
                        except Exception as e:
                            error_msg = f"创建订阅 {mediainfo.title_year} S{season:02d} 时发生错误: {str(e)}"
                            logger.error(error_msg)
                            continue
                            
                except Exception as e:
                    error_msg = f"处理部分缺失的剧集 {tmdb_id} 时发生错误: {str(e)}"
                    logger.error(error_msg)
                    continue
            
        except Exception as e:
            logger.error(f"检查并创建缺失订阅时发生错误: {str(e)}")
        
        return {
            "created_count": created_count,
            "created_subscribes": created_subscribes
        }
    
    def _send_sync_notification(self, updated_count: int, skipped_count: int, 
                               error_count: int, updated_subscribes: List[Dict],
                               created_count: int = 0, created_subscribes: List[Dict] = None,
                               cancelled_count: int = 0, cancelled_subscribes: List[Dict] = None):
        """
        发送同步通知
        
        Args:
            updated_count: 更新的订阅数量
            skipped_count: 跳过的订阅数量
            error_count: 错误的订阅数量
            updated_subscribes: 更新的订阅列表
            created_count: 创建的订阅数量
            created_subscribes: 创建的订阅列表
            cancelled_count: 取消的订阅数量
            cancelled_subscribes: 取消的订阅列表
        """
        if created_subscribes is None:
            created_subscribes = []
        if cancelled_subscribes is None:
            cancelled_subscribes = []
        try:
            # 构建通知消息
            message = f"Emby集数同步任务完成\n\n"
            message += f"更新订阅: {updated_count} 个\n"
            if created_count > 0:
                message += f"创建订阅: {created_count} 个\n"
            if cancelled_count > 0:
                message += f"取消订阅: {cancelled_count} 个\n"
            message += f"跳过订阅: {skipped_count} 个\n"
            
            if error_count > 0:
                message += f"错误: {error_count} 个\n"
            
            if updated_subscribes:
                message += f"\n更新的订阅:\n"
                for sub_info in updated_subscribes[:10]:  # 最多显示10个
                    changes = []
                    if sub_info.get('removed_episodes'):
                        changes.append(f"移除: {', '.join(map(str, sub_info['removed_episodes']))}")
                    if sub_info.get('added_episodes'):
                        changes.append(f"新增: {', '.join(map(str, sub_info['added_episodes']))}")
                    change_str = " | ".join(changes) if changes else "无变化"
                    message += f"  {sub_info['name']} S{sub_info['season']:02d} - {change_str}\n"
                
                if len(updated_subscribes) > 10:
                    message += f"  ... 还有 {len(updated_subscribes) - 10} 个订阅已更新\n"
            
            if created_subscribes:
                message += f"\n创建的订阅:\n"
                for sub_info in created_subscribes[:10]:  # 最多显示10个
                    episodes_str = f"集数: {', '.join(map(str, sub_info['episodes'][:10]))}"
                    if len(sub_info['episodes']) > 10:
                        episodes_str += f" 等{len(sub_info['episodes'])}集"
                    message += f"  {sub_info['name']} S{sub_info['season']:02d} - {episodes_str}\n"
                
                if len(created_subscribes) > 10:
                    message += f"  ... 还有 {len(created_subscribes) - 10} 个订阅已创建\n"
            
            if cancelled_subscribes:
                message += f"\n取消的订阅:\n"
                for sub_info in cancelled_subscribes[:10]:
                    message += f"  {sub_info['name']} S{sub_info['season']:02d} - 全集完成({sub_info.get('total')})\n"
                if len(cancelled_subscribes) > 10:
                    message += f"  ... 还有 {len(cancelled_subscribes) - 10} 个订阅已取消\n"
            
            # 发送通知
            notification = Notification(
                channel="EmbyEpisodeSync",
                mtype=NotificationType.Manual,
                title="Emby集数同步完成",
                text=message,
                image=""
            )
            
            logger.info(f"发送同步通知: {message}")
            
        except Exception as e:
            logger.error(f"发送同步通知时发生错误: {str(e)}")

