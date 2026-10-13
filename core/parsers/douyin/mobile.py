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

from astrbot.api import logger

from .urlpick import order

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
#: p9 / p6 / p11 节点正常 —— 具体排序规则见 urlpick.order()


def _https_urls(node: Any) -> list[str]:
    """从 {url_list: [...]} 形态里取出可用的 https 直链"""
    if not isinstance(node, dict):
        return []
    urls = node.get("url_list")
    if not isinstance(urls, list):
        return []
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


def _clean(url: str) -> str:
    """去掉带水印的 playwm 端点"""
    return url.replace("playwm", "play")


def _ordered(urls: list[str]) -> list[str]:
    """好节点排前面，并统一去掉 playwm 水印端点"""
    return [_clean(u) for u in order(urls)]


def _play_urls(node: Any) -> list[str]:
    """取 play_addr / cover 之类节点里的全部候选直链（按可用性排序）"""
    return _ordered(_https_urls(node))


def _play_url(node: Any) -> str | None:
    """取节点里的首选直链"""
    urls = _play_urls(node)
    return urls[0] if urls else None


def pick_image_urls(node: Any) -> list[str]:
    """图集静态图的候选直链（原图 jpeg 优先，webp 备选）"""
    return _ordered(_https_urls(node))


def pick_image_url(node: Any) -> str | None:
    """图集静态图的首选直链"""
    urls = pick_image_urls(node)
    return urls[0] if urls else None


def pick_aweme(body: Any, aweme_id: str) -> dict[str, Any] | None:
    """从 feed 响应里挑出目标作品

    **只认精确命中**：该协议返回的是「推荐流」，被请求的作品通常排在首位，
    但并非总是出现（实测部分 slides 作品就不在列表里）。此前找不到时会退而
    取第一条，会把一条毫不相干的视频当成用户要的链接发出去，因此改为严格匹配，
    匹配不到就交给上层走其它通道。
    """
    if not isinstance(body, dict):
        return None
    items = body.get("aweme_list")
    if not isinstance(items, list) or not items:
        return None
    for it in items:
        if isinstance(it, dict) and str(it.get("aweme_id")) == str(aweme_id):
            return it
    logger.debug(
        "[抖音] 移动端 feed 未包含目标作品 %s，实际返回: %s",
        aweme_id,
        [str(it.get("aweme_id")) for it in items[:5] if isinstance(it, dict)],
    )
    return None


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


def image_url_lists(aweme: dict[str, Any]) -> list[list[str]]:
    """图集每张图的候选直链（保持与 images 相同下标顺序，首选在前）"""
    return [pick_image_urls(img) for img in images(aweme)]


def image_urls(aweme: dict[str, Any]) -> list[str]:
    """图集静态图首选直链列表（保持与 images 相同下标顺序）"""
    return [urls[0] if urls else "" for urls in image_url_lists(aweme)]


def image_fallbacks(aweme: dict[str, Any]) -> dict[str, list[str]]:
    """{静态图首选直链: [备用直链...]}，供下载时逐条回退"""
    return {
        urls[0]: urls[1:] for urls in image_url_lists(aweme) if len(urls) > 1
    }


def live_urls(image: dict[str, Any]) -> list[str]:
    """单张图的实况 mp4 候选直链；不是实况图返回空列表"""
    video = image.get("video")
    if not isinstance(video, dict):
        return []
    return best_play_urls(video)


def live_url(image: dict[str, Any]) -> str | None:
    """单张图的实况 mp4 首选直链；不是实况图返回 None"""
    urls = live_urls(image)
    return urls[0] if urls else None


def best_play_urls(video: dict[str, Any]) -> list[str]:
    """从 video 对象里挑最优直链，并返回同档位的备用节点

    规则与文档一致：优先 ``is_h265 == 0``（兼容性最好，H.265 在部分客户端黑屏），
    同编码内取码率最高档；没有 ``bit_rate`` 时退回 ``play_addr``。
    返回列表首条即最优档，其余为同一文件的其它节点/档位，主选 403 时依次回退。
    """
    entries: list[tuple[int, int, list[str]]] = []  # (是否h265, 码率, 候选直链)
    for entry in video.get("bit_rate") or []:
        if not isinstance(entry, dict):
            continue
        urls = _play_urls(entry.get("play_addr"))
        if not urls:
            continue
        is_h264 = 0 if entry.get("is_h265") in (0, None) else 1
        bit_rate = entry.get("bit_rate")
        entries.append((is_h264, int(bit_rate or 0), urls))

    if entries:
        # 先按 h264 优先，再按码率降序
        entries.sort(key=lambda c: (c[0], -c[1]))
        result: list[str] = []
        for _, _, urls in entries:
            for url in urls:
                if url not in result:
                    result.append(url)
        return result

    return _play_urls(video.get("play_addr"))


def best_play_url(video: dict[str, Any]) -> str | None:
    """从 video 对象里挑首选直链"""
    urls = best_play_urls(video)
    return urls[0] if urls else None


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


def video_urls(aweme: dict[str, Any]) -> list[str]:
    """视频作品的 mp4 候选直链（首选在前）

    注意：图集作品的 ``aweme["video"]`` 里放的是**背景音乐**（.mp3），
    调用方需先用 :func:`is_image_post` 分流，这里也额外做一层过滤。
    """
    video = aweme.get("video")
    if not isinstance(video, dict):
        return []
    return [
        u
        for u in best_play_urls(video)
        if not u.endswith(".mp3") and "ies-music" not in u
    ]


def video_url(aweme: dict[str, Any]) -> str | None:
    """视频作品的首选 mp4 直链"""
    urls = video_urls(aweme)
    return urls[0] if urls else None


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
