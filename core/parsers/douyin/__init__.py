import random
import re
from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import urlencode, urlparse

import msgspec
from aiohttp import ClientError

from astrbot.api import logger

from ...browser import BrowserFetcher
from ...config import PluginConfig
from ...cookie import CookieJar
from ...data import AudioContent, MediaContent, ParseResult, TextContent, VideoContent
from ...shortlink import ShortLinkClient
from ...utils import clean_song_title, sanitize_filename
from ..base import (
    BaseParser,
    Downloader,
    ParseException,
    Platform,
    RedirectException,
    handle,
)
from .abogus import build_abogus_url
from . import mobile
from .mobile import fetch_aweme as fetch_mobile_aweme
from .video import RouterData, parse_song_title


@dataclass(slots=True)
class ProbedVideo:
    url: str
    size: int
    headers: dict[str, str]


class DouyinParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="douyin", display_name="抖音")
    PLAY_RATIOS: ClassVar[tuple[str, ...]] = ("1080p", "720p", "540p", "360p")
    TTWID_REGISTER_URL: ClassVar[str] = (
        "https://ttwid.bytedance.com/ttwid/union/register/"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.douyin
        self.cookiejar = CookieJar(config, self.mycfg, domain="douyin.com")
        self._set_cookies()

        # 浏览器兜底开关：抖音 2026-09 起对 web 接口强制校验 x-secsdk-web-signature，
        # 纯 HTTP 一律 403，只能借真实浏览器页面代发
        self.browser_fallback = self.mycfg.browser_fallback is not False
        self.browser = BrowserFetcher(
            headless=self.mycfg.browser_headless is not False,
            # 空值 / "auto" 交由 BrowserFetcher 按平台排序（Linux 不会白试 msedge）
            channel=self.mycfg.browser_channel or "auto",
            idle_timeout=int(self.mycfg.browser_idle_timeout or 300),
        )

    async def close_session(self) -> None:
        """关闭解析器时一并释放浏览器"""
        await self.browser.close()
        await super().close_session()

    def _set_cookies(self, cookies_str: str = ""):
        """设置cookie到请求头"""
        cookies_str = cookies_str or self.cookiejar.cookies_str
        if cookies_str:
            self.ios_headers["Cookie"] = cookies_str
            self.android_headers["Cookie"] = cookies_str

    @property
    def media_headers(self) -> dict[str, str]:
        """媒体直链下载用请求头

        抖音接口返回的 url_list 顺序是随机的，其中 v26-web.douyinvod.com 这类
        节点带防盗链，缺少抖音域 Referer 时直接 403（表现为「此项媒体下载失败」），
        host 无法预知，故统一带上 Referer。
        """
        return {**self.android_headers, "Referer": "https://www.douyin.com/"}

    def _sync_headers_for_url(self, url: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        if cookies_str := self.cookiejar.get_cookie_header_for_url(url):
            headers["Cookie"] = cookies_str
        elif self._is_iesdouyin_url(url):
            if cookies_str := self.cookiejar.get_cookie_header(domain="iesdouyin.com"):
                headers["Cookie"] = cookies_str
        return headers

    @staticmethod
    def _is_iesdouyin_url(url: str) -> bool:
        hostname = urlparse(url).hostname or ""
        return hostname == "iesdouyin.com" or hostname.endswith(".iesdouyin.com")

    def _has_ttwid(self) -> bool:
        cookies = self.cookiejar.get(domain="iesdouyin.com") or {}
        return bool(cookies.get("ttwid"))

    # https://v.douyin.com/_2ljF4AmKL8
    @handle("v.douyin", r"v\.douyin\.com/[a-zA-Z0-9_\-]+")
    @handle("jx.douyin", r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")
    async def _parse_short_link(self, searched: re.Match[str]):
        url = f"https://{searched.group(0)}"
        return await self.parse_with_redirect(url)

    # https://www.douyin.com/video/7521023890996514083
    # https://www.douyin.com/note/7469411074119322899
    @handle("", r"(?<![A-Za-z0-9_/=:%?&.-])(?P<vid>\d{18,20})(?!\d)")
    @handle("aweme_id", r"aweme_id[=:/\s]+(?P<vid>\d{10,})")
    @handle("aweme", r"aweme/(?P<vid>\d{10,})")
    @handle("douyin", r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)")
    @handle("iesdouyin", r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    @handle("m.douyin", r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)")
    # https://jingxuan.douyin.com/m/video/7574300896016862490?app=yumme&utm_source=copy_link
    @handle(
        "jingxuan.douyin",
        r"jingxuan\.douyin.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    )
    async def _parse_douyin(self, searched: re.Match[str]):
        ty = searched.groupdict().get("ty") or "video"
        vid = searched.group("vid")
        logger.debug(f"[抖音] 解析类型: {ty}, ID: {vid}")
        if ty == "slides":
            return await self.parse_slides(vid)

        await self.ensure_ttwid()
        share_url = self._build_iesdouyin_url(ty, vid)
        logger.debug(f"[抖音] 使用 canonical share 页解析: {share_url}")

        result: "ParseResult | None" = None
        try:
            result = await self.parse_video(share_url, vid)
        except ParseException as e:
            logger.warning(f"[抖音] canonical share 页解析失败 {share_url}, 错误: {e}")

        if result is not None and result.contents:
            return result

        # 分享页拿不到内容（空壳 HTML / 被风控）时，改走移动端 App 协议：
        # 免 Cookie、免签名、免浏览器，且能拿到实况(Live Photo) mp4 直链
        if mobile_result := await self.parse_mobile(vid):
            logger.info(f"[抖音] 分享页不可用，已改用移动端协议解析 {vid}")
            return mobile_result

        raise ParseException("分享已删除或资源直链提取失败, 请稍后再试")

    async def parse_mobile(self, aweme_id: str) -> ParseResult | None:
        """纯移动端协议解析单条作品（视频 / 图文 / 实况通吃，全程不用浏览器）

        实况直链就在 ``images[].video.bit_rate[].play_addr`` 里，因此「有实况的图
        直接发实况 mp4、其余发静态图」，与分享页路线的行为保持一致。
        """
        aweme = await self._fetch_aweme_mobile(aweme_id)
        if not aweme:
            return None

        contents: list[MediaContent] = []
        if mobile.is_image_post(aweme):
            static_urls = mobile.image_urls(aweme)
            for index, image in enumerate(mobile.images(aweme)):
                if live := mobile.live_url(image):
                    contents.extend(
                        self.create_dynamic_contents([live], headers=self.media_headers)
                    )
                elif index < len(static_urls) and static_urls[index]:
                    contents.extend(
                        self.create_image_contents(
                            [static_urls[index]], headers=self.media_headers
                        )
                    )
        elif url := mobile.video_url(aweme):
            contents.append(
                self.create_video_content(
                    url,
                    mobile.cover_url(aweme),
                    mobile.duration(aweme),
                    headers=self.media_headers,
                )
            )

        if not contents:
            logger.warning(f"[抖音] 移动端协议未取到可用媒体: {aweme_id}")
            return None

        author = self.create_author(
            mobile.author_name(aweme),
            mobile.author_avatar(aweme),
            headers=self.ios_headers,
        )
        return self.result(
            title=mobile.desc(aweme),
            author=author,
            contents=contents,
            timestamp=mobile.create_time(aweme),
            extra={
                "song_title": mobile.song_title(aweme, parse_song_title),
                "aweme_id": aweme_id,
            },
        )

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}/"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}/"

    async def ensure_ttwid(self) -> None:
        if self._has_ttwid():
            return

        logger.debug("[抖音] 当前缺少匿名 ttwid，尝试注册")
        headers = self.ios_headers.copy()
        headers.update(
            {
                "Content-Type": "application/json",
                "Referer": "https://www.iesdouyin.com/",
            }
        )
        payload = {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.iesdouyin.com",
            "union": True,
            "fid": "",
        }
        try:
            async with self.session.post(
                self.TTWID_REGISTER_URL,
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    raise ParseException(f"ttwid register status: {resp.status}")
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                self.cookiejar.update_from_response(set_cookie_headers)
                self._set_cookies()
                body = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            raise ParseException("ttwid register failed") from e

        if not isinstance(body, dict):
            raise ParseException("ttwid register returned invalid body")

        if callback_url := body.get("redirect_url"):
            callback_headers = self._sync_headers_for_url(callback_url)
            callback_headers["Referer"] = "https://www.iesdouyin.com/"
            try:
                async with self.session.get(
                    callback_url,
                    headers=callback_headers,
                    allow_redirects=False,
                ) as resp:
                    if resp.status >= 400:
                        raise ParseException(f"ttwid callback status: {resp.status}")
                    set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                    self.cookiejar.update_from_response(set_cookie_headers)
                    self._set_cookies()
            except (ClientError, TimeoutError) as e:
                raise ParseException("ttwid callback failed") from e

        if not self._has_ttwid():
            raise ParseException("ttwid register returned no cookie")

    async def parse_with_redirect(self, url: str) -> "ParseResult":
        """先重定向再解析，并更新 cookies"""
        logger.debug(f"[抖音] 短链重定向请求: {url}")
        async with self.session.get(
            url, headers=self.ios_headers, allow_redirects=False
        ) as resp:
            logger.debug(f"[抖音] 短链重定向响应状态码: {resp.status}")
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            # 只有在状态码是重定向状态码时才获取 Location
            redirect_url = url
            if resp.status in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location", url)
                logger.debug(f"[抖音] 重定向到: {redirect_url}")

        if redirect_url == url:
            raise ParseException(f"无法重定向 URL: {url}")

        keyword, searched = self.search_url(redirect_url)
        return await self.parse(keyword, searched)

    async def parse_video(self, url: str, video_id: str | None = None):
        await self.ensure_ttwid()
        share_headers = self._sync_headers_for_url(url)
        async with self.session.get(
            url, headers=share_headers, allow_redirects=False
        ) as resp:
            if resp.status != 200:
                raise ParseException(f"status: {resp.status}")
            text = await resp.text()
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

        pattern = re.compile(
            pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )
        matched = pattern.search(text)

        if not matched or not matched.group(1):
            logger.debug("[抖音] 未在HTML中找到 window._ROUTER_DATA")
            raise ParseException("can't find _ROUTER_DATA in html")

        logger.debug("[抖音] 成功提取 window._ROUTER_DATA")

        video_data = msgspec.json.decode(
            matched.group(1).strip(), type=RouterData
        ).video_data
        logger.debug(
            f"[抖音] 解析成功 - 作者: {video_data.author.nickname}, 描述: {video_data.desc[:50]}..."
        )
        # 使用新的简洁构建方式
        contents = []

        # 添加图片/实况内容：有实况的图只发实况，其余发静态图
        if image_urls := video_data.image_urls:
            live_map = await self._fetch_live_map(video_id) if video_id else {}
            logger.debug(
                f"[抖音] 检测到图文内容，图片数量: {len(image_urls)}，"
                f"实况数量: {len(live_map)}"
            )
            contents.extend(
                self._create_media_contents(
                    image_urls, live_map, headers=self.media_headers
                )
            )

        # 添加视频内容
        elif video_data.video:
            cover_url = video_data.cover_url
            duration = video_data.video.duration if video_data.video else 0
            logger.debug(f"[抖音] 检测到视频内容，时长: {duration}秒")
            video_headers = self._build_media_headers(url)
            video_url = None
            if play_token := video_data.play_token:
                try:
                    probed = await self.probe_video_url(play_token, url)
                    video_url = probed.url
                    video_headers = probed.headers
                    logger.debug(
                        f"[抖音] play 端点探测成功，文件大小: {probed.size} 字节"
                    )
                except ParseException as e:
                    logger.warning(f"[抖音] play 端点探测失败，回退 play_addr: {e}")
            video_url = video_url or video_data.video_url
            if video_url:
                contents.append(
                    self.create_video_content(
                        video_url, cover_url, duration, headers=video_headers
                    )
                )

        # 构建作者
        author = self.create_author(
            video_data.author.nickname, video_data.avatar_url, headers=self.ios_headers
        )

        return self.result(
            title=video_data.desc,
            author=author,
            contents=contents,
            timestamp=video_data.create_time,
            # 供「/音乐 文件」把音频落盘成「作者_歌名.mp3」
            extra={"song_title": video_data.song_title, "aweme_id": video_id},
        )

    @staticmethod
    def _build_play_url(video_id: str, ratio: str) -> str:
        return (
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={video_id}&ratio={ratio}"
        )

    def _build_media_headers(self, referer: str) -> dict[str, str]:
        headers = self.ios_headers.copy()
        headers.pop("Cookie", None)
        headers["Referer"] = referer
        return headers

    async def probe_video_url(self, video_id: str, referer: str) -> ProbedVideo:
        probed_by_size: dict[int, ProbedVideo] = {}

        for ratio in self.PLAY_RATIOS:
            play_url = self._build_play_url(video_id, ratio)
            headers = self._build_media_headers(referer)
            headers["Range"] = "bytes=0-1"
            try:
                async with self.session.get(
                    play_url,
                    headers=headers,
                    allow_redirects=True,
                ) as resp:
                    if resp.status >= 400:
                        logger.debug(
                            f"[抖音] ratio={ratio} 探测失败，状态码: {resp.status}"
                        )
                        continue
                    size = self._extract_response_size(resp.headers)
                    if size <= 0:
                        logger.debug(f"[抖音] ratio={ratio} 未拿到有效文件大小")
                        continue
                    final_url = str(resp.url)
            except (ClientError, TimeoutError) as e:
                logger.debug(f"[抖音] ratio={ratio} 探测请求失败: {e}")
                continue

            probed_by_size.setdefault(
                size, ProbedVideo(final_url, size, self._build_media_headers(referer))
            )

        if not probed_by_size:
            raise ParseException("can't probe play endpoint")

        return max(probed_by_size.values(), key=lambda item: item.size)

    @staticmethod
    def _extract_response_size(headers) -> int:
        if content_range := headers.get("Content-Range"):
            if matched := re.search(r"/(\d+)\s*$", content_range):
                return int(matched.group(1))
        if content_length := headers.get("Content-Length"):
            try:
                return int(content_length)
            except ValueError:
                return 0
        return 0

    async def _fetch_slides_detail(self, video_id: str):
        """请求 slidesinfo 接口，返回第一个作品详情（接口对非"实况图集"返回空）"""
        from .slides import SlidesInfo

        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        logger.debug(f"[抖音] 请求参数: {params}")
        async with self.session.get(
            url, params=params, headers=self.android_headers
        ) as resp:
            logger.debug(f"[抖音] 幻灯片API响应状态码: {resp.status}")
            resp.raise_for_status()
            # 从响应中提取 Set-Cookie 并更新
            set_cookie_headers = resp.headers.getall("Set-Cookie", [])
            self.cookiejar.update_from_response(set_cookie_headers)
            self._set_cookies()

            response_text = await resp.read()
            logger.debug(f"[抖音] 幻灯片API响应体大小: {len(response_text)} 字节")
            slides_info = msgspec.json.decode(response_text, type=SlidesInfo)

        if not slides_info.aweme_details:
            return None
        return slides_info.aweme_details[0]

    async def _fetch_live_map(self, video_id: str) -> dict[int, str]:
        """补充查询实况(动态)直链，返回 {图片下标: 实况 mp4 直链}

        分享页 HTML 和 slidesinfo 接口里的 images[].video 恒为 null，
        实况直链只在带 a_bogus 签名的 web 详情接口里返回，因此查
        aweme/detail 接口取 images[].video.play_addr。下标与分享页的
        images 顺序一致（已实测比对），可用于剔除同一张图的静态图。
        """
        try:
            detail = await self._fetch_aweme_detail(video_id)
        except (ClientError, TimeoutError, ValueError, ParseException) as e:
            # 这里若静默失败，用户只会看到「实况变静态图」而没有任何提示，
            # 因此用 warning 级记录，便于在面板日志里定位。
            logger.warning(f"[抖音] 实况直链获取失败，本次将发送静态图: {e}")
            return {}

        if not detail:
            logger.warning("[抖音] 实况直链获取失败：详情接口未返回作品数据")
            return {}

        live_map: dict[int, str] = {}
        for index, image in enumerate(detail.get("images") or []):
            if not isinstance(image, dict):
                continue
            video = image.get("video")
            if isinstance(video, dict) and (url := self._pick_url_list(video, "play_addr")):
                live_map[index] = url
        return live_map

    def _create_media_contents(
        self,
        image_urls: list[str],
        live_map: dict[int, str],
        headers: dict[str, str],
    ) -> list[MediaContent]:
        """按图生成内容：有实况的图只发实况，其余发静态图"""
        contents: list[MediaContent] = []
        count = max(len(image_urls), max(live_map, default=-1) + 1)
        for index in range(count):
            if live_url := live_map.get(index):
                contents.extend(
                    self.create_dynamic_contents(
                        [live_url], headers=self.media_headers
                    )
                )
            elif index < len(image_urls):
                contents.extend(
                    self.create_image_contents([image_urls[index]], headers=headers)
                )
        return contents

    async def parse_slides(self, video_id: str):
        slides_data = await self._fetch_slides_detail(video_id)
        if slides_data is None:
            # slidesinfo 只服务「纯幻灯片」作品，普通图文/实况会返回 filter_list；
            # 这类作品改走移动端 App 协议，同样不需要浏览器
            if mobile_result := await self.parse_mobile(video_id):
                logger.info(f"[抖音] slidesinfo 未收录，已改用移动端协议解析 {video_id}")
                return mobile_result
            raise ParseException("slidesinfo 接口未返回作品数据")
        logger.debug(
            f"[抖音] 幻灯片解析成功 - 作者: {slides_data.name}, 描述: {slides_data.desc[:50]}..."
        )
        contents = []

        # 添加图片/实况内容：有实况的图只发实况，其余发静态图
        if image_urls := slides_data.image_urls:
            live_map = await self._fetch_live_map(video_id)
            logger.debug(
                f"[抖音] 检测到幻灯片图片，数量: {len(image_urls)}，"
                f"实况数量: {len(live_map)}"
            )
            contents.extend(
                self._create_media_contents(
                    image_urls, live_map, headers=self.media_headers
                )
            )

        # 构建作者
        author = self.create_author(
            slides_data.name, slides_data.avatar_url, headers=self.android_headers
        )

        return self.result(
            title=slides_data.desc,
            author=author,
            contents=contents,
            timestamp=slides_data.create_time,
        )

    # ---------------- PC web 接口（a_bogus 签名） ----------------
    # 用于主页作品列表（/全部）和图集实况直链补充

    USER_POST_URL: ClassVar[str] = "https://www.douyin.com/aweme/v1/web/aweme/post/"
    DETAIL_URL: ClassVar[str] = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
    """作品详情接口，是图集实况(mp4)直链的唯一来源"""
    ABOGUS_UA: ClassVar[str] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
    )
    """主页作品接口要求 PC 端 UA，与 a_bogus 指纹中使用的 UA 必须一致"""
    USER_POST_PAGE_COUNT: ClassVar[int] = 20
    """单次请求的作品条数"""

    # https://www.douyin.com/user/MS4wLjABAAAA...
    # https://www.iesdouyin.com/share/user/MS4wLjABAAAA...
    @handle(
        "user",
        r"(?:douyin|iesdouyin)\.com/(?:share/)?user/(?P<sec_uid>[A-Za-z0-9_\-]+)",
    )
    async def _parse_user_home(self, searched: re.Match[str]):
        sec_uid = searched.group("sec_uid")
        logger.debug(f"[抖音] 解析用户主页作品列表: {sec_uid}")
        return await self.parse_user_works(sec_uid)

    @staticmethod
    def _gen_false_mstoken() -> str:
        base_str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"
        return "".join(random.choice(base_str) for _ in range(126)) + "=="

    def _build_web_params(self) -> dict[str, str]:
        """PC web 端接口的公共参数

        参数顺序会影响 a_bogus 签名，顺序参考 f2 的实现，勿随意调整。
        """
        return {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "publish_video_strategy_type": "2",
            "pc_libra_divert": "Windows",
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Edge",
            "browser_version": "130.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "130.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "12",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "100",
            "msToken": self._gen_false_mstoken(),
        }

    def _build_user_post_params(self, sec_uid: str) -> dict[str, str]:
        """主页作品列表接口参数"""
        params = self._build_web_params()
        params.update(
            {
                "max_cursor": "0",
                "count": str(self.USER_POST_PAGE_COUNT),
                "sec_user_id": sec_uid,
            }
        )
        return params

    def _build_detail_params(self, aweme_id: str) -> dict[str, str]:
        """作品详情接口参数"""
        params = self._build_web_params()
        params["aweme_id"] = aweme_id
        return params

    @staticmethod
    def _pick_url_list(node: dict, key: str) -> str | None:
        """从 node[key].url_list 里取第一个地址"""
        child = node.get(key)
        if isinstance(child, dict) and (urls := child.get("url_list")):
            return urls[0]
        return None

    async def _request_web_api(
        self, endpoint: str, params: dict[str, str], referer: str
    ) -> dict:
        """请求 PC web 接口，返回 JSON body

        先走普通 HTTP（带 a_bogus 签名）；抖音自 2026-09 起对 post/detail 等接口
        加了 Argus 风控，纯 HTTP 会 403 `Uifid Not Found`，此时改用真实浏览器
        页面代发——页面里的 secsdk 会自动补上 x-secsdk-web-signature。
        """
        try:
            return await self._request_web_api_http(endpoint, params, referer)
        except ParseException as e:
            if not (self.browser_fallback and self._is_risk_control(e)):
                raise
            logger.warning(f"[抖音] 接口被风控拦截，改用浏览器代发: {e}")
            return await self._request_web_api_browser(endpoint, params, referer)

    @staticmethod
    def _is_risk_control(err: Exception) -> bool:
        """判断异常是否属于抖音风控拦截（403 / Argus / Uifid）"""
        text = str(err)
        return any(k in text for k in ("403", "Argus", "Uifid", "uifid"))

    async def _request_web_api_http(
        self, endpoint: str, params: dict[str, str], referer: str
    ) -> dict:
        """带 a_bogus 签名的普通 HTTP 请求"""
        await self.ensure_ttwid()

        headers = {
            "User-Agent": self.ABOGUS_UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
        }
        # ttwid 注册时挂在 .iesdouyin.com 下，而接口在 www.douyin.com，
        # 按域名筛选会拿不到 ttwid，这里直接发送完整 cookie 串
        if cookies_str := self.cookiejar.cookies_str:
            headers["Cookie"] = cookies_str

        url = build_abogus_url(endpoint, params, self.ABOGUS_UA)
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise ParseException(f"抖音接口状态码: {resp.status}")
                set_cookie_headers = resp.headers.getall("Set-Cookie", [])
                self.cookiejar.update_from_response(set_cookie_headers)
                self._set_cookies()
                body = await resp.json(content_type=None)
        except ParseException:
            raise
        except (ClientError, TimeoutError, ValueError) as e:
            raise ParseException("请求抖音接口失败") from e

        if not isinstance(body, dict):
            raise ParseException("抖音接口返回异常")

        status = body.get("status_code")
        if status not in (0, None):
            raise ParseException(f"抖音接口返回异常: status_code={status}")

        return body

    async def _request_web_api_browser(
        self, endpoint: str, params: dict[str, str], referer: str
    ) -> dict:
        """经真实浏览器页面代发请求

        页面内的 secsdk 会自动补上签名，因此这里无需 a_bogus，直接拼原始参数。
        """
        if not self.browser.available():
            raise ParseException(
                "抖音接口被风控拦截，且当前环境未安装 playwright，无法启用浏览器兜底。"
                "「/全部」主页作品清单目前仍只能靠浏览器代发（单条作品链接已改为免浏览器）；"
                "如需该功能请执行 python -m playwright install --with-deps chromium"
            )

        url = f"{endpoint}?{urlencode(params, safe='')}"
        body = await self.browser.get_json(url, referer=referer)
        if body is None:
            reason = self.browser.last_error or "未知原因"
            raise ParseException(f"浏览器代发请求失败：{reason}")

        status = body.get("status_code")
        if status not in (0, None):
            raise ParseException(f"抖音接口返回异常: status_code={status}")

        return body

    async def _fetch_aweme_detail(self, aweme_id: str) -> dict | None:
        """查作品详情

        **优先走移动端 App 协议**（免 Cookie、免签名、免浏览器）——实况 mp4 直链
        就在返回的 ``images[].video`` 里（见 mobile.py 的实测说明）。只有在移动端
        也取不到时，才退回 PC web 详情接口，那条路被 Argus 风控，需要浏览器代发。
        """
        if detail := await self._fetch_aweme_mobile(aweme_id):
            return detail
        return await self._fetch_aweme_web(aweme_id)

    async def _fetch_aweme_mobile(self, aweme_id: str) -> dict | None:
        """移动端 feed 协议取作品详情（免浏览器）"""
        try:
            aweme = await fetch_mobile_aweme(self.session, aweme_id)
        except Exception as e:  # noqa: BLE001 - 取不到就走 web 兜底
            logger.debug(f"[抖音] 移动端详情接口异常: {e}")
            return None
        if aweme:
            logger.debug(f"[抖音] 移动端协议命中作品详情: {aweme_id}")
        return aweme

    async def _fetch_aweme_web(self, aweme_id: str) -> dict | None:
        """PC web 详情接口（被 Argus 风控，需要浏览器代发签名）"""
        body = await self._request_web_api(
            self.DETAIL_URL,
            self._build_detail_params(aweme_id),
            referer=f"https://www.douyin.com/video/{aweme_id}",
        )
        detail = body.get("aweme_detail")
        return detail if isinstance(detail, dict) else None

    @staticmethod
    def _pick_cover(item: dict) -> str | None:
        """取作品封面：视频取 video.cover，图集取首图"""
        video = item.get("video")
        if isinstance(video, dict):
            for key in ("cover", "origin_cover", "dynamic_cover"):
                node = video.get(key)
                if isinstance(node, dict) and (urls := node.get("url_list")):
                    return urls[0]
        images = item.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and (urls := image.get("url_list")):
                    return urls[0]
        return None

    async def fetch_user_works(self, sec_uid: str, limit: int) -> list[dict]:
        """带 a_bogus 签名翻页拉取主页作品，最多 limit 条"""
        works: list[dict] = []
        max_cursor = "0"

        while len(works) < limit:
            params = self._build_user_post_params(sec_uid)
            params["max_cursor"] = max_cursor
            params["count"] = str(min(self.USER_POST_PAGE_COUNT, limit - len(works)))
            body = await self._request_web_api(
                self.USER_POST_URL,
                params,
                referer=f"https://www.douyin.com/user/{sec_uid}",
            )

            items = [
                item for item in (body.get("aweme_list") or []) if isinstance(item, dict)
            ]
            works.extend(items)

            next_cursor = body.get("max_cursor")
            if not items or not body.get("has_more") or not next_cursor:
                break
            next_cursor = str(next_cursor)
            if next_cursor == max_cursor:
                break
            max_cursor = next_cursor

        return works[:limit]

    @staticmethod
    def _pick_avatar(author: dict) -> str | None:
        """取作者头像"""
        for key in ("avatar_thumb", "avatar_medium", "avatar_larger"):
            node = author.get(key)
            if isinstance(node, dict) and (urls := node.get("url_list")):
                return urls[0]
        return None

    @staticmethod
    def _pick_play_url(item: dict) -> str | None:
        """取作品直链

        视频取 play_addr（无水印 CDN 直链）；图集的 play_addr 会是背景音乐，
        所以图集改取首图地址。
        """
        images = item.get("images")
        if isinstance(images, list) and images:
            for image in images:
                if isinstance(image, dict) and (urls := image.get("url_list")):
                    return urls[0]
            return None

        video = item.get("video")
        if isinstance(video, dict):
            node = video.get("play_addr")
            if isinstance(node, dict) and (urls := node.get("url_list")):
                return urls[0]
        return None

    def _build_work_card(self, item: dict, author) -> "ParseResult | None":
        """把一条作品转成「卡片结果」，供 sender 渲染成卡片"""
        if not item.get("aweme_id"):
            return None

        contents: list = []
        images = item.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, dict) and (urls := first.get("url_list")):
                contents = self.create_image_contents([urls[0]], headers=self.media_headers)

        if not contents and (cover := self._pick_cover(item)):
            cover_task = self.downloader.download_img(
                cover, headers=self.media_headers, proxy=self.proxy
            )
            # path_task 不会被使用（卡片只取 cover），直接复用封面任务避免多余下载
            contents = [VideoContent(cover_task, cover_task, 0.0)]

        return ParseResult(
            platform=self.platform,
            author=author,
            title=str(item.get("desc") or "").strip() or "（无标题）",
            timestamp=int(item.get("create_time") or 0) or None,
            url=self._pick_play_url(item),
            contents=contents,
        )

    async def parse_user_works(
        self, sec_uid: str, limit: int | None = None
    ) -> "ParseResult":
        """主页作品：每条作品渲染成一张卡片 + 一条直链文字"""
        limit = limit or self.cfg.all_works_limit or 50
        works = await self.fetch_user_works(sec_uid, limit)
        if not works:
            raise ParseException("未获取到该用户的作品，可能主页不可见或没有公开作品")

        nickname = ""
        avatar_url = None
        for item in works:
            raw_author = item.get("author")
            if not isinstance(raw_author, dict):
                continue
            nickname = str(raw_author.get("nickname") or nickname)
            avatar_url = self._pick_avatar(raw_author) or avatar_url
            if nickname and avatar_url:
                break

        # 同一作者复用同一个 Author 对象，头像只会下载一次
        author = (
            self.create_author(nickname, avatar_url, headers=self.ios_headers)
            if nickname
            else None
        )

        cards = []
        shortener = ShortLinkClient(self.cfg, self.session)
        for item in works:
            card = self._build_work_card(item, author)
            if not card:
                continue
            # 卡片下方的直链换成短链；短链服务未配置或创建失败时保留原始直链
            if card.url:
                card.url = await shortener.shorten(card.url) or card.url
            cards.append(card)

        logger.debug(f"[抖音] 主页作品卡片构建完成，共 {len(cards)} 条")

        return self.result(
            title=f"{nickname or '抖音用户'}的作品列表",
            author=author,
            url=f"https://www.douyin.com/user/{sec_uid}",
            extra={"cards": cards},
        )

    @staticmethod
    def _pick_music_url(item: dict) -> str | None:
        """取作品背景音乐的 mp3 直链"""
        music = item.get("music")
        if not isinstance(music, dict):
            return None

        play_url = music.get("play_url")
        if isinstance(play_url, dict):
            for url in play_url.get("url_list") or []:
                if isinstance(url, str) and url.startswith("http"):
                    return url
            uri = play_url.get("uri")
            if isinstance(uri, str) and uri.startswith("http"):
                return uri
        return None

    @staticmethod
    def _pick_song_title(item: dict) -> str | None:
        """抖音官方识别的歌名

        原声作品的 music.title 只会是「@XXX创作的原声」，真正的歌名在
        music.extra.music_display_mapping_title 里（解析细节见 video.parse_song_title）；
        没有该字段而 music.title 又不是原声占位时，music.title 本身就是歌名。
        """
        music = item.get("music")
        if not isinstance(music, dict):
            return None
        if title := parse_song_title(music.get("extra")):
            return title

        raw_title = music.get("title")
        if isinstance(raw_title, str) and not raw_title.startswith("@"):
            return clean_song_title(raw_title)
        return None

    @staticmethod
    def _music_label(item: dict) -> str:
        """音乐名：优先抖音官方识别的歌名，识别不到时退回清洗过的作品文案

        抖音没识别出歌名（如本人原声翻唱）时只能退回作品文案，
        而文案里通常带一串 #话题，需要清洗后再用。
        """
        if title := DouyinParser._pick_song_title(item):
            return title
        return clean_song_title(item.get("desc")) or "（无标题）"

    async def _resolve_sec_uid(self, text: str) -> str:
        """从消息文本里解析抖音主页的 sec_uid

        既接受主页地址（douyin.com/user/xxx），也接受主页分享短链
        （v.douyin.com/xxx）——分享短链需要先跟随跳转才能拿到主页地址。
        """
        try:
            keyword, searched = self.search_url(text)
        except ParseException as e:
            raise ParseException("未在消息中找到抖音链接") from e

        if keyword != "user":
            url = searched.group(0)
            if not url.startswith("http"):
                url = f"https://{url}"
            try:
                resolved = await self.get_redirect_url(url)
                keyword, searched = self.search_url(resolved)
            except (ParseException, RedirectException) as e:
                raise ParseException("无法解析该抖音分享链接，请稍后再试") from e

        if keyword != "user":
            raise ParseException("该命令仅支持抖音主页链接（或主页分享链接）")

        return searched.group("sec_uid")

    async def parse_user_works_from_text(
        self, text: str, limit: int | None = None
    ) -> "ParseResult":
        """从消息文本里的抖音链接解析主页作品清单"""
        return await self.parse_user_works(await self._resolve_sec_uid(text), limit)

    async def parse_user_music_from_text(
        self, text: str, limit: int | None = None, as_media: bool = False
    ) -> "ParseResult":
        """从消息文本里的抖音链接解析音乐清单"""
        return await self.parse_user_music(
            await self._resolve_sec_uid(text), limit, as_media
        )

    async def parse_user_music(
        self, sec_uid: str, limit: int | None = None, as_media: bool = False
    ) -> "ParseResult":
        """主页全部作品的音乐

        as_media 为 False 时逐条换成短链后以文本清单发送；
        为 True 时把每首音乐下载成音频文件发送（文件名「昵称_歌名.mp3」）。
        """
        limit = limit or self.cfg.all_works_limit or 50
        works = await self.fetch_user_works(sec_uid, limit)
        if not works:
            raise ParseException("未获取到该用户的作品，可能主页不可见或没有公开作品")

        nickname = ""
        for item in works:
            raw_author = item.get("author")
            if isinstance(raw_author, dict) and (name := raw_author.get("nickname")):
                nickname = str(name)
                break

        name = nickname or "抖音用户"
        if as_media:
            contents = await self._download_user_music(works, name)
            if not contents:
                raise ParseException("未在该用户的作品中找到音乐")
            logger.debug(f"[抖音] 主页音乐音频下载任务已创建，共 {len(contents)} 条")
            return self.result(
                title=f"{name}的全部音乐",
                url=f"https://www.douyin.com/user/{sec_uid}",
                contents=contents,
            )

        shortener = ShortLinkClient(self.cfg, self.session)
        lines: list[str] = []
        seen: set[str] = set()
        for item in works:
            music_url = self._pick_music_url(item)
            # 同一首背景音乐只留一条，避免刷屏并节省短码
            if not music_url or music_url in seen:
                continue
            seen.add(music_url)
            # 短链服务未配置或创建失败时退回原始直链
            link = await shortener.shorten(music_url) or music_url
            lines.append(f"{len(lines) + 1}.{name}_{self._music_label(item)} {link}")

        if not lines:
            raise ParseException("未在该用户的作品中找到音乐直链")

        logger.debug(f"[抖音] 主页音乐清单构建完成，共 {len(lines)} 条")
        text = "\n".join(
            [f"抖音 @{nickname or '未知用户'} 的全部音乐（{len(lines)} 首）", *lines]
        )
        return self.result(
            title=f"{nickname or '抖音用户'}的全部音乐",
            url=f"https://www.douyin.com/user/{sec_uid}",
            contents=[TextContent(text)],
        )

    async def fill_song_title(self, parse_res: ParseResult) -> None:
        """补全作品官方识别的歌名

        分享页的 music 里没有 extra 字段，歌名只在作品详情接口中返回，
        因此这里按需多查一次详情。「/音乐 文件」要拿歌名当文件名时才调用。
        """
        if parse_res.extra.get("song_title"):
            return

        aweme_id = parse_res.extra.get("aweme_id")
        if not aweme_id:
            return

        try:
            detail = await self._fetch_aweme_detail(str(aweme_id))
        except (ClientError, TimeoutError, ValueError, ParseException) as e:
            logger.debug(f"[抖音] 歌名补充查询失败: {e}")
            return

        music = (detail or {}).get("music")
        if not isinstance(music, dict):
            return
        if title := parse_song_title(music.get("extra")):
            parse_res.extra["song_title"] = title
            logger.debug(f"[抖音] 歌名补充成功: {title}")

    async def _download_user_music(
        self, works: list[dict], name: str
    ) -> list[MediaContent]:
        """把主页作品的背景音乐逐条下载成音频文件"""
        contents: list[MediaContent] = []
        seen: set[str] = set()
        for item in works:
            music_url = self._pick_music_url(item)
            # 同一首背景音乐只发一次
            if not music_url or music_url in seen:
                continue
            seen.add(music_url)
            file_name = sanitize_filename(f"{name}_{self._music_label(item)}") + ".mp3"
            contents.append(
                AudioContent(
                    self.downloader.download_audio(
                        music_url,
                        audio_name=file_name,
                        headers=self.ios_headers,
                        proxy=self.proxy,
                    )
                )
            )
        return contents
