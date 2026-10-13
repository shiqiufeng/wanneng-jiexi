# main.py

import asyncio
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core import AstrBotConfig
from astrbot.core.message.components import At, Image, Json, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .core.album import AlbumError, GroupAlbumUploader, UploadReport
from .core.arbiter import ArbiterContext, EmojiLikeArbiter
from .core.clean import CacheCleaner
from .core.config import PluginConfig
from .core.data import (
    AudioContent,
    DynamicContent,
    GraphicsContent,
    ImageContent,
    MediaContent,
    ParseResult,
    VideoContent,
)
from .core.debounce import Debouncer
from .core.download import Downloader
from .core.exception import ParseException
from .core.help import build_help
from .core.parsers import BaseParser, BilibiliParser, DouyinParser
from .core.render import Renderer
from .core.sender import MessageSender
from .core.shortlink import ShortLinkClient
from .core.utils import (
    clean_song_title,
    extract_audio,
    extract_json_url,
    sanitize_filename,
)

AUDIO_ONLY_PREFIX = re.compile(r"^/?音乐[\s:：]*")
"""仅解析音频的触发前缀，如 "/音乐" 、"音乐" """

ALL_WORKS_PREFIX = re.compile(r"^/?全部[\s:：]*")
"""主页作品清单的触发前缀，如 "/全部" 、"全部" """

ALL_MUSIC_PREFIX = re.compile(r"^/?全部音乐[\s:：]*")
"""主页音乐清单的触发前缀，如 "/全部音乐" 、"全部音乐" """

ALBUM_PREFIX = re.compile(r"^/?相册少女集[\s:：]*")
"""群相册上传的触发前缀，如 "/相册少女集 <链接>"

解析出的图片/视频会上传到群相册（相册名见配置项「相册名称」），
不会再往会话里发媒体本身。
"""

LIMIT_PARAM = re.compile(r"^(\d+)\s+")
"""清单条数参数，如 "/全部 10 <链接>"。要求数字后紧跟空白，
避免误吃链接正文里的 "5.35"、"11/03" 等"""

MODE_PARAM = re.compile(r"^(文件|语音)\s+")
"""音频发送方式参数，如 "/音乐 文件 <链接>"、"/全部音乐 语音 10 <链接>" """

MAX_LIMIT_PARAM = 200
"""清单条数参数的上限，避免超长翻页"""

HELP_CMD = re.compile(r"^/?(?:解析帮助|解析说明|帮助|parserhelp)$", re.IGNORECASE)
"""帮助命令，斜杠可省略（如 "/解析帮助"、"解析帮助"）"""

SELFTEST_CMD = re.compile(r"^/?(?:相册自检|相册检测)$")
"""群相册自检命令，斜杠可省略"""

SHORTLINK_CMD = re.compile(r"^/?(?:短链验证|短链测试)$")
"""短链自检命令，斜杠可省略"""


def parse_params(text: str) -> tuple[str, int | None, str | None]:
    """剥离命令开头的「条数」与「发送方式」参数

    两个参数可以任意顺序出现（如 "文件 10" 或 "10 文件"），返回剩余文本。

    Returns:
        tuple[str, int | None, str | None]: 剩余文本、条数、发送方式（file/voice）
    """
    limit: int | None = None
    mode: str | None = None

    while True:
        if limit is None and (matched := LIMIT_PARAM.match(text)):
            limit = max(1, min(int(matched.group(1)), MAX_LIMIT_PARAM))
            text = text[matched.end() :]
            continue
        if mode is None and (matched := MODE_PARAM.match(text)):
            mode = "file" if matched.group(1) == "文件" else "voice"
            text = text[matched.end() :]
            continue
        break

    return text, limit, mode


class ParserPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context=context)
        # 渲染器
        self.renderer = Renderer(self.cfg)
        # 下载器
        self.downloader = Downloader(self.cfg)
        # 防抖器
        self.debouncer = Debouncer(self.cfg)
        # 仲裁器
        self.arbiter = EmojiLikeArbiter()
        # 消息发送器
        self.sender = MessageSender(self.cfg, self.renderer)
        # 缓存清理器
        self.cleaner = CacheCleaner(self.cfg)
        # 关键词 -> Parser 映射
        self.parser_map: dict[str, BaseParser] = {}
        # 关键词 -> 正则 列表
        self.key_pattern_list: list[tuple[str, re.Pattern[str]]] = []

    async def initialize(self):
        """加载、重载插件时触发"""
        # 加载渲染器资源
        await asyncio.to_thread(Renderer.load_resources)
        # 注册解析器
        self._register_parser()
        # 命令健康自检（只写日志，不改行为）
        self._log_command_health()

    async def terminate(self):
        """插件卸载时触发"""
        # 关下载器里的会话
        await self.downloader.close()
        # 关所有解析器里的会话 (去重后的实例)
        unique_parsers = set(self.parser_map.values())
        for parser in unique_parsers:
            await parser.close_session()
        # 关缓存清理器
        await self.cleaner.stop()

    def _register_parser(self):
        """注册解析器（以 parser.enable 为唯一启用来源）"""
        # 所有 Parser 子类
        all_subclass = BaseParser.get_all_subclass()
        enabled_platforms = set(self.cfg.parser.enabled_platforms())

        enabled_classes: list[type[BaseParser]] = []
        enabled_names: list[str] = []
        for cls in all_subclass:
            platform_name = cls.platform.name

            if platform_name not in enabled_platforms:
                logger.debug(f"[parser] 平台未启用或未配置: {platform_name}")
                continue

            enabled_classes.append(cls)
            enabled_names.append(platform_name)

            # 一个平台一个 parser 实例
            parser = cls(self.cfg, self.downloader)

            # 关键词 → parser
            for keyword, _ in cls._key_patterns:
                self.parser_map[keyword] = parser

        logger.debug(f"启用平台: {'、'.join(enabled_names) if enabled_names else '无'}")

        # -------- 关键词-正则表（统一生成） --------
        patterns: list[tuple[str, re.Pattern[str]]] = []

        for cls in enabled_classes:
            for kw, pat in cls._key_patterns:
                patterns.append((kw, re.compile(pat) if isinstance(pat, str) else pat))

        # 长关键词优先，避免短词抢匹配
        patterns.sort(key=lambda x: -len(x[0]))

        self.key_pattern_list = patterns

        logger.debug(f"[parser] 关键词-正则对已生成: {[kw for kw, _ in patterns]}")

    def _get_parser_by_type(self, parser_type):
        for parser in self.parser_map.values():
            if isinstance(parser, parser_type):
                return parser
        raise ValueError(f"未找到类型为 {parser_type} 的 parser 实例")

    def _log_command_health(self) -> None:
        """启动时把本插件处理函数的注册/启用情况写进日志

        AstrBot 的「指令管理」可以把某条指令停用或改名，被停用的指令会在唤醒
        阶段被静默跳过（`star_handler.get_handlers_by_event_type` 里
        `if not handler.enabled: continue`），用户侧的表现就是「命令完全没反应」，
        日志里也不会有任何报错。这里打一行 INFO/WARNING，方便一眼区分
        「插件根本没注册上」和「指令被停用了」。

        只读诊断，拿不到注册表就静默跳过，不影响插件运行。
        """
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except Exception as e:  # noqa: BLE001 - 诊断用途，失败即跳过
            logger.debug(f"[万能解析] 处理函数自检跳过：{e}")
            return

        mine = [
            handler
            for handler in star_handlers_registry
            if getattr(handler, "handler_module_path", "") == __name__
        ]
        if not mine:
            logger.warning(
                "[万能解析] AstrBot 注册表里没有本插件的任何处理函数，"
                "所有命令都不会有响应，请重新加载插件"
            )
            return

        disabled = [
            handler.handler_name
            for handler in mine
            if not getattr(handler, "enabled", True)
        ]
        if disabled:
            logger.warning(
                f"[万能解析] 已注册处理函数 {len(mine)} 个，其中 {len(disabled)} 个"
                f"在「指令管理」里被停用：{'、'.join(disabled)}。"
                "被停用的指令不会有任何响应，请到面板的指令管理里重新启用"
            )
        else:
            logger.info(f"[万能解析] 已注册处理函数 {len(mine)} 个，全部启用")

    async def _to_audio_only(
        self, parse_res: ParseResult, *, file_name: str | None = None
    ) -> ParseResult:
        """把解析结果裁剪为纯音频

        优先复用解析器已产出的音频内容，否则从视频/实况中提取音轨。

        Args:
            parse_res (ParseResult): 原始解析结果
            file_name (str | None): 音频落盘文件名（含后缀），如「昵称_歌名.mp3」；
                文件发送时用文件名区分音频，为空则沿用原文件名

        Raises:
            ValueError: 没有可提取音频的媒体，或音频提取失败
        """
        if audios := parse_res.audio_contents:
            parse_res.contents = list(audios)
        else:
            media = next(
                (
                    cont
                    for cont in parse_res.contents
                    if isinstance(cont, (VideoContent, DynamicContent))
                ),
                None,
            )
            if media is None:
                raise ValueError("该作品没有可提取的音频")
            try:
                src_path = await media.get_path()
                audio_path = src_path.with_name(f"{src_path.stem}_audio.mp3")
                await extract_audio(v_path=src_path, output_path=audio_path)
            except Exception as e:
                logger.error(f"提取音频失败: {e}")
                raise ValueError(f"音频提取失败：{e}") from e
            parse_res.contents = [AudioContent(audio_path)]

        if file_name:
            parse_res.contents = await self._rename_audios(parse_res, file_name)

        # 原发送分组引用了被裁剪掉的内容，需要丢弃
        parse_res.send_groups = []
        return parse_res

    async def _rename_audios(
        self, parse_res: ParseResult, file_name: str
    ) -> list[AudioContent]:
        """把音频文件重命名成指定名称（多条时追加序号）"""
        target = Path(file_name)
        renamed: list[AudioContent] = []
        for index, audio in enumerate(parse_res.audio_contents):
            path = await audio.get_path()
            new_path = (
                path.with_name(file_name)
                if index == 0
                else path.with_name(f"{target.stem}({index + 1}){target.suffix}")
            )
            if new_path != path:
                await asyncio.to_thread(path.replace, new_path)
            renamed.append(AudioContent(new_path, audio.duration))
        return renamed

    @staticmethod
    def _build_audio_file_name(parse_res: ParseResult) -> str | None:
        """「/音乐 文件」的落盘文件名：作者名_歌曲名.mp3

        歌名识别不到时会退回作品文案，需清洗掉文案里的 #话题。
        """
        title = (
            clean_song_title(parse_res.extra.get("song_title") or parse_res.title) or ""
        )
        author = parse_res.author.name.strip() if parse_res.author else ""
        name = "_".join(part for part in (author, title) if part)
        return f"{sanitize_filename(name)}.mp3" if name else None

    @staticmethod
    def _collect_album_media(
        parse_res: ParseResult,
    ) -> tuple[list[MediaContent], list[MediaContent]]:
        """把解析结果拆成「图片 / 视频」两组待上传媒体

        图文卡片的 GraphicsContent 本身也是图片，一并算进图片组。
        """
        images: list[MediaContent] = []
        videos: list[MediaContent] = []
        for cont in parse_res.contents:
            if isinstance(cont, (ImageContent, GraphicsContent)):
                images.append(cont)
            elif isinstance(cont, (VideoContent, DynamicContent)):
                videos.append(cont)
        return images, videos

    async def _upload_to_album(self, event: AstrMessageEvent, parse_res: ParseResult):
        """把解析结果上传到群相册，并回执结果

        抖音实况：静态图按图片上传，实况 mp4 按视频上传；NapCat 版本过旧
        没有视频接口时自动退化为上传视频封面。整个过程不往会话里发媒体。
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("群相册上传仅支持 QQ（NapCat）协议端")
            return

        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result(
                "请在群聊里使用「/相册少女集」，私聊无法确定相册归属"
            )
            return

        bot = event.bot
        uploader = GroupAlbumUploader(
            name=self.cfg.album_name,
            title_comment=self.cfg.album_title_comment,
            base64_fallback=self.cfg.album_base64_fallback,
            max_media=self.cfg.album_max_media,
        )

        try:
            album = await uploader.find_album(bot, str(group_id))
        except AlbumError as e:
            yield event.plain_result(f"群相册不可用：{e}")
            return

        if album is None:
            names = await uploader.album_names(bot, str(group_id))
            if names:
                tip = (
                    "群里现有相册：" + "、".join(names) + "\n"
                    f"把插件配置里的「相册名称」改成上面其中之一即可。"
                )
            else:
                tip = (
                    "没有读到任何群相册，请确认：\n"
                    "· 群相册里确实已创建该相册，名称与配置完全一致\n"
                    "· 协议端为 NapCat 且版本 ≥ v4.18.30\n"
                    "管理员可用 /相册自检 查看逐项探测结果。"
                )
            yield event.plain_result(
                f"没有找到名为「{self.cfg.album_name}」的群相册。\n{tip}"
            )
            return

        images, videos = self._collect_album_media(parse_res)
        capacity = uploader.max_media
        report = UploadReport(album_name=album.name)

        # 图片：先全部下载再一次性上传（同一批在相册里显示为同一条）
        image_paths: list[Path] = []
        for cont in images[:capacity]:
            try:
                image_paths.append(await cont.get_path())
            except Exception as e:  # noqa: BLE001 - 单个下载失败不影响其余
                report.failures.append(f"图片下载失败：{e}")
        if len(images) > capacity:
            report.skipped.append(
                f"图片超过 {capacity} 项上限，略过 {len(images) - capacity} 张"
            )

        if image_paths:
            await uploader.upload_images(bot, str(group_id), album, image_paths, report)

        # 视频体积大，逐个下载逐个传，避免一次性占满内存
        remain = max(0, capacity - len(image_paths))
        todo = videos[:remain]
        if len(videos) > len(todo):
            report.skipped.append(f"视频超过上限，略过 {len(videos) - len(todo)} 个")
        for cont in todo:
            try:
                path = await cont.get_path()
            except Exception as e:  # noqa: BLE001
                report.failures.append(f"视频下载失败：{e}")
                continue
            cover = None
            if isinstance(cont, VideoContent):
                try:
                    cover = await cont.get_cover_path()
                except Exception:  # noqa: BLE001 - 封面只是兜底，拿不到就算了
                    cover = None
            await uploader.upload_video(
                bot, str(group_id), album, path, report, cover=cover
            )

        # 群相册上传接口没有标题参数，文案落到相册评论上
        title = (parse_res.title or parse_res.text or "").strip()
        if title and report.total and self.cfg.album_title_comment:
            if await uploader.write_title(bot, str(group_id), album, title):
                report.titles = report.total

        lines = report.lines()
        if title:
            lines.append(f"文案：{title[:80]}")
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """消息的统一入口"""
        umo = event.unified_msg_origin

        # -------- 自带命令入口（不依赖 @filter.command 通道） --------
        #
        # AstrBot 的「指令管理」允许把某条指令停用或改名，被停用的指令在唤醒阶段
        # 会被静默跳过（`if not handler.enabled: continue`），用户侧表现为
        # 「命令完全没反应，日志也不报错」。on_message 是
        # EventMessageType.ALL 处理器、不参与指令管理，因此这里用与 /音乐、/全部
        # 相同的方式兜底出命令；斜杠可有可无（唤醒前缀被剥离后两种写法都落到这里）。
        cmd = event.message_str.strip()
        if HELP_CMD.match(cmd):
            yield event.plain_result(self._help_text())
            return
        if SELFTEST_CMD.match(cmd):
            if not event.is_admin():
                yield event.plain_result(
                    f"「/相册自检」仅限管理员使用（当前 ID：{event.get_sender_id()}）"
                )
                return
            yield event.plain_result(await self._album_selftest_lines(event))
            return
        if SHORTLINK_CMD.match(cmd):
            if not event.is_admin():
                yield event.plain_result(
                    f"「/短链验证」仅限管理员使用（当前 ID：{event.get_sender_id()}）"
                )
                return
            yield event.plain_result(await self._shortlink_lines())
            return

        # 白名单
        if self.cfg.whitelist and umo not in self.cfg.whitelist:
            return

        # 黑名单
        if self.cfg.blacklist and umo in self.cfg.blacklist:
            return

        # 消息链
        chain = event.get_messages()
        if not chain:
            return

        seg1 = chain[0]
        text = event.message_str

        # 群相册上传："/相册少女集 <链接>" 时剥离前缀，解析后上传到群相册
        album_upload = False
        if self.cfg.album_enabled and (prefix := ALBUM_PREFIX.match(text)):
            album_upload = True
            text = text[prefix.end() :]

        # 仅解析音频："/音乐 <链接>" 时剥离前缀，只发送音频
        audio_only = False
        if prefix := AUDIO_ONLY_PREFIX.match(text):
            audio_only = True
            text = text[prefix.end() :]

        # 主页音乐清单："/全部音乐 <主页链接>" 时剥离前缀，只发送音乐短链清单
        # 必须先于「/全部」判断，「全部音乐」的前缀会被「全部」抢先匹配
        all_music = False
        if prefix := ALL_MUSIC_PREFIX.match(text):
            all_music = True
            text = text[prefix.end() :]

        # 主页作品清单："/全部 <主页链接>" 时剥离前缀，只发送标题+链接清单
        all_works = False
        if prefix := ALL_WORKS_PREFIX.match(text):
            all_works = True
            text = text[prefix.end() :]

        # 位置参数：条数与音频发送方式，两种顺序都可（如 "文件 10"、"10 文件"）
        limit_param: int | None = None
        mode_param: str | None = None
        if audio_only or all_music or all_works:
            text, limit_param, mode_param = parse_params(text)

        # 指定机制：专门@其他bot的消息不解析
        self_id = event.get_self_id()
        mentioned_ids: set[str] = set()
        for seg in chain:
            if isinstance(seg, At):
                mentioned_ids.add(str(seg.qq))
            elif isinstance(seg, Plain):
                mentioned_ids.update(re.findall(r"<@!?([^>\s]+)>", seg.text))
        if (
            self.cfg.require_at_in_group
            and not isinstance(event, AiocqhttpMessageEvent)
            and event.get_message_type() == MessageType.GROUP_MESSAGE
            and self_id not in mentioned_ids
        ):
            return
        if mentioned_ids and self_id not in mentioned_ids:
            return

        # 卡片解析：扫描整条消息链，兼容 @ + JSON 卡片等组合消息。
        for seg in chain:
            if not isinstance(seg, Json):
                continue
            parsed_url = extract_json_url(seg.data)
            logger.debug(f"解析Json组件: {parsed_url}")
            if parsed_url:
                text = parsed_url
                break

        # 引用解析
        reply_seg = next((seg for seg in chain if isinstance(seg, Reply)), None)
        if self.cfg.enable_reply_parse and reply_seg and reply_seg.chain:
            reply_texts = []
            for seg in reply_seg.chain:
                if isinstance(seg, Plain):
                    reply_texts.append(seg.text)
                elif isinstance(seg, Json):
                    reply_texts.append(extract_json_url(seg.data))
            if reply_texts:
                text = "".join(reply_texts)

        if not text:
            return

        # 核心匹配逻辑 ：关键词 + 正则双重判定，汇集了所有解析器的正则对。
        keyword: str = ""
        searched: re.Match[str] | None = None
        for kw, pat in self.key_pattern_list:
            if kw not in text:
                continue
            if m := pat.search(text):
                keyword, searched = kw, m
                break
        if searched is None:
            return
        logger.debug(f"匹配结果: {keyword}, {searched}")

        # 仲裁机制
        if isinstance(event, AiocqhttpMessageEvent) and not event.is_private_chat():
            raw = event.message_obj.raw_message
            if not isinstance(raw, dict):
                logger.warning(f"Unexpected raw_message type: {type(raw)}")
                return
            is_win = await self.arbiter.compete(
                bot=event.bot,
                ctx=ArbiterContext(
                    message_id=int(raw["message_id"]),
                    msg_time=int(raw["time"]),
                    self_id=int(raw["self_id"]),
                ),
            )
            if not is_win:
                logger.debug("Bot在仲裁中输了, 跳过解析")
                return
            logger.debug("Bot在仲裁中胜出, 准备解析...")

        # 基于link防抖（音频模式/作品清单/相册上传与普通解析是不同意图，不共用防抖）
        link = searched.group(0)
        if (
            not audio_only
            and not all_works
            and not all_music
            and not album_upload
            and self.debouncer.hit_link(umo, link)
        ):
            logger.warning(f"[链接防抖] 链接 {link} 在防抖时间内，跳过解析")
            return

        # 音乐清单会创建短链，解析前先防抖，避免重复消耗短码
        if all_music and self.debouncer.hit_resource(umo, f"allmusic:{link}"):
            logger.warning(f"[资源防抖] 音乐清单 {link} 在防抖时间内，跳过解析")
            return

        # 解析
        if all_music or all_works:
            try:
                douyin: DouyinParser = self._get_parser_by_type(DouyinParser)  # type: ignore
            except ValueError:
                yield event.plain_result("抖音解析器未启用")
                return
            # 交给抖音解析器自行识别链接：主页地址与主页分享短链都能处理
            try:
                if all_music:
                    parse_res = await douyin.parse_user_music_from_text(
                        text, limit_param, as_media=mode_param is not None
                    )
                else:
                    parse_res = await douyin.parse_user_works_from_text(
                        text, limit_param
                    )
            except ParseException as e:
                yield event.plain_result(str(e))
                return
        else:
            parse_res = await self.parser_map[keyword].parse(keyword, searched)

        # 基于资源ID防抖（音频/作品清单使用独立指纹，避免与普通解析互相干扰）
        resource_id = parse_res.get_resource_id()
        if audio_only:
            resource_id = f"audio:{resource_id}"
        elif all_works:
            resource_id = f"allworks:{resource_id}"
        elif album_upload:
            # 相册写入有副作用，重复触发会重复上传，用独立指纹再拦一次
            resource_id = f"album:{resource_id}"
        if not all_music and self.debouncer.hit_resource(umo, resource_id):
            logger.warning(f"[资源防抖] 资源 {resource_id} 在防抖时间内，跳过发送")
            return

        # 上传到群相册：只回执结果，不往会话里发媒体本身
        if album_upload:
            async for res in self._upload_to_album(event, parse_res):
                yield res
            return

        # 仅发送音频
        if audio_only:
            file_name = None
            if mode_param == "file":
                # 文件发送时把音频落盘成「作者名_歌曲名.mp3」，便于区分
                parser = self.parser_map[keyword]
                if isinstance(parser, DouyinParser):
                    await parser.fill_song_title(parse_res)
                file_name = self._build_audio_file_name(parse_res)
            try:
                parse_res = await self._to_audio_only(parse_res, file_name=file_name)
            except ValueError as e:
                yield event.plain_result(str(e))
                return

        # 发送：音频模式只在音频类命令里覆盖配置项
        await self.sender.send_parse_result(
            event,
            parse_res,
            audio_mode=mode_param if (audio_only or all_music) else None,
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启解析")
    async def open_parser(self, event: AstrMessageEvent):
        """开启当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.remove_blacklist(umo)
        yield event.plain_result("当前会话的解析已开启")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭解析")
    async def close_parser(self, event: AstrMessageEvent):
        """关闭当前会话的解析"""
        umo = event.unified_msg_origin
        self.cfg.add_blacklist(umo)
        yield event.plain_result("当前会话的解析已关闭")

    @filter.command("音乐")
    async def music_parser(self, event: AstrMessageEvent):
        """仅解析音频：/音乐 <链接>"""
        # 实际解析由 on_message 统一完成，此函数仅用于注册 /音乐 命令
        pass

    @filter.command("全部")
    async def all_works_parser(self, event: AstrMessageEvent):
        """主页作品清单：/全部 <主页链接>"""
        # 实际解析由 on_message 统一完成，此函数仅用于注册 /全部 命令
        pass

    @filter.command("全部音乐")
    async def all_music_parser(self, event: AstrMessageEvent):
        """主页音乐清单：/全部音乐 <主页链接>"""
        # 实际解析由 on_message 统一完成，此函数仅用于注册 /全部音乐 命令
        pass

    @filter.command("相册少女集", alias={"相册上传"})
    async def album_upload_parser(self, event: AstrMessageEvent):
        """上传到群相册：/相册少女集 <链接>"""
        # 实际解析与上传由 on_message 统一完成，此函数仅用于注册命令
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("相册自检", alias={"相册检测"})
    async def album_selftest(self, event: AstrMessageEvent):
        """检查群相册上传是否就绪：列出群内相册，并探测各接口是否可用"""
        # 实际输出由 on_message 统一完成，此函数仅用于注册命令
        pass

    async def _album_selftest_lines(self, event: AstrMessageEvent) -> str:
        """「/相册自检」的正文

        走 on_message 通道执行：AstrBot 的指令管理可以把 @filter.command 注册的
        指令停用/改名，被停用的指令在唤醒阶段被静默跳过（零回复）；on_message
        不参与指令管理，因此把命令正文放在这里才能保证一定可达。
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            return "相册自检仅支持 QQ（NapCat）协议端"

        group_id = event.get_group_id()
        if not group_id:
            return "请在群聊里使用「/相册自检」"

        bot = event.bot
        gid = str(group_id)
        uploader = GroupAlbumUploader(
            name=self.cfg.album_name,
            title_comment=self.cfg.album_title_comment,
            base64_fallback=self.cfg.album_base64_fallback,
            max_media=self.cfg.album_max_media,
        )
        lines = [f"相册自检（群 {gid}）", f"目标相册：{self.cfg.album_name}"]

        try:
            names = await uploader.album_names(bot, gid)
            album = await uploader.find_album(bot, gid)
        except AlbumError as e:
            lines.append(f"[失败] 读取群相册：{e}")
            return "\n".join(lines)

        lines.append(f"[通过] 群内相册：{'、'.join(names) if names else '（空）'}")
        if album is not None:
            lines.append(f"[通过] 目标相册命中：{album.name}（{album.album_id}）")
        elif names:
            lines.append("[失败] 相册名对不上，把「相册名称」改成上面列出的名字之一")
        else:
            lines.append(
                "[失败] 没读到任何一个群相册：确认群相册里已建好相册，"
                "且协议端 NapCat ≥ v4.18.30"
            )

        # 探测各接口是否存在（只判 action 是否认得，不会真的上传）
        if album is not None:
            probes: tuple[tuple[str, str, dict], ...] = (
                ("多图一次性上传", "upload_images_to_qun_album", {"files": []}),
                ("视频上传", "upload_video_to_qun_album", {"file": ""}),
            )
            for label, action, extra in probes:
                ok, reason = await uploader.probe(
                    bot,
                    action,
                    group_id=gid,
                    album_id=album.album_id,
                    album_name=album.name,
                    **extra,
                )
                lines.append(f"[{'通过' if ok else '失败'}] {label}：{'可用' if ok else reason}")

            ok, reason = await uploader.probe(
                bot,
                "do_group_album_comment",
                group_id=gid,
                album_id=album.album_id,
                lloc="",
                content="",
            )
            lines.append(f"[{'通过' if ok else '失败'}] 文案写评论：{'可用' if ok else reason}")

        return "\n".join(lines)

    def _enabled_platform_names(self) -> list[str]:
        """收集已启用平台的显示名称（按注册顺序去重）"""
        seen: set[int] = set()
        names: list[str] = []
        for parser in self.parser_map.values():
            if id(parser) in seen:
                continue
            seen.add(id(parser))
            names.append(parser.platform.display_name)
        return names

    @filter.command("解析帮助", alias={"解析说明", "帮助", "parserhelp"})
    async def parser_help(self, event: AstrMessageEvent):
        """查看万能解析的使用帮助"""
        # 实际输出由 on_message 统一完成，此函数仅用于注册命令
        pass

    def _help_text(self) -> str:
        """「/解析帮助」的正文"""
        return build_help(self._enabled_platform_names(), self.cfg.album_name)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("短链验证", alias={"短链测试"})
    async def verify_shortlink(self, event: AstrMessageEvent):
        """自检短链服务是否可用：配置、创建短链、短链跳转（经代理/不走代理）"""
        # 实际输出由 on_message 统一完成，此函数仅用于注册命令
        pass

    async def _shortlink_lines(self) -> str:
        """「/短链验证」的正文"""
        shortener = ShortLinkClient(self.cfg, self.downloader.client)
        results = await shortener.verify()
        return "\n".join(
            f"[{'通过' if ok else '失败'}] {name}：{detail}" for name, ok, detail in results
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("登录B站", alias={"blogin", "登录b站"})
    async def login_bilibili(self, event: AstrMessageEvent):
        """扫码登录B站"""
        parser: BilibiliParser = self._get_parser_by_type(BilibiliParser)  # type: ignore
        qrcode = await parser.login.login_with_qrcode()
        yield event.chain_result([Image.fromBytes(qrcode)])
        async for msg in parser.login.check_qr_state():
            yield event.plain_result(msg)
