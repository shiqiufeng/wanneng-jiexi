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

⚠️ 建相册 / 删相册：靠「原始发包」补齐
--------------------------------------
群相册在内核里走的不是 OIDB，而是 qzone 的 **trpc** 服务：

    QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.GetAlbumList   列相册
    QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.GetMediaList   列媒体
    QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.AddAlbum       建相册
    QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.DeleteAlbum    删相册

NapCat 的内核（``NodeIKernelAlbumService``）里声明了 ``addAlbum`` /
``deleteAlbum``，但 OneBot 层**没有把它们暴露成 action**：``router.ts`` 的
ActionName 里相册相关的只有 ``get_qun_album_list``、``get_group_album_media_list``、
``upload_image_to_qun_album``、``upload_images_to_qun_album``、
``upload_video_to_qun_album``、``do_group_album_comment``、
``set_group_album_media_like``、``cancel_group_album_media_like``、
``del_group_album_media``（删的是相册里的**媒体**，不是相册本身）。

于是 :meth:`GroupAlbumUploader.create_album` / :meth:`delete_album` 走三级回退：

1. **候选 action 逐个探测**（参数名也逐个形状试）。LLOneBot 就有
   ``create_group_album`` / ``delete_group_album``，本插件的候选表已经覆盖；
   哪天 NapCat 或某个分支补上，同样无需改代码即生效。
2. **NapCat 原始发包**：``send_packet`` 允许直接发一条 PB 包（cmd + protobuf
   十六进制体）。客户端本来就会发 AddAlbum / DeleteAlbum 这两条包，把它们原样
   造出来发出去即可 —— 这条通路不依赖协议端补 action。发之前先用
   ``get_packet_status`` 探活（PacketBackend 被禁用或当前 QQ 版本/架构不受支持
   时它会直接拒绝，原话带官方文档链接，原样透给用户）。
3. 都不行时**如实回报**「协议端没有这个接口」并给出人工操作步骤。

无论走哪条路，建/删之后都会**重新拉一次相册列表做校验**：接口说成功但列表里
没出现（或没消失）就判失败，绝不假装建好了。响应的 retCode / retMsg 会被解析
出来写进回执，方便定位「权限不足」「相册名重复」这类服务端拒绝。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
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

#: 「创建群相册」的候选 action 名，按顺序探测，第一个被协议端认得的就用它。
#:
#: NapCat（main 分支 router.ts）**没有**把建相册暴露成 OneBot action，
#: 但 LLOneBot 有 ``create_group_album``（见其
#: ``src/onebot11/action/llbot/group/GroupAlbum/CreateGroupAlbum.ts``），
#: 所以这份候选表在 LLOneBot 上是真能命中的；哪天 NapCat 或某个分支按其中
#: 任一名字补上，本插件无需改代码即生效。全都不认得时才退到原始发包通路。
_CREATE_ALBUM_ACTIONS = (
    "create_qun_album",
    "create_group_album",
    "add_qun_album",
    "add_group_album",
)

#: 「删除群相册」的候选 action 名。LLOneBot 用的是 ``delete_group_album``。
#: 注意 ``del_group_album_media`` 不在其中：它删的是相册里的媒体，不是相册
#: 本身，语义不同不能顶替。
_DELETE_ALBUM_ACTIONS = (
    "delete_qun_album",
    "del_qun_album",
    "delete_group_album",
    "del_group_album",
)

#: 各协议端的**参数名**也不一样（LLOneBot：``group_id + name + desc``；
#: 别的实现可能叫 ``album_name`` / ``group_name``），因此每个 action 名字都
#: 按这几种形状依次试，命中「参数不对」就换下一种，命中「没这个 action」就换
#: 下一个名字。
_CREATE_ALBUM_PARAMS: tuple[tuple[str, ...], ...] = (
    ("group_id", "name", "desc"),
    ("group_id", "album_name"),
    ("group_id", "group_name"),
    ("group_id", "name"),
    ("group_id", "album_name", "name"),
)

_DELETE_ALBUM_PARAMS: tuple[tuple[str, ...], ...] = (
    ("group_id", "album_id"),
    ("group_id", "album_name"),
    ("group_id", "album_id", "album_name"),
    ("group_id", "name"),
)

