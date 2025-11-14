import os
import time
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
try:
    from typing_extensions import Annotated
except ImportError:
    from typing import Annotated

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select, and_
from sqlalchemy.orm import Session

from app.plugins import _PluginBase
from app.core.config import settings
from app.core.security import verify_apikey
from app.schemas import Notification, NotificationType, ContentType
from app.schemas.types import EventType
from app.log import logger
from app.db import get_db, SessionFactory
from app.db.models.transferhistory import TransferHistory
from app.chain.storage import StorageChain
from app.core.event import eventmanager
from app.modules.filemanager import FileManagerModule


class FileSweeper(_PluginBase):
    """转移失败文件清理器插件 - 定时删除或转移MoviePilot转移失败的文件"""
    
    # 插件信息
    plugin_name = "FileSweeper"
    plugin_desc = "定时删除或转移MoviePilot转移失败的文件，支持智能模式根据失败原因自动决定删除或转移"
    plugin_icon = "refresh2.png"
    plugin_color = "#FF6B6B"
    plugin_version = "2.4"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "filesweeper"
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self._enabled = config.get("enabled", False) if config else False
        self._cron = config.get("cron", "0 2 * * *") if config else "0 2 * * *"  # 默认每天凌晨2点执行
        self._dry_run = config.get("dry_run", False) if config else False
        self._send_notification = config.get("send_notification", True) if config else True
        
        # 转移失败文件清理配置
        self._failed_transfer_age_hours = self._to_float(config.get("failed_transfer_age_hours", 24) if config else 24, 24.0)
        
        # 转移模式配置
        self._transfer_mode = config.get("transfer_mode", "delete") if config else "delete"  # delete 或 transfer
        self._transfer_target_dir = config.get("transfer_target_dir", "") if config else ""
        
        # 智能处理模式：根据失败原因决定删除或转移
        self._smart_mode = config.get("smart_mode", False) if config else False
        self._duplicate_file_delete = config.get("duplicate_file_delete", True) if config else True  # "存在同名文件"是否删除
        
        logger.info(f"FileSweeper插件初始化完成，启用状态: {self._enabled}")
        if self._enabled:
            logger.info(f"定时任务: {self._cron}")
            logger.info(f"转移失败文件最大年龄: {self._failed_transfer_age_hours}小时")
            logger.info(f"预览模式: {self._dry_run}")
            if self._smart_mode:
                logger.info(f"处理模式: 智能模式（同名文件删除，其他转移）")
            else:
                logger.info(f"处理模式: {'转移' if self._transfer_mode == 'transfer' else '删除'}")
            if self._transfer_mode == "transfer" or self._smart_mode:
                logger.info(f"转移目标目录: {self._transfer_target_dir}")

    def get_state(self) -> bool:
        """获取插件状态"""
        return self._enabled

    @staticmethod
    def get_command() -> list:
        """获取插件命令"""
        return [
            {
                "cmd": "/filesweeper",
                "event": EventType.PluginAction,
                "desc": "手动执行转移失败文件清理",
                "category": "清理",
                "data": {
                    "action": "manual_clean"
                }
            }
        ]

    def get_api(self) -> list:
        """获取API接口"""
        return [
            {
                "path": "/manual_clean",
                "endpoint": self.manual_clean,
                "methods": ["POST"],
                "summary": "手动执行转移失败文件清理",
                "description": "手动触发转移失败文件清理任务"
            },
            {
                "path": "/preview_clean",
                "endpoint": self.preview_clean,
                "methods": ["POST"],
                "summary": "预览转移失败文件清理",
                "description": "预览将要清理的转移失败文件，不实际删除"
            }
        ]

    def get_form(self) -> tuple:
        """获取配置表单"""
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
                                            "model": "dry_run",
                                            "label": "预览模式（不实际删除）",
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
                                            "placeholder": "0 2 * * * (每天凌晨2点)",
                                            "hint": "使用cron表达式，如：0 2 * * * 表示每天凌晨2点执行"
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
                                            "model": "failed_transfer_age_hours",
                                            "label": "转移失败文件最大年龄（小时）",
                                            "type": "number",
                                            "placeholder": "24"
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "send_notification",
                                            "label": "发送通知",
                                            "color": "primary"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VDivider",
                                        "props": {
                            "class": "my-4"
                                        }
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
                                            "text": "处理模式：可选择删除文件或转移到指定文件夹。转移模式下，只转移文件不转移文件夹结构，转移完成后如果原文件夹为空则删除。"
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
                                            "model": "transfer_mode",
                                            "label": "处理模式",
                                            "items": [
                                                {"title": "删除文件", "value": "delete"},
                                                {"title": "转移到文件夹", "value": "transfer"}
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
                                            "model": "transfer_target_dir",
                                            "label": "转移目标文件夹",
                                            "placeholder": "/path/to/target/directory",
                                            "hint": "转移模式下，文件将转移到此文件夹中的同名文件夹内"
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
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "smart_mode",
                                            "label": "智能处理模式（根据失败原因决定删除或转移）",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "text": "智能模式：失败原因为\"存在同名文件\"的记录将直接删除，其他失败原因的文件将转移到目标文件夹。启用智能模式时，处理模式将自动设置为转移模式。"
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
                                            "text": "此插件专门用于清理MoviePilot转移失败的文件。插件会查询数据库中转移失败的记录，删除或转移超过指定时间的源文件，并发送相应的事件通知。"
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
                                            "type": "warning",
                                            "text": "警告：此插件会删除转移失败的文件，请谨慎配置。建议先在预览模式下测试。"
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
            "cron": "0 2 * * *",
            "dry_run": False,
            "send_notification": True,
            "failed_transfer_age_hours": 24,
            "transfer_mode": "delete",
            "transfer_target_dir": "",
            "smart_mode": False,
            "duplicate_file_delete": True
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
                "id": "FileSweeper",
                "name": "转移失败文件清理服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._execute_clean,
                "kwargs": {}
            }]
        return []

    def stop_service(self):
        """停止插件"""
        logger.info("FileSweeper插件已停止")

    def manual_clean(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        手动执行转移失败文件清理任务
        
        Args:
            request_data: 请求数据
            apikey: API密钥
            
        Returns:
            Dict: 清理结果
        """
        try:
            logger.info("开始手动执行转移失败文件清理任务")
            
            # 执行清理
            result = self._execute_clean()
            
            return {
                "success": True,
                "message": "手动转移失败文件清理任务执行完成",
                "result": result
            }
            
        except Exception as e:
            error_msg = f"手动转移失败文件清理任务执行失败：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def preview_clean(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        预览转移失败文件清理任务
        
        Args:
            request_data: 请求数据
            apikey: API密钥
            
        Returns:
            Dict: 预览结果
        """
        try:
            logger.info("开始预览转移失败文件清理任务")
            
            # 临时启用预览模式
            original_dry_run = self._dry_run
            self._dry_run = True
            
            # 执行清理
            result = self._execute_clean()
            
            # 恢复原始设置
            self._dry_run = original_dry_run
            
            return {
                "success": True,
                "message": "预览转移失败文件清理任务完成",
                "result": result
            }
            
        except Exception as e:
            error_msg = f"预览转移失败文件清理任务失败：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def _execute_clean(self) -> Dict[str, Any]:
        """
        执行转移失败文件清理任务
        
        Returns:
            Dict: 清理结果
        """
        try:
            logger.info("=== 开始执行转移失败文件清理任务 ===")
            
            cleaned_files = []
            cleaned_dirs = []
            total_size = 0
            errors = []
            
            # 清理转移失败的文件
            logger.info("开始清理转移失败的文件")
            failed_result = self._clean_failed_transfer_files()
            cleaned_files.extend(failed_result["files"])
            cleaned_dirs.extend(failed_result["dirs"])
            total_size += failed_result["size"]
            errors.extend(failed_result["errors"])
            
            # 发送通知
            if self._send_notification and (cleaned_files or cleaned_dirs):
                self._send_clean_notification(cleaned_files, cleaned_dirs, total_size, errors)
            
            result = {
                "success": len(errors) == 0,
                "message": f"转移失败文件清理完成 - 文件: {len(cleaned_files)}, 目录: {len(cleaned_dirs)}, 总大小: {self._format_size(total_size)}",
                "cleaned_files": cleaned_files,
                "cleaned_dirs": cleaned_dirs,
                "total_size": total_size,
                "errors": errors,
                "dry_run": self._dry_run
            }
            
            logger.info(f"=== 转移失败文件清理任务完成 ===")
            logger.info(f"结果: {result['message']}")
            if errors:
                logger.warning(f"错误数量: {len(errors)}")
                for error in errors:
                    logger.warning(f"错误: {error}")
            
            return result
            
        except Exception as e:
            error_msg = f"执行转移失败文件清理任务时发生错误: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "cleaned_files": [],
                "cleaned_dirs": [],
                "total_size": 0,
                "errors": [error_msg]
            }

    def _transfer_file_to_target(self, src_fileitem: Any, target_dir: str, folder_name: str) -> Tuple[bool, str, int]:
        """
        将文件转移到目标文件夹
        参考 gdcloudlinkmonitor 插件的实现方式
        
        Args:
            src_fileitem: 源文件项
            target_dir: 目标目录
            folder_name: 目标文件夹名称（同名文件夹）
            
        Returns:
            Tuple[bool, str, int]: (是否成功, 错误信息, 转移的文件数量)
        """
        try:
            logger.info(f"开始转移文件: {src_fileitem.path} (类型: {src_fileitem.type}, 存储: {src_fileitem.storage}) -> {target_dir}/{folder_name}")
            
            target_path = Path(target_dir)
            if not target_path.exists():
                target_path.mkdir(parents=True, exist_ok=True)
                logger.info(f"创建目标目录: {target_dir}")
            
            # 创建同名文件夹
            target_folder = target_path / folder_name
            if not target_folder.exists():
                target_folder.mkdir(parents=True, exist_ok=True)
                logger.info(f"创建目标文件夹: {target_folder}")
            
            transferred_count = 0
            storage_chain = StorageChain()
            file_manager = FileManagerModule()
            
            # 如果源文件是文件，直接转移
            if src_fileitem.type == "file":
                src_path = Path(src_fileitem.path)
                # 检查是否是本地文件
                if src_fileitem.storage == "local" and src_path.exists():
                    logger.debug(f"处理本地文件: {src_path}")
                    target_file = target_folder / src_path.name
                    if target_file.exists():
                        # 文件已存在，添加时间戳
                        logger.warning(f"目标文件已存在，将重命名: {target_file}")
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        name_parts = src_path.stem, timestamp, src_path.suffix
                        new_name = f"{name_parts[0]}_{name_parts[1]}{name_parts[2]}"
                        target_file = target_folder / new_name
                    
                    # 使用 FileManagerModule 获取本地存储操作对象进行转移
                    try:
                        # 获取本地存储操作对象
                        from app.modules.filemanager.storages import StorageBase
                        from app.modules.filemanager.storages.local import LocalStorage
                        local_storage = LocalStorage()
                        
                        # 使用 move 方法转移文件
                        if local_storage.move(src_fileitem, target_folder, target_file.name):
                            logger.info(f"转移文件成功: {src_path} -> {target_file}")
                            transferred_count += 1
                        else:
                            raise Exception(f"使用存储操作对象转移文件失败")
                    except Exception as e:
                        logger.error(f"转移文件失败: {src_path} -> {target_file}, 错误: {str(e)}")
                        # 如果使用存储操作对象失败，回退到 shutil.move
                        try:
                            shutil.move(str(src_path), str(target_file))
                            logger.info(f"转移文件成功（回退方法）: {src_path} -> {target_file}")
                            transferred_count += 1
                        except Exception as e2:
                            logger.error(f"转移文件失败（回退方法）: {src_path} -> {target_file}, 错误: {str(e2)}")
                            raise
                elif not src_path.exists() and src_fileitem.storage == "local":
                    logger.warning(f"本地文件不存在: {src_path}, 存储类型: {src_fileitem.storage}")
                    raise Exception(f"源文件不存在: {src_path}")
                else:
                    # 非本地文件，使用 StorageChain 下载后转移
                    logger.info(f"处理非本地文件: {src_fileitem.path}, 存储类型: {src_fileitem.storage}")
                    # 下载文件到本地临时位置
                    logger.debug(f"开始下载文件: {src_fileitem.path}")
                    local_file = storage_chain.download_file(src_fileitem)
                    if local_file and local_file.exists():
                        logger.debug(f"文件下载成功: {local_file}")
                        target_file = target_folder / Path(src_fileitem.path).name
                        if target_file.exists():
                            # 文件已存在，添加时间戳
                            logger.warning(f"目标文件已存在，将重命名: {target_file}")
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            name_parts = target_file.stem, timestamp, target_file.suffix
                            new_name = f"{name_parts[0]}_{name_parts[1]}{name_parts[2]}"
                            target_file = target_folder / new_name
                        try:
                            shutil.move(str(local_file), str(target_file))
                            logger.info(f"转移文件成功: {src_fileitem.path} -> {target_file}")
                            transferred_count += 1
                            # 删除源文件
                            logger.debug(f"删除源文件: {src_fileitem.path}")
                            storage_chain.delete_file(src_fileitem)
                        except Exception as e:
                            logger.error(f"转移文件失败: {local_file} -> {target_file}, 错误: {str(e)}")
                            raise
                    else:
                        error_msg = f"无法下载文件: {src_fileitem.path}, 下载结果: {local_file}"
                        logger.error(error_msg)
                        raise Exception(error_msg)
            
            # 如果源文件是文件夹，遍历其中的所有文件
            elif src_fileitem.type == "dir":
                src_path = Path(src_fileitem.path)
                # 检查是否是本地文件夹
                if src_fileitem.storage == "local" and src_path.exists() and src_path.is_dir():
                    logger.debug(f"处理本地文件夹: {src_path}")
                    # 获取本地存储操作对象
                    from app.modules.filemanager.storages.local import LocalStorage
                    local_storage = LocalStorage()
                    
                    # 遍历文件夹中的所有文件（递归）
                    for root, dirs, files in os.walk(src_path):
                        for file in files:
                            src_file = Path(root) / file
                            # 只使用文件名，不保留文件夹结构
                            target_file = target_folder / file
                            
                            # 如果目标文件已存在，添加时间戳
                            if target_file.exists():
                                logger.warning(f"目标文件已存在，将重命名: {target_file}")
                                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                name_parts = target_file.stem, timestamp, target_file.suffix
                                new_name = f"{name_parts[0]}_{name_parts[1]}{name_parts[2]}"
                                target_file = target_folder / new_name
                            
                            try:
                                # 使用 LocalStorage 的 move 方法转移文件
                                from app.schemas import FileItem
                                src_fileitem_local = FileItem(
                                    storage="local",
                                    path=str(src_file),
                                    name=src_file.name,
                                    type="file"
                                )
                                if local_storage.move(src_fileitem_local, target_folder, target_file.name):
                                    logger.info(f"转移文件成功: {src_file} -> {target_file}")
                                    transferred_count += 1
                                else:
                                    # 如果使用存储操作对象失败，回退到 shutil.move
                                    shutil.move(str(src_file), str(target_file))
                                    logger.info(f"转移文件成功（回退方法）: {src_file} -> {target_file}")
                                    transferred_count += 1
                            except Exception as e:
                                logger.error(f"转移文件失败: {src_file} -> {target_file}, 错误: {str(e)}")
                                raise
                elif not src_path.exists():
                    logger.warning(f"本地文件夹不存在: {src_path}, 存储类型: {src_fileitem.storage}")
                    raise Exception(f"源文件夹不存在: {src_path}")
                else:
                    # 非本地文件夹，使用 StorageChain 列出文件
                    logger.info(f"处理非本地文件夹: {src_fileitem.path}, 存储类型: {src_fileitem.storage}")
                    logger.debug(f"开始列出文件夹中的文件: {src_fileitem.path}")
                    files = storage_chain.list_files(src_fileitem, recursion=True)
                    if files:
                        logger.debug(f"找到 {len(files)} 个文件/文件夹")
                        for file_item in files:
                            if file_item.type == "file":
                                logger.debug(f"处理文件: {file_item.path}")
                                # 下载文件到本地临时位置
                                local_file = storage_chain.download_file(file_item)
                                if local_file and local_file.exists():
                                    # 只使用文件名，不保留文件夹结构
                                    target_file = target_folder / Path(file_item.path).name
                                    if target_file.exists():
                                        # 文件已存在，添加时间戳
                                        logger.warning(f"目标文件已存在，将重命名: {target_file}")
                                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                                        name_parts = target_file.stem, timestamp, target_file.suffix
                                        new_name = f"{name_parts[0]}_{name_parts[1]}{name_parts[2]}"
                                        target_file = target_folder / new_name
                                    try:
                                        shutil.move(str(local_file), str(target_file))
                                        logger.info(f"转移文件成功: {file_item.path} -> {target_file}")
                                        transferred_count += 1
                                        # 删除源文件
                                        logger.debug(f"删除源文件: {file_item.path}")
                                        storage_chain.delete_file(file_item)
                                    except Exception as e:
                                        logger.error(f"转移文件失败: {local_file} -> {target_file}, 错误: {str(e)}")
                                        raise
                                else:
                                    logger.warning(f"无法下载文件: {file_item.path}, 下载结果: {local_file}")
                    else:
                        error_msg = f"无法列出文件夹中的文件: {src_fileitem.path}"
                        logger.error(error_msg)
                        raise Exception(error_msg)
            
            logger.info(f"转移文件成功: {src_fileitem.path} -> {target_dir}/{folder_name}, 转移文件数: {transferred_count}")
            return True, "", transferred_count
            
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            error_msg = f"转移文件失败: {src_fileitem.path} -> {target_dir}/{folder_name}, 错误: {str(e)}"
            logger.error(error_msg)
            logger.error(f"转移失败详细错误信息:\n{error_detail}")
            return False, error_msg, 0
    
    def _clean_failed_transfer_files(self) -> Dict[str, Any]:
        """
        清理转移失败的文件
            
        Returns:
            Dict: 清理结果
        """
        cleaned_files = []
        cleaned_dirs = []
        total_size = 0
        errors = []
        
        try:
            # 计算时间阈值
            cutoff_time = datetime.now() - timedelta(hours=float(self._failed_transfer_age_hours))
            cutoff_time_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")
            
            logger.info(f"查询转移失败的文件，时间阈值: {cutoff_time_str}")
            
            # 创建数据库会话
            db = SessionFactory()
            try:
                # 查询转移失败的记录
                failed_transfers = db.query(TransferHistory).filter(
                    and_(
                        TransferHistory.status == False,  # 转移失败
                        TransferHistory.date <= cutoff_time_str  # 超过指定时间
                    )
                ).all()
                
                logger.info(f"找到 {len(failed_transfers)} 条转移失败记录")
                
                for transfer in failed_transfers:
                    try:
                        # 处理源文件
                        if transfer.src_fileitem:
                            src_fileitem_data = transfer.src_fileitem
                            if isinstance(src_fileitem_data, dict):
                                from app.schemas import FileItem
                                src_fileitem = FileItem(**src_fileitem_data)
                                # 读取大小（如有）用于统计
                                file_size = int(src_fileitem_data.get("size", 0)) if isinstance(src_fileitem_data.get("size", 0), (int, float)) else 0
                                
                                if not self._dry_run:
                                    # 智能模式：根据失败原因决定删除或转移
                                    should_delete = False
                                    should_transfer = False
                                    
                                    if self._smart_mode:
                                        # 检查失败原因
                                        errmsg = transfer.errmsg or ""
                                        if "存在同名文件" in errmsg or "同名文件" in errmsg:
                                            should_delete = True
                                            logger.info(f"检测到同名文件失败原因，将删除: {src_fileitem.path}")
                                        else:
                                            should_transfer = True
                                            logger.info(f"其他失败原因，将转移: {src_fileitem.path}")
                                    else:
                                        # 非智能模式，使用配置的处理模式
                                        if self._transfer_mode == "transfer":
                                            should_transfer = True
                                        else:
                                            should_delete = True
                                    
                                    if should_transfer:
                                        # 转移模式
                                        if not self._transfer_target_dir:
                                            error_msg = f"转移模式已启用但未配置目标文件夹"
                                            logger.error(error_msg)
                                            errors.append(error_msg)
                                            continue
                                        
                                        # 生成文件夹名称（使用标题或路径名）
                                        folder_name = transfer.title or Path(src_fileitem.path).stem
                                        if not folder_name:
                                            folder_name = f"failed_transfer_{transfer.id}"
                                        
                                        # 转移文件
                                        logger.info(f"准备转移文件: {src_fileitem.path}, 标题: {transfer.title}, 失败原因: {transfer.errmsg}")
                                        success, error_msg, transferred_count = self._transfer_file_to_target(
                                            src_fileitem, self._transfer_target_dir, folder_name
                                        )
                                        
                                        if success:
                                            logger.info(f"转移转移失败文件成功: {src_fileitem.path} -> {self._transfer_target_dir}/{folder_name}, 转移文件数: {transferred_count}")
                                            
                                            # 检查原文件夹是否为空，如果为空则删除
                                            if src_fileitem.type == "dir":
                                                src_path = Path(src_fileitem.path)
                                                if src_path.exists() and src_path.is_dir():
                                                    try:
                                                        # 检查文件夹是否为空
                                                        if not any(src_path.iterdir()):
                                                            storage_chain = StorageChain()
                                                            storage_chain.delete_file(src_fileitem)
                                                            logger.info(f"删除空文件夹: {src_fileitem.path}")
                                                    except Exception as e:
                                                        logger.warning(f"检查或删除空文件夹失败: {src_fileitem.path}, {str(e)}")
                                            
                                            # 发送下载文件删除事件（因为文件已转移，相当于删除）
                                            eventmanager.send_event(
                                                EventType.DownloadFileDeleted,
                                                {
                                                    "src": transfer.src,
                                                    "hash": transfer.download_hash
                                                }
                                            )
                                            
                                            # 删除转移记录
                                            TransferHistory.delete(db, transfer.id)
                                            logger.info(f"已删除转移记录: ID={transfer.id}, 标题={transfer.title}")
                                            
                                            cleaned_files.append({
                                                "path": src_fileitem.path,
                                                "size": file_size,
                                                "modified": transfer.date,
                                                "title": transfer.title,
                                                "type": "failed_transfer",
                                                "transferred_to": f"{self._transfer_target_dir}/{folder_name}",
                                                "transferred_count": transferred_count
                                            })
                                            total_size += file_size
                                        else:
                                            logger.error(f"转移文件失败: {src_fileitem.path}")
                                            logger.error(f"转移失败详情: {error_msg}")
                                            logger.error(f"转移记录信息 - ID: {transfer.id}, 标题: {transfer.title}, 失败原因: {transfer.errmsg}")
                                            errors.append(error_msg)
                                    elif should_delete:
                                        # 删除模式
                                        storage_chain = StorageChain()
                                        success = storage_chain.delete_media_file(src_fileitem)

                                        if success:
                                            logger.info(f"删除转移失败文件: {src_fileitem.path}")

                                            # 发送下载文件删除事件
                                            eventmanager.send_event(
                                                EventType.DownloadFileDeleted,
                                                {
                                                    "src": transfer.src,
                                                    "hash": transfer.download_hash
                                                }
                                            )

                                            # 删除转移记录（仅在删除成功后）
                                            TransferHistory.delete(db, transfer.id)

                                            # 记录删除原因
                                            reason = None
                                            if self._smart_mode:
                                                errmsg = transfer.errmsg or ""
                                                if "存在同名文件" in errmsg or "同名文件" in errmsg:
                                                    reason = "duplicate"
                                            
                                            cleaned_files.append({
                                                "path": src_fileitem.path,
                                                "size": file_size,
                                                "modified": transfer.date,
                                                "title": transfer.title,
                                                "type": "failed_transfer",
                                                "reason": reason
                                            })
                                            total_size += file_size
                                        else:
                                            error_msg = f"删除转移失败文件失败: {src_fileitem.path}"
                                            logger.error(error_msg)
                                            errors.append(error_msg)
                                else:
                                    # 预览模式
                                    # 智能模式预览
                                    if self._smart_mode:
                                        errmsg = transfer.errmsg or ""
                                        if "存在同名文件" in errmsg or "同名文件" in errmsg:
                                            logger.info(f"[预览] 将删除转移失败文件（同名文件）: {src_fileitem.path}")
                                        else:
                                            folder_name = transfer.title or Path(src_fileitem.path).stem
                                            if not folder_name:
                                                folder_name = f"failed_transfer_{transfer.id}"
                                            logger.info(f"[预览] 将转移转移失败文件: {src_fileitem.path} -> {self._transfer_target_dir}/{folder_name}")
                                    elif self._transfer_mode == "transfer":
                                        folder_name = transfer.title or Path(src_fileitem.path).stem
                                        if not folder_name:
                                            folder_name = f"failed_transfer_{transfer.id}"
                                        logger.info(f"[预览] 将转移转移失败文件: {src_fileitem.path} -> {self._transfer_target_dir}/{folder_name}")
                                    else:
                                        logger.info(f"[预览] 将删除转移失败文件: {src_fileitem.path}")
                                    
                                    cleaned_files.append({
                                        "path": src_fileitem.path,
                                        "size": file_size,
                                        "modified": transfer.date,
                                        "title": transfer.title,
                                        "type": "failed_transfer"
                                    })
                                    total_size += file_size
                            else:
                                logger.warning(f"转移记录 {transfer.id} 的 src_fileitem 格式错误")
                        else:
                            logger.warning(f"转移记录 {transfer.id} 没有源文件信息")
                    except Exception as e:
                        error_msg = f"处理转移失败记录 {transfer.id} 时发生错误: {str(e)}"
                        logger.error(error_msg)
                        errors.append(error_msg)
                            
                # 提交数据库更改
                if not self._dry_run:
                    db.commit()
                    logger.info("数据库更改已提交")
                
            finally:
                db.close()
                            
        except Exception as e:
            error_msg = f"清理转移失败文件时发生错误: {str(e)}"
            logger.error(error_msg)
            errors.append(error_msg)
        
        return {
            "files": cleaned_files,
            "dirs": cleaned_dirs,
            "size": total_size,
            "errors": errors
        }


    def _format_size(self, size_bytes: int) -> str:
        """
        格式化文件大小
        
        Args:
            size_bytes: 字节数
            
        Returns:
            str: 格式化后的大小
        """
        if size_bytes == 0:
            return "0 B"
        
        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        value = float(size_bytes)
        while value >= 1024 and i < len(size_names) - 1:
            value /= 1024.0
            i += 1
        
        return f"{value:.2f} {size_names[i]}"

    def _send_clean_notification(self, cleaned_files: List[Dict], cleaned_dirs: List[Dict], 
                                total_size: int, errors: List[str]):
        """
        发送转移失败文件清理通知
        
        Args:
            cleaned_files: 已清理的文件列表
            cleaned_dirs: 已清理的目录列表
            total_size: 总大小
            errors: 错误列表
        """
        try:
            # 统计转移和删除的文件
            transferred_files = [f for f in cleaned_files if f.get("transferred_to")]
            deleted_files = [f for f in cleaned_files if not f.get("transferred_to")]
            
            # 构建通知消息
            if self._smart_mode:
                mode_text = "智能模式"
            else:
                mode_text = "转移" if self._transfer_mode == "transfer" else "删除"
            message = f"转移失败文件清理任务完成\n\n"
            message += f"处理模式: {mode_text}\n"
            message += f"处理文件: {len(cleaned_files)} 个\n"
            
            if transferred_files:
                message += f"转移文件: {len(transferred_files)} 个\n"
                total_transferred = sum(f.get("transferred_count", 0) for f in transferred_files)
                if total_transferred > 0:
                    message += f"转移文件数: {total_transferred} 个\n"
            
            if deleted_files:
                message += f"删除文件: {len(deleted_files)} 个\n"
                if self._smart_mode:
                    message += f"（其中同名文件: {len([f for f in deleted_files if f.get('reason') == 'duplicate'])} 个）\n"
            
            message += f"释放空间: {self._format_size(total_size)}\n"
            
            if (self._transfer_mode == "transfer" or self._smart_mode) and transferred_files:
                message += f"\n转移目标: {self._transfer_target_dir}"
            
            if errors:
                message += f"\n错误: {len(errors)} 个"
            
            if self._dry_run:
                message = f"[预览模式] {message}"
            
            # 发送通知
            notification = Notification(
                channel="FileSweeper",
                mtype=NotificationType.Manual,
                title="转移失败文件清理完成",
                text=message,
                image=""
            )
            
            # 这里需要根据实际的通知系统来发送
            # 由于没有看到具体的通知发送方法，这里只是示例
            logger.info(f"发送清理通知: {message}")
            
        except Exception as e:
            logger.error(f"发送清理通知时发生错误: {str(e)}")

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        """
        安全地将值转换为float，失败则返回默认值
        """
        try:
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return float(value)
            s = str(value).strip()
            if s == "":
                return default
            return float(s)
        except Exception:
            return default

    def run_service(self):
        """运行服务"""
        if not self._enabled:
            return
        
        logger.info("FileSweeper服务正在运行")
        if self._cron:
            logger.info(f"定时任务已注册: {self._cron}")
        else:
            logger.warning("未配置定时任务表达式")
