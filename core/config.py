from __future__ import annotations

import json
import zoneinfo
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_plugin_path,
)


class ConfigNode:
    """
    配置节点, 把 dict 变成强类型对象。

    规则：
    - schema 来自子类类型注解
    - 声明字段：读写，写回底层 dict
    - 未声明字段和下划线字段：仅挂载属性，不写回
    - 支持 ConfigNode 多层嵌套（lazy + cache）
    """

    _SCHEMA_CACHE: dict[type, dict[str, type]] = {}
    _FIELDS_CACHE: dict[type, set[str]] = {}

    @classmethod
    def _schema(cls) -> dict[str, type]:
        return cls._SCHEMA_CACHE.setdefault(cls, get_type_hints(cls))

    @classmethod
    def _fields(cls) -> set[str]:
        return cls._FIELDS_CACHE.setdefault(
            cls,
            {k for k in cls._schema() if not k.startswith("_")},
        )

    @staticmethod
    def _is_optional(tp: type) -> bool:
        if get_origin(tp) in (Union, UnionType):
            return type(None) in get_args(tp)
        return False

    def __init__(self, data: MutableMapping[str, Any]):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_children", {})
        for key, tp in self._schema().items():
            if key.startswith("_"):
                continue
            if key in data:
                continue
            if hasattr(self.__class__, key):
                continue
            if self._is_optional(tp):
                continue
            logger.warning(f"[config:{self.__class__.__name__}] 缺少字段: {key}")

    def __getattr__(self, key: str) -> Any:
        if key in self._fields():
            value = self._data.get(key)
            tp = self._schema().get(key)

            if isinstance(tp, type) and issubclass(tp, ConfigNode):
                children: dict[str, ConfigNode] = self.__dict__["_children"]
                if key not in children:
                    if not isinstance(value, MutableMapping):
                        raise TypeError(
                            f"[config:{self.__class__.__name__}] "
                            f"字段 {key} 期望 dict，实际是 {type(value).__name__}"
                        )
                    children[key] = tp(value)
                return children[key]

            return value

        if key in self.__dict__:
            return self.__dict__[key]

        raise AttributeError(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in self._fields():
            self._data[key] = value
            return
        object.__setattr__(self, key, value)

    def raw_data(self) -> Mapping[str, Any]:
        """
        底层配置 dict 的只读视图
        """
        return MappingProxyType(self._data)

    def save_config(self) -> None:
        """
        保存配置到磁盘（仅允许在根节点调用）
        """
        if not isinstance(self._data, AstrBotConfig):
            raise RuntimeError(
                f"{self.__class__.__name__}.save_config() 只能在根配置节点上调用"
            )
        self._data.save_config()


class ConfigNodeContainer:
    """
    配置节点容器, 把 list 的 dict 变成 dict 的对象集合。

    - nodes: list[dict[str, Any]]
    - item_cls 用于包装 dict 成强类型节点
    - key_name 作为属性名访问, 默认为 "__template_key"
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        item_cls: type[ConfigNode],
        key_name="__template_key",
    ):
        self._nodes: dict[str, ConfigNode] = {}
        for node in nodes:
            key = node.get(key_name)
            if not key:
                logger.warning(f"[node] 缺少 {key_name}，已跳过")
                continue
            if key in self._nodes:
                logger.warning(f"[node] {key} 重复配置，已覆盖")
            self._nodes[key] = item_cls(node)

    def __getattr__(self, name: str) -> ConfigNode:
        if name in self._nodes:
            return self._nodes[name]
        raise AttributeError(name)

    def __iter__(self):
        return iter(self._nodes.values())

    def keys(self):
        return self._nodes.keys()

    def items(self):
        return self._nodes.items()


# ================ 插件自定义配置 ==================


class ParserItem(ConfigNode):
    __template_key: str
    enable: bool
    use_proxy: bool
    cookies: str | None
    show_body_text: bool | None
    show_work_content: bool | None
    text_max_length: int | None
    video_send_mode: str | None
    video_codec_list: list | None
    video_quality: str | None
    video_codec: str | None
    video_format: str | None
    metube_url: str | None
    wait_timeout: int | None
    delete_after_fetch: bool | None
    nsfw: str | None
    max_page: int | None
    send_blue_links: bool | None
    qzone_credential_source: str | None
    snowluma_http_url: str | None
    snowluma_access_token: str | None
    snowluma_credential_cache_seconds: int | None
    # 抖音浏览器兜底（绕过 Argus 风控）
    browser_fallback: bool | None
    browser_headless: bool | None
    browser_channel: str | None
    browser_idle_timeout: int | None

    @property
    def name(self) -> str:
        return self._data.get("__template_key")


class ParserConfig(ConfigNodeContainer):
    acfun: ParserItem
    allcpp: ParserItem
    bilibili: ParserItem
    douyin: ParserItem
    instagram: ParserItem
    kuaishou: ParserItem
    metube: ParserItem
    ncm: ParserItem
    nga: ParserItem
    tiktok: ParserItem
    twitter: ParserItem
    weibo: ParserItem
    xiaoheihe: ParserItem
    zhihu: ParserItem
    xhs: ParserItem
    youtube: ParserItem
    iwara: ParserItem
    shipinhao: ParserItem
    pixiv: ParserItem
    qzone: ParserItem

    def __init__(self, nodes: list[dict[str, Any]]):
        super().__init__(nodes, item_cls=ParserItem)

    def platforms(self) -> list[str]:
        return list(self._nodes.keys())

    def enabled_platforms(self) -> list[str]:
        return [k for k, v in self._nodes.items() if getattr(v, "enable", True)]


class PluginConfig(ConfigNode):
    whitelist: list[str]
    blacklist: list[str]

    arbiter: bool
    require_at_in_group: bool
    enable_reply_parse: bool
    debounce_interval: int

    source_max_size: int
    source_max_minute: int

    audio_send_mode: str
    voice_max_second: int
    all_works_limit: int
    single_heavy_render_card: bool
    forward_threshold: int

    shortlink_api: str
    shortlink_password: str

    show_download_fail_tip: bool
    download_timeout: int
    download_retry_times: int
    common_timeout: int

    proxy: str | None

    clean_cron: str

    album_enabled: bool
    album_name: str
    album_title_comment: bool
    album_base64_fallback: bool
    album_max_media: int

    parsers_template: list[dict[str, Any]]

    _plugin_name = "万能解析"
    """插件包名，需与 plugins 目录下的文件夹名一致"""

    _legacy_plugin_name = "astrbot_plugin_parser_live"
    """历史包名，用于兼容旧安装的数据目录"""

    def __init__(self, config: AstrBotConfig, context: Context):
        super().__init__(config)
        self.context = context
        self.admins_id = self.context.get_config().get("admins_id", [])

        # ---------- 内置配置 ----------
        self.emoji_cdn = "https://cdn.jsdelivr.net/npm/emoji-datasource-facebook@14.0.0/img/facebook/64/"
        self.emoji_style = "FACEBOOK"  # 可选：APPLE、FACEBOOK、GOOGLE、TWITTER

        # ---------- 派生字段 ----------
        self.proxy = self.proxy or None
        self.max_duration = self.source_max_minute * 60
        self.max_size = self.source_max_size * 1024 * 1024

        # 旧配置文件里没有该字段，缺省或非法时按「自动」处理
        if self.audio_send_mode not in ("auto", "voice", "file"):
            self.audio_send_mode = "auto"
        self.voice_max_second = self.voice_max_second or 60
        self.all_works_limit = self.all_works_limit or 50

        # 群相册：相册名留空时按「少女集」处理，与命令名保持一致
        self.album_name = (self.album_name or "").strip() or "少女集"
        self.album_max_media = self.album_max_media or 20

        # 短链服务(Url-Shorten-Worker)：地址与密码都填了才启用
        self.shortlink_api = (self.shortlink_api or "").strip()
        self.shortlink_password = (self.shortlink_password or "").strip()

        tz = context.get_config().get("timezone")
        self.timezone = (
            zoneinfo.ZoneInfo(tz) if tz else zoneinfo.ZoneInfo("Asia/Shanghai")
        )

        # ---------- 路径 ----------
        # 插件目录：优先按插件包名定位；若目录名与包名不一致（改名/手动重命名），
        # 回退到本文件所在目录的上一级，保证 default_template.json 等资源仍能找到。
        self.plugin_dir = Path(get_astrbot_plugin_path()) / self._plugin_name
        if not self.plugin_dir.is_dir():
            self.plugin_dir = Path(__file__).resolve().parent.parent

        # 数据目录：同样做兼容——若新包名的数据目录尚不存在，而旧包名的目录存在，
        # 则继续沿用旧目录，避免升级后丢失 cookies / cache。
        data_root = Path(get_astrbot_plugin_data_path())
        self.data_dir = data_root / self._plugin_name
        if not self.data_dir.is_dir():
            legacy_dir = data_root / self._legacy_plugin_name
            if legacy_dir.is_dir():
                self.data_dir = legacy_dir
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_dir = self.data_dir / "cookies"
        self.cookie_dir.mkdir(parents=True, exist_ok=True)
        self.default_template_file = self.plugin_dir / "default_template.json"

        # ---------- Parser ----------
        if not self.parsers_template:
            self.parsers_template[:] = self.load_parser_template(
                self.default_template_file
            )
            self.save_config()

        self.parser = ParserConfig(self.parsers_template)

    @staticmethod
    def load_parser_template(file: Path) -> list[dict[str, Any]]:
        try:
            with file.open(encoding="utf-8-sig") as f:
                template = json.loads(f.read())
                logger.info(f"[parser] 加载模板成功: {file}")
                return template
        except Exception as e:
            logger.error(f"[parser] 加载模板失败: {e}")
            return []

    def add_blacklist(self, umo: str):
        if umo not in self.blacklist:
            self.blacklist.append(umo)
            self.save_config()

    def remove_blacklist(self, umo: str):
        if umo in self.blacklist:
            self.blacklist.remove(umo)
            self.save_config()