#: 判定「这次失败只是参数名/参数个数不对」的线索：换一个参数形状再试，
#: 而不是当成真正的失败回报。故意不收录「缺少」这类泛化词，避免把
#: 「缺少权限」误判成参数问题。
#:
#: 这几条覆盖了常见协议端框架的参数校验措辞：TypeBox/ajv 的
#: ``must have required property``、LLOneBot Schema 的 ``unexpected property``、
#: 以及 ``unknown field`` 这类手写校验。
_PARAM_ERROR_HINTS = (
    "missing",
    "required",
    "invalid",
    "unexpected",
    "unknown field",
    "unknown property",
    "additional propert",
    "schema",
    "参数",
    "不能为空",
)

#: 相册名的长度上限，避免用户把一整句话当相册名
MAX_ALBUM_NAME = 24

#: 相册名两侧可能要剥掉的引号/括号
_NAME_TRIM = "\"'“”‘’《》〈〉「」『』[]【】()（）"

#: 相册名末尾可能要剥掉的标点
_NAME_TAIL = re.compile(r"[\s。，,.!！?？;；:：]+$")


# ================ 相册命令解析 ================
#
# 解析逻辑放在这里（而不是 main.py）是为了能被离线测试直接执行：main.py 一
# import 就要拉起整个 AstrBot，本模块只需要一个日志桩，命令的边界用例（相册名
# 里混进链接、把「/相册自检」当成上传到「自检」相册……）才测得到。

ALBUM_HEAD = re.compile(r"^/?相册")
"""群相册上传的命令头，相册名直接写在命令里：

    /相册旅游 <链接>        上传到「旅游」
    /相册少女集 <链接>      上传到「少女集」
    /相册 <链接>            上传到配置项「默认相册名称」里的相册

解析出的图片/视频会上传进该群相册，不会再往会话里发媒体本身。
"""

ALBUM_NAME_TOKEN = re.compile(r"^([^\s:：]{1,%d})[\s:：]*" % MAX_ALBUM_NAME)
"""命令头后面紧跟的相册名，如「/相册旅游 <链接>」里的「旅游」"""

_URLISH = re.compile(r"(https?|www\.|://)", re.IGNORECASE)
"""判断一段文本是不是链接的开头，避免把「/相册https://x」里的 https 当成相册名"""

ALBUM_RESERVED_NAMES = frozenset(
    {"上传", "上传到相册", "自检", "检测", "列表", "清单", "帮助"}
)
"""这些词是别的命令或被忽略的写法，不能当相册名"""

ALBUM_CREATE_CMD = re.compile(r"^/?创建相册[\s:：]*(.+)$")
"""创建群相册命令：/创建相册旅游、/创建相册 吃喝玩乐"""

ALBUM_DELETE_CMD = re.compile(r"^/?删除相册[\s:：]*(.+)$")
"""删除群相册命令：/删除相册旅游（仅相册所有者可用）"""

ALBUM_LIST_CMD = re.compile(r"^/?(?:相册列表|相册清单)$")
"""列出群内相册：/相册列表"""


def clean_album_name(raw: str) -> str:
    """清理用户输入的相册名：剥掉两侧引号、末尾标点与空白"""
    name = (raw or "").strip()
    # 「旅游」。这种引号与标点交替出现的写法要剥好几轮才干净
    for _ in range(4):
        trimmed = _NAME_TAIL.sub("", name.strip(_NAME_TRIM).strip())
        if trimmed == name:
            break
        name = trimmed
    return name.strip()


def split_album_command(text: str) -> tuple[str, str] | None:
    """把「/相册<名字> <链接>」拆成 (相册名, 剩余文本)

    相册名省略时名字为空串（调用方回落到配置里的默认相册）。不是相册命令时
    返回 ``None``。

    名字必须是**不含空白的短词**，所以「/相册 https://x」会被识别成「没写
    相册名」，而不是把 ``https`` 当成相册名；「/相册自检」这类别的命令也不会
    被当成「上传到『自检』相册」。
    """
    head = ALBUM_HEAD.match(text)
    if head is None:
        return None
    rest = text[head.end() :]
    token = ALBUM_NAME_TOKEN.match(rest)
    if token is None:
        return "", rest
    name = token.group(1)
    if _URLISH.search(name) or name in ALBUM_RESERVED_NAMES:
        return "", rest
    return name, rest[token.end() :]


