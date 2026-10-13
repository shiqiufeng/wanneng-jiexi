import json
from random import choice
from typing import Any
from urllib.parse import parse_qs, urlparse

from msgspec import Struct, field

from ...utils import clean_song_title
from ..base import ParseException


def parse_song_title(extra: Any) -> str | None:
    """从 music.extra 里取抖音官方识别的歌名

    extra 形如 {"music_display_mapping_title": "偷听对白 - 想念奶奶做的饭"}，
    只取 " - " 之前的部分；识别不到时返回 None。
    分享页给的是 JSON 字符串，web 接口给的是字符串或对象，这里都兼容。
    """
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except ValueError:
            return None
    if not isinstance(extra, dict):
        return None

    title = clean_song_title(extra.get("music_display_mapping_title"))
    if not title:
        return None
    return clean_song_title(title.split(" - ")[0])


class Music(Struct):
    title: str | None = None
    extra: Any = None

    @property
    def song_title(self) -> str | None:
        return parse_song_title(self.extra)


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    uri: str | None = None
    url_list: list[str] = field(default_factory=list)


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None
    music: Music | None = None

    @property
    def song_title(self) -> str | None:
        """官方识别的歌名，识别不到返回 None"""
        return self.music.song_title if self.music else None

    @property
    def image_urls(self) -> list[str]:
        return [choice(image.url_list) for image in self.images] if self.images else []

    @property
    def video_url(self) -> str | None:
        if not self.video or not self.video.play_addr.url_list:
            return None
        return choice(self.video.play_addr.url_list).replace("playwm", "play")

    @property
    def play_token(self) -> str | None:
        if not self.video:
            return None

        play_addr = self.video.play_addr
        if play_addr.uri:
            return play_addr.uri

        for url in play_addr.url_list:
            query = parse_qs(urlparse(url).query)
            if video_id := query.get("video_id"):
                return video_id[0]
        return None

    @property
    def cover_url(self) -> str | None:
        return choice(self.video.cover.url_list) if self.video else None

    @property
    def avatar_url(self) -> str | None:
        if avatar := self.author.avatar_thumb:
            return choice(avatar.url_list)
        elif avatar := self.author.avatar_medium:
            return choice(avatar.url_list)
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if len(self.item_list) == 0:
            raise ParseException("can't find data in videoInfoRes")
        return choice(self.item_list)


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(
        name="videoInfoRes", default_factory=VideoInfoRes
    )


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        if page := self.loader_data.video_page:
            return page.video_info_res.video_data
        elif page := self.loader_data.note_page:
            return page.video_info_res.video_data
        raise ParseException(
            "can't find video_(id)/page or note_(id)/page in router data"
        )
