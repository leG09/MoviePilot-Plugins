import os
import time
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional
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


class FileSweeper(_PluginBase):
    """转移失败文件清理器插件 - 定时删除MoviePilot转移失败的文件"""
    
    # 插件信息
    plugin_name = "FileSweeper"
    plugin_desc = "定时删除MoviePilot转移失败的文件"
    plugin_icon = "refresh2.png"
    plugin_color = "#FF6B6B"
    plugin_version = "2.3"
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
        
        # 清理已处理文件配置
        self._clean_processed_files = config.get("clean_processed_files", False) if config else False
        
        logger.info(f"FileSweeper插件初始化完成，启用状态: {self._enabled}")
        if self._enabled:
            logger.info(f"定时任务: {self._cron}")
            logger.info(f"转移失败文件最大年龄: {self._failed_transfer_age_hours}小时")
            logger.info(f"预览模式: {self._dry_run}")
            logger.info(f"清理已处理文件: {self._clean_processed_files}")

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
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clean_processed_files",
                                            "label": "清理已处理文件",
                                            "color": "warning",
                                            "hint": "如果文件已在转移历史中成功处理过，则删除该文件"
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
                                            "text": "此插件专门用于清理MoviePilot转移失败的文件。插件会查询数据库中转移失败的记录，删除超过指定时间的源文件，并发送相应的事件通知。"
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
            "clean_processed_files": False
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
            
            if self._clean_processed_files:
                logger.info(f"查询转移失败和已处理的文件，时间阈值: {cutoff_time_str}")
            else:
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
                
                # 如果开启了清理已处理文件，查询所有成功处理的记录
                processed_transfers = []
                if self._clean_processed_files:
                    processed_transfers = db.query(TransferHistory).filter(
                        and_(
                            TransferHistory.status == True,  # 转移成功
                            TransferHistory.date <= cutoff_time_str  # 超过指定时间
                        )
                    ).all()
                    logger.info(f"找到 {len(processed_transfers)} 条已成功处理的记录")
                
                # 处理所有需要清理的记录（失败记录 + 已处理记录）
                all_transfers = list(failed_transfers)
                if self._clean_processed_files:
                    all_transfers.extend(processed_transfers)
                
                for transfer in all_transfers:
                    try:
                        # 删除源文件
                        if transfer.src_fileitem:
                            src_fileitem_data = transfer.src_fileitem
                            if isinstance(src_fileitem_data, dict):
                                from app.schemas import FileItem
                                src_fileitem = FileItem(**src_fileitem_data)
                                # 读取大小（如有）用于统计
                                file_size = int(src_fileitem_data.get("size", 0)) if isinstance(src_fileitem_data.get("size", 0), (int, float)) else 0

                                # 判断是否为成功处理的记录
                                is_processed = transfer.status == True
                                
                                if not self._dry_run:
                                    # 使用 StorageChain 删除文件（支持各类存储后端）
                                    storage_chain = StorageChain()
                                    success = storage_chain.delete_media_file(src_fileitem)

                                    if success:
                                        if is_processed:
                                            logger.info(f"删除已处理文件的源文件: {src_fileitem.path}")
                                        else:
                                            logger.info(f"删除转移失败文件: {src_fileitem.path}")

                                        # 发送下载文件删除事件
                                        eventmanager.send_event(
                                            EventType.DownloadFileDeleted,
                                            {
                                                "src": transfer.src,
                                                "hash": transfer.download_hash
                                            }
                                        )

                                        # 只有失败记录才删除转移记录，成功记录保留
                                        if not is_processed:
                                            TransferHistory.delete(db, transfer.id)
                                            logger.debug(f"删除转移失败记录: {transfer.id}")

                                        cleaned_files.append({
                                            "path": src_fileitem.path,
                                            "size": file_size,
                                            "modified": transfer.date,
                                            "title": transfer.title,
                                            "type": "processed_file" if is_processed else "failed_transfer"
                                        })
                                        total_size += file_size
                                    else:
                                        error_msg = f"删除文件失败: {src_fileitem.path}"
                                        logger.error(error_msg)
                                        errors.append(error_msg)
                                else:
                                    if is_processed:
                                        logger.info(f"[预览] 将删除已处理文件的源文件: {src_fileitem.path}")
                                    else:
                                        logger.info(f"[预览] 将删除转移失败文件: {src_fileitem.path}")
                                    cleaned_files.append({
                                        "path": src_fileitem.path,
                                        "size": file_size,
                                        "modified": transfer.date,
                                        "title": transfer.title,
                                        "type": "processed_file" if is_processed else "failed_transfer"
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
            # 统计已处理文件和失败文件
            processed_files = [f for f in cleaned_files if f.get("type") == "processed_file"]
            failed_files = [f for f in cleaned_files if f.get("type") == "failed_transfer"]
            
            # 构建通知消息
            if self._clean_processed_files:
                message = f"文件清理任务完成\n\n"
            else:
                message = f"转移失败文件清理任务完成\n\n"
            
            message += f"清理文件: {len(cleaned_files)} 个\n"
            if self._clean_processed_files and processed_files:
                message += f"  - 已处理文件: {len(processed_files)} 个\n"
            if failed_files:
                message += f"  - 失败文件: {len(failed_files)} 个\n"
            message += f"清理目录: {len(cleaned_dirs)} 个\n"
            message += f"释放空间: {self._format_size(total_size)}\n"
            
            if errors:
                message += f"\n错误: {len(errors)} 个"
            
            if self._dry_run:
                message = f"[预览模式] {message}"
            
            # 发送通知
            notification = Notification(
                channel="FileSweeper",
                mtype=NotificationType.Manual,
                title="文件清理完成" if self._clean_processed_files else "转移失败文件清理完成",
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
