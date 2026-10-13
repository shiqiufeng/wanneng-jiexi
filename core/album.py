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
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import logger

# 单文件超过该体积时不再尝试 base64 兜底（视频动辄几十 MB，base64 会把
# 请求体撑爆，且 NapCat 侧要整段读进内存）
BASE64_MAX_MB = 24

# album_list 里可能出现的字段别名，NapCat 不同版本命名不完全一致
_ID_KEYS = ("album_id", "id", "albumId", "albumid")
_NAME_KEYS = ("album_name", "name", "title", "albumName")

# media_list 里媒体 ID 的字段别名
_MEDIA_ID_KEYS = ("lloc", "media_id", "mediaId", "id", "pic_key")

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
    titles: int = 0
    """成功写入文案的媒体数（评论形式）"""
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
        if self.titles:
            out.append(f"已为 {self.titles} 项写入文案")
        for item in self.skipped:
            out.append(f"· 跳过：{item}")
        for item in self.failures:
            out.append(f"· 失败：{item}")
        return out


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
    def _ok(resp: Any) -> bool:
        """判断 OneBot 响应是否为成功"""
        if resp is None:
            return False
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

    # ---------------- 上传 ----------------

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
        """按候选顺序尝试上传单个文件，返回最后一次响应"""
        payload = {
            "group_id": str(group_id),
            "album_id": album.album_id,
            "album_name": album.name,
        }
        last: Any = None
        for index, value in enumerate(self._file_candidates(path)):
            try:
                resp = await self._call(bot, action, **{key: value}, **payload)
            except AlbumError as exc:
                last = _error_response(str(exc))
                continue
            if self._ok(resp):
                if index:
                    logger.info(f"[album] {path.name} 经 base64 兜底上传成功")
                return resp
            last = resp
            # 接口不存在就没必要再换 file 形式重试
            if "不支持" in self._fail_reason(resp):
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
        """上传图片：先试一次性多图，不行再逐张"""
        if not paths:
            return

        payload = {
            "group_id": str(group_id),
            "album_id": album.album_id,
            "album_name": album.name,
        }
        if len(paths) > 1:
            try:
                resp = await self._call(
                    bot,
                    "upload_images_to_qun_album",
                    files=[str(p) for p in paths],
                    **payload,
                )
            except AlbumError as exc:
                resp = _error_response(str(exc))
            if self._ok(resp):
                report.images += len(paths)
                return
            logger.info(
                f"[album] 多图一次性上传不可用（{self._fail_reason(resp)}），改逐张上传"
            )

        for path in paths:
            resp = await self._upload_one(
                bot, "upload_image_to_qun_album", group_id, album, path
            )
            if self._ok(resp):
                report.images += 1
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
    ) -> None:
        """上传视频；接口不支持时退化为上传封面图"""
        resp = await self._upload_one(
            bot, "upload_video_to_qun_album", group_id, album, path
        )
        if self._ok(resp):
            report.videos += 1
            return

        reason = self._fail_reason(resp)
        if cover is not None and cover.exists():
            logger.info(f"[album] 视频上传失败（{reason}），改为上传封面图")
            cover_resp = await self._upload_one(
                bot, "upload_image_to_qun_album", group_id, album, cover
            )
            if self._ok(cover_resp):
                report.images += 1
                report.skipped.append(f"视频 {path.name} 存成了封面图（{reason}）")
                return
            report.failures.append(
                f"视频 {path.name}：{reason}；封面兜底也失败（{self._fail_reason(cover_resp)}）"
            )
            return
        report.failures.append(f"视频 {path.name}：{reason}")

    async def write_title(
        self, bot: Any, group_id: str, album: Album, text: str
    ) -> bool:
        """把作品文案写成相册里最新一条媒体的评论

        群相册上传接口本身没有标题/描述参数，评论是 NapCat 能提供的
        最接近的落点。任何一步拿不到就放弃，不影响上传结果。
        """
        text = (text or "").strip()
        if not text:
            return False
        try:
            resp = await self._call(
                bot,
                "get_group_album_media_list",
                group_id=str(group_id),
                album_id=album.album_id,
            )
        except AlbumError:
            return False
        if not self._ok(resp):
            return False

        data = self._payload(resp)
        media_list: Any = None
        batch_id = ""
        if isinstance(data, dict):
            for key in ("media_list", "media", "feed_list", "list"):
                if isinstance(data.get(key), list):
                    media_list = data[key]
                    break
            batch = data.get("batch")
            if isinstance(batch, dict):
                # NapCat：batch_id 可取自 batch.link_id
                batch_id = _first(batch, ("link_id", "batch_id", "batchId"))
        elif isinstance(data, list):
            media_list = data

        if not isinstance(media_list, list) or not media_list:
            return False

        latest = media_list[0]
        if not isinstance(latest, dict):
            return False

        # lloc 定位单张图；拿不到时退回 batch_id 评论整个上传批次
        lloc = _first(latest, _MEDIA_ID_KEYS)
        if not batch_id:
            batch_id = _first(latest, ("batch_id", "batchId"))
        if not lloc and not batch_id:
            return False

        payload: dict[str, Any] = {
            "group_id": str(group_id),
            "album_id": album.album_id,
            "content": text,
        }
        if lloc:
            payload["lloc"] = lloc
        if batch_id:
            payload["batch_id"] = batch_id

        try:
            resp2 = await self._call(bot, "do_group_album_comment", **payload)
        except AlbumError:
            return False
        return self._ok(resp2)
