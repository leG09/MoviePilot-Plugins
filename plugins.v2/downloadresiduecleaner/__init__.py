import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

try:
    from typing_extensions import Annotated
except ImportError:
    from typing import Annotated

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.security import verify_apikey
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType


lock = threading.Lock()


class DownloadResidueCleaner(_PluginBase):
    plugin_name = "下载残留目录清理"
    plugin_desc = "清理下载目录中转移后留下的空目录、压缩分卷、NFO、SFV、截图等残留目录。"
    plugin_icon = "refresh2.png"
    plugin_color = "#607D8B"
    plugin_version = "1.0.0"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "downloadresiduecleaner"
    plugin_order = 30
    auth_level = 1

    MEDIA_SUFFIXES = {
        ".mkv",
        ".mp4",
        ".avi",
        ".ts",
        ".m2ts",
        ".iso",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
    }
    RESIDUE_SUFFIXES = {
        ".rar",
        ".sfv",
        ".nfo",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".txt",
    }

    _enabled = False
    _onlyonce = False
    _dry_run = True
    _notify = True
    _clean_empty = True
    _clean_residue = True
    _download_root = "/media/downloads"
    _min_age_minutes = 30
    _cron = "17 * * * *"
    _scheduler = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}

        self._enabled = bool(config.get("enabled", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._dry_run = bool(config.get("dry_run", True))
        self._notify = bool(config.get("notify", True))
        self._clean_empty = bool(config.get("clean_empty", True))
        self._clean_residue = bool(config.get("clean_residue", True))
        self._download_root = str(config.get("download_root") or "/media/downloads").strip()
        self._min_age_minutes = self._to_int(config.get("min_age_minutes"), 30)
        self._cron = str(config.get("cron") or "17 * * * *").strip()

        if self._min_age_minutes < 0:
            self._min_age_minutes = 30
        if not self._cron:
            self._cron = "17 * * * *"

        logger.info(
            f"下载残留目录清理初始化：enabled={self._enabled}, dry_run={self._dry_run}, "
            f"root={self._download_root}, min_age_minutes={self._min_age_minutes}, cron={self._cron}"
        )

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.clean,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="下载残留目录清理",
            )
            if self._scheduler.get_jobs():
                self._scheduler.start()

            self._onlyonce = False
            self.__update_config()

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "dry_run": self._dry_run,
            "notify": self._notify,
            "clean_empty": self._clean_empty,
            "clean_residue": self._clean_residue,
            "download_root": self._download_root,
            "min_age_minutes": self._min_age_minutes,
            "cron": self._cron,
        })

    def get_state(self) -> bool:
        return bool(self._enabled and self._download_root and self._cron)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/clean_download_residue",
                "event": EventType.PluginAction,
                "desc": "清理下载残留目录",
                "category": "清理",
                "data": {"action": "clean_download_residue"},
            },
            {
                "cmd": "/preview_download_residue",
                "event": EventType.PluginAction,
                "desc": "预览下载残留目录",
                "category": "清理",
                "data": {"action": "preview_download_residue"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/clean",
                "endpoint": self.api_clean,
                "methods": ["POST"],
                "summary": "清理下载残留目录",
            },
            {
                "path": "/preview",
                "endpoint": self.api_preview,
                "methods": ["POST"],
                "summary": "预览下载残留目录",
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        try:
            return [{
                "id": "DownloadResidueCleaner",
                "name": "下载残留目录清理服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.clean,
                "kwargs": {},
            }]
        except Exception as e:
            logger.error(f"下载残留目录清理创建定时任务失败：{e}")
            return []

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not event or not event.event_data:
            return
        action = event.event_data.get("action")
        if action not in {"clean_download_residue", "preview_download_residue"}:
            return

        original_dry_run = self._dry_run
        if action == "preview_download_residue":
            self._dry_run = True

        result = self.clean()
        self._dry_run = original_dry_run

        text = self._format_result(result)
        self.post_message(
            channel=event.event_data.get("channel"),
            title="下载残留目录清理完成",
            text=text,
            mtype=NotificationType.Plugin,
        )

    def api_clean(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        return {"success": True, "result": self.clean()}

    def api_preview(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        original_dry_run = self._dry_run
        self._dry_run = True
        try:
            return {"success": True, "result": self.clean()}
        finally:
            self._dry_run = original_dry_run

    def get_form(self) -> tuple:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件", "color": "primary"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "dry_run", "label": "预览模式", "color": "warning"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "onlyonce", "label": "立即运行一次", "color": "success"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "download_root",
                                        "label": "下载目录",
                                        "placeholder": "/media/downloads",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "min_age_minutes",
                                        "label": "最小目录年龄(分钟)",
                                        "type": "number",
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "Cron 表达式",
                                        "placeholder": "17 * * * *",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clean_empty", "label": "清理空目录", "color": "primary"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clean_residue", "label": "清理残留目录", "color": "primary"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知", "color": "primary"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": "只检查下载目录顶层子目录；包含媒体文件或 .!qB 的目录会跳过。残留目录指只包含 rar/r00-r99/nfo/sfv/png/jpg/webp/txt 等附属文件的目录。",
                        },
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "dry_run": True,
            "notify": True,
            "clean_empty": True,
            "clean_residue": True,
            "download_root": "/media/downloads",
            "min_age_minutes": 30,
            "cron": "17 * * * *",
        }

    def get_page(self) -> List[dict]:
        return []

    def clean(self) -> Dict[str, Any]:
        with lock:
            return self.__clean()

    def __clean(self) -> Dict[str, Any]:
        root = Path(self._download_root).resolve()
        result = {
            "root": str(root),
            "dry_run": self._dry_run,
            "scanned": 0,
            "removed": 0,
            "would_remove": 0,
            "skipped_young": 0,
            "failed": 0,
            "items": [],
            "errors": [],
        }

        if not root.is_dir():
            msg = f"下载目录不存在或不是目录：{root}"
            logger.warning(msg)
            result["errors"].append(msg)
            return result

        cutoff = time.time() - self._min_age_minutes * 60
        for child in root.iterdir():
            result["scanned"] += 1
            if not child.is_dir() or child.is_symlink():
                continue

            try:
                stat = child.lstat()
            except FileNotFoundError:
                continue
            if stat.st_mtime > cutoff:
                result["skipped_young"] += 1
                continue

            kind = self.__classify_directory(child)
            if kind == "empty" and not self._clean_empty:
                continue
            if kind == "residue_only" and not self._clean_residue:
                continue
            if kind not in {"empty", "residue_only"}:
                continue

            item = {"path": str(child), "kind": kind}
            if self._dry_run:
                result["would_remove"] += 1
                result["items"].append(item)
                logger.info(f"预览清理 {kind}：{child}")
                continue

            try:
                if kind == "empty":
                    child.rmdir()
                else:
                    shutil.rmtree(child)
            except Exception as e:
                result["failed"] += 1
                item["error"] = str(e)
                result["errors"].append(f"{child}: {e}")
                logger.error(f"清理 {kind} 失败：{child} - {e}")
            else:
                result["removed"] += 1
                result["items"].append(item)
                logger.info(f"已清理 {kind}：{child}")

        summary = self._format_result(result)
        logger.info(summary)
        if self._notify and (result["removed"] or result["would_remove"] or result["failed"]):
            self.post_message(
                mtype=NotificationType.Plugin,
                title="下载残留目录清理",
                text=summary,
            )
        return result

    def __classify_directory(self, path: Path) -> str:
        files = []
        for child in path.rglob("*"):
            if child.is_symlink():
                return ""
            if child.is_file():
                files.append(child)

        if not files:
            return "empty"
        if any(child.name.endswith(".!qB") for child in files):
            return ""
        if any(child.suffix.lower() in self.MEDIA_SUFFIXES for child in files):
            return ""
        if all(self.__is_residue_file(child) for child in files):
            return "residue_only"
        return ""

    def __is_residue_file(self, path: Path) -> bool:
        suffix = path.suffix.lower()
        if suffix in self.RESIDUE_SUFFIXES:
            return True
        name = path.name.lower()
        return len(name) > 4 and name[-4] == "." and name[-3] == "r" and name[-2:].isdigit()

    @staticmethod
    def _format_result(result: Dict[str, Any]) -> str:
        action_key = "would_remove" if result.get("dry_run") else "removed"
        lines = [
            f"目录：{result.get('root')}",
            f"模式：{'预览' if result.get('dry_run') else '删除'}",
            f"扫描：{result.get('scanned', 0)}",
            f"{'预计清理' if result.get('dry_run') else '已清理'}：{result.get(action_key, 0)}",
            f"跳过新目录：{result.get('skipped_young', 0)}",
            f"失败：{result.get('failed', 0)}",
        ]
        items = result.get("items") or []
        if items:
            lines.append("项目：")
            for item in items[:20]:
                lines.append(f"- [{item.get('kind')}] {item.get('path')}")
            if len(items) > 20:
                lines.append(f"- ... 另 {len(items) - 20} 项")
        errors = result.get("errors") or []
        if errors:
            lines.append("错误：")
            for error in errors[:5]:
                lines.append(f"- {error}")
        return "\n".join(lines)

    def stop_service(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown()
            except Exception as e:
                logger.warning(f"停止下载残留目录清理调度器失败：{e}")
            self._scheduler = None
