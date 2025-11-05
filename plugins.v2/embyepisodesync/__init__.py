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
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
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
    plugin_version = "1.0"
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
        
        # 初始化帮助类
        self._mediaserver_helper = MediaServerHelper()
        
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
                name="Emby集数同步"
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
            "onlyonce": False
        }
    
    def get_page(self) -> list:
        """获取插件页面"""
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
            "onlyonce": self._onlyonce
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
    
    def _execute_sync(self) -> Dict[str, Any]:
        """
        执行Emby集数同步任务
        
        Returns:
            Dict: 同步结果
        """
        try:
            logger.info("=== 开始执行Emby集数同步任务 ===")
            
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
            logger.info(f"找到 {len(tv_subscribes)} 个TV类型订阅")
            
            updated_count = 0
            skipped_count = 0
            error_count = 0
            updated_subscribes = []
            
            for subscribe in tv_subscribes:
                try:
                    # 跳过洗版订阅
                    if subscribe.best_version:
                        skipped_count += 1
                        continue
                    
                    # 跳过没有note的订阅
                    if not subscribe.note:
                        skipped_count += 1
                        continue
                    
                    # 获取订阅的已下载集数
                    current_episodes = subscribe.note or []
                    if not current_episodes:
                        skipped_count += 1
                        continue
                    
                    # 查询Emby中的集数
                    emby_item_id, emby_season_episodes = emby.get_tv_episodes(
                        title=subscribe.name,
                        year=subscribe.year,
                        tmdb_id=subscribe.tmdbid,
                        season=subscribe.season
                    )
                    
                    if not emby_item_id or not emby_season_episodes:
                        logger.debug(f"订阅 {subscribe.name} S{subscribe.season:02d} 在Emby中未找到")
                        skipped_count += 1
                        continue
                    
                    # 获取对应季的集数列表
                    emby_episodes = emby_season_episodes.get(subscribe.season, [])
                    if not emby_episodes:
                        logger.debug(f"订阅 {subscribe.name} S{subscribe.season:02d} 在Emby中未找到集数")
                        skipped_count += 1
                        continue
                    
                    # 计算需要保留的集数（只在Emby中存在的集数）
                    emby_episodes_set = set(emby_episodes)
                    current_episodes_set = set(current_episodes)
                    
                    # 只保留在Emby中存在的集数
                    new_episodes = sorted(list(current_episodes_set.intersection(emby_episodes_set)))
                    
                    # 如果集数有变化，更新订阅
                    if set(new_episodes) != current_episodes_set:
                        removed_episodes = sorted(list(current_episodes_set - emby_episodes_set))
                        
                        logger.info(f"订阅 {subscribe.name} S{subscribe.season:02d} 更新集数：")
                        logger.info(f"  当前集数: {sorted(current_episodes)}")
                        logger.info(f"  Emby集数: {sorted(emby_episodes)}")
                        logger.info(f"  移除集数: {removed_episodes}")
                        logger.info(f"  新集数: {new_episodes}")
                        
                        # 更新订阅的note字段
                        subscribe_oper.update(subscribe.id, {
                            "note": new_episodes
                        })
                        
                        updated_count += 1
                        updated_subscribes.append({
                            "name": subscribe.name,
                            "season": subscribe.season,
                            "removed_episodes": removed_episodes,
                            "new_episodes": new_episodes
                        })
                    else:
                        logger.debug(f"订阅 {subscribe.name} S{subscribe.season:02d} 集数无需更新")
                        skipped_count += 1
                        
                except Exception as e:
                    error_msg = f"处理订阅 {subscribe.name} S{subscribe.season:02d} 时发生错误: {str(e)}"
                    logger.error(error_msg)
                    error_count += 1
            
            # 发送通知
            if self._send_notification and updated_count > 0:
                self._send_sync_notification(updated_count, skipped_count, error_count, updated_subscribes)
            
            result = {
                "success": error_count == 0,
                "message": f"Emby集数同步完成 - 更新: {updated_count}, 跳过: {skipped_count}, 错误: {error_count}",
                "updated_count": updated_count,
                "skipped_count": skipped_count,
                "error_count": error_count,
                "updated_subscribes": updated_subscribes
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
    
    def _send_sync_notification(self, updated_count: int, skipped_count: int, 
                               error_count: int, updated_subscribes: List[Dict]):
        """
        发送同步通知
        
        Args:
            updated_count: 更新的订阅数量
            skipped_count: 跳过的订阅数量
            error_count: 错误的订阅数量
            updated_subscribes: 更新的订阅列表
        """
        try:
            # 构建通知消息
            message = f"Emby集数同步任务完成\n\n"
            message += f"更新订阅: {updated_count} 个\n"
            message += f"跳过订阅: {skipped_count} 个\n"
            
            if error_count > 0:
                message += f"错误: {error_count} 个\n"
            
            if updated_subscribes:
                message += f"\n更新的订阅:\n"
                for sub_info in updated_subscribes[:10]:  # 最多显示10个
                    removed_str = f"移除: {', '.join(map(str, sub_info['removed_episodes']))}" if sub_info['removed_episodes'] else "无移除"
                    message += f"  {sub_info['name']} S{sub_info['season']:02d} - {removed_str}\n"
                
                if len(updated_subscribes) > 10:
                    message += f"  ... 还有 {len(updated_subscribes) - 10} 个订阅已更新\n"
            
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

