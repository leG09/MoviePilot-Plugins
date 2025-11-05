import datetime
import json
import re
import shutil
import subprocess
import threading
import traceback
import time
import requests
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app import schemas
from app.chain.media import MediaChain
from app.chain.storage import StorageChain
from app.chain.tmdb import TmdbChain
from app.chain.transfer import TransferChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfoPath
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.plugins import _PluginBase
from app.schemas import NotificationType, TransferInfo, TransferDirectoryConf
from app.schemas.types import EventType, MediaType, SystemConfigKey
from app.utils.string import StringUtils
from app.utils.system import SystemUtils

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = monpath
        self.sync = sync

    def on_created(self, event):
        self.sync.event_handler(event=event, text="创建",
                                mon_path=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.sync.event_handler(event=event, text="移动",
                                mon_path=self._watch_path, event_path=event.dest_path)


class GDCloudLinkMonitor(_PluginBase):
    # 插件名称
    plugin_name = "多目录实时监控"
    # 插件描述
    plugin_desc = "监控多目录文件变化，自动转移媒体文件，支持轮询分发、网盘限制检测和转移前目录刷新。"
    # 插件图标
    plugin_icon = "Linkease_A.png"
    # 插件版本
    plugin_version = "3.0.12" 
    # 插件作者
    plugin_author = "leGO9"
    # 作者主页
    author_url = "https://github.com/leG09"
    # 插件配置项ID前缀
    plugin_config_prefix = "gd_cloudlinkmonitor_"
    # 加载顺序
    plugin_order = 4
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    transferhis = None
    downloadhis = None
    transferchian = None
    tmdbchain = None
    storagechain = None
    _observer = []
    _enabled = False
    _notify = False
    _onlyonce = False
    _history = False
    _scrape = False
    _category = False
    _refresh = False
    _softlink = False
    _strm = False
    _cron = None
    filetransfer = None
    mediaChain = None
    _size = 0
    # 模式 compatibility/fast
    _mode = "compatibility"
    # 转移方式
    _transfer_type = "softlink"
    _monitor_dirs = ""
    _exclude_keywords = ""
    _interval: int = 10
    # 存储源目录与目的目录关系
    _dirconf: Dict[str, Optional[List[Path]]] = {}
    # 存储每个源目录的轮询分发索引
    _round_robin_index: Dict[str, int] = {}
    # 状态文件路径
    _state_file: Path = None
    # 存储源目录转移方式
    _transferconf: Dict[str, Optional[str]] = {}
    _overwrite_mode: Dict[str, Optional[str]] = {}
    _medias = {}
    # 退出事件
    _event = threading.Event()
    
    # 日志监控相关属性
    _log_monitor_enabled = False
    _log_path = ""
    _log_check_interval = 60  # 检查间隔(秒)
    _error_threshold = 3  # 错误阈值
    _recovery_check_interval = 1800  # 恢复检查间隔(30分钟)
    _mount_path_mapping = ""  # 挂载路径映射配置
    _disabled_destinations: Dict[str, Dict[str, Any]] = {}  # 被禁用的目标目录
    _log_monitor_thread = None
    _recovery_thread = None
    
    # 目录刷新相关属性
    _refresh_before_transfer = False  # 转移前是否刷新目录
    _refresh_api_url = ""  # 刷新API完整地址
    _refresh_api_key = ""  # 刷新API密钥
    
    # AI 规范命名相关属性
    _ai_provider = "gemini"  # 可选：gemini/openai/grok
    _ai_rename_enabled = False
    _ai_api_url = ""
    _ai_model = "gemini-2.0-flash"
    _ai_api_keys: List[str] = []  # 多Key支持
    _ai_key_index: int = 0
    _ai_keys_file: Path = None
    _ai_timeout = 30
    _ai_cache_days = 5  # AI识别记录缓存天数
    _ai_prompt_template = (
        "基于文件夹内所有媒体文件名，生成规范化目录名。\n"
        "输入：该文件夹内所有文件的完整路径列表。\n"
        "输出：仅返回严格JSON：{\"directory_name\":\"...\"}，不要额外文本/解释/代码块。\n"
        "规则：1) 剧集目录名须含完整季集范围(如 S01E01-E10)；2) 电影目录名不可含季集信息；\n"
        "3) 标题保留原文件名中的中文；若无中文则用英文；技术信息(分辨率/编码/音频/发布组)用英文；\n"
        "4) 尽量包含：标题、年份、分辨率、视频编码、音频、发布组；5) 需能代表整季/整片内容。\n"
        "示例(剧集)：{\"directory_name\":\"孤注一掷：托特纳姆热刺.S01E01-E10.1080p.WEB-DL.H265.DDP5.1-GROUP\"}\n"
        "示例(电影)：{\"directory_name\":\"无名之辈：否极泰来.2025.1080p.WEB-DL.H265.DDP5.1-GROUP\"}"
    )
    
    # 运行期缓存：已处理过AI规范化的目录，避免重复请求
    _ai_processed_dirs = set()
    # 持久化记录：已规范化的目录/文件，避免后续再调用AI
    _ai_state_file: Path = None
    _ai_normalized_dirs = set()
    _ai_normalized_files = set()

    def _load_ai_normalized_state(self):
        try:
            if not self._ai_state_file or not self._ai_state_file.exists():
                return
            with open(self._ai_state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
                # 检查缓存时间
                cache_time = data.get('cache_time')
                if cache_time:
                    cache_datetime = datetime.datetime.fromisoformat(cache_time)
                    days_diff = (datetime.datetime.now() - cache_datetime).total_seconds() / (24 * 3600)
                    if days_diff > self._ai_cache_days:
                        logger.info(f"AI缓存记录已过期({self._ai_cache_days}天，实际{days_diff:.1f}天)，清空缓存")
                        self._ai_normalized_dirs = set()
                        self._ai_normalized_files = set()
                        self._save_ai_normalized_state()  # 清空并保存
                        return
                
                # 加载记录
                dirs = data.get('dirs') or []
                files = data.get('files') or []
                self._ai_normalized_dirs = set(dirs)
                self._ai_normalized_files = set(files)
                logger.info(f"已加载AI规范化记录：目录{len(self._ai_normalized_dirs)}个，文件{len(self._ai_normalized_files)}个")
        except Exception as e:
            logger.debug(f"加载AI规范化记录失败：{e}")

    def _save_ai_normalized_state(self):
        try:
            if not self._ai_state_file:
                return
            payload = {
                'cache_time': datetime.datetime.now().isoformat(),
                'dirs': sorted(list(self._ai_normalized_dirs)),
                'files': sorted(list(self._ai_normalized_files))
            }
            with open(self._ai_state_file, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"保存AI规范化记录失败：{e}")

    def _load_ai_keys_state(self):
        try:
            if not self._ai_keys_file or not self._ai_keys_file.exists():
                return [], 0
            with open(self._ai_keys_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                keys = data.get('keys') if isinstance(data, dict) else None
                current_index = data.get('current_index', 0) if isinstance(data, dict) else 0
                if isinstance(keys, list):
                    return [k for k in keys if isinstance(k, str) and k], current_index
                if isinstance(data, list):
                    return [k for k in data if isinstance(k, str) and k], 0
        except Exception as e:
            logger.debug(f"加载AI密钥记录失败：{e}")
        return [], 0

    def _save_ai_keys_state(self, keys: List[str]):
        try:
            if not self._ai_keys_file:
                return
            with open(self._ai_keys_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'keys': keys,
                    'current_index': self._ai_key_index
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"保存AI密钥记录失败：{e}")

    def _save_state_to_file(self):
        """将轮询状态保存到文件"""
        if not self._state_file:
            return
        try:
            with open(self._state_file, 'w', encoding='utf-8') as f:
                json.dump(self._round_robin_index, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"无法保存轮询状态到文件 {self._state_file}: {e}")

    def _load_state_from_file(self):
        """从文件加载轮询状态"""
        if not self._state_file or not self._state_file.exists():
            return {}
        try:
            with open(self._state_file, 'r', encoding='utf-8') as f:
                state = json.load(f)
                # 确保加载的状态是正确的字典格式
                if isinstance(state, dict):
                    logger.info(f"成功从 {self._state_file} 加载轮询状态。")
                    return state
                return {}
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"无法从 {self._state_file} 加载轮询状态: {e}")
            return {}

    def _get_round_robin_destination(self, mon_path: str, mediainfo: MediaInfo) -> Optional[Path]:
        """
        根据配置的目录顺序选择一个目标目录。
        1. 按照配置的目录顺序进行轮询选择。
        2. 跳过被禁用的目标目录。
        3. 忽略历史记录，严格按照配置顺序进行转移。
        """
        all_destinations = self._dirconf.get(mon_path)
        if not all_destinations:
            logger.error(f"监控源 {mon_path} 未配置目标目录")
            return None

        # 获取可用的目标目录（排除被禁用的）
        available_destinations = self._get_available_destinations(mon_path)
        if not available_destinations:
            logger.error(f"监控源 {mon_path} 没有可用的目标目录（所有目录都已被禁用）")
            return None

        # 如果只有一个可用目标目录，则直接返回
        if len(available_destinations) == 1:
            return available_destinations[0]

        # 按照配置的目录顺序进行轮询选择，忽略历史记录
        logger.info(f"为 '{mediainfo.title_year}' 按照配置顺序选择目标目录。")
        
        # 每次轮询都从第一个目录开始，除非目录被禁用
        # 获取第一个可用的目录
        chosen_dest = available_destinations[0]
        
        # 更新全局索引为选择的目录在所有目录中的位置
        chosen_global_index = all_destinations.index(chosen_dest)
        self._round_robin_index[mon_path] = chosen_global_index
        
        # 将新状态持久化到文件
        self._save_state_to_file()

        logger.info(f"针对 '{mon_path}' 的轮询机制选择了可用目录: {chosen_dest}")

        return chosen_dest

    def _parse_mount_path_mapping(self) -> Dict[str, str]:
        """
        解析挂载路径映射配置
        格式：/gd2:/mnt/CloudDrive/gd2,/gd3:/mnt/CloudDrive/gd3
        返回：{'/gd2': '/mnt/CloudDrive/gd2', '/gd3': '/mnt/CloudDrive/gd3'}
        """
        mapping = {}
        if not self._mount_path_mapping:
            return mapping
            
        try:
            # 分割每个映射项
            for mapping_item in self._mount_path_mapping.split(','):
                mapping_item = mapping_item.strip()
                if not mapping_item or ':' not in mapping_item:
                    continue
                    
                parts = mapping_item.split(':', 1)
                if len(parts) == 2:
                    log_path = parts[0].strip()
                    real_path = parts[1].strip()
                    mapping[log_path] = real_path
                    
        except Exception as e:
            logger.error(f"解析挂载路径映射配置时出错: {e}")
            
        return mapping

    def _convert_to_mount_path(self, file_path: str) -> str:
        """
        将实际文件路径转换为挂载路径格式
        例如：/mnt/CloudDrive/gd3/media/3/... -> /gd3/media/3/...
        """
        if not self._mount_path_mapping:
            return file_path
            
        mount_mapping = self._parse_mount_path_mapping()
        
        # 遍历映射，找到匹配的实际路径并转换为挂载路径
        for mount_path, real_path in mount_mapping.items():
            if file_path.startswith(real_path):
                # 替换实际路径为挂载路径
                converted_path = file_path.replace(real_path, mount_path, 1)
                logger.debug(f"路径转换: {file_path} -> {converted_path}")
                return converted_path
                
        # 如果没有找到匹配的映射，返回原路径
        logger.debug(f"未找到匹配的路径映射，使用原路径: {file_path}")
        return file_path

    def _find_matching_destinations(self, mount_path: str) -> List[str]:
        """
        根据日志中的挂载路径找到匹配的目标目录
        """
        matching_destinations = []
        
        # 解析路径映射配置
        mount_mapping = self._parse_mount_path_mapping()
        
        # 如果有配置映射，使用映射查找
        if mount_mapping and mount_path in mount_mapping:
            real_mount_path = mount_mapping[mount_path]
            logger.debug(f"使用映射配置：{mount_path} -> {real_mount_path}")
            
            # 查找包含这个真实挂载路径的目标目录
            for mon_path, destinations in self._dirconf.items():
                if not destinations:
                    continue
                    
                for dest in destinations:
                    dest_str = str(dest)
                    if dest_str.startswith(real_mount_path):
                        matching_destinations.append(dest_str)
                        logger.debug(f"找到匹配的目标目录：{dest_str}")
        else:
            # 如果没有配置映射，尝试直接匹配或模糊匹配
            logger.debug(f"未找到映射配置，尝试直接匹配挂载路径：{mount_path}")
            
            for mon_path, destinations in self._dirconf.items():
                if not destinations:
                    continue
                    
                for dest in destinations:
                    dest_str = str(dest)
                    
                    # 直接匹配
                    if dest_str.startswith(mount_path):
                        matching_destinations.append(dest_str)
                        logger.debug(f"直接匹配到目标目录：{dest_str}")
                    # 模糊匹配：检查目标路径中是否包含挂载路径的关键部分
                    elif mount_path.startswith('/') and mount_path[1:] in dest_str:
                        matching_destinations.append(dest_str)
                        logger.debug(f"模糊匹配到目标目录：{dest_str}")
                        
        return matching_destinations

    def _get_latest_log_file_path(self) -> Path:
        """
        获取日志目录中最新（按修改时间）的标准日期格式日志文件路径
        只读取格式如 2025-10-16.log 的文件，排除 backup.2025-10-15.log 等备份文件
        """
        if not self._log_path:
            return None
            
        log_dir = Path(self._log_path)
        if not log_dir.exists() or not log_dir.is_dir():
            return None
            
        # 查找所有.log文件
        all_log_files = list(log_dir.glob("*.log"))
        if not all_log_files:
            return None
            
        # 过滤出标准日期格式的日志文件（YYYY-MM-DD.log）
        date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}\.log$')
        log_files = [f for f in all_log_files if date_pattern.match(f.name)]
        
        if not log_files:
            logger.debug(f"未找到标准日期格式的日志文件，跳过备份文件: {[f.name for f in all_log_files]}")
            return None
            
        # 按文件修改时间排序，返回最新的文件
        latest_file = max(log_files, key=lambda f: f.stat().st_mtime)
        file_mtime = datetime.datetime.fromtimestamp(latest_file.stat().st_mtime)
        logger.debug(f"选择最新的标准日志文件: {latest_file} (修改时间: {file_mtime})")
        return latest_file

    def _check_cloud_errors(self) -> Dict[str, int]:
        """
        检查日志文件中的云盘错误
        返回每个挂载目录的错误计数
        """
        error_counts = {}
        
        logger.debug(f"开始检查云盘错误，日志路径：{self._log_path}")
        
        # 获取最新的日志文件
        log_file = self._get_latest_log_file_path()
        
        logger.debug(f"检查最新的日志文件：{log_file}")
            
        if not log_file:
            logger.debug("未找到日志文件，跳过")
            return error_counts
                
        if not log_file.exists():
            logger.debug(f"日志文件不存在：{log_file}")
            return error_counts
                
        try:
            file_size = log_file.stat().st_size
            logger.debug(f"日志文件大小：{file_size} bytes")
            
            with open(log_file, 'r', encoding='utf-8') as f:
                # 只读取最近的内容，避免处理过大的日志文件
                start_pos = max(0, file_size - 1024 * 1024)  # 读取最后1MB
                f.seek(start_pos)
                content = f.read()
                
            logger.debug(f"读取了 {len(content)} 字符的日志内容")
                
            # 查找网盘限制错误
            error_patterns = [
                r'upload error for (/[^/]+)/.*User rate limit exceeded',
                r'upload error for (/[^/]+)/.*quota.*exceeded',
                r'upload error for (/[^/]+)/.*403.*exceeded'
            ]
            
            for i, pattern in enumerate(error_patterns):
                matches = re.findall(pattern, content, re.IGNORECASE)
                logger.debug(f"模式 {i+1} 匹配到 {len(matches)} 个错误")
                
                for mount_path in matches:
                    if mount_path not in error_counts:
                        error_counts[mount_path] = 0
                    error_counts[mount_path] += 1
                    logger.debug(f"挂载路径 {mount_path} 错误计数增加到 {error_counts[mount_path]}")
                    
        except Exception as e:
            logger.warning(f"检查日志文件 {log_file} 时出错: {e}")
            import traceback
            logger.debug(f"错误详情: {traceback.format_exc()}")
    
        logger.info(f"日志检查完成，错误统计：{error_counts}")        
        return error_counts

    def _disable_destination(self, destination_path: str, reason: str = "网盘限制"):
        """
        禁用一个目标目录
        """
        self._disabled_destinations[destination_path] = {
            'reason': reason,
            'disabled_time': datetime.datetime.now(),
            'error_count': self._disabled_destinations.get(destination_path, {}).get('error_count', 0) + 1
        }
        logger.warning(f"已禁用目标目录 {destination_path}，原因：{reason}")

    def _enable_destination(self, destination_path: str):
        """
        重新启用一个目标目录
        """
        if destination_path in self._disabled_destinations:
            del self._disabled_destinations[destination_path]
            logger.info(f"已重新启用目标目录 {destination_path}")

    def _is_destination_disabled(self, destination_path: str) -> bool:
        """
        检查目标目录是否被禁用
        """
        return destination_path in self._disabled_destinations

    def _get_available_destinations(self, mon_path: str) -> List[Path]:
        """
        获取可用的目标目录（排除被禁用的）
        """
        all_destinations = self._dirconf.get(mon_path, [])
        if not all_destinations:
            return []
            
        available_destinations = []
        for dest in all_destinations:
            if not self._is_destination_disabled(str(dest)):
                available_destinations.append(dest)
                
        return available_destinations

    def _log_monitor_worker(self):
        """
        日志监控工作线程
        """
        logger.info(f"日志监控线程开始运行，配置：启用={self._log_monitor_enabled}, 路径={self._log_path}, 间隔={self._log_check_interval}秒")
        
        while not self._event.is_set():
            try:
                if not self._log_monitor_enabled:
                    logger.debug("日志监控未启用，跳过检查")
                    time.sleep(self._log_check_interval)
                    continue
                
                if not self._log_path:
                    logger.debug("日志路径未配置，跳过检查")
                    time.sleep(self._log_check_interval)
                    continue
                
                logger.info(f"开始检查日志文件，路径：{self._log_path}")
                    
                # 检查云盘错误
                error_counts = self._check_cloud_errors()
                
                if error_counts:
                    logger.info(f"检测到错误统计：{error_counts}")
                else:
                    logger.debug("未检测到网盘限制错误")
                
                # 处理错误超过阈值的挂载目录
                for mount_path, count in error_counts.items():
                    logger.info(f"挂载路径 {mount_path} 错误次数：{count}，阈值：{self._error_threshold}")
                    
                    if count >= self._error_threshold:
                        logger.warning(f"挂载路径 {mount_path} 错误次数达到阈值，开始禁用相关目录")
                        
                        # 使用新的路径匹配方法查找目标目录
                        matching_destinations = self._find_matching_destinations(mount_path)
                        
                        if not matching_destinations:
                            logger.warning(f"未找到与挂载路径 {mount_path} 匹配的目标目录")
                            continue
                            
                        logger.info(f"找到 {len(matching_destinations)} 个匹配的目标目录")
                        
                        for dest_str in matching_destinations:
                            if not self._is_destination_disabled(dest_str):
                                logger.info(f"准备禁用目标目录：{dest_str}")
                                self._disable_destination(dest_str, f"网盘限制错误达到阈值({count}次)")
                                
                                # 发送通知
                                if self._notify:
                                    self.post_message(
                                        title="网盘限制检测",
                                        text=f"检测到目标目录 {dest_str} 出现网盘限制错误 {count} 次，已自动禁用。",
                                        mtype=NotificationType.Manual
                                    )
                            else:
                                logger.debug(f"目标目录 {dest_str} 已被禁用，跳过")
                
                logger.debug(f"日志检查完成，等待 {self._log_check_interval} 秒后下次检查")
                time.sleep(self._log_check_interval)
                
            except Exception as e:
                logger.error(f"日志监控线程出错: {e}")
                import traceback
                logger.error(f"错误详情: {traceback.format_exc()}")
                time.sleep(self._log_check_interval)

    def _recovery_worker(self):
        """
        恢复检查工作线程
        """
        while not self._event.is_set():
            try:
                current_time = datetime.datetime.now()
                
                # 检查被禁用的目录是否可以恢复
                for dest_path, info in list(self._disabled_destinations.items()):
                    disabled_time = info['disabled_time']
                    time_diff = (current_time - disabled_time).total_seconds()
                    
                    # 如果禁用时间超过恢复检查间隔，尝试恢复
                    if time_diff > self._recovery_check_interval:
                        # 简单的恢复策略：超过恢复间隔就重新启用
                        self._enable_destination(dest_path)
                        
                        if self._notify:
                            self.post_message(
                                title="目标目录恢复",
                                text=f"目标目录 {dest_path} 已重新启用。",
                                mtype=NotificationType.Manual
                            )
                
                time.sleep(self._recovery_check_interval)
                
            except Exception as e:
                logger.error(f"恢复检查线程出错: {e}")
                time.sleep(self._recovery_check_interval)

    def _call_refresh_api(self, file_path: str) -> bool:
        """
        调用目录刷新API
        
        Args:
            file_path: 文件路径
            
        Returns:
            bool: 是否成功
        """
        if not self._refresh_before_transfer:
            logger.debug("目录刷新功能未启用，跳过")
            return True
            
        if not self._refresh_api_url or not self._refresh_api_key:
            logger.warning("目录刷新API配置不完整，跳过")
            return True
            
        try:
            # 使用配置的完整API URL
            url = self._refresh_api_url
            
            # 构建请求参数
            params = {
                "apikey": self._refresh_api_key
            }
            
            # 构建请求数据
            data = {
                "path_type": "source",
                "data": [
                    {
                        "action": "create",
                        "source_file": file_path
                    }
                ]
            }
            
            logger.info(f"调用目录刷新API: {url}")
            logger.debug(f"请求参数: {params}")
            logger.debug(f"请求数据: {data}")
            
            # 发送请求
            response = requests.post(
                url=url,
                params=params,
                json=data,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    logger.info(f"目录刷新成功: {result.get('message', '')}")
                    return True
                else:
                    logger.warning(f"目录刷新失败: {result.get('message', '未知错误')}")
                    return False
            else:
                logger.error(f"目录刷新API请求失败，状态码: {response.status_code}")
                return False
                
        except requests.exceptions.Timeout:
            logger.error("目录刷新API请求超时")
            return False
        except requests.exceptions.RequestException as e:
            logger.error(f"目录刷新API请求异常: {e}")
            return False
        except Exception as e:
            logger.error(f"调用目录刷新API时发生未知错误: {e}")
            return False

    def _start_log_monitoring(self):
        """
        启动日志监控
        """
        logger.info(f"准备启动日志监控，配置检查：启用={self._log_monitor_enabled}, 路径={self._log_path}")
        
        if not self._log_monitor_enabled:
            logger.info("日志监控功能未启用")
            return
            
        if not self._log_path:
            logger.warning("日志路径未配置，无法启动日志监控")
            return
            
        # 检查日志路径是否存在
        log_path = Path(self._log_path)
        if not log_path.exists():
            logger.warning(f"日志路径不存在：{self._log_path}")
        else:
            logger.info(f"日志路径验证成功：{self._log_path}")
            
        # 启动日志监控线程
        if not self._log_monitor_thread or not self._log_monitor_thread.is_alive():
            self._log_monitor_thread = threading.Thread(target=self._log_monitor_worker, daemon=True)
            self._log_monitor_thread.start()
            logger.info("日志监控线程已启动")
            
        # 启动恢复检查线程
        if not self._recovery_thread or not self._recovery_thread.is_alive():
            self._recovery_thread = threading.Thread(target=self._recovery_worker, daemon=True)
            self._recovery_thread.start()
            logger.info("恢复检查线程已启动")

    def _call_ai_normalize_naming(self, directory_path: str) -> Optional[Dict[str, str]]:
        """
        调用AI接口生成规范化命名，基于文件夹内所有文件名，返回 {"directory_name": str}
        """
        if not self._ai_rename_enabled:
            return None
        if not self._ai_api_url or not self._ai_api_keys:
            logger.debug("AI重命名未配置(缺少API Key)，跳过")
            return None
        try:
            # 收集文件夹内所有文件名
            dir_path = Path(directory_path)
            if not dir_path.exists() or not dir_path.is_dir():
                logger.warning(f"目录不存在或不是目录：{directory_path}")
                return None
                
            # 获取目录内所有媒体文件
            file_list = SystemUtils.list_files(dir_path, settings.RMT_MEDIAEXT)
            if not file_list:
                logger.warning(f"目录内没有找到媒体文件：{directory_path}")
                return None
                
            # 构建精简的文件列表：仅文件名、限制数量，减少token
            max_files = 60
            file_names = [Path(f).name for f in file_list[:max_files]]
            file_paths_text = "\n".join(file_names)
            if len(file_list) > max_files:
                file_paths_text += f"\n... (共{len(file_list)}个，仅展示前{max_files}个)"

            # 仅传目录名，避免冗长绝对路径
            dir_name_only = Path(directory_path).name
            prompt = f"{self._ai_prompt_template}\n目录名：{dir_name_only}\n文件：\n{file_paths_text}"
            
            # 轮换Key：若多Key存在，按索引取用
            if self._ai_key_index < 0 or self._ai_key_index >= len(self._ai_api_keys):
                self._ai_key_index = 0
            active_key = self._ai_api_keys[self._ai_key_index]

            provider = (self._ai_provider or "gemini").lower()

            if provider == "gemini":
                payload = {
                    "contents": [
                        {"parts": [{"text": prompt}]}
                    ],
                    "generationConfig": {
                        "response_mime_type": "application/json"
                    }
                }
                headers = {
                    "Content-Type": "application/json"
                }
                url = self._ai_api_url or f"https://generativelanguage.googleapis.com/v1beta/models/{self._ai_model}:generateContent"
                params = {"key": active_key}
                logger.info(f"调用AI重命名接口(Gemini)，使用密钥索引: {self._ai_key_index}/{len(self._ai_api_keys)-1}")
                resp = requests.post(url, headers=headers, params=params, json=payload, timeout=self._ai_timeout)
                if resp.status_code != 200:
                    try:
                        logger.info(f"AI响应体: {resp.text}")
                    except Exception:
                        pass
                    logger.warning(f"AI接口返回状态码异常: {resp.status_code}")
                    if self._ai_api_keys and resp.status_code in (400, 401, 403, 429):
                        prev = self._ai_key_index
                        self._ai_key_index = (self._ai_key_index + 1) % len(self._ai_api_keys)
                        if self._ai_key_index != prev:
                            if resp.status_code == 429:
                                logger.info(f"AI密钥配额超限，自动切换至索引 {self._ai_key_index}")
                            else:
                                logger.info(f"AI密钥可能失效，自动切换至索引 {self._ai_key_index}")
                            self._save_ai_keys_state(self._ai_api_keys)
                        else:
                            if resp.status_code == 429:
                                logger.warning("所有AI密钥都已达到配额限制")
                            else:
                                logger.warning("所有AI密钥都已失效")
                    return None
                data = resp.json()
                # 解析Gemini输出
                text = None
                try:
                    candidates = data.get("candidates") or []
                    for c in candidates:
                        content = c.get("content") or {}
                        parts = content.get("parts") or []
                        for p in parts:
                            if isinstance(p, dict) and p.get("text"):
                                text = p.get("text")
                                break
                        if text:
                            break
                except Exception:
                    pass
                if not text:
                    text = json.dumps(data)
            else:
                # OpenAI/Grok（OpenAI兼容）
                base_url = self._ai_api_url
                if not base_url:
                    base_url = "https://api.openai.com/v1/chat/completions" if provider == "openai" else "https://api.x.ai/v1/chat/completions"
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {active_key}"
                }
                model = self._ai_model or ("gpt-4o-mini" if provider == "openai" else "grok-2-latest")
                payload = {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你是一个媒体目录命名助手。严格返回JSON对象，不要额外文字。"
                        },
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"}
                }
                logger.info(f"调用AI重命名接口({provider})，使用密钥索引: {self._ai_key_index}/{len(self._ai_api_keys)-1}")
                resp = requests.post(base_url, headers=headers, json=payload, timeout=self._ai_timeout)
                if resp.status_code != 200:
                    try:
                        logger.info(f"AI响应体: {resp.text}")
                    except Exception:
                        pass
                    logger.warning(f"AI接口返回状态码异常: {resp.status_code}")
                    if self._ai_api_keys and resp.status_code in (400, 401, 403, 429):
                        prev = self._ai_key_index
                        self._ai_key_index = (self._ai_key_index + 1) % len(self._ai_api_keys)
                        if self._ai_key_index != prev:
                            if resp.status_code == 429:
                                logger.info(f"AI密钥配额超限，自动切换至索引 {self._ai_key_index}")
                            else:
                                logger.info(f"AI密钥可能失效，自动切换至索引 {self._ai_key_index}")
                            self._save_ai_keys_state(self._ai_api_keys)
                        else:
                            if resp.status_code == 429:
                                logger.warning("所有AI密钥都已达到配额限制")
                            else:
                                logger.warning("所有AI密钥都已失效")
                    return None
                data = resp.json()
                # 解析OpenAI兼容输出
                text = None
                try:
                    choices = data.get("choices") or []
                    if choices:
                        message = choices[0].get("message") or {}
                        text = message.get("content")
                except Exception:
                    pass
                if not text:
                    text = json.dumps(data)
            # 去除```包裹
            if isinstance(text, str):
                cleaned = text.strip()
                if cleaned.startswith("```"):
                    cleaned = re.sub(r"^```[a-zA-Z0-9]*\n", "", cleaned)
                    if cleaned.endswith("```"):
                        cleaned = cleaned[:-3]
                # 提取JSON对象
                match = re.search(r"\{[\s\S]*\}", cleaned)
                if match:
                    cleaned = match.group(0)
                try:
                    result = json.loads(cleaned)
                except Exception:
                    logger.warning("AI返回内容解析失败，跳过")
                    return None
            else:
                return None
            directory_name = (result.get("directory_name") or "").strip()
            if not directory_name:
                logger.warning("AI返回缺少directory_name字段，跳过")
                return None
            # 基本非法字符清理
            directory_name = re.sub(r"[\\/:*?\"<>|]", " ", directory_name).strip()
            return {"directory_name": directory_name}
        except requests.exceptions.Timeout:
            logger.warning("AI接口请求超时")
            return None
        except Exception as e:
            logger.warning(f"AI接口调用异常: {e}")
            return None

    def _apply_ai_folder_renaming(self, directory_path: Path, suggestion: Dict[str, str]) -> bool:
        """
        根据AI建议安全重命名文件夹
        """
        try:
            if not suggestion:
                return False
                
            current_dir = directory_path
            current_parent = current_dir.parent
            new_dir_name = Path(suggestion.get("directory_name")).name
            
            # 如果新目录名与当前目录名相同，无需重命名
            if new_dir_name == current_dir.name:
                logger.info(f"目录名无需更改：{current_dir}")
                return True
                
            target_dir = current_parent / new_dir_name
            
            # 检查目标目录是否已存在
            if target_dir.exists() and not target_dir.samefile(current_dir):
                logger.warning(f"目标目录已存在，放弃AI重命名：{target_dir}")
                return False
                
            # 执行目录重命名
            current_dir.rename(target_dir)
            logger.info(f"目录已重命名：{current_dir} -> {target_dir}")
            return True
            
        except Exception as e:
            logger.warning(f"AI文件夹重命名失败: {e}")
            return False


    def init_plugin(self, config: dict = None):
        self.transferhis = TransferHistoryOper()
        self.downloadhis = DownloadHistoryOper()
        self.transferchian = TransferChain()
        self.tmdbchain = TmdbChain()
        self.mediaChain = MediaChain()
        self.storagechain = StorageChain()
        self.filetransfer = FileManagerModule()
        
        # 初始化状态文件路径
        self._state_file = self.get_data_path() / "cloudlinkmonitor_state.json"
        # 初始化AI规范化记录文件
        self._ai_state_file = self.get_data_path() / "cloudlinkmonitor_ai_normalized.json"
        # 初始化AI密钥记录文件
        self._ai_keys_file = self.get_data_path() / "cloudlinkmonitor_ai_keys.json"
        
        # 清空配置并在启动时从文件加载状态
        self._dirconf = {}
        self._transferconf = {}
        self._overwrite_mode = {}
        self._round_robin_index = self._load_state_from_file()
        # 加载AI规范化记录
        self._load_ai_normalized_state()

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._history = config.get("history")
            self._scrape = config.get("scrape")
            self._category = config.get("category")
            self._refresh = config.get("refresh")
            self._mode = config.get("mode")
            self._transfer_type = config.get("transfer_type")
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._interval = config.get("interval") or 10
            self._cron = config.get("cron")
            self._size = config.get("size") or 0
            self._softlink = config.get("softlink")
            self._strm = config.get("strm")
            
            # 日志监控相关配置
            self._log_monitor_enabled = config.get("log_monitor_enabled", False)
            self._log_path = config.get("log_path", "")
            self._log_check_interval = config.get("log_check_interval", 60)
            self._error_threshold = config.get("error_threshold", 3)
            self._recovery_check_interval = config.get("recovery_check_interval", 1800)
            self._mount_path_mapping = config.get("mount_path_mapping", "")
            
            # 目录刷新相关配置
            self._refresh_before_transfer = config.get("refresh_before_transfer", False)
            self._refresh_api_url = config.get("refresh_api_url", "")
            self._refresh_api_key = config.get("refresh_api_key", "")
            
            # AI 规范命名配置（提示词为内置，不允许前端自定义）
            self._ai_provider = (config.get("ai_provider") or "gemini").lower()
            self._ai_rename_enabled = config.get("ai_rename_enabled", False)
            self._ai_api_url = config.get("ai_api_url", "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent")
            self._ai_model = config.get("ai_model", "gemini-2.0-flash")
            if 'ai_api_keys' in config:
                keys_text = config.get("ai_api_keys", "")
                # 配置中提供了字段：以用户输入为准（覆盖式保存，可清空）
                input_keys = [k.strip() for k in re.split(r"[\n,]", keys_text or "") if k.strip()]
                self._ai_api_keys = input_keys
                self._save_ai_keys_state(self._ai_api_keys)
            else:
                # 未提供字段：沿用持久化文件中的密钥
                self._ai_api_keys, saved_index = self._load_ai_keys_state()
                self._ai_key_index = saved_index if self._ai_api_keys else -1
            self._ai_timeout = config.get("ai_timeout", 30)
            self._ai_cache_days = config.get("ai_cache_days", 5)

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            # 定时服务管理器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

            # 读取目录配置
            monitor_dirs = self._monitor_dirs.split("\n")
            if not monitor_dirs:
                return
            for mon_path_conf in monitor_dirs:
                # 格式源目录:目的目录
                if not mon_path_conf:
                    continue

                # 自定义覆盖方式
                _overwrite_mode = 'never'
                if mon_path_conf.count("@") == 1:
                    _overwrite_mode = mon_path_conf.split("@")[1]
                    mon_path_conf = mon_path_conf.split("@")[0]

                # 自定义转移方式
                _transfer_type = self._transfer_type
                if mon_path_conf.count("#") == 1:
                    _transfer_type = mon_path_conf.split("#")[1]
                    mon_path_conf = mon_path_conf.split("#")[0]
                
                paths = mon_path_conf.split(":", 1)
                mon_path = ""
                if len(paths) > 1:
                    mon_path = paths[0].strip()
                    dest_paths_str = paths[1].split(',')
                    target_paths = [Path(p.strip()) for p in dest_paths_str if p.strip()]
                    if target_paths:
                        self._dirconf[mon_path] = target_paths
                        logger.info(f"监控目录 '{mon_path}' -> 目标目录: {target_paths}")
                    else:
                        self._dirconf[mon_path] = None
                else:
                    mon_path = paths[0].strip()
                    if mon_path:
                        self._dirconf[mon_path] = None

                if not mon_path:
                    continue
                    
                self._transferconf[mon_path] = _transfer_type
                self._overwrite_mode[mon_path] = _overwrite_mode

                # 启用目录监控
                if self._enabled:
                    target_paths_check = self._dirconf.get(mon_path) or []
                    can_monitor = True
                    for target_path in target_paths_check:
                        try:
                            if target_path and target_path.is_relative_to(Path(mon_path)):
                                logger.warn(f"目标目录 {target_path} 是监控目录 {mon_path} 的子目录，无法监控")
                                self.systemmessage.put(f"目标目录 {target_path} 是监控目录 {mon_path} 的子目录，无法监控")
                                can_monitor = False
                                break
                        except Exception as e:
                            logger.debug(str(e))
                            pass
                    
                    if not can_monitor:
                        continue

                    try:
                        observer = PollingObserver(timeout=10) if self._mode == "compatibility" else Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(mon_path, self), path=mon_path, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{mon_path} 的实时监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"实时监控服务启动异常：{err_msg}，请在宿主机上执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     && echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     && sudo sysctl -p
                                     """)
                        else:
                            logger.error(f"{mon_path} 启动实时监控失败：{err_msg}")
                        self.systemmessage.put(f"{mon_path} 启动实时监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("实时监控服务启动，立即运行一次")
                self._scheduler.add_job(name="实时监控",
                                        func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                        )
                self._onlyonce = False
                self.__update_config()

            # 启动日志监控
            self._start_log_monitoring()

            # 启动定时服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()
    
    # ... 省略无需改动的代码 ...
    # ... __update_config, remote_sync, sync_all, event_handler ...
    # ... __handle_file, send_msg, get_state, get_command, get_api ...
    # ... get_service, sync, get_form, get_page, stop_service ...
    # 粘贴时请确保将所有未显示的方法也一并保留在原位
    
    #【重要提示】以下是占位符，请确保将您原代码中从 __update_config 到 stop_service 的所有方法
    # 完整地复制并粘贴到这个位置，以保证插件的完整性。
    # 为了简洁，这里不再重复粘贴那些没有改动的方法。
    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "mode": self._mode,
            "transfer_type": self._transfer_type,
            "monitor_dirs": self._monitor_dirs,
            "exclude_keywords": self._exclude_keywords,
            "interval": self._interval,
            "history": self._history,
            "softlink": self._softlink,
            "cron": self._cron,
            "strm": self._strm,
            "scrape": self._scrape,
            "category": self._category,
            "size": self._size,
            "refresh": self._refresh,
            "log_monitor_enabled": self._log_monitor_enabled,
            "log_path": self._log_path,
            "log_check_interval": self._log_check_interval,
            "error_threshold": self._error_threshold,
            "recovery_check_interval": self._recovery_check_interval,
            "mount_path_mapping": self._mount_path_mapping,
            "refresh_before_transfer": self._refresh_before_transfer,
            "refresh_api_url": self._refresh_api_url,
            "refresh_api_key": self._refresh_api_key,
            # AI 规范命名
            "ai_provider": self._ai_provider,
            "ai_rename_enabled": self._ai_rename_enabled,
            "ai_api_url": self._ai_api_url,
            "ai_model": self._ai_model,
            # 为保护密钥，不在前端回显单Key；仅对多Key以掩码数量显示
            "ai_api_keys": "\n".join(self._ai_api_keys) if self._ai_api_keys else "",
            "ai_timeout": self._ai_timeout,
            "ai_cache_days": self._ai_cache_days,
            "ai_prompt_template": self._ai_prompt_template,
        })

    @eventmanager.register(EventType.PluginAction)
    def remote_sync(self, event: Event):
        """
        远程全量同步
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cloud_link_sync":
                return
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始同步云盘实时监控目录 ...",
                              userid=event.event_data.get("user"))
        self.sync_all()
        if event:
            self.post_message(channel=event.event_data.get("channel"),
                              title="云盘实时监控目录同步完成！", userid=event.event_data.get("user"))

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步云盘实时监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            logger.info(f"开始处理监控目录 {mon_path} ...")
            list_files = SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT)
            logger.info(f"监控目录 {mon_path} 共发现 {len(list_files)} 个文件")
            # 遍历目录下所有文件
            for file_path in list_files:
                logger.info(f"开始处理文件 {file_path} ...")
                self.__handle_file(event_path=str(file_path), mon_path=mon_path)
        logger.info("全量同步云盘实时监控目录完成！")

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param mon_path: 监控目录
        :param text: 事件描述
        :param event_path: 事件文件路径
        """
        if not event.is_directory:
            # 文件发生变化
            logger.debug("文件%s：%s" % (text, event_path))
            self.__handle_file(event_path=event_path, mon_path=mon_path)

    def __handle_file(self, event_path: str, mon_path: str):
        """
        同步一个文件
        :param event_path: 事件文件路径
        :param mon_path: 监控目录
        """
        file_path = Path(event_path)
        try:
            if not file_path.exists():
                return
            # 全程加锁
            with lock:
                transfer_history = self.transferhis.get_by_src(event_path)
                if transfer_history:
                    logger.info("文件已处理过：%s" % event_path)
                    # 触发AI规范命名 - 修复bug：只对媒体文件夹进行重命名，不对监控根目录重命名
                    try:
                        if not self._ai_rename_enabled:
                            logger.debug("AI规范命名未启用，跳过触发")
                        else:
                            ep_path = Path(event_path)
                            dir_path = ep_path.parent
                            dir_key = str(dir_path)
                            
                            # 安全检查：防止重命名监控根目录
                            if str(dir_path) == mon_path:
                                logger.warning(f"AI重命名跳过：不能重命名监控根目录 {dir_path}")
                                return
                            
                            # 检查是否为媒体文件夹（包含媒体文件的直接父目录）
                            is_media_folder = False
                            try:
                                # 检查目录是否包含媒体文件
                                media_files = SystemUtils.list_files(dir_path, settings.RMT_MEDIAEXT)
                                if media_files:
                                    # 检查是否所有媒体文件都在这个目录的直接子级
                                    for media_file in media_files:
                                        if Path(media_file).parent != dir_path:
                                            break
                                    else:
                                        is_media_folder = True
                            except Exception as e:
                                logger.debug(f"检查媒体文件夹时出错: {e}")
                            
                            if not is_media_folder:
                                logger.debug(f"AI重命名跳过：{dir_path} 不是媒体文件夹")
                                return
                            
                            if dir_key in self._ai_normalized_dirs:
                                logger.info(f"目录已记录为已规范命名，跳过AI：{dir_key}")
                                # 如果文件已处理过且AI规范命名也触发过，则删除转移记录
                                if transfer_history and transfer_history.id:
                                    try:
                                        self.transferhis.delete(transfer_history.id)
                                        logger.info(f"已删除转移记录（ID: {transfer_history.id}），文件：{event_path}，目录：{dir_key}")
                                    except Exception as e:
                                        logger.error(f"删除转移记录失败：{e}")
                            elif dir_key in self._ai_processed_dirs:
                                logger.info(f"目录本次运行已触发过AI，跳过重复调用：{dir_key}")
                            else:
                                logger.info(f"准备触发AI规范命名，媒体文件夹：{dir_key}")
                                ai_suggestion = self._call_ai_normalize_naming(str(dir_path))
                                if ai_suggestion:
                                    # 执行文件夹重命名
                                    rename_ok = self._apply_ai_folder_renaming(directory_path=dir_path, suggestion=ai_suggestion)
                                    if rename_ok:
                                        # 标记目录已处理，避免重复请求
                                        self._ai_processed_dirs.add(dir_key)
                                        # 记录规范化已完成
                                        self._ai_normalized_dirs.add(dir_key)
                                        self._save_ai_normalized_state()
                                        logger.info(f"AI文件夹重命名成功：{dir_path}")
                                    else:
                                        logger.warning(f"AI文件夹重命名失败：{dir_path}")
                    except Exception as e:
                        logger.debug(f"AI重命名触发异常: {e}")
                    return

                # 回收站及隐藏的文件不处理
                if event_path.find('/@Recycle/') != -1 \
                        or event_path.find('/#recycle/') != -1 \
                        or event_path.find('/.') != -1 \
                        or event_path.find('/@eaDir') != -1:
                    logger.debug(f"{event_path} 是回收站或隐藏的文件")
                    return

                # 命中过滤关键字不处理
                if self._exclude_keywords:
                    for keyword in self._exclude_keywords.split("\n"):
                        if keyword and re.findall(keyword, event_path):
                            logger.info(f"{event_path} 命中过滤关键字 {keyword}，不处理")
                            return

                # 整理屏蔽词不处理
                transfer_exclude_words = self.systemconfig.get(SystemConfigKey.TransferExcludeWords)
                if transfer_exclude_words:
                    for keyword in transfer_exclude_words:
                        if not keyword:
                            continue
                        if keyword and re.search(r"%s" % keyword, event_path, re.IGNORECASE):
                            logger.info(f"{event_path} 命中整理屏蔽词 {keyword}，不处理")
                            return

                # 不是媒体文件不处理
                if file_path.suffix not in settings.RMT_MEDIAEXT:
                    logger.debug(f"{event_path} 不是媒体文件")
                    return

                # 判断是不是蓝光目录
                if re.search(r"BDMV[/\\]STREAM", event_path, re.IGNORECASE):
                    # 截取BDMV前面的路径
                    blurray_dir = event_path[:event_path.find("BDMV")]
                    file_path = Path(blurray_dir)
                    logger.info(f"{event_path} 是蓝光目录，更正文件路径为：{str(file_path)}")
                    # 查询历史记录，已转移的不处理
                    if self.transferhis.get_by_src(str(file_path)):
                        logger.info(f"{file_path} 已整理过")
                        return

                # 元数据
                file_meta = MetaInfoPath(file_path)
                if not file_meta.name:
                    logger.error(f"{file_path.name} 无法识别有效信息")
                    return

                # 判断文件大小
                if self._size and float(self._size) > 0 and file_path.stat().st_size < float(self._size) * 1024 ** 3:
                    logger.info(f"{file_path} 文件大小小于监控文件大小，不处理")
                    return

                # 查找这个文件项
                file_item = self.storagechain.get_file_item(storage="local", path=file_path)
                if not file_item:
                    logger.warn(f"{file_path.name} 未找到对应的文件")
                    return
                    
                # 识别媒体信息
                mediainfo: MediaInfo = self.chain.recognize_media(meta=file_meta)
                if not mediainfo:
                    logger.warn(f'未识别到媒体信息，标题：{file_meta.name}')
                    # 新增转移成功历史记录
                    his = self.transferhis.add_fail(
                        fileitem=file_item,
                        mode=self._transferconf.get(mon_path),
                        meta=file_meta
                    )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{file_path.name} 未识别到媒体信息，无法入库！\n"
                                  f"回复：```\n/redo {his.id} [tmdbid]|[类型]\n``` 手动识别转移。"
                        )
                    return

                # 如果未开启新增已入库媒体是否跟随TMDB信息变化则根据tmdbid查询之前的title
                if not settings.SCRAP_FOLLOW_TMDB:
                    transfer_history = self.transferhis.get_by_type_tmdbid(tmdbid=mediainfo.tmdb_id,
                                                                           mtype=mediainfo.type.value)
                    if transfer_history:
                        mediainfo.title = transfer_history.title
                logger.info(f"{file_path.name} 识别为：{mediainfo.type.value} {mediainfo.title_year}")
                
                # 使用新的轮询逻辑查询转移目的目录
                target: Path = self._get_round_robin_destination(mon_path=mon_path, mediainfo=mediainfo)
                if not target:
                    logger.error(f"无法为 '{file_path.name}' 确定目标目录，已跳过。")
                    return

                # 查询转移方式
                transfer_type = self._transferconf.get(mon_path)

                # 获取集数据
                if mediainfo.type == MediaType.TV:
                    episodes_info = self.tmdbchain.tmdb_episodes(tmdbid=mediainfo.tmdb_id,
                                                                 season=1 if file_meta.begin_season is None else file_meta.begin_season)
                else:
                    episodes_info = None

                # 直接创建转移目录配置，不使用DirectoryHelper避免历史记录检查
                target_dir = TransferDirectoryConf()
                target_dir.library_path = target
                target_dir.transfer_type = transfer_type
                target_dir.scraping = self._scrape
                target_dir.renaming = True
                target_dir.notify = False
                target_dir.overwrite_mode = self._overwrite_mode.get(mon_path) or 'never'
                target_dir.library_storage = "local"
                target_dir.library_category_folder = self._category
                
                if not target_dir.library_path:
                    logger.error(f"未配置监控目录 {mon_path} 的目的目录")
                    return

                # 转移前调用目录刷新API（使用目标文件完整路径，与transhandler保持一致）
                if self._refresh_before_transfer:
                    try:
                        handler = TransHandler()
                        # 1) 与整理一致地计算目标目录（含类型/分类）
                        dest_dir_path = handler.get_dest_dir(
                            mediainfo=mediainfo,
                            target_dir=target_dir,
                            need_type_folder=False,
                            need_category_folder=target_dir.library_category_folder
                        )
                        # 2) 与整理一致地生成最终目标文件路径
                        if target_dir.renaming:
                            rename_format = settings.RENAME_FORMAT(mediainfo.type)
                            target_path_obj = handler.get_rename_path(
                                path=dest_dir_path,
                                template_string=rename_format,
                                rename_dict=handler.get_naming_dict(
                                    meta=file_meta,
                                    mediainfo=mediainfo,
                                    episodes_info=episodes_info,
                                    file_ext=file_path.suffix
                                )
                            )
                        else:
                            target_path_obj = dest_dir_path / file_path.name

                        target_file_path = target_path_obj.as_posix()
                        # 转换为挂载路径格式（用于CloudDrive API）
                        mount_file_path = self._convert_to_mount_path(target_file_path)
                        logger.info(f"转移前调用目录刷新API: {mount_file_path} (原路径: {target_file_path})")
                        refresh_success = self._call_refresh_api(mount_file_path)
                        if not refresh_success:
                            logger.warning(f"目录刷新失败，但继续执行文件转移: {target_file_path}")
                        else:
                            logger.info(f"目录刷新成功，准备转移文件: {target_file_path}")
                    except Exception as e:
                        logger.warning(f"构建目标路径失败，跳过目录刷新: {e}")

                # 转移文件
                transferinfo: TransferInfo = self.chain.transfer(fileitem=file_item,
                                                                 meta=file_meta,
                                                                 mediainfo=mediainfo,
                                                                 target_directory=target_dir,
                                                                 episodes_info=episodes_info)

                if not transferinfo:
                    logger.error("文件转移模块运行失败")
                    return

                if not transferinfo.success:
                    # 转移失败
                    logger.warn(f"{file_path.name} 入库失败：{transferinfo.message}")

                    if self._history:
                        # 新增转移失败历史记录
                        self.transferhis.add_fail(
                            fileitem=file_item,
                            mode=transfer_type,
                            meta=file_meta,
                            mediainfo=mediainfo,
                            transferinfo=transferinfo
                        )
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.Manual,
                            title=f"{mediainfo.title_year}{file_meta.season_episode} 入库失败！",
                            text=f"原因：{transferinfo.message or '未知'}",
                            image=mediainfo.get_message_image()
                        )
                    # 入库因同名冲突失败时触发AI规范命名 - 修复bug：只对媒体文件夹进行重命名
                    try:
                        if self._ai_rename_enabled:
                            conflict = False
                            msg_text = (transferinfo.message or "")
                            if any(k in msg_text for k in ["同名", "已存在", "exist", "exists"]):
                                conflict = True
                            if conflict:
                                ep_path = Path(event_path)
                                dir_path = ep_path.parent
                                dir_key = str(dir_path)
                                
                                # 安全检查：防止重命名监控根目录
                                if str(dir_path) == mon_path:
                                    logger.warning(f"AI重命名跳过(失败分支)：不能重命名监控根目录 {dir_path}")
                                else:
                                    # 检查是否为媒体文件夹
                                    is_media_folder = False
                                    try:
                                        media_files = SystemUtils.list_files(dir_path, settings.RMT_MEDIAEXT)
                                        if media_files:
                                            for media_file in media_files:
                                                if Path(media_file).parent != dir_path:
                                                    break
                                            else:
                                                is_media_folder = True
                                    except Exception as e:
                                        logger.debug(f"检查媒体文件夹时出错(失败分支): {e}")
                                    
                                    if is_media_folder and dir_key not in self._ai_processed_dirs and dir_key not in self._ai_normalized_dirs:
                                        logger.info("检测到同名冲突，触发AI规范命名流程(失败分支)")
                                        ai_suggestion = self._call_ai_normalize_naming(str(dir_path))
                                        if ai_suggestion:
                                            rename_ok = self._apply_ai_folder_renaming(directory_path=dir_path, suggestion=ai_suggestion)
                                            if rename_ok:
                                                self._ai_processed_dirs.add(dir_key)
                                                self._ai_normalized_dirs.add(dir_key)
                                                self._save_ai_normalized_state()
                                                logger.info(f"AI文件夹重命名成功(失败分支)：{dir_path}")
                                            else:
                                                logger.warning(f"AI文件夹重命名失败(失败分支)：{dir_path}")
                                    elif not is_media_folder:
                                        logger.debug(f"AI重命名跳过(失败分支)：{dir_path} 不是媒体文件夹")
                    except Exception as e:
                        logger.debug(f"AI重命名(失败分支)触发异常: {e}")
                    return

                if self._history:
                    # 新增转移成功历史记录
                    self.transferhis.add_success(
                        fileitem=file_item,
                        mode=transfer_type,
                        meta=file_meta,
                        mediainfo=mediainfo,
                        transferinfo=transferinfo
                    )

                # 刮削
                if self._scrape:
                    self.mediaChain.scrape_metadata(fileitem=transferinfo.target_diritem,
                                                    meta=file_meta,
                                                    mediainfo=mediainfo)

                if self._notify:
                    # 发送消息汇总
                    media_list = self._medias.get(mediainfo.title_year + " " + file_meta.season) or {}
                    if media_list:
                        media_files = media_list.get("files") or []
                        if media_files:
                            file_exists = False
                            for file in media_files:
                                if str(file_path) == file.get("path"):
                                    file_exists = True
                                    break
                            if not file_exists:
                                media_files.append({
                                    "path": str(file_path),
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                })
                        else:
                            media_files = [
                                {
                                    "path": str(file_path),
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                }
                            ]
                        media_list = {
                            "files": media_files,
                            "time": datetime.datetime.now()
                        }
                    else:
                        media_list = {
                            "files": [
                                {
                                    "path": str(file_path),
                                    "mediainfo": mediainfo,
                                    "file_meta": file_meta,
                                    "transferinfo": transferinfo
                                }
                            ],
                            "time": datetime.datetime.now()
                        }
                    self._medias[mediainfo.title_year + " " + file_meta.season] = media_list

                if self._refresh:
                    # 广播事件
                    self.eventmanager.send_event(EventType.TransferComplete, {
                        'meta': file_meta,
                        'mediainfo': mediainfo,
                        'transferinfo': transferinfo
                    })

                if self._softlink:
                    # 通知实时软连接生成
                    self.eventmanager.send_event(EventType.PluginAction, {
                        'file_path': str(transferinfo.target_item.path),
                        'action': 'softlink_file'
                    })

                if self._strm:
                    # 通知Strm助手生成
                    self.eventmanager.send_event(EventType.PluginAction, {
                        'file_path': str(transferinfo.target_item.path),
                        'action': 'cloudstrm_file'
                    })

                # 移动模式删除空目录
                if transfer_type == "move":
                    for file_dir in file_path.parents:
                        if len(str(file_dir)) <= len(str(Path(mon_path))):
                            # 重要，删除到监控目录为止
                            break
                        files = SystemUtils.list_files(file_dir, settings.RMT_MEDIAEXT + settings.DOWNLOAD_TMPEXT)
                        if not files:
                            logger.warn(f"移动模式，删除空目录：{file_dir}")
                            shutil.rmtree(file_dir, ignore_errors=True)

        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias or not self._medias.keys():
            return

        # 遍历检查是否已刮削完，发送消息
        for medis_title_year_season in list(self._medias.keys()):
            media_list = self._medias.get(medis_title_year_season)
            logger.info(f"开始处理媒体 {medis_title_year_season} 消息")

            if not media_list:
                continue

            # 获取最后更新时间
            last_update_time = media_list.get("time")
            media_files = media_list.get("files")
            if not last_update_time or not media_files:
                continue

            transferinfo = media_files[0].get("transferinfo")
            file_meta = media_files[0].get("file_meta")
            mediainfo = media_files[0].get("mediainfo")
            # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
            if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._interval) \
                    or mediainfo.type == MediaType.MOVIE:
                # 发送通知
                if self._notify:

                    # 汇总处理文件总大小
                    total_size = 0
                    file_count = 0

                    # 剧集汇总
                    episodes = []
                    for file in media_files:
                        transferinfo_item = file.get("transferinfo")
                        total_size += transferinfo_item.total_size
                        file_count += 1

                        file_meta_item = file.get("file_meta")
                        if file_meta_item and file_meta_item.begin_episode:
                            episodes.append(file_meta_item.begin_episode)

                    transferinfo.total_size = total_size
                    # 汇总处理文件数量
                    transferinfo.file_count = file_count

                    # 剧集季集信息 S01 E01-E04 || S01 E01、E02、E04
                    season_episode = None
                    # 处理文件多，说明是剧集，显示季入库消息
                    if mediainfo.type == MediaType.TV:
                        # 季集文本
                        season_episode = f"{file_meta.season} {StringUtils.format_ep(episodes)}"
                    # 发送消息
                    self.transferchian.send_transfer_message(meta=file_meta,
                                                             mediainfo=mediainfo,
                                                             transferinfo=transferinfo,
                                                             season_episode=season_episode)
                # 发送完消息，移出key
                del self._medias[medis_title_year_season]
                continue

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cloud_link_sync",
            "event": EventType.PluginAction,
            "desc": "云盘实时监控同步",
            "category": "",
            "data": {
                "action": "cloud_link_sync"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/cloud_link_sync",
            "endpoint": self.sync,
            "methods": ["GET"],
            "summary": "云盘实时监控同步",
            "description": "云盘实时监控同步",
        }]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        """
        if self._enabled and self._cron:
            return [{
                "id": "CloudLinkMonitor",
                "name": "云盘实时监控全量同步服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.sync_all,
                "kwargs": {}
            }]
        return []

    def sync(self) -> schemas.Response:
        """
        API调用目录同步
        """
        self.sync_all()
        return schemas.Response(success=True)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 在提示信息中更新配置说明
        placeholder_text = (
            '每一行一个监控配置，支持以下格式：\n'
            '1. 单目标目录：监控目录:转移目的目录\n'
            '2. 多目标轮询：监控目录:目的1,目的2,目的3\n'
            '3. 自定义转移方式：监控目录:转移目的目录#转移方式\n'
            '支持的转移方式: move, copy, link, softlink, rclone_copy, rclone_move'
        )
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'history', 'label': '存储历史记录'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'scrape', 'label': '是否刮削'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'category', 'label': '是否二级分类'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'refresh', 'label': '刷新媒体库'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'softlink', 'label': '联动实时软连接'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'strm', 'label': '联动Strm生成'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'mode', 'label': '监控模式',
                                        'items': [{'title': '兼容模式', 'value': 'compatibility'},
                                                  {'title': '性能模式', 'value': 'fast'}]
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'transfer_type', 'label': '转移方式',
                                        'items': [{'title': '移动', 'value': 'move'},
                                                  {'title': '复制', 'value': 'copy'},
                                                  {'title': '硬链接', 'value': 'link'},
                                                  {'title': '软链接', 'value': 'softlink'},
                                                  {'title': 'Rclone复制', 'value': 'rclone_copy'},
                                                  {'title': 'Rclone移动', 'value': 'rclone_move'}]
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'interval', 'label': '入库消息延迟', 'placeholder': '10'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol', 'props': {'cols': 12, 'md': 4},
                            'content': [{
                                'component': 'VTextField',
                                'props': {'model': 'cron', 'label': '定时任务', 'placeholder': '留空则不执行定时同步'}
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol', 'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextarea',
                                'props': {'model': 'monitor_dirs', 'label': '监控目录', 'rows': 5,
                                          'placeholder': placeholder_text}
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol', 'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextarea',
                                'props': {'model': 'exclude_keywords', 'label': '排除关键词', 'rows': 2,
                                          'placeholder': '每一行一个关键词'}
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'log_monitor_enabled', 'label': '启用日志监控'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'log_path', 'label': '日志文件夹路径', 'placeholder': '/path/to/logs'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'log_check_interval', 'label': '日志检查间隔(秒)', 'placeholder': '60'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'error_threshold', 'label': '错误阈值', 'placeholder': '3'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'recovery_check_interval', 'label': '恢复检查间隔(秒)', 'placeholder': '1800'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [{
                            'component': 'VCol', 'props': {'cols': 12},
                            'content': [{
                                'component': 'VTextarea',
                                'props': {'model': 'mount_path_mapping', 'label': '挂载路径映射', 'rows': 3,
                                          'placeholder': '格式：/gd2:/mnt/CloudDrive/gd2,/gd3:/mnt/CloudDrive/gd3\n用于映射日志中的挂载路径到实际目录路径'}
                            }]
                        }]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'refresh_before_transfer', 'label': '转移前刷新目录'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'refresh_api_url', 'label': '刷新API完整地址', 'placeholder': '请输入完整的CloudDriveWebhook API地址，如：http://server:port/api/v1/plugin/CloudDriveWebhook/clouddrive_webhook'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'refresh_api_key', 'label': '刷新API密钥', 'placeholder': '请输入CloudDriveWebhook插件API密钥'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VSelect',
                                    'props': {
                                        'model': 'ai_provider',
                                        'label': 'AI提供商',
                                        'items': [
                                            {'title': 'Gemini', 'value': 'gemini'},
                                            {'title': 'OpenAI', 'value': 'openai'},
                                            {'title': 'Grok', 'value': 'grok'}
                                        ]
                                    }
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'ai_rename_enabled', 'label': '启用AI规范命名'}}]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'ai_api_url', 'label': 'AI接口地址', 'placeholder': 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent'}
                                }]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'ai_model', 'label': 'AI模型', 'placeholder': 'gemini-2.0-flash'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [{
                                    'component': 'VTextarea',
                                    'props': {'model': 'ai_api_keys', 'label': 'AI API Keys(多行/逗号分隔，按顺序自动切换)', 'rows': 4,
                                              'placeholder': 'key1\nkey2\nkey3'}
                                }]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'ai_timeout', 'label': 'AI超时(秒)', 'placeholder': '30'}
                                }]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{
                                    'component': 'VTextField',
                                    'props': {'model': 'ai_cache_days', 'label': 'AI缓存天数', 'placeholder': '5'}
                                }]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False, "notify": False, "onlyonce": False, "history": False, "scrape": False, "category": False,
            "refresh": True, "softlink": False, "strm": False, "mode": "fast", "transfer_type": "softlink",
            "monitor_dirs": "", "exclude_keywords": "", "interval": 10, "cron": "", "size": 0,
            "log_monitor_enabled": False, "log_path": "", "log_check_interval": 60, "error_threshold": 3, 
            "recovery_check_interval": 1800, "mount_path_mapping": "",
            "refresh_before_transfer": False, "refresh_api_url": "", "refresh_api_key": "",
            "ai_provider": "gemini", "ai_rename_enabled": False, "ai_api_url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            "ai_model": "gemini-2.0-flash", "ai_timeout": 30, "ai_cache_days": 5
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        # 停止事件监控
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
        
        # 停止调度器
        if self._scheduler:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._event.set()
                self._scheduler.shutdown()
                self._event.clear()
            self._scheduler = None
            
        # 停止日志监控线程
        if self._log_monitor_thread and self._log_monitor_thread.is_alive():
            try:
                self._event.set()
                self._log_monitor_thread.join(timeout=5)
            except Exception as e:
                logger.error(f"停止日志监控线程时出错: {e}")
                
        # 停止恢复检查线程
        if self._recovery_thread and self._recovery_thread.is_alive():
            try:
                self._event.set()
                self._recovery_thread.join(timeout=5)
            except Exception as e:
                logger.error(f"停止恢复检查线程时出错: {e}")
                
        # 重置事件
        self._event.clear()