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

from app.plugins import _PluginBase
from app.core.config import settings
from app.core.security import verify_apikey
from app.schemas import Notification, NotificationType, ContentType
from app.schemas.event import EventType
from app.log import logger


class FileSweeper(_PluginBase):
    """文件清理器插件 - 定时删除指定目录超过N小时的文件夹和文件"""
    
    # 插件信息
    plugin_name = "FileSweeper"
    plugin_desc = "定时删除指定目录超过N小时的文件夹和文件，支持多种过滤条件"
    plugin_icon = "refresh2.png"
    plugin_color = "#FF6B6B"
    plugin_version = "1.0"
    plugin_author = "leGO9"
    author_url = "https://github.com/leG09"
    plugin_config_prefix = "filesweeper"
    
    def init_plugin(self, config: dict = None):
        """初始化插件"""
        self._enabled = config.get("enabled", False) if config else False
        self._cron = config.get("cron", "0 2 * * *") if config else "0 2 * * *"  # 默认每天凌晨2点执行
        self._clean_directories = config.get("clean_directories", "") if config else ""
        self._max_age_hours = config.get("max_age_hours", 24) if config else 24
        self._delete_empty_dirs = config.get("delete_empty_dirs", True) if config else True
        self._delete_files = config.get("delete_files", True) if config else True
        self._file_extensions = config.get("file_extensions", "") if config else ""
        self._exclude_patterns = config.get("exclude_patterns", "") if config else ""
        self._dry_run = config.get("dry_run", False) if config else False
        self._send_notification = config.get("send_notification", True) if config else True
        self._min_size_mb = config.get("min_size_mb", 0) if config else 0
        self._max_size_mb = config.get("max_size_mb", 0) if config else 0
        
        logger.info(f"FileSweeper插件初始化完成，启用状态: {self._enabled}")
        if self._enabled:
            logger.info(f"清理目录: {self._clean_directories}")
            logger.info(f"最大年龄: {self._max_age_hours}小时")
            logger.info(f"定时任务: {self._cron}")

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
                "desc": "手动执行文件清理",
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
                "summary": "手动执行清理",
                "description": "手动触发自动清理任务"
            },
            {
                "path": "/preview_clean",
                "endpoint": self.preview_clean,
                "methods": ["POST"],
                "summary": "预览清理",
                "description": "预览将要清理的文件和目录，不实际删除"
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
                                            "model": "max_age_hours",
                                            "label": "最大年龄（小时）",
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
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "clean_directories",
                                            "label": "清理目录列表",
                                            "placeholder": "每行一个目录路径，例如：\n/tmp/downloads\n/var/cache\n/media/temp",
                                            "rows": 4,
                                            "hint": "每行一个目录路径，支持绝对路径"
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
                                            "model": "delete_files",
                                            "label": "删除文件",
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
                                            "model": "delete_empty_dirs",
                                            "label": "删除空目录",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "file_extensions",
                                            "label": "文件扩展名过滤",
                                            "placeholder": ".tmp,.log,.cache",
                                            "hint": "逗号分隔的文件扩展名，留空表示所有文件"
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
                                            "model": "exclude_patterns",
                                            "label": "排除模式",
                                            "placeholder": "*.important,*.backup",
                                            "hint": "逗号分隔的排除模式，支持通配符"
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "min_size_mb",
                                            "label": "最小文件大小（MB）",
                                            "type": "number",
                                            "placeholder": "0"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_size_mb",
                                            "label": "最大文件大小（MB）",
                                            "type": "number",
                                            "placeholder": "0（无限制）"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                            "text": "警告：此插件会删除文件，请谨慎配置。建议先在预览模式下测试。"
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
            "clean_directories": "",
            "max_age_hours": 24,
            "delete_empty_dirs": True,
            "delete_files": True,
            "file_extensions": "",
            "exclude_patterns": "",
            "dry_run": False,
            "send_notification": True,
            "min_size_mb": 0,
            "max_size_mb": 0
        }

    def get_page(self) -> list:
        """获取插件页面"""
        pass

    def stop_service(self):
        """停止插件"""
        logger.info("FileSweeper插件已停止")

    def manual_clean(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        手动执行清理任务
        
        Args:
            request_data: 请求数据
            apikey: API密钥
            
        Returns:
            Dict: 清理结果
        """
        try:
            logger.info("开始手动执行清理任务")
            
            # 执行清理
            result = self._execute_clean()
            
            return {
                "success": True,
                "message": "手动清理任务执行完成",
                "result": result
            }
            
        except Exception as e:
            error_msg = f"手动清理任务执行失败：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def preview_clean(self, request_data: Dict[str, Any], apikey: Annotated[str, verify_apikey]) -> Dict[str, Any]:
        """
        预览清理任务
        
        Args:
            request_data: 请求数据
            apikey: API密钥
            
        Returns:
            Dict: 预览结果
        """
        try:
            logger.info("开始预览清理任务")
            
            # 临时启用预览模式
            original_dry_run = self._dry_run
            self._dry_run = True
            
            # 执行清理
            result = self._execute_clean()
            
            # 恢复原始设置
            self._dry_run = original_dry_run
            
            return {
                "success": True,
                "message": "预览清理任务完成",
                "result": result
            }
            
        except Exception as e:
            error_msg = f"预览清理任务失败：{str(e)}"
            logger.error(error_msg)
            return {"success": False, "message": error_msg}

    def _execute_clean(self) -> Dict[str, Any]:
        """
        执行清理任务
        
        Returns:
            Dict: 清理结果
        """
        try:
            if not self._clean_directories:
                return {
                    "success": False,
                    "message": "未配置清理目录",
                    "cleaned_files": [],
                    "cleaned_dirs": [],
                    "total_size": 0
                }
            
            # 解析清理目录
            directories = [d.strip() for d in self._clean_directories.split('\n') if d.strip()]
            
            # 解析文件扩展名
            extensions = [ext.strip().lower() for ext in self._file_extensions.split(',') if ext.strip()] if self._file_extensions else []
            
            # 解析排除模式
            exclude_patterns = [pattern.strip() for pattern in self._exclude_patterns.split(',') if pattern.strip()] if self._exclude_patterns else []
            
            # 计算时间阈值
            cutoff_time = datetime.now() - timedelta(hours=self._max_age_hours)
            
            cleaned_files = []
            cleaned_dirs = []
            total_size = 0
            errors = []
            
            logger.info(f"开始清理任务，时间阈值: {cutoff_time}")
            logger.info(f"清理目录: {directories}")
            logger.info(f"文件扩展名过滤: {extensions}")
            logger.info(f"排除模式: {exclude_patterns}")
            logger.info(f"预览模式: {self._dry_run}")
            
            for directory in directories:
                try:
                    if not os.path.exists(directory):
                        logger.warning(f"目录不存在: {directory}")
                        continue
                    
                    logger.info(f"开始清理目录: {directory}")
                    
                    # 清理文件和目录
                    dir_result = self._clean_directory(
                        directory, cutoff_time, extensions, exclude_patterns
                    )
                    
                    cleaned_files.extend(dir_result["files"])
                    cleaned_dirs.extend(dir_result["dirs"])
                    total_size += dir_result["size"]
                    errors.extend(dir_result["errors"])
                    
                except Exception as e:
                    error_msg = f"清理目录 {directory} 时发生错误: {str(e)}"
                    logger.error(error_msg)
                    errors.append(error_msg)
            
            # 发送通知
            if self._send_notification and (cleaned_files or cleaned_dirs):
                self._send_clean_notification(cleaned_files, cleaned_dirs, total_size, errors)
            
            result = {
                "success": len(errors) == 0,
                "message": f"清理完成 - 文件: {len(cleaned_files)}, 目录: {len(cleaned_dirs)}, 总大小: {self._format_size(total_size)}",
                "cleaned_files": cleaned_files,
                "cleaned_dirs": cleaned_dirs,
                "total_size": total_size,
                "errors": errors,
                "dry_run": self._dry_run
            }
            
            logger.info(f"清理任务完成: {result['message']}")
            return result
            
        except Exception as e:
            error_msg = f"执行清理任务时发生错误: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "cleaned_files": [],
                "cleaned_dirs": [],
                "total_size": 0,
                "errors": [error_msg]
            }

    def _clean_directory(self, directory: str, cutoff_time: datetime, 
                        extensions: List[str], exclude_patterns: List[str]) -> Dict[str, Any]:
        """
        清理单个目录
        
        Args:
            directory: 目录路径
            cutoff_time: 时间阈值
            extensions: 文件扩展名列表
            exclude_patterns: 排除模式列表
            
        Returns:
            Dict: 清理结果
        """
        cleaned_files = []
        cleaned_dirs = []
        total_size = 0
        errors = []
        
        try:
            # 遍历目录
            for root, dirs, files in os.walk(directory, topdown=False):
                # 处理文件
                if self._delete_files:
                    for file in files:
                        file_path = os.path.join(root, file)
                        try:
                            # 检查文件是否应该被删除
                            if self._should_delete_file(file_path, cutoff_time, extensions, exclude_patterns):
                                file_size = os.path.getsize(file_path)
                                
                                if not self._dry_run:
                                    os.remove(file_path)
                                    logger.info(f"删除文件: {file_path}")
                                else:
                                    logger.info(f"[预览] 将删除文件: {file_path}")
                                
                                cleaned_files.append({
                                    "path": file_path,
                                    "size": file_size,
                                    "modified": datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
                                })
                                total_size += file_size
                                
                        except Exception as e:
                            error_msg = f"删除文件 {file_path} 时发生错误: {str(e)}"
                            logger.error(error_msg)
                            errors.append(error_msg)
                
                # 处理空目录
                if self._delete_empty_dirs:
                    for dir_name in dirs:
                        dir_path = os.path.join(root, dir_name)
                        try:
                            # 检查目录是否为空且超过时间阈值
                            if self._should_delete_directory(dir_path, cutoff_time):
                                if not self._dry_run:
                                    shutil.rmtree(dir_path)
                                    logger.info(f"删除目录: {dir_path}")
                                else:
                                    logger.info(f"[预览] 将删除目录: {dir_path}")
                                
                                cleaned_dirs.append({
                                    "path": dir_path,
                                    "modified": datetime.fromtimestamp(os.path.getmtime(dir_path)).isoformat()
                                })
                                
                        except Exception as e:
                            error_msg = f"删除目录 {dir_path} 时发生错误: {str(e)}"
                            logger.error(error_msg)
                            errors.append(error_msg)
                            
        except Exception as e:
            error_msg = f"遍历目录 {directory} 时发生错误: {str(e)}"
            logger.error(error_msg)
            errors.append(error_msg)
        
        return {
            "files": cleaned_files,
            "dirs": cleaned_dirs,
            "size": total_size,
            "errors": errors
        }

    def _should_delete_file(self, file_path: str, cutoff_time: datetime, 
                           extensions: List[str], exclude_patterns: List[str]) -> bool:
        """
        判断文件是否应该被删除
        
        Args:
            file_path: 文件路径
            cutoff_time: 时间阈值
            extensions: 文件扩展名列表
            exclude_patterns: 排除模式列表
            
        Returns:
            bool: 是否应该删除
        """
        try:
            # 检查文件是否存在
            if not os.path.exists(file_path):
                return False
            
            # 检查文件修改时间
            file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
            if file_mtime > cutoff_time:
                return False
            
            # 检查文件扩展名
            if extensions:
                file_ext = os.path.splitext(file_path)[1].lower()
                if file_ext not in extensions:
                    return False
            
            # 检查排除模式
            if exclude_patterns:
                import fnmatch
                for pattern in exclude_patterns:
                    if fnmatch.fnmatch(os.path.basename(file_path), pattern):
                        return False
            
            # 检查文件大小
            file_size = os.path.getsize(file_path)
            if self._min_size_mb > 0 and file_size < self._min_size_mb * 1024 * 1024:
                return False
            if self._max_size_mb > 0 and file_size > self._max_size_mb * 1024 * 1024:
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"检查文件 {file_path} 时发生错误: {str(e)}")
            return False

    def _should_delete_directory(self, dir_path: str, cutoff_time: datetime) -> bool:
        """
        判断目录是否应该被删除
        
        Args:
            dir_path: 目录路径
            cutoff_time: 时间阈值
            
        Returns:
            bool: 是否应该删除
        """
        try:
            # 检查目录是否存在
            if not os.path.exists(dir_path):
                return False
            
            # 检查目录是否为空
            if os.listdir(dir_path):
                return False
            
            # 检查目录修改时间
            dir_mtime = datetime.fromtimestamp(os.path.getmtime(dir_path))
            if dir_mtime > cutoff_time:
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"检查目录 {dir_path} 时发生错误: {str(e)}")
            return False

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
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024.0
            i += 1
        
        return f"{size_bytes:.2f} {size_names[i]}"

    def _send_clean_notification(self, cleaned_files: List[Dict], cleaned_dirs: List[Dict], 
                                total_size: int, errors: List[str]):
        """
        发送清理通知
        
        Args:
            cleaned_files: 已清理的文件列表
            cleaned_dirs: 已清理的目录列表
            total_size: 总大小
            errors: 错误列表
        """
        try:
            # 构建通知消息
            message = f"自动清理任务完成\n\n"
            message += f"清理文件: {len(cleaned_files)} 个\n"
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
                title="文件清理任务完成",
                text=message,
                image=""
            )
            
            # 这里需要根据实际的通知系统来发送
            # 由于没有看到具体的通知发送方法，这里只是示例
            logger.info(f"发送清理通知: {message}")
            
        except Exception as e:
            logger.error(f"发送清理通知时发生错误: {str(e)}")

    def run_service(self):
        """运行服务"""
        if not self._enabled:
            return
        
        # 这里可以添加定时任务逻辑
        # 由于MoviePilot的定时任务机制可能不同，这里只是示例
        logger.info("FileSweeper服务正在运行")
