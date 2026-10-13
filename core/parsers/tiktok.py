import re
from typing import Any, ClassVar

from msgspec import convert

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import Author, Platform, VideoContent
from ..download import Downloader, VideoInfo
from .base import BaseParser, handle

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic")
VIDEO_EXTS = (".mp4", ".webm", ".mov", ".m4v")


def _entry_is_video(entry: dict[str, Any]) -> bool:
    """判断 yt-dlp 图集条目是实况(Live Photo)视频还是静态图片"""
    ext = (entry.get("ext") or "").lower()
    if ext in ("jpg", "jpeg", "png", "webp", "gif", "heic"):
        return False
    if ext in ("mp4", "webm", "mov", "m4v"):
        return True
    vcodec = entry.get("vcodec")
    if vcodec and vcodec != "none":
        return True
    media_url = str(entry.get("url", "")).split("?", 1)[0].lower()
    return not media_url.endswith(IMAGE_EXTS)


class TikTokParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="tiktok", display_name="TikTok")

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.headers.update(
            {"Origin": "https://www.tiktok.com", "Referer": "https://www.tiktok.com/"}
        )
        self.mycfg = config.parser.tiktok
        self.cookiejar = CookieJar(config, self.mycfg, "tiktok.com")

    @handle("tiktok.com", r"(www|vt|vm)\.tiktok\.com/[A-Za-z0-9._?%&+\-=/#@]*")
    async def _parse(self, searched: re.Match[str]):
        # 从匹配对象中获取原始URL
        url, prefix = searched.group(0), searched.group(1)

        if prefix in ("vt", "vm"):
            url = await self.get_redirect_url(url)

        raw = await self.downloader.ytdlp_extract_raw(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
        )

        # 图集(photo mode)：yt-dlp 返回 playlist，条目里同时含静态图和实况(Live Photo)视频
        if entries := raw.get("entries"):
            return self._parse_photo_mode(raw, entries)

        # 单个视频
        info = convert(raw, VideoInfo)
        cover = self.downloader.download_img(
            url=info.thumbnail, headers=self.headers, proxy=self.proxy
        )
        video = self.downloader.ytdlp_download_video(
            url,
            cookiefile=self.cookiejar.cookie_file,
            headers=self.headers,
            proxy=self.proxy,
            format="best",
        )

        return self.result(
            title=info.title,
            author=Author(name=info.channel),
            contents=[VideoContent(video, cover, duration=info.duration)],
            timestamp=info.timestamp,
        )

    def _parse_photo_mode(self, raw: dict[str, Any], entries: list[Any]):
        image_urls: list[str] = []
        dynamic_urls: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            media_url = entry.get("url")
            if not media_url:
                continue
            if _entry_is_video(entry):
                dynamic_urls.append(media_url)
            else:
                image_urls.append(media_url)

        contents = []
        if image_urls:
            contents.extend(
                self.create_image_contents(image_urls, headers=self.headers)
            )
        # 实况(Live Photo)按 mp4 发送
        if dynamic_urls:
            contents.extend(
                self.create_dynamic_contents(dynamic_urls, headers=self.headers)
            )

        author_name = raw.get("channel") or raw.get("uploader") or ""
        return self.result(
            title=raw.get("title") or "",
            author=Author(name=author_name),
            contents=contents,
            timestamp=raw.get("timestamp"),
        )