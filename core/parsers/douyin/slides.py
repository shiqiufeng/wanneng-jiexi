from msgspec import Struct, field

from .urlpick import order


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    # avatar_larger: Avatar
    avatar_thumb: Avatar


class SlidesData(Struct):
    author: Author
    desc: str
    create_time: int
    images: list[Image]

    @property
    def name(self) -> str:
        return self.author.nickname

    @property
    def avatar_url(self) -> str | None:
        urls = order(self.author.avatar_thumb.url_list)
        return urls[0] if urls else None

    @property
    def image_url_lists(self) -> list[list[str]]:
        """每张图的全部候选直链（按可用性排序，首选在前）

        接口一次会给 3~4 个等价节点，个别节点（``*-pc-sign``、``*-ex-gddgtc-*``）
        会 403，因此保留整组候选供下载时回退，而不是随机挑一条。
        """
        return [order(image.url_list) for image in self.images]

    @property
    def image_urls(self) -> list[str]:
        return [urls[0] if urls else "" for urls in self.image_url_lists]

    @property
    def image_fallbacks(self) -> dict[str, list[str]]:
        """{主选直链: [备用直链...]}"""
        return {
            urls[0]: urls[1:]
            for urls in self.image_url_lists
            if len(urls) > 1
        }


class SlidesInfo(Struct):
    aweme_details: list[SlidesData] = field(default_factory=list)