# ================ 建 / 删相册：trpc 原始发包 ================
#
# 群相册走的是 qzone 的 trpc 服务 ``QunAlbum.trpc.qzone.webapp_qun_media.QunMedia``
# （列相册 GetAlbumList、列媒体 GetMediaList、建相册 AddAlbum、删相册
# DeleteAlbum）。NapCat 的内核声明了 addAlbum / deleteAlbum 却没包成 OneBot
# action，但它提供了通用通道 ``send_packet``（cmd + protobuf 十六进制体），
# 把客户端本来就会发的这两条包原样造出来即可。
#
# 服务名与字段号的依据（三处独立来源互相印证）：
#   * LLOneBot ``src/main/qqProtocol/direct-lib/cmd.ts``：
#       CreateAlbum: 'QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.AddAlbum'
#       DeleteAlbum: 'QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.DeleteAlbum'
#   * LLOneBot ``src/main/qqProtocol/mixins/group.ts``：AddAlbumReq 的字段装配
#     （requestId / 空 field2,3 / body.info{groupCode,name,desc,field5=0} /
#      sessionId / headers[fc-appid=100]）
#   * SnowLuma ``packages/proto-defs/src/oidb-actions/group-album.ts``：从 QQ NT
#     AlbumService codec 还原出的同名服务（``…QunMedia.GetAlbumList``）

_QUN_ALBUM_TRPC = "QunAlbum.trpc.qzone.webapp_qun_media.QunMedia"

#: 建相册 / 删相册的 trpc 命令字
TRPC_ADD_ALBUM = f"{_QUN_ALBUM_TRPC}.AddAlbum"
TRPC_DELETE_ALBUM = f"{_QUN_ALBUM_TRPC}.DeleteAlbum"

#: NapCat 的通用发包 action 与发包探活 action
PACKET_SEND_ACTION = "send_packet"
PACKET_STATUS_ACTION = "get_packet_status"

#: 请求头，与 QQ NT 客户端发出的包一致
_ALBUM_APPID_HEADER = ("fc-appid", "100")

#: 发包后隔多久再去复核相册列表（服务端落库有延迟）
PACKET_VERIFY_DELAY = 1.5

#: 复核最多看几次（每次间隔 PACKET_VERIFY_DELAY），避免「刚建好还没同步」被误判成失败
PACKET_VERIFY_ATTEMPTS = 3


def _pb_varint(value: int) -> bytes:
    """protobuf varint（只处理非负整数，够用）"""
    out = bytearray()
    number = max(0, int(value)) & 0xFFFFFFFF
    while True:
        chunk = number & 0x7F
        number >>= 7
        if number:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _pb_field(field_no: int, wire: int) -> bytes:
    return _pb_varint((field_no << 3) | wire)


def _pb_uint(field_no: int, value: int) -> bytes:
    """varint 字段"""
    return _pb_field(field_no, 0) + _pb_varint(value)


def _pb_raw(field_no: int, raw: bytes) -> bytes:
    """length-delimited 字段（bytes / string / 嵌套 message 都是它）"""
    return _pb_field(field_no, 2) + _pb_varint(len(raw)) + raw


def _pb_text(field_no: int, text: str) -> bytes:
    return _pb_raw(field_no, (text or "").encode("utf-8"))


def _pb_album_headers() -> bytes:
    """field 10（repeated QunAlbumMetaHeader）：每条是 {1 name, 2 value}"""
    name, value = _ALBUM_APPID_HEADER
    return _pb_raw(10, _pb_text(1, name) + _pb_text(2, value))


def _pb_envelope(body: bytes) -> bytes:
    """外层公共信封：1 requestId / 2,3 空 bytes / 4 body / 5 sessionId / 10 headers"""
    return (
        _pb_uint(1, random.randint(1, 0x7FFFFFFF))
        + _pb_raw(2, b"")
        + _pb_raw(3, b"")
        + _pb_raw(4, body)
        + _pb_text(5, album_session_id())
        + _pb_album_headers()
    )


def album_session_id(now: float | None = None, rand: int | None = None) -> str:
    """客户端会话号 ``_MMDDHHmmssSSS_xxxxx``（与 QQ NT 客户端同构）

    Args:
        now: 时间戳，测试时注入固定值
        rand: 随机尾巴，测试时注入固定值
    """
    stamp = time.time() if now is None else float(now)
    lt = time.localtime(stamp)
    millis = int((stamp - int(stamp)) * 1000)
    tail = random.randint(10000, 99999) if rand is None else int(rand)
    return "_%02d%02d%02d%02d%02d%03d_%05d" % (
        lt.tm_mon,
        lt.tm_mday,
        lt.tm_hour,
        lt.tm_min,
        lt.tm_sec,
        millis,
        tail,
    )


