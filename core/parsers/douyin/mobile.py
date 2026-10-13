"""抖音移动端 App 协议直连 —— 免浏览器 / 免 Cookie / 免签名

背景
----
2026-09 起抖音在边缘网关部署了 ``ArgusSecurityPlugin``，PC web 的
``/aweme/v1/web/aweme/post|detail`` 强制校验 ``x-secsdk-web-signature``，
纯 HTTP 一律 403 ``Uifid Not Found``，只能用真实浏览器页面代发（见 core/browser.py）。

实测发现
--------
**移动端 App 的推荐流协议不在 Argus 门禁内**：

    GET https://api5-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id=<id>&aid=6383
    User-Agent: com.ss.android.ugc.aweme/300500 (Linux; U; Android 13; zh_CN; ...)

无需 Cookie、无需 ``ttwid``/``a_bogus``/``msToken``/secsdk 签名，HTTP 200，
且 ``aweme_list`` 中**恰好包含被请求的那条作品本身**，字段完整：

* ``video.play_addr.url_list`` / ``video.bit_rate[].play_addr.url_list``
  —— 无水印 mp4 直链（可按 ``is_h265==0`` + 码率挑最优档）
* ``images[].video.bit_rate[].play_addr.url_list``
  —— **实况（Live Photo）mp4 直链**，这正是 web 详情接口被封后唯一缺失的东西
* ``images[].url_list`` / ``cover`` / ``author`` / ``music`` / ``desc`` / ``create_time``

因此**单条作品（视频 + 图文 + 实况）可以完全不用浏览器**，只靠普通 HTTP 即可。

局限
----
该协议只支持按作品 ID 取详情；作者主页作品列表（``/aweme/v1/aweme/post/``）
在该通道返回空列表，主页清单目前仍只能走 web 接口。
"""

from __future__ import annotations

from typing import Any

from aiohttp import ClientTimeout

# 移动端 App UA：必须是 App 形态，浏览器 UA 会被拒绝
MOBILE_UA = (
    "com.ss.android.ugc.aweme/300500 (Linux; U; Android 13; zh_CN; "
    "Pixel 6; Build/TQ3A; Cronet/58.0.2991.0)"
)

# 主节点 + 备用节点，任一个返回目标作品即可
FEED_HOSTS: tuple[str, ...] = (
    "api5-normal-c-hl.amemv.com",
    "aweme.snssdk.com",
    "api-hl.amemv.com",
    "api.amemv.com",
)

FEED_PATH = "/aweme/v1/feed/"

#: aid=6383 实测会把被请求的作品排在首位；1128 作为补充再试一轮
FEED_AIDS: tuple[str, ...] = ("6383", "1128")


#: 实测该节点对部分资源（尤其 image-cut-tos-priv 封面）返回 403，同路径的
#: p9 / p6 / p11 节点正常，因此有替代时把它排到最后
_BAD_NODE = "p3-pc-sign.douyinpic.com"


def _is_bad_node(url: str) -> bool:
    return _BAD_NODE in url


def _https_urls(node: Any) -> list[str]:
    """从 {url_list: [...]} 形态里取出可用的 https 直链"""
    if not isinstance(node, dict):
        return []
    urls = node.get("url_list")
    if not isinstance(urls, list):
        return []
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


def _ordered(urls: list[str]) -> list[str]:
    """好节点排前面（有替代时把已知会 403 的节点降到最后）"""
    if len(urls) < 2:
        return urls
    good = [u for u in urls if not _is_bad_node(u)]
    bad = [u for u in urls if _is_bad_node(u)]
    return good + bad if good else urls


def _play_url(node: Any) -> str | None:
    """取 play_addr / cover 之类节点里的直链（跳过已知坏节点，顺手去水印）"""
    urls = _ordered(_https_urls(node))
    if not urls:
        return None
    return urls[0].replace("playwm", "play")


def pick_image_url(node: Any) -> str | None:
    """图集静态图：偏取 ``url_list`` 末项（最高清无水印原图），并避开坏节点"""
    urls = _https_urls(node)
    if not urls:
        return None
    for url in reversed(urls):
        if not _is_bad_node(url):
            return url
    return urls[-1]


def pick_aweme(body: Any, aweme_id: str) -> dict[str, Any] | None:
    """从 feed 响应里挑出目标作品；找不到时退而取第一条"""
    if not isinstance(body, dict):
        return None
    items = body.get("aweme_list")
    if not isinstance(items, list) or not items:
        return None
    for it in items:
        if isinstance(it, dict) and str(it.get("aweme_id")) == str(aweme_id):
            return it
    first = items[0]
    return first if isinstance(first, dict) else None


