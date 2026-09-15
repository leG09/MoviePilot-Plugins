import fnmatch
import os
import re
import shutil
import stat as stat_module
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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
    plugin_desc = "清理下载目录中转移后留下的空目录、字幕、NFO、压缩分卷、截图、过期临时文件等残留目录。"
    plugin_icon = "refresh2.png"
    plugin_color = "#607D8B"
    plugin_version = "1.1.0"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "downloadresiduecleaner"
    plugin_order = 30
    auth_level = 1

    # 媒体文件后缀：目录中出现任意一个即视为有效下载，整个目录跳过
    MEDIA_SUFFIXES = {
        ".mkv",
        ".mp4",
        ".avi",
        ".ts",
        ".m2ts",
        ".m2v",
        ".iso",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpg",
        ".mpeg",
        ".vob",
        ".rmvb",
        ".rm",
        ".asf",
        ".divx",
        ".ogm",
        ".ogv",
        ".3gp",
        ".3g2",
        ".tp",
        ".trp",
        ".rec",
        ".wtv",
        ".dvr-ms",
        ".mxf",
        ".f4v",
        ".mpe",
        ".m1v",
        ".bdmv",
        ".mpls",
    }

    # 附属残留文件后缀：转移后常被遗留的说明/校验/图片/文本类文件
    RESIDUE_SUFFIXES = {
        ".rar",
        ".sfv",
        ".srr",
        ".srs",
        ".nfo",
        ".md5",
        ".sha1",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".tiff",
        ".txt",
        ".text",
        ".log",
        ".url",
        ".htm",
        ".html",
        ".pdf",
        ".diz",
        ".rtf",
        ".csv",
        ".json",
        ".xml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".torrent",
        ".sample",
    }

    # 字幕文件后缀：媒体文件已转移、仅剩字幕时视为残留
    SUBTITLE_SUFFIXES = {
        ".srt",
        ".ass",
        ".ssa",
        ".sub",
        ".idx",
        ".sup",
        ".vtt",
        ".smi",
        ".sami",
        ".mks",
        ".usf",
        ".ssf",
        ".psb",
        ".aqt",
        ".jss",
        ".rt",
        ".stl",
        ".dks",
        ".pjs",
        ".subtitles",
    }

    # 下载器/传输工具的临时文件后缀：超过「临时文件年龄」仍未完成即视为残留
    TEMP_SUFFIXES = {
        ".tmp",
        ".temp",
        ".part",
        ".partial",
        ".crdownload",
        ".download",
        ".downloading",
        ".mtp",
        ".bcv",
        ".opdownload",
    }

    # 活跃下载标记：命中即认为目录仍在下载，无条件跳过
    ACTIVE_SUFFIXES = {
        ".!qb",
        ".!ut",
        ".aria2",
        ".td",
        ".xltd",
        ".bc!",
        ".mtd",
        ".!qbit",
        ".part.met",
    }

    # 默认忽略的目录名（glob 模式），避免误删回收站等系统目录
    DEFAULT_IGNORE_PATTERNS = [
        ".recycle*",
        "@recycle",
        "@eadir",
        "#recycle",
        "lost+found",
        ".snapshots",
        ".trash*",
        ".qbittorrent*",
        ".temporary*",
        ".zfile*",
    ]

    # r00-r99 之类的分卷压缩包
    _RAR_PART_RE = re.compile(r"\.r\d{2}$")

    _enabled = False
    _onlyonce = False
    _dry_run = True
    _notify = True
    _clean_empty = True
    _clean_residue = True
    _clean_subtitle = True
    _clean_temp = True
    _download_root = "/media/downloads"
    _min_age_minutes = 30
    _temp_age_minutes = 1440
    _cron = "17 * * * *"
    _extra_suffixes: List[str] = []
    _ignore_patterns: List[str] = []
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
        self._clean_subtitle = bool(config.get("clean_subtitle", True))
        self._clean_temp = bool(config.get("clean_temp", True))
        self._download_root = str(config.get("download_root") or "/media/downloads").strip()
        self._min_age_minutes = self._to_int(config.get("min_age_minutes"), 30)
        self._temp_age_minutes = self._to_int(config.get("temp_age_minutes"), 1440)
        self._cron = str(config.get("cron") or "17 * * * *").strip()
        self._extra_suffixes = self._split_list(config.get("extra_suffixes"))
        self._ignore_patterns = self._split_list(config.get("ignore_patterns")) or list(self.DEFAULT_IGNORE_PATTERNS)

        if self._min_age_minutes < 0:
            self._min_age_minutes = 30
        if self._temp_age_minutes < 0:
            self._temp_age_minutes = 1440
        if not self._cron:
            self._cron = "17 * * * *"

        logger.info(
            f"下载残留目录清理初始化：enabled={self._enabled}, dry_run={self._dry_run}, "
            f"root={self._download_root}, min_age_minutes={self._min_age_minutes}, "
            f"temp_age_minutes={self._temp_age_minutes}, "
            f"clean=[empty={self._clean_empty}, residue={self._clean_residue}, "
            f"subtitle={self._clean_subtitle}, temp={self._clean_temp}], cron={self._cron}"
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

    @staticmethod
    def _split_list(value: Any) -> List[str]:
        """把逗号/空格/换行分隔的字符串拆成小写去重列表"""
        if not value:
            return []
        if isinstance(value, (list, tuple, set)):
            items = [str(item) for item in value]
        else:
            items = re.split(r"[,\s;]+", str(value))
        result = []
        for item in items:
            item = item.strip().lower()
            if item and item not in result:
                result.append(item)
        return result

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "dry_run": self._dry_run,
            "notify": self._notify,
            "clean_empty": self._clean_empty,
            "clean_residue": self._clean_residue,
            "clean_subtitle": self._clean_subtitle,
            "clean_temp": self._clean_temp,
            "download_root": self._download_root,
            "min_age_minutes": self._min_age_minutes,
            "temp_age_minutes": self._temp_age_minutes,
            "cron": self._cron,
            "extra_suffixes": ",".join(self._extra_suffixes),
            "ignore_patterns": ",".join(self._ignore_patterns),
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

        dry_run = True if action == "preview_download_residue" else None
        result = self.clean(dry_run=dry_run)

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
        return {"success": True, "result": self.clean(dry_run=True)}

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
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clean_empty", "label": "清理空目录", "color": "primary"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clean_residue", "label": "清理附属残留", "color": "primary"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clean_subtitle", "label": "清理残留字幕", "color": "primary"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "clean_temp", "label": "清理过期临时文件", "color": "primary"},
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "temp_age_minutes",
                                        "label": "临时文件年龄(分钟)",
                                        "type": "number",
                                        "hint": "超过该时长未变化的 .tmp/.part 才清理，默认 1440（24小时）",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 9},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "发送通知", "color": "primary"},
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
                                        "model": "extra_suffixes",
                                        "label": "附加残留后缀",
                                        "placeholder": ".ass,.ssa",
                                        "hint": "额外按残留处理的文件后缀，逗号分隔",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "ignore_patterns",
                                        "label": "忽略目录名",
                                        "placeholder": ".recycle*,@eaDir,lost+found",
                                        "hint": "跳过匹配的顶层目录名（glob），留空使用内置默认值",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "text": (
                                "只检查下载目录的顶层子目录，并以目录内最近一次文件变动时间判断年龄。"
                                "以下情况一律跳过：目录内含媒体文件、含 .!qB/.!ut/.aria2 等下载中标记、"
                                "含未超时的 .tmp/.part 临时文件、含无法识别的文件、含符号链接或以 . 开头的隐藏目录。"
                                "可清理的残留类型：空目录（含只剩空子目录）、附属残留（nfo/sfv/图片/txt/rar 分卷等）、"
                                "残留字幕（srt/ass/sub/idx/sup 等）、过期临时文件。"
                            ),
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
            "clean_subtitle": True,
            "clean_temp": True,
            "download_root": "/media/downloads",
            "min_age_minutes": 30,
            "temp_age_minutes": 1440,
            "cron": "17 * * * *",
            "extra_suffixes": "",
            "ignore_patterns": ",".join(self.DEFAULT_IGNORE_PATTERNS),
        }

    def get_page(self) -> List[dict]:
        return []

    def clean(self, dry_run: Optional[bool] = None) -> Dict[str, Any]:
        with lock:
            return self.__clean(self._dry_run if dry_run is None else bool(dry_run))

    def __clean(self, dry_run: bool) -> Dict[str, Any]:
        root = Path(self._download_root).resolve()
        result = {
            "root": str(root),
            "dry_run": dry_run,
            "scanned": 0,
            "removed": 0,
            "would_remove": 0,
            "skipped_young": 0,
            "skipped_ignored": 0,
            "skipped_disabled": 0,
            "failed": 0,
            "freed_bytes": 0,
            "kinds": {},
            "items": [],
            "errors": [],
        }

        if not root.is_dir():
            msg = f"下载目录不存在或不是目录：{root}"
            logger.warning(msg)
            result["errors"].append(msg)
            return result

        now = time.time()
        min_age_seconds = self._min_age_minutes * 60
        temp_age_seconds = self._temp_age_minutes * 60

        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            msg = f"读取下载目录失败：{root} - {e}"
            logger.error(msg)
            result["errors"].append(msg)
            return result

        for child in children:
            result["scanned"] += 1

            try:
                if child.is_symlink() or not child.is_dir():
                    continue
            except OSError:
                continue

            # 隐藏目录与忽略名单
            if child.name.startswith(".") or self.__is_ignored(child.name):
                result["skipped_ignored"] += 1
                continue

            files, latest_mtime, unsafe = self.__walk(child)
            if unsafe:
                logger.debug(f"跳过无法安全判定的目录：{child}")
                continue

            try:
                dir_mtime = child.lstat().st_mtime
            except OSError:
                continue
            # 以目录自身与内部所有条目的最近变动时间判断年龄，避免误删正在写入的目录
            if max(dir_mtime, latest_mtime) > now - min_age_seconds:
                result["skipped_young"] += 1
                continue

            categories, blocked = self.__categorize(files, now, temp_age_seconds)
            if blocked:
                continue

            kind, enabled = self.__resolve_kind(categories, files)
            if not enabled:
                result["skipped_disabled"] += 1
                logger.debug(f"跳过未启用清理类型的目录 [{kind}]：{child}")
                continue

            size = sum(item[1] for item in files)
            item = {"path": str(child), "kind": kind, "files": len(files), "bytes": size}
            result["kinds"][kind] = result["kinds"].get(kind, 0) + 1

            if dry_run:
                result["would_remove"] += 1
                result["freed_bytes"] += size
                result["items"].append(item)
                logger.info(f"预览清理 [{kind}] {len(files)} 个文件 {self._human_size(size)}：{child}")
                continue

            try:
                # 统一使用 rmtree，兼容「目录内只剩空子目录」的场景（rmdir 会报 Directory not empty）
                shutil.rmtree(child)
            except Exception as e:
                result["failed"] += 1
                result["kinds"][kind] -= 1
                item["error"] = str(e)
                result["errors"].append(f"{child}: {e}")
                logger.error(f"清理 [{kind}] 失败：{child} - {e}")
            else:
                result["removed"] += 1
                result["freed_bytes"] += size
                result["items"].append(item)
                logger.info(f"已清理 [{kind}] {len(files)} 个文件 {self._human_size(size)}：{child}")

        summary = self._format_result(result)
        logger.info(summary)
        if self._notify and (result["removed"] or result["would_remove"] or result["failed"]):
            self.post_message(
                mtype=NotificationType.Plugin,
                title="下载残留目录清理",
                text=summary,
            )
        return result

    def __is_ignored(self, name: str) -> bool:
        lowered = name.lower()
        for pattern in self._ignore_patterns:
            if fnmatch.fnmatch(lowered, pattern):
                return True
        return False

    @staticmethod
    def __walk(path: Path) -> Tuple[List[Tuple[Path, int, float]], float, bool]:
        """
        非递归跟随符号链接地遍历目录。
        返回（普通文件列表[(路径, 大小, 修改时间)], 内部条目最近修改时间, 是否存在无法安全判定的条目）
        """
        files: List[Tuple[Path, int, float]] = []
        latest = 0.0
        unsafe = False
        stack = [path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                unsafe = True
                continue
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    unsafe = True
                    continue
                if entry.is_symlink() or (
                    not stat_module.S_ISREG(st.st_mode) and not stat_module.S_ISDIR(st.st_mode)
                ):
                    # 符号链接、管道、套接字、设备文件等一律视为不安全
                    unsafe = True
                    continue
                if st.st_mtime > latest:
                    latest = st.st_mtime
                if stat_module.S_ISDIR(st.st_mode):
                    stack.append(Path(entry.path))
                else:
                    files.append((Path(entry.path), st.st_size, st.st_mtime))
        return files, latest, unsafe

    def __categorize(
        self,
        files: List[Tuple[Path, int, float]],
        now: float,
        temp_age_seconds: int,
    ) -> Tuple[Set[str], bool]:
        """把文件归类，返回（可清理类别集合, 是否应整体跳过）"""
        categories: Set[str] = set()
        for path, _size, mtime in files:
            suffix = self.__suffix_of(path.name)
            if suffix in self.ACTIVE_SUFFIXES or self.__is_active_name(path.name):
                return set(), True
            if suffix in self.MEDIA_SUFFIXES:
                return set(), True
            if suffix in self.TEMP_SUFFIXES:
                # 未超时的临时文件说明仍在写入，整个目录跳过
                if now - mtime < temp_age_seconds:
                    return set(), True
                categories.add("temp")
                continue
            if suffix in self.SUBTITLE_SUFFIXES:
                categories.add("subtitle")
                continue
            if self.__is_residue(suffix, path.name):
                categories.add("residue")
                continue
            # 存在无法识别的文件，保守跳过
            logger.debug(f"目录含无法识别的文件，跳过：{path}")
            return set(), True
        return categories, False

    @staticmethod
    def __suffix_of(name: str) -> str:
        """获取小写后缀，兼容形如 .srt 这种「只有后缀」的文件名"""
        suffix = Path(name).suffix.lower()
        if not suffix and name.startswith("."):
            suffix = name.lower()
        return suffix

    @staticmethod
    def __is_active_name(name: str) -> bool:
        lowered = name.lower()
        return lowered.endswith(".!qb") or lowered.endswith(".aria2") or lowered.endswith(".!ut")

    def __is_residue(self, suffix: str, name: str) -> bool:
        if suffix in self.RESIDUE_SUFFIXES:
            return True
        if suffix in self._extra_suffixes:
            return True
        lowered = name.lower()
        if self._RAR_PART_RE.search(lowered):
            return True
        # 无后缀的常见残留说明文件
        if not suffix and lowered in {"sample", "proof", "thumbs", "covers", "subs", "subtitles", "nfo", "sfv"}:
            return True
        return False

    def __resolve_kind(self, categories: Set[str], files: List[Tuple[Path, int, float]]) -> Tuple[str, bool]:
        """根据类别集合生成可读的类型标签，并判断对应清理开关是否开启"""
        if not categories:
            return "empty", self._clean_empty

        allowed = {
            "temp": self._clean_temp,
            "subtitle": self._clean_subtitle,
            "residue": self._clean_residue,
        }
        enabled = all(allowed.get(category, False) for category in categories)
        # 展示顺序：temp > subtitle > residue
        order = ["temp", "subtitle", "residue"]
        kind = "+".join(category for category in order if category in categories)
        return kind, enabled

    @staticmethod
    def _human_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
            value /= 1024
        return f"{value:.1f}TB"

    @staticmethod
    def _format_result(result: Dict[str, Any]) -> str:
        dry_run = result.get("dry_run")
        action_key = "would_remove" if dry_run else "removed"
        lines = [
            f"目录：{result.get('root')}",
            f"模式：{'预览' if dry_run else '删除'}",
            f"扫描：{result.get('scanned', 0)}",
            f"{'预计清理' if dry_run else '已清理'}：{result.get(action_key, 0)}",
            f"{'预计释放' if dry_run else '已释放'}：{DownloadResidueCleaner._human_size(result.get('freed_bytes', 0))}",
            f"跳过新目录：{result.get('skipped_young', 0)}",
            f"失败：{result.get('failed', 0)}",
        ]
        kinds = result.get("kinds") or {}
        if kinds:
            detail = "，".join(f"{kind} {count}" for kind, count in sorted(kinds.items()) if count > 0)
            if detail:
                lines.append(f"类型：{detail}")
        items = result.get("items") or []
        if items:
            lines.append("项目：")
            for item in items[:20]:
                lines.append(
                    f"- [{item.get('kind')}] {item.get('path')}"
                    f"（{item.get('files', 0)} 文件 / {DownloadResidueCleaner._human_size(item.get('bytes', 0))}）"
                )
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