def build_create_album_payload(group_id: str, name: str, desc: str = "") -> str:
    """拼 ``QunMedia.AddAlbum`` 请求体 → 十六进制字符串

    字段号对照 QQ NT 内核 AlbumService（LLOneBot ``Action.AddAlbumReq``）：

        1  requestId   uint32
        2  field2      bytes（空）
        3  field3      bytes（空）
        4  body        { 1 info { 2 groupCode, 3 name, 4 desc, 5 uint32=0 } }
        5  sessionId   string
        10 headers     repeated { 1 name, 2 value }
    """
    info = (
        _pb_text(2, str(group_id))
        + _pb_text(3, name)
        + _pb_text(4, desc or "")
        + _pb_uint(5, 0)
    )
    return _pb_envelope(_pb_raw(1, info)).hex()


def build_delete_album_payload(group_id: str, album_id: str) -> str:
    """拼 ``QunMedia.DeleteAlbum`` 请求体 → 十六进制字符串

    与建相册同一个信封，body 换成 ``{ 1 groupCode, 2 albumId }``。
    """
    body = _pb_text(1, str(group_id)) + _pb_text(2, album_id)
    return _pb_envelope(body).hex()


def _pb_read_varint(raw: bytes, pos: int) -> tuple[int | None, int]:
    """读一个 varint，返回 (值, 新位置)；越界或过长返回 (None, 位置)"""
    value = 0
    shift = 0
    while pos < len(raw) and shift <= 63:
        byte = raw[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
    return None, pos


def parse_pb(raw: bytes | None) -> dict[int, list[Any]]:
    """最小 protobuf 解析：把顶层字段扫成 ``{字段号: [值, ...]}``

    varint 变成 int，length-delimited 变成 bytes。只吃这两种线格式（响应里也只
    用得到这两种），遇到别的直接停，绝不抛异常 —— 解析失败按「没解析出东西」
    处理，成败最终仍由相册列表复核决定。
    """
    fields: dict[int, list[Any]] = {}
    if not raw:
        return fields
    pos = 0
    size = len(raw)
    while pos < size:
        key, pos = _pb_read_varint(raw, pos)
        if key is None:
            break
        wire = key & 0x07
        if wire == 0:
            value, pos = _pb_read_varint(raw, pos)
            if value is None:
                break
            fields.setdefault(key >> 3, []).append(value)
        elif wire == 2:
            length, pos = _pb_read_varint(raw, pos)
            if length is None or pos + length > size:
                break
            fields.setdefault(key >> 3, []).append(raw[pos : pos + length])
            pos += length
        else:
            break  # 定长/deprecated group 不在响应里，遇到就停
    return fields


def _pb_first(fields: dict[int, list[Any]], field_no: int) -> Any:
    values = fields.get(field_no)
    return values[0] if values else None


def parse_album_packet_response(raw: bytes | None) -> tuple[int, str, str]:
    """从 AddAlbum / DeleteAlbum 的响应里取 ``(retCode, retMsg, albumId)``

    响应结构（LLOneBot ``AddAlbumResp``）：
    ``2 retCode`` / ``3 retMsg`` / ``4 body { 1 info { 1 albumId, … } }``。

    空响应按「成功」处理（``(0, "", "")``）：成败最终以相册列表复核为准，
    这里解析只是为了把服务端的拒绝原因说清楚。
    """
    fields = parse_pb(raw)
    code = _pb_first(fields, 2)
    message = _pb_first(fields, 3)
    text = (
        message.decode("utf-8", "ignore")
        if isinstance(message, (bytes, bytearray))
        else str(message or "")
    )

    album_id = ""
    body = _pb_first(fields, 4)
    if isinstance(body, (bytes, bytearray)):
        info = _pb_first(parse_pb(bytes(body)), 1)
        if isinstance(info, (bytes, bytearray)):
            candidate = _pb_first(parse_pb(bytes(info)), 1)
            if isinstance(candidate, (bytes, bytearray)):
                album_id = candidate.decode("utf-8", "ignore")

    try:
        return int(code or 0), text.strip(), album_id
    except (TypeError, ValueError):
        return 0, text.strip(), album_id


def hex_response_to_bytes(resp: Any) -> bytes | None:
    """把 ``send_packet`` 的响应（十六进制字符串）转成 bytes

    故意不复用 :meth:`GroupAlbumUploader._payload`：那个是给 JSON 响应用的，
    形如 ``123456`` 的纯十六进制串会被 ``json.loads`` 当成整数解析，字节就丢了。
    """
    if isinstance(resp, (bytes, bytearray)):
        return bytes(resp)
    if isinstance(resp, dict) and "data" in resp and any(
        key in resp for key in ("status", "retcode", "wording", "stream")
    ):
        resp = resp["data"]
    if isinstance(resp, str):
        text = resp.strip()
        if text and len(text) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in text):
            try:
                return bytes.fromhex(text)
            except ValueError:
                return None
    return None


