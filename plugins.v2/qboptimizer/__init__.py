import copy
import math
import posixpath
import re
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
from app.core.context import Context
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.helper.downloader import DownloaderHelper
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo
from app.schemas.types import EventType

lock = threading.Lock()


class QbOptimizer(_PluginBase):
    plugin_name = "QB种子优化"
    plugin_desc = "保护过滤、持续观察后清理异常下载、安全换源、队列优化和磁盘空间控制"
    plugin_icon = "Qbittorrent_A.png"
    plugin_version = "2.1"
    plugin_author = "leG09"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "qboptimizer_"
    plugin_order = 10
    auth_level = 1

    _DEFAULTS = {
        "enabled": False, "notify": True, "onlyonce": False,
        "cron": "0 */6 * * *", "downloaders": [], "search_sites": [],
        "enable_zero_seed_clean": True, "enable_timeout_clean": True,
        "enable_unrecognized_clean": False, "enable_auto_redownload": True,
        "exclude_keywords": "", "exclude_categories": "", "exclude_labels": "",
        "zero_seed_threshold": 0, "timeout_hours": 24,
        "max_download_slots": 5, "max_zero_seed_process": 50,
        "max_timeout_process": 50, "max_unrecognized_process": 50,
        "max_priority_process": 100, "enable_score_priority": True,
        "speed_weight": 1.0, "seeder_weight": 0.5, "min_score_threshold": 1.0,
        "enable_disk_monitor": False, "disk_space_threshold": 10.0,
        "io_cache_threshold": 70.0, "io_queue_threshold": 8000,
        "speed_limit_mbps": 1.0, "monitor_duration": 30, "monitor_interval": 3,
        "enable_download_threshold": False, "min_disk_space_threshold": 50.0,
        "confirm_checks": 3, "cleanup_grace_hours": 1.0,
        "protect_progress": 95.0, "slow_speed_kib": 100.0,
        "priority_cooldown_minutes": 30, "resume_margin_gb": 5.0,
    }
    _ACTIVE = {"downloading", "stalleddl"}
    _PAUSED = {"pauseddl", "stoppeddl"}
    _scheduler = None

    def init_plugin(self, config: dict = None):
        self.stop_service()
        # 等待正在执行的任务退出后再替换配置，避免运行中改变保护规则。
        with lock:
            self._event = threading.Event()
            config = dict(config or {})
            for key, default in self._DEFAULTS.items():
                value = config.get(key, copy.deepcopy(default))
                if isinstance(default, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("true", "1")
                elif isinstance(default, (int, float)):
                    try:
                        value = float(value)
                        if not math.isfinite(value):
                            raise ValueError(key)
                        value = max(0, value)
                        if isinstance(default, int):
                            value = int(value)
                    except (TypeError, ValueError):
                        value = default
                elif isinstance(default, list):
                    value = value if isinstance(value, list) else []
                else:
                    value = str(value or "")
                setattr(self, "_" + key, value)
            self._confirm_checks = max(2, self._confirm_checks)
            self._cleanup_grace_hours = max(0.25, self._cleanup_grace_hours)
            self._protect_progress = min(100, max(1, self._protect_progress))
            self._timeout_hours = max(1, self._timeout_hours)
            self._slow_speed_kib = max(1, self._slow_speed_kib)
            self._monitor_interval = max(1, self._monitor_interval)
            self._monitor_duration = min(300, max(1, self._monitor_duration))
            self._speed_limit_mbps = max(0.01, self._speed_limit_mbps)
            self._max_download_slots = max(1, self._max_download_slots)
            self._cron = self._cron or self._DEFAULTS["cron"]
            try:
                CronTrigger.from_crontab(self._cron)
            except ValueError:
                logger.warning("【QB种子优化】无效执行周期，使用默认周期")
                self._cron = self._DEFAULTS["cron"]
            self._runtime = self.get_data("runtime_v2_1") or {}
            if not isinstance(self._runtime, dict):
                self._runtime = {}
            self._compile_filters()
            if self._onlyonce:
                self._onlyonce = False
                self.update_config({**config, **{key: getattr(self, "_" + key) for key in self._DEFAULTS}})
                if self._downloaders:
                    self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                    self._scheduler.add_job(
                        self.optimize_torrents, trigger="date",
                        run_date=datetime.now(pytz.timezone(settings.TZ)) + timedelta(seconds=3))
                    self._scheduler.start()

    def get_state(self) -> bool:
        return bool(getattr(self, "_enabled", False) and self._downloaders and self._cron)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {"cmd": "/qb_optimize", "event": EventType.PluginAction, "desc": "执行QB种子优化",
             "category": "QB优化", "data": {"action": "qb_optimize"}},
            {"cmd": "/qb_status", "event": EventType.PluginAction, "desc": "查看QB优化状态",
             "category": "QB优化", "data": {"action": "qb_status"}},
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state():
            return [{"id": "QbOptimizer", "name": "QB种子优化服务",
                     "trigger": CronTrigger.from_crontab(self._cron),
                     "func": self.optimize_torrents, "kwargs": {}}]
        return []

    @property
    def service_infos(self) -> Optional[Dict[str, ServiceInfo]]:
        services = DownloaderHelper().get_services(name_filters=self._downloaders) or {}
        return {name: info for name, info in services.items()
                if self.check_is_qb(info) and not info.instance.is_inactive()}

    @staticmethod
    def check_is_qb(service_info) -> bool:
        return DownloaderHelper().is_downloader(service_type="qbittorrent", service=service_info)

    def __get_downloader(self, name: str):
        info = (self.service_infos or {}).get(name)
        return info.instance if info else None

    @eventmanager.register(EventType.PluginAction)
    def handle_qb_optimize(self, event: Event):
        if self._enabled and event and (event.event_data or {}).get("action") == "qb_optimize":
            self.optimize_torrents()

    @eventmanager.register(EventType.PluginAction)
    def handle_qb_status(self, event: Event):
        if self._enabled and event and (event.event_data or {}).get("action") == "qb_status":
            self.show_status()

    @staticmethod
    def _number(obj, name, default=0):
        try:
            value = float(getattr(obj, name, default))
            return value if math.isfinite(value) else default
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _state(torrent):
        return str(getattr(torrent, "state", "") or "").lower()

    def _store(self, name):
        store = self._runtime.get(name)
        if not isinstance(store, dict):
            store = {}
            self._runtime[name] = store
        for section in ("observations", "paused", "replacements", "speeds"):
            if not isinstance(store.get(section), dict):
                store[section] = {}
        return store

    def _save_runtime(self):
        # 写入失败向上传播，不能在没有保存归属/换源记录的情况下继续破坏性操作。
        self.save_data("runtime_v2_1", self._runtime)

    def _compile_filters(self):
        def lines(text):
            return {line.strip().casefold() for line in text.splitlines() if line.strip()}
        self._filters = (lines(self._exclude_keywords), lines(self._exclude_categories), lines(self._exclude_labels))

    def _exclusion_reason(self, torrent):
        keywords, categories, labels = self._filters
        name = str(getattr(torrent, "name", "") or "").casefold()
        category = str(getattr(torrent, "category", "") or "").strip().casefold()
        tags = getattr(torrent, "tags", "") or ""
        if isinstance(tags, str):
            tags = tags.split(",")
        tags = {str(tag).strip().casefold() for tag in tags if str(tag).strip()}
        if any(word in name for word in keywords):
            return "排除关键字"
        if category in categories:
            return "排除分类"
        if tags & labels:
            return "排除标签"
        return ""

    def _is_excluded_torrent(self, torrent) -> bool:
        return bool(self._exclusion_reason(torrent))

    def _fetch(self, downloader, ids=None):
        torrents, error = downloader.get_torrents(**({"ids": ids} if ids is not None else {}))
        if error:
            raise RuntimeError(f"获取种子失败: {error}")
        return list(torrents or [])

    def _fresh(self, downloader, torrent_hash):
        return next((t for t in self._fetch(downloader, [torrent_hash])
                     if str(getattr(t, "hash", "")).lower() == torrent_hash.lower()), None)

    def optimize_torrents(self):
        if not lock.acquire(blocking=False):
            logger.info("【QB种子优化】已有任务运行，本次跳过")
            return
        try:
            for name in self._downloaders:
                if self._event.is_set():
                    break
                try:
                    downloader = self.__get_downloader(name)
                    if not downloader:
                        continue
                    torrents = self._fetch(downloader)
                    store = self._store(name)
                    live = {t.hash for t in torrents}
                    for section in ("observations", "paused", "replacements"):
                        store[section] = {key: value for key, value in store.get(section, {}).items() if key in live}
                    # 每个下载器独立限速和空间控制，空间紧张也允许暂停任务。
                    constrained = self._monitor_disk_and_io(downloader, name)
                    self._manage_download_threshold(downloader, self._fetch(downloader), name, allow_resume=not constrained)
                    counts, failures = {}, []
                    for kind in ("zero", "slow", "unrecognized"):
                        if self._event.is_set():
                            break
                        removed, failed = self._clean_torrents(
                            downloader, self._fetch(downloader), name, kind, allow_replace=not constrained)
                        counts[kind] = removed
                        failures.extend(failed)
                    if not constrained and not self._event.is_set():
                        self._boost_priority_torrents(downloader, self._fetch(downloader), name)
                    self._save_runtime()
                    logger.info(f"【QB种子优化】{name} 实际移除: {counts}; 未完成操作: {len(failures)}")
                    if self._notify and (any(counts.values()) or failures):
                        self.post_message(mtype=NotificationType.Download, title=f"【QB优化】{name}",
                                          text=f"实际移除: {counts}\n" + "\n".join(failures[:20]))
                except Exception as exc:
                    logger.error(f"【QB种子优化】{name} 本轮停止: {exc}")
        finally:
            lock.release()

    def _basic_cleanable(self, torrent):
        if torrent is None or self._event.is_set() or self._is_excluded_torrent(torrent):
            return False
        if self._state(torrent) not in self._ACTIVE or getattr(torrent, "force_start", False):
            return False
        progress = self._number(torrent, "progress", 1)
        added = self._number(torrent, "added_on", 0)
        return (0 <= progress < self._protect_progress / 100 and added > 0
                and time.time() - added >= self._cleanup_grace_hours * 3600)

    def _remaining(self, torrent):
        # qB 的 amount_left 对应选中文件，比整种体积推算更准确。
        value = self._number(torrent, "amount_left", -1)
        return value if value >= 0 else max(0, self._number(torrent, "size") * (1 - self._number(torrent, "progress")))

    def _matches_cleanup(self, torrent, kind, previous=None):
        if not self._basic_cleanable(torrent):
            return False
        speed = self._number(torrent, "dlspeed", -1)
        if speed < 0:
            return False
        if kind == "zero":
            seeds = self._number(torrent, "num_complete", -1)
            connected = self._number(torrent, "num_seeds", -1)
            availability = self._number(torrent, "availability", -1)
            return (seeds >= 0 and seeds <= self._zero_seed_threshold and connected == 0
                    and speed == 0 and availability >= 0 and availability < 1)
        if kind == "slow":
            if previous:
                elapsed = time.time() - previous["last"]
                if elapsed > 0:
                    delta = self._number(torrent, "downloaded") - previous.get("downloaded", 0)
                    speed = max(speed, max(0, delta) / elapsed)
            return (speed < self._slow_speed_kib * 1024 and self._remaining(torrent) > 0
                    and (speed == 0 or self._remaining(torrent) / speed > self._timeout_hours * 3600))
        # 元数据尚未获取/正常下载中的任务不因识别失败而清理。
        if self._state(torrent) != "stalleddl" or speed > 0:
            return False
        meta = MetaInfo(str(getattr(torrent, "name", "") or ""))
        return MediaChain().recognize_by_meta(meta) is None

    def _observe(self, torrent, name, kind):
        records = self._store(name)["observations"].setdefault(torrent.hash, {})
        previous = records.get(kind)
        now = time.time()
        duration = (self._timeout_hours if kind == "slow" else self._cleanup_grace_hours) * 3600
        # 防止把离线很久、重新添加的任务和旧观察记录拼在一起。
        if previous and (now < previous["last"] or now - previous["last"] > max(86400, duration * 2)
                         or previous.get("added_on") != self._number(torrent, "added_on")):
            previous = None
        if not self._matches_cleanup(torrent, kind, previous):
            records.pop(kind, None)
            return False
        if previous and kind != "slow" and self._number(torrent, "progress") > previous["progress"]:
            previous = None
        if previous is None:
            previous = {"first": now, "last": now, "count": 1,
                        "progress": self._number(torrent, "progress"),
                        "downloaded": self._number(torrent, "downloaded"),
                        "added_on": self._number(torrent, "added_on")}
            records[kind] = previous
            return False
        # 连点“立即运行”不能凑满确认次数。
        if now - previous["last"] >= 60:
            previous.update(last=now, count=previous["count"] + 1,
                            progress=self._number(torrent, "progress"),
                            downloaded=self._number(torrent, "downloaded"))
        return previous["count"] >= self._confirm_checks and now - previous["first"] >= duration

    def _clean_torrents(self, downloader, torrents, name, kind, allow_replace=True):
        switch, limit = {
            "zero": (self._enable_zero_seed_clean, self._max_zero_seed_process),
            "slow": (self._enable_timeout_clean, self._max_timeout_process),
            "unrecognized": (self._enable_unrecognized_clean, self._max_unrecognized_process),
        }[kind]
        if not switch:
            for records in self._store(name)["observations"].values():
                records.pop(kind, None)
            return 0, []
        candidates = []
        for torrent in torrents:
            if self._event.is_set():
                return 0, []
            try:
                reason = self._exclusion_reason(torrent)
                if reason:
                    logger.info(f"【QB种子优化】过滤保护: {torrent.name} ({reason})")
                if self._observe(torrent, name, kind):
                    candidates.append(torrent)
            except Exception as exc:
                self._store(name)["observations"].pop(torrent.hash, None)
                logger.warning(f"【QB种子优化】无法确认 {torrent.name}，保留任务: {exc}")
        self._save_runtime()
        removed, failures = 0, []
        for torrent in candidates[:limit]:
            if self._event.is_set():
                break
            try:
                # 再次获取，不以本轮开始时的快照进行删除。
                fresh = self._fresh(downloader, torrent.hash)
                if not self._matches_cleanup(fresh, kind):
                    self._store(name)["observations"].pop(torrent.hash, None)
                    continue
                if self._number(fresh, "progress") > self._number(torrent, "progress") and kind != "slow":
                    continue
                replacing = self._enable_auto_redownload and kind != "unrecognized"
                if replacing:
                    if not allow_replace or not self._replacement_space_ok(downloader):
                        failures.append(f"{torrent.name}: 空间或监控状态不允许换源，保留原任务")
                        continue
                    if not self._replace_torrent(downloader, fresh, name):
                        failures.append(f"{torrent.name}: 未确认合适替代任务，保留原任务")
                        continue
                    # 搜索和添加耗时较长，再次核对保护规则和状态。
                    fresh = self._fresh(downloader, torrent.hash)
                    if not self._matches_cleanup(fresh, kind):
                        continue
                deleted = self._delete_checked(downloader, fresh, name, kind, keep_files=replacing)
                if deleted:
                    removed += 1
                    self._store(name)["observations"].pop(torrent.hash, None)
                    self._store(name)["replacements"].pop(torrent.hash, None)
                else:
                    failures.append(f"{torrent.name}: 删除未确认，未计入成功")
            except Exception as exc:
                failures.append(f"{torrent.name}: 操作失败，{exc}")
        self._save_runtime()
        return removed, failures

    def _clean_zero_seed_torrents(self, downloader_obj, torrents, downloader_name):
        return self._clean_torrents(downloader_obj, torrents, downloader_name, "zero")

    def _clean_timeout_torrents(self, downloader_obj, torrents, downloader_name):
        return self._clean_torrents(downloader_obj, torrents, downloader_name, "slow")

    def _clean_unrecognized_torrents(self, downloader_obj, torrents, downloader_name):
        removed, _ = self._clean_torrents(downloader_obj, torrents, downloader_name, "unrecognized")
        return removed

    @staticmethod
    def _path(value):
        value = str(value or "").replace("\\", "/")
        if not value or not (value.startswith("/") or re.match(r"^[A-Za-z]:/", value)):
            return ""
        path = posixpath.normpath(value)
        return path.casefold() if re.match(r"^[A-Za-z]:/", path) else path

    def _file_paths(self, downloader, torrent):
        root = self._path(getattr(torrent, "save_path", ""))
        download_root = self._path(getattr(torrent, "download_path", ""))
        files = downloader.get_files(tid=torrent.hash)
        if not root or not files:
            return None
        paths = set()
        for item in files:
            file_name = getattr(item, "name", "")
            if not file_name:
                return None
            for base in {root, download_root} - {""}:
                paths.add(self._path(posixpath.join(base, str(file_name).replace("\\", "/"))))
        return paths

    def _can_delete_files(self, downloader, torrent):
        """辅种共享文件、无法取到文件清单时只删任务，保留数据。"""
        own = self._file_paths(downloader, torrent)
        if not own:
            return False
        for other in self._fetch(downloader):
            if other.hash == torrent.hash:
                continue
            paths = self._file_paths(downloader, other)
            if paths is None or own & paths:
                return False
        return True

    def _delete_checked(self, downloader, torrent, name, kind, keep_files=False):
        if torrent is None:
            return False
        latest = self._fresh(downloader, torrent.hash)
        if not self._matches_cleanup(latest, kind):
            return False
        if self._number(latest, "progress") > self._number(torrent, "progress") and kind != "slow":
            return False
        delete_files = not keep_files and self._can_delete_files(downloader, latest)
        logger.info(f"【QB种子优化】{name} 删除复核通过: {latest.name}, 原因={kind}, 删除文件={delete_files}")
        # 不在超时后盲目重试删除，避免重复操作共享路径。
        result = downloader.delete_torrents(delete_file=delete_files, ids=[latest.hash])
        return bool(result) and self._fresh(downloader, latest.hash) is None

    @staticmethod
    def _get_seeder_count(torrent) -> int:
        known = [QbOptimizer._number(torrent, field, -1) for field in ("num_complete", "num_seeds")]
        return int(max(0, *known))

    @staticmethod
    def _get_queue_position(torrent) -> int:
        value = QbOptimizer._number(torrent, "priority", -1)
        return int(value) if value > 0 else 10 ** 9

    def _score(self, torrent, name):
        now = time.time()
        samples = self._store(name).setdefault("speeds", {})
        previous = samples.get(torrent.hash)
        speed = max(0, self._number(torrent, "dlspeed"))
        if previous and previous.get("added_on") == self._number(torrent, "added_on"):
            elapsed = now - previous["time"]
            if 60 <= elapsed <= 86400:
                average = max(0, self._number(torrent, "downloaded") - previous["downloaded"]) / elapsed
                speed = (speed + average) / 2
        samples[torrent.hash] = {"time": now, "downloaded": self._number(torrent, "downloaded"),
                                 "added_on": self._number(torrent, "added_on")}
        # 对做种数取对数，防止几百个做种压倒实际下载速度；接近完成的任务优先。
        return (speed / 1024 ** 2 * self._speed_weight
                + math.log2(1 + self._get_seeder_count(torrent)) * self._seeder_weight
                + self._number(torrent, "progress") * 2)

    def _boost_priority_torrents(self, downloader_obj, torrents, downloader_name):
        if not self._enable_score_priority or not self._max_priority_process:
            return 0
        preferences = downloader_obj.qbc.app_preferences()
        if not preferences.get("queueing_enabled", False):
            logger.info("【QB种子优化】qB未启用队列，跳过优先级操作")
            return 0
        store = self._store(downloader_name)
        scored = []
        live = {t.hash for t in torrents}
        store["speeds"] = {key: value for key, value in store.get("speeds", {}).items() if key in live}
        for torrent in torrents:
            if (self._state(torrent) not in self._ACTIVE | {"queueddl"}
                    or self._number(torrent, "progress", 1) >= 1
                    or getattr(torrent, "force_start", False)):
                continue
            score = self._score(torrent, downloader_name)
            if score >= self._min_score_threshold:
                scored.append((torrent, score))
        if time.time() - store.get("priority_at", 0) < self._priority_cooldown_minutes * 60:
            return 0
        # 先排序再限制数量，不主动启动/探测排队任务。
        scored.sort(key=lambda item: (-item[1], self._get_queue_position(item[0])))
        selected = scored[:self._max_priority_process]
        if not selected:
            return 0
        desired = [t.hash for t, _ in selected]
        current = sorted(scored, key=lambda item: self._get_queue_position(item[0]))[:len(selected)]
        if desired == [t.hash for t, _ in current]:
            return 0
        # 得分相近时保持现有顺序，避免网络短暂波动引起换位。
        score_map = {t.hash: score for t, score in scored}
        meaningful = any(score_map[new] > score_map[old.hash] * 1.2
                         for new, (old, _) in zip(desired, current) if new != old.hash)
        if not meaningful:
            return 0
        changed = 0
        for torrent, _ in reversed(selected):
            if self._event.is_set():
                break
            fresh = self._fresh(downloader_obj, torrent.hash)
            if fresh and self._state(fresh) in self._ACTIVE | {"queueddl"} and self._number(fresh, "progress", 1) < 1:
                downloader_obj.qbc.torrents_top_priority(torrent_hashes=[fresh.hash])
                changed += 1
        if changed:
            store["priority_at"] = time.time()
            self._save_runtime()
        return changed

    def _server_state(self, downloader):
        data = downloader.qbc.sync_maindata(rid=0)
        state = data.get("server_state") if data else None
        if not isinstance(state, dict):
            raise RuntimeError("无法读取下载器状态")
        return state

    @staticmethod
    def _metric(state, key):
        try:
            value = float(state[key])
            return value if value >= 0 and math.isfinite(value) else None
        except (KeyError, TypeError, ValueError):
            return None

    def _replacement_space_ok(self, downloader):
        if not self._enable_disk_monitor and not self._enable_download_threshold:
            return True
        free = self._metric(self._server_state(downloader), "free_space_on_disk")
        threshold = max(self._disk_space_threshold if self._enable_disk_monitor else 0,
                        self._min_disk_space_threshold if self._enable_download_threshold else 0)
        return free is not None and free / 1024 ** 3 >= threshold + self._resume_margin_gb

    def _monitor_disk_and_io(self, downloader, name):
        store = self._store(name)
        owned = store.get("limit")
        if not self._enable_disk_monitor and not owned:
            return False
        limits = downloader.get_speed_limit()
        if limits is None:
            return True
        current = float(limits[0])
        if owned and abs(current - owned["applied"]) > 0.001:
            # 用户或其他插件改动过限速，放弃控制权。
            store.pop("limit", None)
            self._save_runtime()
            return True if self._enable_disk_monitor else False
        should_limit = False
        if self._enable_disk_monitor:
            samples = []
            checks = max(1, self._monitor_duration // self._monitor_interval)
            for index in range(checks):
                if self._event.is_set():
                    return True
                state = self._server_state(downloader)
                values = [self._metric(state, key) for key in
                          ("free_space_on_disk", "write_cache_overload", "queued_io_jobs")]
                if any(value is None for value in values):
                    return True  # 未知指标不能当作0，也不能据此解除限速。
                samples.append(values)
                # 空间紧张立即限速，不等待整段采样结束。
                if values[0] / 1024 ** 3 < self._disk_space_threshold:
                    should_limit = True
                    break
                if index < checks - 1 and self._event.wait(self._monitor_interval):
                    return True
            if not should_limit:
                cache_avg = sum(row[1] for row in samples) / len(samples)
                queue_avg = sum(row[2] for row in samples) / len(samples)
                should_limit = cache_avg > self._io_cache_threshold or queue_avg > self._io_queue_threshold
                # 恢复设置保留余量，避免刚好卡在阈值附近反复切换。
                if owned and (min(row[0] for row in samples) / 1024 ** 3 < self._disk_space_threshold + self._resume_margin_gb
                              or cache_avg > self._io_cache_threshold * 0.8
                              or queue_avg > self._io_queue_threshold * 0.8):
                    should_limit = True
        # 采样期间的人工修改也不能覆盖。
        latest_limits = downloader.get_speed_limit()
        if self._event.is_set() or latest_limits is None or abs(float(latest_limits[0]) - current) > 0.001:
            return True
        if should_limit:
            applied = self._speed_limit_mbps * 1024
            if current > 0 and current <= applied:
                return True
            store["limit"] = {"original": owned["original"] if owned else current, "applied": applied}
            self._save_runtime()
            downloader.qbc.transfer_set_download_limit(limit=int(applied * 1024))
        elif owned:
            downloader.qbc.transfer_set_download_limit(limit=int(owned["original"] * 1024))
            store.pop("limit", None)
            self._save_runtime()
        return should_limit

    def _manage_download_threshold(self, downloader, torrents, name, allow_resume=True):
        if not self._enable_download_threshold:
            return False
        free = self._metric(self._server_state(downloader), "free_space_on_disk")
        if free is None:
            return False
        active = [t for t in torrents if self._state(t) in self._ACTIVE | {"forceddl", "metadl", "forcedmetadl"}
                  and self._number(t, "progress", 1) < 1]
        # 元数据/剩余大小未知时不能乐观地恢复更多任务。
        if any(self._state(t) in {"metadl", "forcedmetadl"} or self._number(t, "amount_left", -1) < 0 for t in active):
            return False
        available = free - sum(self._remaining(t) for t in active)
        minimum = self._min_disk_space_threshold * 1024 ** 3
        store = self._store(name)
        paused = store["paused"]
        changed = False
        for torrent in sorted(active, key=lambda t: (self._number(t, "dlspeed"), self._number(t, "progress"))):
            if available >= minimum or self._event.is_set():
                break
            if getattr(torrent, "force_start", False) or self._state(torrent) == "forceddl":
                continue
            fresh = self._fresh(downloader, torrent.hash)
            if not fresh or self._state(fresh) not in self._ACTIVE or self._number(fresh, "progress", 1) >= 1:
                continue
            paused[fresh.hash] = {"added_on": self._number(fresh, "added_on"), "confirmed": False}
            self._save_runtime()
            try:
                result = downloader.stop_torrents(ids=[fresh.hash])
                check = self._fresh(downloader, fresh.hash)
                if result and check and self._state(check) in self._PAUSED:
                    paused[fresh.hash]["confirmed"] = True
                    available += self._remaining(torrent)
                    changed = True
                else:
                    paused.pop(fresh.hash, None)
            finally:
                self._save_runtime()
        if not allow_resume:
            return changed
        # 用新快照重算，暂停失败或期间新增任务都不能释放虚假的容量。
        torrents = self._fetch(downloader)
        active = [t for t in torrents if self._state(t) in self._ACTIVE | {"forceddl", "metadl", "forcedmetadl"} and self._number(t, "progress", 1) < 1]
        if any(self._number(t, "amount_left", -1) < 0 or self._state(t) in {"metadl", "forcedmetadl"} for t in active):
            return changed
        latest_free = self._metric(self._server_state(downloader), "free_space_on_disk")
        if latest_free is None:
            return changed
        available = latest_free - sum(self._remaining(t) for t in active)
        slots = max(0, self._max_download_slots - len(active))
        candidates = [t for t in torrents if t.hash in paused]
        for torrent in sorted(candidates, key=self._remaining):
            record = paused[torrent.hash]
            if (self._state(torrent) not in self._PAUSED or self._number(torrent, "progress", 1) >= 1
                    or record.get("added_on") != self._number(torrent, "added_on")):
                paused.pop(torrent.hash, None)
                continue
            if not record.get("confirmed") or self._event.is_set() or slots <= 0:
                continue
            remaining = self._number(torrent, "amount_left", -1)
            if remaining <= 0 or available - remaining < minimum + self._resume_margin_gb * 1024 ** 3:
                continue
            # 自动恢复前再次确认仍是同一个暂停任务。
            fresh = self._fresh(downloader, torrent.hash)
            if fresh is None or self._state(fresh) not in self._PAUSED or self._number(fresh, "added_on") != record["added_on"]:
                continue
            started = downloader.start_torrents(ids=[fresh.hash])
            check = self._fresh(downloader, fresh.hash)
            if started and check and self._state(check) in self._ACTIVE:
                paused.pop(fresh.hash, None)
                available -= remaining
                slots -= 1
                changed = True
                self._save_runtime()
        self._save_runtime()
        return changed

    @staticmethod
    def _media_key(media):
        if media is None:
            return None
        for field in ("tmdb_id", "douban_id", "bangumi_id"):
            value = getattr(media, field, None)
            if value:
                return field, str(value), str(getattr(media, "type", ""))
        return None

    @staticmethod
    def _resolution(meta):
        value = str(getattr(meta, "resource_pix", "") or "").lower()
        if value in ("4k", "uhd"):
            return 2160
        match = re.search(r"(4320|2160|1440|1080|720|576|480)", value)
        return int(match.group(1)) if match else 0

    def _same_media(self, original_meta, original_media, candidate):
        meta, media = candidate.meta_info, candidate.media_info
        if not meta or not self._media_key(original_media) or self._media_key(media) != self._media_key(original_media):
            return False
        original_year = getattr(original_meta, "year", None)
        if original_year and str(getattr(meta, "year", "")) != str(original_year):
            return False
        for attr in ("season_list", "episode_list"):
            if set(getattr(original_meta, attr, None) or []) != set(getattr(meta, attr, None) or []):
                return False
        if self._resolution(meta) < self._resolution(original_meta):
            return False
        original_title = str(getattr(original_meta, "org_string", "") or "").casefold()
        candidate_title = str(getattr(candidate.torrent_info, "title", "") or "").casefold()
        for marker in ("remux", "bluray", "hdr", "dolby vision"):
            if marker in original_title and marker not in candidate_title:
                return False
        return True

    def _search_torrents(self, keyword):
        search = SearchChain()
        results, seen = [], set()
        for page in range(3):
            if self._event.is_set():
                break
            batch = search.search_by_title(title=keyword, page=page, sites=self._search_sites or None)
            if not batch:
                break
            for result in batch:
                info = result.torrent_info
                key = (getattr(info, "site", None), getattr(info, "enclosure", None), getattr(info, "title", None))
                if key not in seen:
                    seen.add(key)
                    results.append(result)
        return results

    def _select_best_torrent(self, search_results, original_torrent_info):
        original_meta = MetaInfo(original_torrent_info["name"])
        original_media = MediaChain().recognize_by_meta(original_meta)
        if not self._media_key(original_media):
            return None
        existing = {str(value).lower() for value in original_torrent_info.get("existing_hashes", [])}
        existing.add(str(original_torrent_info["hash"]).lower())
        qualified = []
        for result in search_results:
            info = result.torrent_info
            if not info or not self._same_media(original_meta, original_media, result):
                continue
            if str(getattr(info, "info_hash", "") or "").lower() in existing:
                continue
            # 有些站点不提供hash，也排除完全相同的标题和大小。
            if (getattr(info, "title", "") == original_torrent_info["name"]
                    and self._number(info, "size") == original_torrent_info["size"]):
                continue
            seeds = self._number(info, "seeders", -1)
            if seeds <= 0:
                continue
            qualified.append((seeds, result))
        return max(qualified, key=lambda pair: pair[0])[1] if qualified else None

    def _download_torrent(self, search_result, original_torrent_info):
        media = copy.deepcopy(search_result.media_info)
        media.category = original_torrent_info["category"]
        context = Context(meta_info=search_result.meta_info, media_info=media, torrent_info=search_result.torrent_info)
        return DownloadChain().download_single(
            context=context, downloader=original_torrent_info["downloader"],
            save_path=original_torrent_info["save_path"], label=original_torrent_info["tags"] or None)

    @staticmethod
    def _reported_hash(result):
        """从不同版本 DownloadChain 的返回值中提取可能的 qB hash。"""
        values = []
        if isinstance(result, str):
            values.append(result)
        elif isinstance(result, dict):
            values.extend(result.get(key) for key in ("hash", "info_hash", "download_id", "id"))
        else:
            values.extend(getattr(result, key, None) for key in ("hash", "info_hash", "download_id", "id"))
        for value in values:
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{32,64}", value.strip()):
                return value.strip()
        return ""

    def _find_added_hash(self, downloader, existing_hashes, original_hash, result):
        existing = {str(value).casefold() for value in existing_hashes}
        original = str(original_hash).casefold()
        current = self._fetch(downloader)
        added = [torrent for torrent in current
                 if str(getattr(torrent, "hash", "")).casefold() not in existing | {original}]
        reported = self._reported_hash(result)
        if reported:
            reported_torrent = next(
                (torrent for torrent in current
                 if str(getattr(torrent, "hash", "")).casefold() == reported.casefold()),
                None)
            if reported_torrent and str(reported_torrent.hash).casefold() != original:
                return reported_torrent.hash
        if len(added) == 1:
            return added[0].hash
        return reported if reported else ""

    def _replace_torrent(self, downloader, torrent, name):
        replacements = self._store(name)["replacements"]
        pending = replacements.get(torrent.hash)
        if pending:
            # 曾成功提交添加请求时不重复搜索/添加；等真实任务出现后再删旧任务。
            if not pending.get("hash"):
                logger.warning(f"【QB种子优化】{torrent.name} 上次添加结果未知，请人工核查后重置插件数据")
                return False
            new = self._fresh(downloader, pending["hash"])
            return new is not None and new.hash != torrent.hash
        save_path = getattr(torrent, "save_path", "")
        if not save_path:
            return False
        meta = MetaInfo(torrent.name)
        keyword = getattr(meta, "name", None) or getattr(meta, "title", None)
        if not keyword:
            return False
        info = {"name": torrent.name, "size": self._number(torrent, "size"), "hash": torrent.hash,
                "downloader": name, "save_path": save_path,
                "category": getattr(torrent, "category", "") or "",
                "tags": getattr(torrent, "tags", "") or "",
                "existing_hashes": [t.hash for t in self._fetch(downloader)]}
        best = self._select_best_torrent(self._search_torrents(keyword), info)
        if best is None or self._event.is_set():
            return False
        # 添加前最后检查过滤，搜索期间新增保护标签也应生效。
        fresh = self._fresh(downloader, torrent.hash)
        if not self._basic_cleanable(fresh):
            return False
        info.update(category=getattr(fresh, "category", "") or "", tags=getattr(fresh, "tags", "") or "")
        # 标记添加中。异常或进程重启后不自动重试，以免重复下载。
        replacements[torrent.hash] = {"hash": "", "at": time.time()}
        self._save_runtime()
        result = self._download_torrent(best, info)
        new_hash = self._find_added_hash(downloader, info["existing_hashes"], torrent.hash, result)
        if not new_hash and not result:
            replacements.pop(torrent.hash, None)
            self._save_runtime()
            return False
        if not new_hash:
            # 添加请求返回成功但 qB 尚未出现任务，记录未知状态，避免下一轮重复添加。
            replacements[torrent.hash] = {"hash": "", "at": time.time()}
            self._save_runtime()
            return False
        if str(new_hash).casefold() in {str(value).casefold() for value in info["existing_hashes"]}:
            replacements.pop(torrent.hash, None)
            self._save_runtime()
            return False
        replacements[torrent.hash] = {"hash": new_hash, "at": time.time()}
        self._save_runtime()
        new = self._fresh(downloader, new_hash)
        return new is not None and new.hash != torrent.hash

    def show_status(self):
        for name in self._downloaders:
            try:
                downloader = self.__get_downloader(name)
                if not downloader:
                    continue
                torrents = self._fetch(downloader)
                protected = sum(self._is_excluded_torrent(t) for t in torrents)
                downloading = sum(self._state(t) in self._ACTIVE for t in torrents)
                text = f"种子总数: {len(torrents)}\n下载中: {downloading}\n过滤保护: {protected}"
                logger.info(f"【QB种子优化】{name}: {text}")
                if self._notify:
                    self.post_message(mtype=NotificationType.Download, title=f"【QB状态】{name}", text=text)
            except Exception as exc:
                logger.error(f"【QB种子优化】{name} 状态读取失败: {exc}")

    def get_page(self) -> List[dict]:
        return []

    def stop_service(self):
        if hasattr(self, "_event"):
            self._event.set()
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None


    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12}, 'content': [
                                {'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal',
                                 'text': '清理需多次观察并在删除前复核；换源确认新任务加入后才移除旧任务，保留旧数据文件。保护过滤仅作用于清理。'}}]},
                            *[{'component': 'VCol', 'props': {'cols': 12, 'md': 4}, 'content': [
                                {'component': 'VTextField', 'props': {'model': model, 'label': label,
                                 'type': 'number', 'min': minimum, 'step': step}}]}
                              for model, label, minimum, step in (
                                  ('confirm_checks', '清理最少确认次数', 2, 1),
                                  ('cleanup_grace_hours', '新任务宽限及异常观察时长（小时）', 0.25, 0.25),
                                  ('protect_progress', '达到此进度不清理（%）', 1, 1),
                                  ('slow_speed_kib', '低速阈值（KiB/s）', 1, 1),
                                  ('priority_cooldown_minutes', '优先级调整间隔（分钟）', 0, 1),
                                  ('resume_margin_gb', '恢复下载额外预留空间（GB）', 0, 1),
                                  ('max_download_slots', '自动恢复时最多同时下载任务数', 1, 1),
                              )],
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12},
                             'content': [{'component': 'VDivider', 'props': {'text': '🔍 过滤设置：匹配的种子跳过全部清理'}}]},
                            *[
                                {'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                                 'content': [{'component': 'VTextarea', 'props': {
                                     'model': model, 'label': label, 'rows': 3,
                                     'placeholder': placeholder}}]}
                                for model, label, placeholder in (
                                    ('exclude_keywords', '🔤 排除关键字', '种子名称包含关键字时跳过，每行一个'),
                                    ('exclude_categories', '📂 排除分类', '分类名称，每行一个'),
                                    ('exclude_labels', '🏷️ 排除标签', '标签名称，每行一个'),
                                )
                            ],
                        ],
                    },
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
                                            'label': '低速观察时长及预计剩余时长（小时）',
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
                    
                    # 识别不到媒体信息任务配置组
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
                                            'text': '清理识别不到媒体信息任务'
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
                                            'model': 'enable_unrecognized_clean',
                                            'label': '启用识别不到媒体信息清理',
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
                                            'model': 'max_unrecognized_process',
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
                                            'model': 'monitor_duration',
                                            'label': '持续监测时长（秒）',
                                            'placeholder': '30',
                                            'type': 'number',
                                            'min': 10,
                                            'max': 300
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
                                            'model': 'monitor_interval',
                                            'label': '监测间隔（秒）',
                                            'placeholder': '3',
                                            'type': 'number',
                                            'min': 1,
                                            'max': 30
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
                    
                    # 下载阈值控制配置组
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
                                            'text': '下载阈值控制'
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
                                            'model': 'enable_download_threshold',
                                            'label': '启用下载阈值控制',
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
                                            'model': 'min_disk_space_threshold',
                                            'label': '最小磁盘空间阈值（GB）',
                                            'placeholder': '50',
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '按选中文件的剩余下载量预留空间，优先暂停低速、低进度任务。只恢复本插件已确认暂停的任务，并预留额外空间；手动暂停、错误状态和强制下载任务不自动恢复。升级前的暂停任务不接管。'
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
                                            'text': '评分使用平滑下载速度、log2(1+做种数)及进度；差异超过20%且过了调整间隔才重排队列。不主动启动任务探测速率；qB需启用队列。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], copy.deepcopy(self._DEFAULTS)