async def fetch_aweme(
    session: Any,
    aweme_id: str,
    *,
    timeout: int = 15,
) -> dict[str, Any] | None:
    """经移动端 feed 协议取回作品 JSON（免浏览器 / 免 Cookie / 免签名）

    Args:
        session: aiohttp 的 ClientSession（插件的 self.session）
        aweme_id: 作品 ID
        timeout: 单节点超时秒数

    Returns:
        dict | None: 作品对象；全部节点失败返回 None
    """
    headers = {"User-Agent": MOBILE_UA, "Accept": "application/json"}
    req_timeout = ClientTimeout(total=timeout)
    for aid in FEED_AIDS:
        for host in FEED_HOSTS:
            url = f"https://{host}{FEED_PATH}"
            try:
                async with session.get(
                    url,
                    params={"aweme_id": aweme_id, "aid": aid},
                    headers=headers,
                    timeout=req_timeout,
                ) as resp:
                    if resp.status != 200:
                        continue
                    body = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001 - 单节点失败即换下一个
                continue
            aweme = pick_aweme(body, aweme_id)
            if aweme:
                return aweme
    return None


# ---------------------------------------------------------------- 字段提取 ----


def images(aweme: dict[str, Any]) -> list[dict[str, Any]]:
    """图集图片列表（过滤非 dict 项）"""
    raw = aweme.get("images") or aweme.get("image_list") or []
    return [i for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


def image_urls(aweme: dict[str, Any]) -> list[str]:
    """图集静态图直链列表（保持与 images 相同下标顺序）

    ``url_list`` 末项通常是最高清的无水印原图，优先取它。
    """
    return [pick_image_url(img) or "" for img in images(aweme)]


def live_url(image: dict[str, Any]) -> str | None:
    """单张图的实况 mp4 直链；不是实况图返回 None

    ``images[].video`` 里既有 ``play_addr``（唯一档），也有 ``bit_rate`` 多档，
    这里优先按码率挑 H.264 最高档。
    """
    video = image.get("video")
    if not isinstance(video, dict):
        return None
    return best_play_url(video)


def best_play_url(video: dict[str, Any]) -> str | None:
    """从 video 对象里挑最优直链

    规则与文档一致：优先 ``is_h265 == 0``（兼容性最好，H.265 在部分客户端黑屏），
    同编码内取码率最高档；没有 ``bit_rate`` 时退回 ``play_addr``。
    """
    candidates: list[tuple[int, int, str]] = []  # (是否h264, 码率, url)
    for entry in video.get("bit_rate") or []:
        if not isinstance(entry, dict):
            continue
        url = _play_url(entry.get("play_addr"))
        if not url:
            continue
        is_h264 = 0 if entry.get("is_h265") in (0, None) else 1
        bit_rate = entry.get("bit_rate")
        candidates.append((is_h264, int(bit_rate or 0), url))

    if candidates:
        # 先按 h264 优先，再按码率降序
        candidates.sort(key=lambda c: (c[0], -c[1]))
        return candidates[0][2]

    return _play_url(video.get("play_addr"))


def is_image_post(aweme: dict[str, Any]) -> bool:
    """是否图集/实况作品"""
    return bool(images(aweme)) or bool(aweme.get("is_live_photo"))


def cover_url(aweme: dict[str, Any]) -> str | None:
    """封面：origin_cover → cover → dynamic_cover"""
    video = aweme.get("video")
    if not isinstance(video, dict):
        return None
    for key in ("origin_cover", "cover", "dynamic_cover"):
        if url := _play_url(video.get(key)):
            return url
    return None


def video_url(aweme: dict[str, Any]) -> str | None:
    """视频作品的 mp4 直链

    注意：图集作品的 ``aweme["video"]`` 里放的是**背景音乐**（.mp3），
    调用方需先用 :func:`is_image_post` 分流，这里也额外做一层过滤。
    """
    video = aweme.get("video")
    if not isinstance(video, dict):
        return None
    url = best_play_url(video)
    if not url or url.endswith(".mp3") or "ies-music" in url:
        return None
    return url


def duration(aweme: dict[str, Any]) -> int:
    """时长（毫秒）"""
    video = aweme.get("video")
    if isinstance(video, dict):
        try:
            return int(video.get("duration") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(aweme.get("duration") or 0)
    except (TypeError, ValueError):
        return 0


def author_name(aweme: dict[str, Any]) -> str:
    author = aweme.get("author")
    if isinstance(author, dict):
        return str(author.get("nickname") or "")
    return ""


def author_avatar(aweme: dict[str, Any]) -> str | None:
    author = aweme.get("author")
    if not isinstance(author, dict):
        return None
    for key in ("avatar_thumb", "avatar_medium", "avatar_larger"):
        if url := _play_url(author.get(key)):
            return url
    return None


def desc(aweme: dict[str, Any]) -> str:
    return str(aweme.get("desc") or "")


def create_time(aweme: dict[str, Any]) -> int:
    try:
        return int(aweme.get("create_time") or 0)
    except (TypeError, ValueError):
        return 0


def song_title(aweme: dict[str, Any], parse_song_title) -> str | None:
    """官方识别的歌名（复用 video.parse_song_title 的解析逻辑）"""
    music = aweme.get("music")
    if not isinstance(music, dict):
        return None
    return parse_song_title(music.get("extra"))
