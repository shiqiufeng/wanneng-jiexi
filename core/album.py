"""QQ 群相册上传（NapCat 扩展能力）。

OneBot v11 标准里没有群相册接口，NapCat 用扩展 action 提供：

    get_qun_album_list            列出群相册
    upload_image_to_qun_album     上传单张图片
    upload_images_to_qun_album    一次上传多张（相册里显示为同一条）
    upload_video_to_qun_album     上传视频（封面自动抽首帧）
    do_group_album_comment        给相册里的媒体发评论

其中 ``upload_images_to_qun_album`` / ``upload_video_to_qun_album`` 是
NapCat v4.18.30 新增的，旧版本没有，因此每条上传都带降级链：

    多图一次性上传 → 单张逐张上传
    视频上传       → 抽首帧存成图片（没有封面就直接跳过并记录）

本模块不直连 NapCat 的 HTTP 服务，全部经 ``bot.call_action`` 走 AstrBot
已经建好的连接，因此不需要额外填写地址与 token。

⚠️ 响应形态的坑
----------------
aiocqhttp 的 ``bot.call_action()`` **已经把 OneBot 信封剥掉**，只返回响应里的
``data`` 字段。也就是说 ``get_qun_album_list`` 拿到的直接是
``{"album_list": [{"album_id": ..., "album_name": ...}], "attach_info": ...,
"has_more": ...}``，而不是 ``{"status": "ok", "retcode": 0, "data": {...}}``。
早期实现按「完整信封」去取 ``resp["data"]``，于是在真实环境里恒为 ``None``，
相册列表被解析成空 —— 表现就是**群里明明有「少女集」却回复「群里还没有相册」**。

现在统一走 :meth:`GroupAlbumUploader._payload` 归一化：信封、data 本体、
JSON 字符串、纯数组四种形态都能吃下。

⚠️ 「成功但没 data」的坑
------------------------
上传类 action 在 NapCat 里**成功时不返回任何数据**：

* ``uploadImagesToQunAlbum`` 循环传完直接 return（源码里没有 return 值）；
* ``uploadImageToQunAlbum`` 只是前者包了一层单元素数组；
* ``uploadVideoToQunAlbum`` 最后也没有 return。

OneBot 信封里 ``data: undefined`` 序列化后直接消失，aiocqhttp 于是回吐
``None``。早期实现把 ``None`` 一律判成失败 —— 于是**明明上传成功，
回执却是「失败：图片 xxx.jpeg：协议端返回未知错误」**，并且因为判定失败
又退化成逐张上传，把本该合并成一条的图集在相册里刷成一条一条。

现在 :meth:`GroupAlbumUploader._ok` 带 ``void_ok`` 参数，上传/评论这类
「成功即无返回体」的 action 一律按 void 语义判定：**没抛异常就是成功**。

⚠️ 为什么一定要走「一次性多图」
-------------------------------
``uploadImagesToQunAlbum`` 内部给同批图片**共用一个 batchId**，并按
「第几张 / 共几张」（batchIndex / photo_num / batch_num）上报，QQ 相册
才会把它们合并显示成**同一条说说**。逐张调用 ``upload_image_to_qun_album``
时每张都有自己的 batchId，相册里就是一条一张。因此上传路径必须
「先整批、后逐张」，而不是反过来。

⚠️ 文案（作品标题）只能写评论
-----------------------------
上传接口没有标题/描述参数：NapCat 在底层请求里把 ``sPicTitle`` 固定成文件名、
``sPicDesc`` 固定成空串，没有往外暴露，所以**相册条目的「描述」在协议层写不了**。
唯一能把文案送进相册的是 ``do_group_album_comment``（写在那一批媒体的评论里）。

``get_group_album_media_list`` 的返回是 QQ 原生结构原样透传，字段名随版本变化
（``media_list`` / ``mediaList`` / ``media``，批次可能挂 ``batch.link_id`` 也可能在
``media[].batch_id``）。旧实现只认 ``media_list`` 并取第 0 条，键名一变就静默
返回空 —— 表现就是**图片传上去了、文案一个字都没写进去，而回执却照样打
「文案：xxx」**。现在改为扫全树收候选，并用「batchId 是秒级时间戳、刚上传的那批
必然贴近当前时间」来挑出本次上传的那条，逐个候选尝试，最后**如实回执**成败。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import logger

# 单文件超过该体积时不再尝试 base64 兜底（视频动辄几十 MB，base64 会把
# 请求体撑爆，且 NapCat 侧要整段读进内存）
BASE64_MAX_MB = 24

#: 上传最多尝试几次。NapCat 的分片上传会偶发
#: 「分片 N 上传失败: 服务器开小差了,请稍后重试(-114)」，属于可重试的瞬时错误
UPLOAD_ATTEMPTS = 3

#: 重试间隔（秒），给服务端一点喘息时间
UPLOAD_RETRY_DELAY = 2.5

#: 判定「这个失败值得重试」的线索
_TRANSIENT_HINTS = (
    "开小差",
    "-114",
    "timeout",
    "timed out",
    "超时",
    "busy",
    "繁忙",
    "稍后重试",
    "connection",
    "network",
)

# album_list 里可能出现的字段别名，NapCat 不同版本命名不完全一致
_ID_KEYS = ("album_id", "id", "albumId", "albumid")
_NAME_KEYS = ("album_name", "name", "title", "albumName")

#: 媒体条目里图片 ID 的别名。只用精确名字，不认泛化的 ``id``
#: （``cell_media`` 这类结构里也有 ``id``，会指到无关字段上）
_LLOC_KEYS = ("lloc", "lloc_id", "llocId", "pic_key")

#: 上传批次 ID 的别名。同批多图共用一个，用来评论「一整条说说」
_BATCH_ID_KEYS = ("batch_id", "batchId", "batchid")

#: 批次容器里指向这条 feed 的别名（NapCat 文档原话：batch.link_id）
_BATCH_LINK_KEYS = ("link_id", "linkId")

#: 时间字段别名，用来挑「最新那一批」
_TIME_KEYS = (
    "upload_time",
    "uploadTime",
    "uploadTs",
    "create_time",
    "createTime",
    "ctime",
    "time",
    "ts",
)

#: NapCat 的 batchId 就是秒级时间戳（``nextQunAlbumBatchId()`` 取
#: ``Math.floor(Date.now() / 1000)``），因此**刚上传的那一批的 batch_id
#: 必定落在当前时间附近**。用它就能把「本次上传的那条说说」从相册里
#: 成百上千条历史媒体中确定性地挑出来，而不是盲猜 ``media_list[0]``。
_BATCH_FRESH_WINDOW = 900

#: 最多在几条候选上尝试写文案，避免在别人的历史媒体下面乱评论
MAX_COMMENT_TARGETS = 3

#: get_qun_album_list 的 data 形态：{album_list: [...], attach_info, has_more}
#: NapCat 一定用 album_list，其余键名是给别的协议端留的余地
_ALBUM_LIST_KEYS = ("album_list", "albumList", "list", "albums")

#: 相册多时分页，最多翻这么多页，避免 has_more 恒真时打转
MAX_ALBUM_PAGES = 5

#: 判定「这个 action 协议端根本没有」的线索（aiocqhttp 会把 retcode 1404
#: 包装成 ActionFailed，文本里带 ``no such action``）
_MISSING_ACTION_HINTS = (
    "no such action",
    "unknown action",
    "unsupported action",
    "1404",
    "不支持",
    "未知动作",
)


class AlbumError(Exception):
    """群相册操作失败（可把原因直接透给用户）"""


@dataclass(slots=True, frozen=True)
class Album:
    """一个群相册"""

    album_id: str
    name: str


@dataclass(slots=True)
class UploadReport:
    """一次上传的结果汇总，用于给用户回执"""

    album_name: str = ""
    images: int = 0
    """成功上传的图片张数"""
    videos: int = 0
    """成功上传的视频个数"""
    entries: int = 0
    """相册里新增的「说说」条数。多图一次性上传会把同批合并成 1 条"""
    titles: int = 0
    """成功写入文案的条数（评论形式）"""
    comment_errors: list[str] = field(default_factory=list)
    """文案写入失败的原因（如实回执，不再假装写成功）"""
    skipped: list[str] = field(default_factory=list)
    """跳过的项及原因"""
    failures: list[str] = field(default_factory=list)
    """失败项及原因"""

    @property
    def total(self) -> int:
        return self.images + self.videos

    def lines(self) -> list[str]:
        """拼装面向用户的回执行"""
        parts: list[str] = []
        if self.images:
            parts.append(f"图片 {self.images} 张")
        if self.videos:
            parts.append(f"视频 {self.videos} 个")
        head = "、".join(parts) if parts else "0 项"
        out = [f"已上传到群相册「{self.album_name}」：{head}"]
        if self.entries and self.entries < self.total:
            out.append(f"· 相册里合并为 {self.entries} 条")
        if self.titles:
            out.append(f"· 已写入文案：{self.titles} 条")
        for item in self.comment_errors:
            out.append(f"· 文案未写入：{item}")
        for item in self.skipped:
            out.append(f"· 跳过：{item}")
        for item in self.failures:
            out.append(f"· 失败：{item}")
        return out


@dataclass(slots=True, frozen=True)
class MediaTarget:
    """相册里一条（或一批）可评论的媒体

    ``batch_id`` 指向「一整条说说」（同批多图共用一个批次号），
    ``lloc`` 指向单张图。两者至少有一个非空。
    """

    batch_id: str = ""
    lloc: str = ""
    stamp: int = 0
    """从响应里读到的时间戳；0 表示这条没带时间字段"""

    def describe(self) -> str:
        bits = []
        if self.batch_id:
            bits.append(f"batch_id={self.batch_id}")
        if self.lloc:
            bits.append(f"lloc={self.lloc[:16]}")
        return " ".join(bits) or "（空目标）"


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> str:
    """按别名顺序取第一个非空字符串字段"""
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _error_response(message: str) -> dict[str, Any]:
    """把异常包装成 ``_ok()`` 必定判为失败的响应体

    早期实现用裸 ``{"message": ...}`` 表示「调用抛异常了」，而 ``_ok()`` 对
    没有 ``retcode`` 的字典一律返回 True —— 结果接口不存在时，图片/视频会被
    静默计成「上传成功」。这里显式带上 ``status=failed`` / ``retcode=-1``，
    把这条路彻底堵死。
    """
    return {"status": "failed", "retcode": -1, "message": message}


class GroupAlbumUploader:
    """把解析出来的媒体上传到指定群相册。

    Args:
        name: 目标相册名，如「少女集」；不存在时不会创建，直接报错
        title_comment: 上传后是否把作品文案写成相册评论（NapCat 群相册
            接口没有「标题」参数，评论是最接近的落点）
        base64_fallback: 路径方式失败时是否改用 base64 再试一次
        max_media: 单次最多上传多少个媒体，避免超大图集把相册刷屏
    """

    def __init__(
        self,
        *,
        name: str,
        title_comment: bool = True,
        base64_fallback: bool = True,
        max_media: int = 20,
    ) -> None:
        self.name = (name or "").strip()
        self.title_comment = title_comment
        self.base64_fallback = base64_fallback
        self.max_media = max(1, int(max_media or 20))

    # ---------------- 底层调用 ----------------

    @staticmethod
    def _explain_exception(action: str, exc: Exception) -> str:
        """把底层异常翻译成用户能看懂的原因

        aiocqhttp 在 ``retcode != 0`` 时抛 ``ActionFailed``，所以「协议端没有
        这个 action」表现为异常文本 ``no such action`` 而不是返回体。直接把这句
        原文抛给用户没有意义，这里统一换成「版本太旧」的提示。
        """
        lowered = str(exc).lower()
        if getattr(exc, "retcode", None) == 1404 or any(
            hint in lowered for hint in _MISSING_ACTION_HINTS
        ):
            return f"协议端不支持 {action}（群相册需要 NapCat v4.18.30+）"
        return f"{action} 调用异常：{exc}"

    async def _call(self, bot: Any, action: str, **payload: Any) -> Any:
        """调用 NapCat 扩展 action

        aiocqhttp 的 CQHttp 既有 ``call_action(action, **params)``，也支持
        ``bot.<action>(**params)`` 的动态属性写法，两者都试一遍。
        """
        caller = getattr(bot, "call_action", None)
        if callable(caller):
            try:
                return await caller(action, **payload)
            except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
                raise AlbumError(self._explain_exception(action, exc)) from exc

        method = getattr(bot, action, None)
        if callable(method):
            try:
                return await method(**payload)
            except Exception as exc:  # noqa: BLE001
                raise AlbumError(self._explain_exception(action, exc)) from exc

        raise AlbumError(f"当前协议端不支持 {action}（群相册需要 NapCat）")

    @staticmethod
    def _ok(resp: Any, *, void_ok: bool = False) -> bool:
        """判断 OneBot 响应是否为成功

        Args:
            resp: ``call_action`` 的返回值
            void_ok: 该 action 成功时**本来就不返回 data** 时为 True。
                NapCat 的 ``upload_images_to_qun_album`` /
                ``upload_image_to_qun_album`` / ``upload_video_to_qun_album``
                都是这种「void」action：循环传完直接 return，响应体里没有
                ``data`` 字段，aiocqhttp 于是回吐 ``None``。失败时 NapCat 会
                throw，aiocqhttp 抛 ``ActionFailed``（走的是异常分支），
                所以这里**没抛异常就说明成功**。

                早期实现把 ``None`` 一律判成失败，结果上传成功的图被回执成
                「协议端返回未知错误」，还因为判定失败又逐张重传，把本该
                合并成一条的图集在相册里刷成一条一条。
        """
        if resp is None:
            return void_ok
        if not isinstance(resp, dict):
            return True
        status = str(resp.get("status") or "").lower()
        if status in {"failed", "error"}:
            return False
        try:
            retcode = int(resp.get("retcode", 0) or 0)
        except (TypeError, ValueError):
            return False
        # 1404 = action not found（NapCat 版本过旧）
        return retcode == 0

    @staticmethod
    def _is_missing_action(resp_or_reason: Any) -> bool:
        """判断失败原因是不是「协议端根本没有这个 action」

        这类失败不该重试，只能退化成别的接口。
        """
        text = (
            resp_or_reason
            if isinstance(resp_or_reason, str)
            else GroupAlbumUploader._fail_reason(resp_or_reason)
        )
        lowered = text.lower()
        return "不支持" in text or any(
            hint in lowered for hint in _MISSING_ACTION_HINTS
        )

    @staticmethod
    def _is_transient(resp_or_reason: Any) -> bool:
        """判断失败原因是不是值得重试的瞬时错误（分片 -114 / 超时 / 网络）"""
        text = (
            resp_or_reason
            if isinstance(resp_or_reason, str)
            else GroupAlbumUploader._fail_reason(resp_or_reason)
        )
        lowered = text.lower()
        return any(hint in lowered for hint in _TRANSIENT_HINTS)

    @staticmethod
    def _fail_reason(resp: Any) -> str:
        """从响应里取人类可读的失败原因

        retcode 1404 是 OneBot 的「action not found」，对用户来说就是
        NapCat 版本太旧，这种提示比原始的 "no such action" 有用得多。
        """
        if isinstance(resp, dict):
            try:
                retcode = int(resp.get("retcode", 0) or 0)
            except (TypeError, ValueError):
                retcode = 0
            if retcode == 1404:
                return "协议端不支持该接口（群相册需要 NapCat v4.18.30+）"
            for key in ("message", "wording", "msg"):
                text = str(resp.get(key) or "").strip()
                if text:
                    return text
            if retcode:
                return f"retcode={retcode}"
        return "协议端返回未知错误"

    async def _call_quiet(self, bot: Any, action: str, **payload: Any) -> Any:
        """调用扩展 action，把异常统一包装成必定判失败的响应体

        与 :meth:`_call` 的区别是不抛异常，便于上传路径按「候选 → 重试 →
        降级」串起来。
        """
        try:
            return await self._call(bot, action, **payload)
        except AlbumError as exc:
            return _error_response(str(exc))

    # ---------------- 相册定位 ----------------

    @staticmethod
    def _payload(resp: Any) -> Any:
        """把协议端响应归一化成业务 ``data``

        ``bot.call_action()`` 的返回值有两种可能，必须都吃得下：

        * **aiocqhttp（AstrBot 走的实现）已经把信封剥掉**，直接返回响应里的
          ``data`` 字段（官方文档：「函数返回插件响应数据的 data 字段」）。
          此时 ``resp`` 就是 ``{"album_list": [...]}``。
        * 换成别的适配器/协议端时，可能原样回吐 ``{status, retcode, data}``。

        因此这里只认「同时出现 ``data`` 且带信封特征字段」的情况才拆信封，
        避免把业务里恰好叫 ``data`` 的键误当信封拆掉。
        """
        if isinstance(resp, str):
            try:
                resp = json.loads(resp)
            except ValueError:
                return resp
        if isinstance(resp, dict) and "data" in resp and any(
            key in resp for key in ("status", "retcode", "wording", "stream")
        ):
            return resp["data"]
        return resp

    @staticmethod
    def _album_items(data: Any) -> list[dict[str, Any]]:
        """把群相册列表归一化成 dict 列表

        能吃下这几种形态：

        * ``{"album_list": [...]}`` —— NapCat ``get_qun_album_list`` 的 data
        * ``[...]`` —— 有的实现直接把数组当 data
        * ``data`` 本身是被序列化过的 JSON 字符串
        * 数组元素是对象，或对象被序列化成的字符串
        """
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return []

        raw: Any = None
        if isinstance(data, list):
            raw = data
        elif isinstance(data, dict):
            for key in _ALBUM_LIST_KEYS:
                candidate = data.get(key)
                if isinstance(candidate, list):
                    raw = candidate
                    break
        if not isinstance(raw, list):
            return []

        items: list[dict[str, Any]] = []
        for entry in raw:
            if isinstance(entry, dict):
                items.append(entry)
            elif isinstance(entry, str):
                text = entry.strip()
                if text.startswith("{"):
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict):
                        items.append(parsed)
                        continue
                # 只有名字的情况：交给上层按名字匹配，但拿不到 id
                items.append({"album_name": text})
        return items

    async def _list_albums(self, bot: Any, group_id: str) -> list[dict[str, Any]]:
        """取回群内全部相册（必要时按 ``attach_info`` 翻页）

        NapCat 的返回里带 ``has_more`` / ``attach_info``，相册较多的群里目标
        相册可能落在后一页，因此最多翻 :data:`MAX_ALBUM_PAGES` 页。
        """
        items: list[dict[str, Any]] = []
        attach = ""
        for page in range(MAX_ALBUM_PAGES):
            payload: dict[str, Any] = {"group_id": str(group_id)}
            if attach:
                payload["attach_info"] = attach
            resp = await self._call(bot, "get_qun_album_list", **payload)
            if resp is None:
                # 接口认得、但没吐 data：按「没有相册」处理，而不是报故障
                logger.debug("[album] get_qun_album_list 返回空（None）")
                break
            if not self._ok(resp):
                if items:
                    break  # 已经拿到一部分，够用就行
                raise AlbumError(f"获取群相册列表失败：{self._fail_reason(resp)}")

            data = self._payload(resp)
            logger.debug(
                f"[album] get_qun_album_list 第 {page + 1} 页返回类型 "
                f"{type(data).__name__}: {str(data)[:400]}"
            )
            items.extend(self._album_items(data))

            if not isinstance(data, dict) or not data.get("has_more"):
                break
            next_attach = str(data.get("attach_info") or "").strip()
            if not next_attach or next_attach == attach:
                break
            attach = next_attach
        return items

    async def find_album(self, bot: Any, group_id: str) -> Album | None:
        """在群里找目标相册；先精确匹配，再退化为包含匹配"""
        items = await self._list_albums(bot, group_id)

        named: list[tuple[str, str]] = []
        for item in items:
            album_id = _first(item, _ID_KEYS)
            album_name = _first(item, _NAME_KEYS)
            if album_name:
                named.append((album_name, album_id))

        for album_name, album_id in named:
            if album_name == self.name and album_id:
                return Album(album_id, album_name)
        for album_name, album_id in named:
            if self.name and self.name in album_name and album_id:
                logger.info(f"[album] 相册名精确未命中，按包含匹配到「{album_name}」")
                return Album(album_id, album_name)

        # 没命中时把群内相册名写进插件日志，方便对照配置项排查
        logger.info(
            f"[album] 未命中目标相册「{self.name}」，群内相册：{[n for n, _ in named]}"
        )
        return None

    async def album_names(self, bot: Any, group_id: str) -> list[str]:
        """列出群里已有相册名，用于报错时提示"""
        try:
            items = await self._list_albums(bot, group_id)
        except AlbumError:
            return []
        return [n for item in items if (n := _first(item, _NAME_KEYS))]

    async def probe(self, bot: Any, action: str, **payload: Any) -> tuple[bool, str]:
        """探测某个扩展 action 是否可用

        只关心「协议端认不认这个 action」：接口存在但拒绝了我们为探测而故意
        传的空参数，同样说明这个版本支持它。

        注意 aiocqhttp 在 ``retcode != 0`` 时会抛 ``ActionFailed``，所以
        「action 不存在」表现为异常而不是返回体，这里按异常文本判定。

        Returns:
            tuple[bool, str]: 是否可用、失败原因
        """
        try:
            resp = await self._call(bot, action, **payload)
        except AlbumError as exc:
            text = str(exc)
            lowered = text.lower()
            if any(hint in lowered for hint in _MISSING_ACTION_HINTS):
                return False, "协议端不支持该 action（NapCat 需 v4.18.30+）"
            # 接口在，只是嫌参数不对（probe 传的就是空参数）
            return True, text

        if isinstance(resp, dict):
            try:
                retcode = int(resp.get("retcode", 0) or 0)
            except (TypeError, ValueError):
                retcode = 0
            if retcode == 1404:
                return False, "协议端不支持该 action（NapCat 需 v4.18.30+）"
        return True, self._fail_reason(resp)

    # ---------------- 相册媒体定位（写文案用） ----------------

    @staticmethod
    def _stamp(node: dict[str, Any]) -> int:
        """从媒体条目里读一个秒级时间戳，读不到返回 0"""
        for key in _TIME_KEYS:
            if key not in node:
                continue
            try:
                value = int(str(node[key]).strip())
            except (TypeError, ValueError):
                continue
            if value > 1_000_000_000_000:  # 毫秒
                return value // 1000
            if value > 1_000_000_000:  # 秒
                return value
        return 0

    @classmethod
    def _collect_targets(cls, data: Any, *, max_depth: int = 8) -> list[MediaTarget]:
        """把 ``get_group_album_media_list`` 的返回扫成候选评论目标

        NapCat 只是把 QQ 原生的 ``getMediaList`` 响应原样透传，字段名随版本
        变化（``media_list`` / ``mediaList`` / ``media``，批次可能挂在
        ``batch.link_id`` 也可能在 ``media[].batch_id``），所以这里不猜具体
        字段名，而是**整棵树扫一遍**：凡是带 ``lloc`` 或 ``batch_id`` 的对象
        都收下来，去重后按时间倒序排。

        旧实现只认 ``media_list`` 这一个键名并取第 0 条，字段名一变就静默
        返回空 —— 表现就是「图片传上去了，文案一个字都没有」。
        """
        found: list[MediaTarget] = []
        seen: set[tuple[str, str]] = set()
        stack: list[tuple[Any, int]] = [(data, 0)]
        while stack:
            node, depth = stack.pop(0)
            if depth > max_depth:
                continue
            if isinstance(node, dict):
                batch = _first(node, _BATCH_ID_KEYS) or _first(node, _BATCH_LINK_KEYS)
                lloc = _first(node, _LLOC_KEYS)
                if batch or lloc:
                    key = (batch, lloc)
                    if key not in seen:
                        seen.add(key)
                        found.append(MediaTarget(batch, lloc, cls._stamp(node)))
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        stack.append((value, depth + 1))
            elif isinstance(node, list):
                for value in node:
                    if isinstance(value, (dict, list)):
                        stack.append((value, depth + 1))
        # 带时间戳的排在前面（新的在前），没时间戳的保持原顺序
        found.sort(key=lambda target: -target.stamp)
        return found

    @staticmethod
    def _rank_targets(targets: list[MediaTarget], now: int) -> list[MediaTarget]:
        """把「本次上传的那一批」排到最前

        NapCat 的 batchId 取自 ``Math.floor(Date.now() / 1000)``，因此刚传完的
        那批 batch_id 与当前时间只差几秒；相册里成百上千条历史媒体不可能落在
        这个窗口里。用它就能确定性地挑出「刚上传的那条说说」，而不是盲猜列表
        第一条（相册按更新时间倒序不保证，猜错就把文案评论到别人的图下面）。
        """

        def score(target: MediaTarget) -> tuple[int, int]:
            if target.batch_id.isdigit():
                delta = abs(now - int(target.batch_id))
                if delta <= _BATCH_FRESH_WINDOW:
                    return (0, delta)  # 本次上传，越接近当前时间越优先
                return (1, 0)
            return (2, 0)  # 只有 lloc，或 batch_id 不是时间戳

        return sorted(targets, key=score)

    async def media_targets(
        self, bot: Any, group_id: str, album: Album
    ) -> list[MediaTarget]:
        """读取相册媒体列表，返回按「最可能是本次上传」排序的评论目标

        Raises:
            AlbumError: 接口不可用，或协议端明确返回失败
        """
        resp = await self._call(
            bot,
            "get_group_album_media_list",
            group_id=str(group_id),
            album_id=album.album_id,
        )
        if not self._ok(resp):
            raise AlbumError(f"读取相册媒体列表失败：{self._fail_reason(resp)}")

        data = self._payload(resp)
        targets = self._collect_targets(data)
        logger.debug(
            "[album] 相册媒体列表顶层键 "
            f"{list(data)[:16] if isinstance(data, dict) else type(data).__name__}"
            f"，解析出 {len(targets)} 个候选评论目标"
        )
        return self._rank_targets(targets, int(time.time()))

    def _file_candidates(self, path: Path) -> list[str]:
        """file 参数候选：优先本地路径，必要时退化为 base64"""
        candidates = [str(path)]
        if not self.base64_fallback:
            return candidates
        try:
            size_mb = path.stat().st_size / 1024 / 1024
        except OSError:
            return candidates
        if size_mb > BASE64_MAX_MB:
            logger.debug(f"[album] {path.name} 体积 {size_mb:.1f}MB，跳过 base64 兜底")
            return candidates
        try:
            raw = path.read_bytes()
        except OSError:
            return candidates
        candidates.append("base64://" + base64.b64encode(raw).decode("ascii"))
        return candidates

    async def _attempt_upload(
        self, bot: Any, action: str, payload: dict[str, Any], label: str
    ) -> Any:
        """调用一次上传 action，瞬时错误时按固定间隔重试

        上传类 action 成功时没有返回体（见模块顶部「成功但没 data」一节），
        因此判定一律带 ``void_ok=True``，即**没抛异常就是成功**。
        """
        last: Any = None
        for attempt in range(UPLOAD_ATTEMPTS):
            resp = await self._call_quiet(bot, action, **payload)
            if self._ok(resp, void_ok=True):
                if attempt:
                    logger.info(f"[album] {label} 第 {attempt + 1} 次尝试上传成功")
                return resp
            last = resp
            if self._is_missing_action(resp) or not self._is_transient(resp):
                break
            if attempt + 1 < UPLOAD_ATTEMPTS:
                logger.info(
                    f"[album] {label} 上传遇到瞬时错误（{self._fail_reason(resp)}），"
                    f"{UPLOAD_RETRY_DELAY:.1f}s 后重试"
                )
                await asyncio.sleep(UPLOAD_RETRY_DELAY)
        return last

    async def _upload_one(
        self,
        bot: Any,
        action: str,
        group_id: str,
        album: Album,
        path: Path,
        *,
        key: str = "file",
    ) -> Any:
        """按候选顺序尝试上传单个文件，返回最后一次响应

        候选顺序是「本地路径 → base64」，同一候选遇到瞬时错误时由
        :meth:`_attempt_upload` 负责重试。
        """
        payload = {
            "group_id": str(group_id),
            "album_id": album.album_id,
            "album_name": album.name,
        }
        last: Any = None
        for index, value in enumerate(self._file_candidates(path)):
            resp = await self._attempt_upload(
                bot, action, {key: value, **payload}, path.name
            )
            if self._ok(resp, void_ok=True):
                if index:
                    logger.info(f"[album] {path.name} 经 base64 兜底上传成功")
                return resp
            last = resp
            # 接口不存在就没必要再换 file 形式重试
            if self._is_missing_action(resp):
                break
        return last

    async def upload_images(
        self,
        bot: Any,
        group_id: str,
        album: Album,
        paths: list[Path],
        report: UploadReport,
    ) -> None:
        """上传图片：**优先整批**，同批图片在相册里合并成一条说说

        ``upload_images_to_qun_album`` 内部给同批图片共用一个 batchId，并按
        「第几张 / 共几张」上报（NapCat 源码注释：「相册里才会合并成同一条」），
        所以只有整批调用才能得到「一条说说装齐所有图」的效果；逐张调用时每张
        都有各自的 batchId，相册里就变成一条一张。

        只有「协议端没有这个 action」或整批重试后仍失败时才降级为逐张 ——
        降级属于保底形态。
        """
        if not paths:
            return

        payload = {
            "group_id": str(group_id),
            "album_id": album.album_id,
            "album_name": album.name,
        }

        if len(paths) > 1:
            resp = await self._attempt_upload(
                bot,
                "upload_images_to_qun_album",
                {"files": [str(p) for p in paths], **payload},
                f"{len(paths)} 张图片",
            )
            if self._ok(resp, void_ok=True):
                report.images += len(paths)
                report.entries += 1
                return
            reason = self._fail_reason(resp)
            if self._is_missing_action(resp):
                logger.info(
                    f"[album] 协议端不支持多图一次性上传（{reason}），改逐张上传"
                )
            else:
                logger.warning(
                    f"[album] 多图一次性上传失败（{reason}），改逐张上传；"
                    "整批里先传完的图可能已入库，相册里会多出零散条目"
                )

        for path in paths:
            resp = await self._upload_one(
                bot, "upload_image_to_qun_album", group_id, album, path
            )
            if self._ok(resp, void_ok=True):
                report.images += 1
                report.entries += 1
            else:
                report.failures.append(f"图片 {path.name}：{self._fail_reason(resp)}")

    async def upload_video(
        self,
        bot: Any,
        group_id: str,
        album: Album,
        path: Path,
        report: UploadReport,
        *,
        cover: Path | None = None,
    ) -> bool:
        """上传视频；接口不支持时退化为上传封面图

        视频走的是另一条链路（建视频会话 → 分片上传拿 sVid → 单独上传封面
        并带上 vid 挂进相册），封面那一步**不带 batchId**，因此 NapCat 无法
        把视频并进图片那条说说里，视频必然自成一贴。

        Returns:
            bool: 是否成功进入相册（真视频或封面图任一成功即 True）
        """
        resp = await self._upload_one(
            bot, "upload_video_to_qun_album", group_id, album, path
        )
        if self._ok(resp, void_ok=True):
            report.videos += 1
            report.entries += 1
            return True

        reason = self._fail_reason(resp)
        if cover is not None and cover.exists():
            logger.info(f"[album] 视频上传失败（{reason}），改为上传封面图")
            cover_resp = await self._upload_one(
                bot, "upload_image_to_qun_album", group_id, album, cover
            )
            if self._ok(cover_resp, void_ok=True):
                report.images += 1
                report.entries += 1
                report.skipped.append(f"视频 {path.name} 存成了封面图（{reason}）")
                return True
            report.failures.append(
                f"视频 {path.name}：{reason}；封面兜底也失败（{self._fail_reason(cover_resp)}）"
            )
            return False
        report.failures.append(f"视频 {path.name}：{reason}")
        return False

    async def _comment(
        self,
        bot: Any,
        group_id: str,
        album: Album,
        text: str,
        *,
        batch_id: str = "",
        lloc: str = "",
    ) -> tuple[bool, str]:
        """调用一次群相册评论接口，返回 (是否成功, 失败原因)"""
        payload: dict[str, Any] = {
            "group_id": str(group_id),
            "album_id": album.album_id,
            "content": text,
        }
        # NapCat 的 check 要求 lloc / batch_id 至少给一个；两个都给时按单张评论
        if batch_id:
            payload["batch_id"] = batch_id
        if lloc:
            payload["lloc"] = lloc

        try:
            resp = await self._call(bot, "do_group_album_comment", **payload)
        except AlbumError as exc:
            return False, str(exc)
        # 评论接口同样「成功但没有返回体」，按 void 语义判定
        if self._ok(resp, void_ok=True):
            return True, ""
        return False, self._fail_reason(resp)

    async def write_title(
        self, bot: Any, group_id: str, album: Album, text: str
    ) -> tuple[bool, str]:
        """把作品文案写成相册里**本次上传那条说说**的评论

        群相册上传接口本身没有标题/描述参数（NapCat 在底层请求里把
        ``sPicDesc`` 固定成空串，没往外暴露），评论是唯一能把文案送进相册的
        落点。整批图片会被合并成一条说说，因此一次调用就覆盖整批；视频各自
        成贴，每个条目各调一次。

        Returns:
            tuple[bool, str]: 是否写入成功、失败原因（成功时为空串）。
            调用方据此如实回执，不要再无条件打「文案：xxx」。
        """
        text = (text or "").strip()
        if not text:
            return False, "文案为空"

        try:
            targets = await self.media_targets(bot, group_id, album)
        except AlbumError as exc:
            return False, str(exc)

        if not targets:
            return False, "相册媒体列表里没读到可评论的条目（接口返回字段与预期不符）"

        # 优先评论整条说说（batch_id），失败再退到单张图（lloc）
        last = "没能在相册里定位到本次上传的那条媒体"
        for index, target in enumerate(targets[:MAX_COMMENT_TARGETS]):
            if target.batch_id:
                ok, reason = await self._comment(
                    bot, group_id, album, text, batch_id=target.batch_id
                )
                if ok:
                    logger.info(
                        f"[album] 文案已写入相册评论（第 {index + 1} 个候选："
                        f"{target.describe()}）"
                    )
                    return True, ""
                last = reason
            if target.lloc:
                ok, reason = await self._comment(
                    bot, group_id, album, text, lloc=target.lloc
                )
                if ok:
                    logger.info(
                        f"[album] 文案已写入相册评论（第 {index + 1} 个候选："
                        f"{target.describe()}）"
                    )
                    return True, ""
                last = reason

        logger.warning(
            f"[album] 文案写入失败：{last}；本次候选 "
            f"{[t.describe() for t in targets[:MAX_COMMENT_TARGETS]]}"
        )
        return False, last