def album_action_kwargs(
    names: tuple[str, ...], *, group_id: str, name: str, album_id: str = ""
) -> dict[str, Any]:
    """按给定的参数名列表拼一次 ``call_action`` 的关键字参数"""
    values = {
        "group_id": str(group_id),
        "name": name,
        "album_name": name,
        "group_name": name,
        "album_id": album_id,
        "desc": "",
    }
    return {key: values[key] for key in names if key in values}


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
            if action in (PACKET_SEND_ACTION, PACKET_STATUS_ACTION):
                return (
                    f"协议端不支持 {action}（原始发包需要 NapCat，"
                    "并在配置里启用 packetBackend）"
                )
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
    def _is_param_error(reason: Any) -> bool:
        """判断失败原因是不是「只是参数名/参数个数不对」

        这类失败说明 action **是存在的**，只是我们猜的参数名不对，应该换一种
        参数形状再试，而不是当成真正的失败回报给用户。
        """
        text = reason if isinstance(reason, str) else GroupAlbumUploader._fail_reason(reason)
        lowered = text.lower()
        return any(hint in lowered for hint in _PARAM_ERROR_HINTS)

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

    async def list_albums(self, bot: Any, group_id: str) -> list[Album]:
        """取回群内全部相册（名字或 ID 缺失的条目会被丢掉）

        Raises:
            AlbumError: 协议端不支持相册接口，或明确返回失败
        """
        items = await self._list_albums(bot, group_id)
        albums: list[Album] = []
        for item in items:
            name = _first(item, _NAME_KEYS)
            album_id = _first(item, _ID_KEYS)
            if name and album_id:
                albums.append(Album(album_id, name))
        return albums

    async def find_album_exact(self, bot: Any, group_id: str, name: str) -> Album | None:
        """按名字**精确**匹配相册（不做包含匹配）

        建/删相册必须有这个严格版本：包含匹配会把「旅游」命中到「旅游风景」上，
        于是「删除相册旅游」有可能删掉另一个相册。
        """
        wanted = (name or "").strip()
        if not wanted:
            return None
        for album in await self.list_albums(bot, group_id):
            if album.name == wanted:
                return album
        return None

    async def create_album(self, bot: Any, group_id: str, name: str) -> tuple[bool, str]:
        """创建一个群相册

        三级回退（见模块顶部说明）：候选 action 逐个名字×逐个参数形状试 →
        NapCat 原始发包（``send_packet`` 发 trpc ``QunMedia.AddAlbum``）→
        如实回报 + 人工步骤。建完**重新拉一次相册列表**确认真的建出来了，
        接口假装成功不算成功。

        Returns:
            tuple[bool, str]: 是否成功、面向用户的说明或失败原因
        """
        name = (name or "").strip()
        if not name:
            return False, "相册名不能为空"
        if len(name) > MAX_ALBUM_NAME:
            return False, f"相册名太长（上限 {MAX_ALBUM_NAME} 个字）"

        try:
            existing = await self.list_albums(bot, group_id)
        except AlbumError as exc:
            return False, str(exc)

        if any(album.name == name for album in existing):
            return True, f"相册「{name}」已经存在，无需创建"

        for action in _CREATE_ALBUM_ACTIONS:
            for names in _CREATE_ALBUM_PARAMS:
                resp = await self._call_quiet(
                    bot,
                    action,
                    **album_action_kwargs(
                        names, group_id=str(group_id), name=name
                    ),
                )
                if self._ok(resp, void_ok=True):
                    if await self._album_verified(bot, group_id, name, want=True):
                        logger.info(f"[album] 通过 {action} 创建相册「{name}」成功")
                        return True, f"已创建群相册「{name}」"
                    return False, (
                        f"{action} 返回成功，但相册列表里没有出现「{name}」，"
                        "协议端可能没有真正执行；请到群相册里确认"
                    )
                if self._is_missing_action(resp):
                    break  # 这个 action 名字协议端不认，换下一个名字
                reason = self._fail_reason(resp)
                if self._is_param_error(reason):
                    continue  # 只是参数名/个数不对，换一种形状再试
                return False, f"创建相册失败：{reason}"

        # 协议端没把建相册暴露成 action：改发原始包（NapCat 可走通）
        ok, detail = await self._create_album_by_packet(bot, group_id, name)
        if ok:
            return True, detail
        return False, (
            "无法用命令建相册。协议端的 OneBot 层没有建相册 action"
            f"（已试过 {'、'.join(_CREATE_ALBUM_ACTIONS)}），原始发包通路也不可用。\n"
            f"· {detail}\n"
            f"请在 QQ 群聊 → 群相册 → 新建相册，名字填「{name}」（建一次即可），"
            f"之后就能用 /相册{name} + 链接 直接上传到它。"
        )

    async def delete_album(self, bot: Any, group_id: str, name: str) -> tuple[bool, str]:
        """删除一个群相册（只删精确同名的那一个）

        与 :meth:`create_album` 同样的三级回退 + 删完复核，接口假装成功不算成功。

        Returns:
            tuple[bool, str]: 是否成功、面向用户的说明或失败原因
        """
        name = (name or "").strip()
        if not name:
            return False, "相册名不能为空"

        try:
            existing = await self.list_albums(bot, group_id)
        except AlbumError as exc:
            return False, str(exc)

        target = next((album for album in existing if album.name == name), None)
        if target is None:
            names = "、".join(album.name for album in existing) or "（一个都没有）"
            return False, f"群里没有名为「{name}」的相册。现有相册：{names}"

        for action in _DELETE_ALBUM_ACTIONS:
            for names in _DELETE_ALBUM_PARAMS:
                resp = await self._call_quiet(
                    bot,
                    action,
                    **album_action_kwargs(
                        names,
                        group_id=str(group_id),
                        name=target.name,
                        album_id=target.album_id,
                    ),
                )
                if self._ok(resp, void_ok=True):
                    if await self._album_verified(bot, group_id, name, want=False):
                        logger.info(f"[album] 通过 {action} 删除相册「{name}」成功")
                        return True, f"已删除群相册「{name}」"
                    return False, (
                        f"{action} 返回成功，但相册列表里「{name}」仍然存在，"
                        "协议端可能没有真正执行"
                    )
                if self._is_missing_action(resp):
                    break
                reason = self._fail_reason(resp)
                if self._is_param_error(reason):
                    continue
                return False, f"删除相册失败：{reason}"

        ok, detail = await self._delete_album_by_packet(bot, group_id, target)
        if ok:
            return True, detail
        return False, (
            "无法用命令删相册。协议端的 OneBot 层没有删相册 action"
            f"（已试过 {'、'.join(_DELETE_ALBUM_ACTIONS)}），原始发包通路也不可用。\n"
            f"· {detail}\n"
            f"请在 QQ 群聊 → 群相册 → 长按「{name}」→ 删除。"
        )

    # ---------------- 建 / 删相册：原始发包通路 ----------------

    async def packet_ready(self, bot: Any) -> tuple[bool, str]:
        """探测 NapCat 的原始发包能力（PacketBackend）是否可用

        ``get_packet_status`` 成功时返回空体；PacketBackend 被禁用、或当前 QQ
        版本/架构不在支持表里时，NapCat 会在参数校验阶段直接拒绝，aiocqhttp 抛
        ``ActionFailed``，这里把它的原话（含官方文档链接与错误堆栈）透给用户。
        """
        try:
            await self._call(bot, PACKET_STATUS_ACTION)
        except AlbumError as exc:
            reason = str(exc)
            # NapCat 拒绝时会把错误堆栈一起塞进消息里，回执只留前半段（含文档链接）
            if len(reason) > 200:
                reason = reason[:200].rstrip() + "…"
            return False, reason
        return True, ""

    async def _send_album_packet(self, bot: Any, cmd: str, payload_hex: str) -> bytes | None:
        """用 ``send_packet`` 发一条 trpc 包并取回响应字节

        Args:
            cmd: trpc 命令字，如 ``QunAlbum.trpc.qzone.webapp_qun_media.QunMedia.AddAlbum``
            payload_hex: protobuf 请求体的十六进制

        Returns:
            bytes | None: 响应体（拿不到就是 None，按「无返回体」处理）
        """
        resp = await self._call(
            bot, PACKET_SEND_ACTION, cmd=cmd, data=payload_hex, rsp=True
        )
        return hex_response_to_bytes(resp)

    async def _create_album_by_packet(
        self, bot: Any, group_id: str, name: str
    ) -> tuple[bool, str]:
        """发 ``QunMedia.AddAlbum`` 原始包建相册，并用相册列表复核"""
        ready, why = await self.packet_ready(bot)
        if not ready:
            return False, why

        payload = build_create_album_payload(str(group_id), name)
        logger.info(
            f"[album] 原始发包建相册「{name}」：cmd={TRPC_ADD_ALBUM} size={len(payload) // 2}B"
        )
        raw = await self._send_album_packet(bot, TRPC_ADD_ALBUM, payload)
        code, message, album_id = parse_album_packet_response(raw)
        # 一切以相册列表为准：返回码只用来解释失败原因，不用来决定成败 ——
        # 万一响应体形态与预期不同（带了别的信封），也不会把建成功的相册
        # 误报成失败。
        if await self._album_verified(
            bot, group_id, name, want=True, attempts=PACKET_VERIFY_ATTEMPTS
        ):
            logger.info(
                f"[album] 原始发包建相册「{name}」成功"
                f"（album_id={album_id or '未返回'}，返回码 {code}）"
            )
            return True, f"已创建群相册「{name}」"
        if code:
            return False, f"协议端拒绝了建相册请求：{message or f'错误码 {code}'}"
        return False, (
            "建相册请求已发给协议端（返回码 0），但相册列表里还没出现"
            f"「{name}」，可能只是还没同步；稍后发 /相册列表 再看一次"
        )

    async def _delete_album_by_packet(
        self, bot: Any, group_id: str, album: Album
    ) -> tuple[bool, str]:
        """发 ``QunMedia.DeleteAlbum`` 原始包删相册，并用相册列表复核"""
        ready, why = await self.packet_ready(bot)
        if not ready:
            return False, why

        payload = build_delete_album_payload(str(group_id), album.album_id)
        logger.info(
            f"[album] 原始发包删相册「{album.name}」"
            f"（album_id={album.album_id}）：cmd={TRPC_DELETE_ALBUM}"
        )
        raw = await self._send_album_packet(bot, TRPC_DELETE_ALBUM, payload)
        code, message, _ = parse_album_packet_response(raw)
        # 同样以相册列表为准（返回码只解释失败原因）
        if await self._album_verified(
            bot, group_id, album.name, want=False, attempts=PACKET_VERIFY_ATTEMPTS
        ):
            logger.info(f"[album] 原始发包删相册「{album.name}」成功（返回码 {code}）")
            return True, f"已删除群相册「{album.name}」"
        if code:
            return False, f"协议端拒绝了删相册请求：{message or f'错误码 {code}'}"
        return False, (
            f"删相册请求已发给协议端（返回码 0），但相册列表里「{album.name}」"
            "仍然存在，可能只是还没同步；稍后发 /相册列表 再看一次"
        )

    async def _album_verified(
        self, bot: Any, group_id: str, name: str, *, want: bool, attempts: int = 1
    ) -> bool:
        """复核相册是否真的建出来了 / 真的删掉了

        只看接口返回会把「假装成功」当成成功，所以一律回列表里查一遍：
        ``want=True`` 期望存在，``want=False`` 期望消失。列表读不到时按未通过处理。

        ``attempts > 1`` 时每次间隔 :data:`PACKET_VERIFY_DELAY` 再查一遍 ——
        原始发包通路发完立刻查列表有「还没落库」的可能，多等两次更准。
        """
        for attempt in range(max(1, int(attempts))):
            if attempt:
                await asyncio.sleep(PACKET_VERIFY_DELAY)
            try:
                albums = await self.list_albums(bot, group_id)
            except AlbumError as exc:
                logger.warning(f"[album] 复核相册「{name}」时读取列表失败：{exc}")
                continue
            found = any(album.name == name for album in albums)
            if found if want else not found:
                return True
        return False

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
