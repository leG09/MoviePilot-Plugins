from typing import Any, Dict, List, Optional, Tuple

from app.core.event import eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.http import RequestUtils


class TransferWebhook(_PluginBase):
    # 插件名称
    plugin_name = "转移完成Webhook"
    # 插件描述
    plugin_desc = "文件转移成功后立即向指定Webhook推送目标路径，支持路径包含过滤。"
    # 插件图标
    plugin_icon = "webhook.png"
    # 插件版本
    plugin_version = "1.2"
    # 插件作者
    plugin_author = "leGO9"
    # 作者主页
    author_url = "https://github.com/leG09"
    # 插件配置项ID前缀
    plugin_config_prefix = "transferwebhook_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _webhook_url = ""
    _webhook_token = ""
    _path_mappings = ""
    _include_paths = ""
    _timeout = 20

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled", False)
            self._webhook_url = (config.get("webhook_url") or "").strip()
            self._webhook_token = (config.get("webhook_token") or "").strip()
            self._path_mappings = config.get("path_mappings") or ""
            self._include_paths = config.get("include_paths") or ""
            self._timeout = self._to_int(config.get("timeout"), 20)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 6
                                },
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件"
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
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "include_paths",
                                            "label": "包含路径过滤",
                                            "rows": 3,
                                            "placeholder": "/国漫\n# 每行一个包含关键字；留空表示全部发送"
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
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webhook_url",
                                            "label": "Webhook地址",
                                            "placeholder": "http://127.0.0.1:18084/api/storage/providers/gdrive-c38cd822/webhook"
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
                                "props": {
                                    "cols": 12,
                                    "md": 8
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "webhook_token",
                                            "label": "Webhook Token",
                                            "placeholder": "发送到 X-Webhook-Token 请求头"
                                        }
                                    }
                                ]
                            },
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                    "md": 4
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "请求超时（秒）",
                                            "type": "number"
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
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射",
                                            "rows": 5,
                                            "placeholder": "/gd1=>\n/gd2=>/media/gdrive\n# 每行一个规则，命中源前缀后替换为目标前缀"
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
                                "props": {
                                    "cols": 12
                                },
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "文件转移成功后发送 POST JSON：{\"path\": \"映射后的目标路径\"}。路径映射格式为 源前缀=>目标前缀；目标前缀为空表示去掉源前缀，例如 /gd1=> 会把 /gd1/国漫/a.mp4 转为 /国漫/a.mp4。包含路径过滤可配置 /国漫，只有原始路径或映射后路径包含该内容才发送。"
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
            "webhook_url": "",
            "webhook_token": "",
            "path_mappings": "/gd1=>",
            "include_paths": "",
            "timeout": 20
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.TransferComplete)
    def send_transfer_webhook(self, event):
        """
        转移成功后向第三方Webhook推送转移后的文件路径。
        """
        if not self._enabled or not self._webhook_url:
            return

        path = self.__get_target_path(event)
        if not path:
            logger.warning("转移完成Webhook未获取到目标文件路径，跳过发送")
            return

        mapped_path = self.__map_path(path)
        if not self.__match_include_paths(path, mapped_path):
            logger.info(f"转移完成Webhook路径未命中包含过滤，跳过发送：{path}")
            return

        headers = {
            "Content-Type": "application/json"
        }
        if self._webhook_token:
            headers["X-Webhook-Token"] = self._webhook_token

        payload = {"path": mapped_path}
        logger.info(f"转移完成Webhook发送JSON：{payload}")

        try:
            ret = RequestUtils(headers=headers, timeout=self._timeout).post_res(
                self._webhook_url,
                json=payload
            )
            if ret:
                logger.info(f"转移完成Webhook发送成功：{mapped_path} -> {self._webhook_url}")
            elif ret is not None:
                logger.error(f"转移完成Webhook发送失败，状态码：{ret.status_code}，返回信息：{ret.text} {ret.reason}")
            else:
                logger.error(f"转移完成Webhook发送失败，未获取到返回信息：{mapped_path}")
        except Exception as e:
            logger.error(f"转移完成Webhook发送异常：{e}")

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            ret = int(value)
            return ret if ret > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __read_attr(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    def __get_target_path(self, event) -> Optional[str]:
        if not event or not event.event_data:
            return None

        event_data = event.event_data
        transferinfo = self.__read_attr(event_data, "transferinfo")
        if not transferinfo:
            return None

        target_item = self.__read_attr(transferinfo, "target_item")
        path = self.__read_attr(target_item, "path")
        if path:
            return str(path)

        target_diritem = self.__read_attr(transferinfo, "target_diritem")
        path = self.__read_attr(target_diritem, "path")
        if path:
            return str(path)

        return None

    def __map_path(self, path: str) -> str:
        clean_path = self.__normalize_path(path)
        for source, target in self.__parse_mappings():
            if clean_path == source or clean_path.startswith(f"{source}/"):
                suffix = clean_path[len(source):]
                if not target:
                    return self.__normalize_path(suffix or "/")
                return self.__normalize_path(f"{target.rstrip('/')}{suffix}")
        return clean_path

    def __parse_mappings(self) -> List[Tuple[str, str]]:
        mappings = []
        for line in (self._path_mappings or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if "=>" in line:
                source, target = line.split("=>", 1)
            elif "=" in line:
                source, target = line.split("=", 1)
            else:
                logger.warning(f"转移完成Webhook路径映射格式错误，已跳过：{line}")
                continue

            source = self.__normalize_path(source.strip()).rstrip("/")
            target = self.__normalize_path(target.strip()).rstrip("/") if target.strip() else ""
            if not source:
                logger.warning(f"转移完成Webhook路径映射源前缀为空，已跳过：{line}")
                continue
            mappings.append((source, target))
        return mappings

    def __match_include_paths(self, original_path: str, mapped_path: str) -> bool:
        include_paths = self.__parse_include_paths()
        if not include_paths:
            return True

        original_path = self.__normalize_path(original_path)
        mapped_path = self.__normalize_path(mapped_path)
        for include_path in include_paths:
            if include_path in original_path or include_path in mapped_path:
                return True
        return False

    def __parse_include_paths(self) -> List[str]:
        include_paths = []
        for line in (self._include_paths or "").replace(",", "\n").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            include_paths.append(self.__normalize_path(line))
        return include_paths

    @staticmethod
    def __normalize_path(path: str) -> str:
        if not path:
            return ""
        normalized = str(path).replace("\\", "/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")
        if normalized != "/" and normalized.endswith("/"):
            normalized = normalized.rstrip("/")
        if normalized and not normalized.startswith("/"):
            normalized = f"/{normalized}"
        return normalized

    def stop_service(self):
        """
        退出插件
        """
        pass
