from itertools import chain
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    BaseMessageComponent,
    File,
    Image,
    Node,
    Nodes,
    Plain,
    Record,
    Video,
)
from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .config import PluginConfig
from .data import (
    AudioContent,
    DynamicContent,
    FileContent,
    GraphicsContent,
    ImageContent,
    ParseResult,
    SendGroup,
    TextContent,
    VideoContent,
)
from .exception import (
    DownloadException,
    DownloadLimitException,
    DurationLimitException,
    SizeLimitException,
    ZeroSizeException,
)
from .render import Renderer
from .utils import probe_duration


class MessageSender:
    """
    消息发送器

    职责：
    - 根据解析结果（ParseResult）规划发送策略
    - 控制是否渲染卡片、是否强制合并转发
    - 将不同类型的内容转换为 AstrBot 消息组件并发送

    重要原则：
    - 不在此处做解析
    - 不在此处决定“内容是什么”
    - 只负责“怎么发”
    """

    def __init__(self, config: PluginConfig, renderer: Renderer):
        self.cfg = config
        self.renderer = renderer

    def _to_file_uri(self, path: Path) -> str:
        if not path.is_absolute():
            path = path.resolve()
        return path.as_uri()

    @staticmethod
    def _image_from_path(path: Path) -> Image:
        return Image.fromFileSystem(str(path))

    @staticmethod
    def _video_from_path(path: Path) -> Video:
        return Video.fromFileSystem(str(path))

    @staticmethod
    def _record_from_path(path: Path) -> Record:
        return Record.fromFileSystem(str(path))

    async def _audio_segment(
        self,
        cont: AudioContent,
        path: Path,
        *,
        force_file: bool = False,
        audio_mode: str | None = None,
    ) -> BaseMessageComponent:
        """决定音频以语音还是文件形式发送

        QQ 语音（Record）受时长与体积限制，超限会导致整条消息发送失败，
        因此按配置的发送模式选择，超限时自动改为文件发送。

        Args:
            cont (AudioContent): 音频内容
            path (Path): 音频文件路径
            force_file (bool): 强制以文件形式发送（用于语音发送失败后的降级重发）
            audio_mode (str | None): 本次发送强制使用的模式（"file"/"voice"），
                由「/音乐 文件」「/音乐 语音」等命令传入，为空时用配置项
        """
        mode = audio_mode or self.cfg.audio_send_mode
        as_file = force_file or mode == "file"

        if not as_file and mode == "auto":
            duration = cont.duration or await probe_duration(path)
            cont.duration = duration
            # 时长未知（探测失败）时也走文件，避免语音发送失败
            as_file = duration <= 0 or duration > self.cfg.voice_max_second

        if as_file:
            return File(name=path.name, file=self._to_file_uri(path))
        return self._record_from_path(path)

    @staticmethod
    def _has_record(segs: list[BaseMessageComponent]) -> bool:
        """判断消息段中是否包含语音（含合并转发内的语音）"""
        for seg in segs:
            if isinstance(seg, Record):
                return True
            if isinstance(seg, Nodes) and any(
                isinstance(inner, Record)
                for node in seg.nodes
                for inner in node.content
            ):
                return True
        return False

    @staticmethod
    def _iter_contents(result: ParseResult):
        return chain(result.contents, result.repost.contents if result.repost else ())

    def _build_send_plan(
        self,
        result: ParseResult,
        contents: list | tuple | None = None,
        *,
        force_merge_override: bool | None = None,
        render_card_override: bool | None = None,
    ) -> dict:
        """
        根据解析结果生成发送计划（plan）

        plan 只做“策略决策”，不做任何 IO 或发送动作。
        后续发送流程严格按 plan 执行，避免逻辑分散。
        """
        light, heavy = [], []

        # 合并主内容 + 转发内容，统一参与发送策略计算
        iterable = contents if contents is not None else self._iter_contents(result)
        for cont in iterable:
            match cont:
                case ImageContent() | GraphicsContent() | TextContent():
                    light.append(cont)
                case VideoContent() | AudioContent() | FileContent() | DynamicContent():
                    heavy.append(cont)
                case _:
                    light.append(cont)

        # 仅在“单一重媒体且无其他内容”时，才允许渲染卡片
        is_single_heavy = len(heavy) == 1 and not light
        render_card = is_single_heavy and self.cfg.single_heavy_render_card
        if render_card_override is not None:
            render_card = render_card_override
        # 内容数量：预览卡片只是附加的预览消息，不计入合并阈值
        content_count = len(light) + len(heavy)

        # 达到阈值后，强制合并转发，避免刷屏
        # 单个内容（一个作品、一张图、一个视频或一段音频）永不合并，
        # 否则「1 个媒体 + 1 张预览卡片」会被包成一条「聊天记录」
        force_merge = (
            content_count > 1 and content_count >= self.cfg.forward_threshold
        )
        if force_merge_override is not None:
            force_merge = force_merge_override

        return {
            "light": light,
            "heavy": heavy,
            "render_card": render_card,
            # 预览卡片：仅在“渲染卡片 + 不合并”时独立发送
            "preview_card": render_card and not force_merge,
            "force_merge": force_merge,
        }

    async def _send_preview_card(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ):
        """
        发送预览卡片（独立消息）

        场景：
        - 只有一个重媒体
        - 未触发合并转发
        - 卡片作为“预览”，不与正文混合
        """
        if not plan["preview_card"]:
            return

        if image_path := await self.renderer.render_card(result):
            await event.send(event.chain_result([self._image_from_path(image_path)]))

    async def _build_segments(
        self,
        result: ParseResult,
        plan: dict,
        *,
        force_audio_file: bool = False,
        audio_mode: str | None = None,
    ) -> list[BaseMessageComponent]:
        """
        根据发送计划构建消息段列表

        这里负责：
        - 下载媒体
        - 转换为 AstrBot 消息组件

        Args:
            force_audio_file (bool): 强制音频以文件形式发送
            audio_mode (str | None): 本次发送强制使用的音频模式（"file"/"voice"）
        """
        segs: list[BaseMessageComponent] = []

        # 合并转发时，卡片以内联形式作为一个消息段参与合并
        if plan["render_card"] and plan["force_merge"]:
            if image_path := await self.renderer.render_card(result):
                segs.append(self._image_from_path(image_path))

        # 轻媒体处理
        for cont in plan["light"]:
            if isinstance(cont, TextContent):
                if cont.text:
                    segs.append(Plain(cont.text))
                continue

            try:
                path: Path = await cont.get_path()
            except (DownloadLimitException, ZeroSizeException):
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case ImageContent():
                    segs.append(self._image_from_path(path))
                case GraphicsContent() as g:
                    segs.append(self._image_from_path(path))
                    # GraphicsContent 允许携带补充文本
                    if g.text:
                        segs.append(Plain(g.text))
                    if g.alt:
                        segs.append(Plain(g.alt))

        # 重媒体处理
        for cont in plan["heavy"]:
            try:
                path: Path = await cont.get_path()
            except (SizeLimitException, DurationLimitException) as exc:
                if self.cfg.show_download_fail_tip:
                    message = (
                        "此项媒体超过时长限制"
                        if isinstance(exc, DurationLimitException)
                        else "此项媒体超过大小限制"
                    )
                    segs.append(Plain(message))
                continue
            except DownloadException:
                if self.cfg.show_download_fail_tip:
                    segs.append(Plain("此项媒体下载失败"))
                continue

            match cont:
                case VideoContent() | DynamicContent():
                    segs.append(self._video_from_path(path))
                case AudioContent() as audio:
                    segs.append(
                        await self._audio_segment(
                            audio,
                            path,
                            force_file=force_audio_file,
                            audio_mode=audio_mode,
                        )
                    )
                case FileContent():
                    segs.append(File(name=path.name, file=self._to_file_uri(path)))

        return segs

    def _merge_segments_if_needed(
        self,
        event: AstrMessageEvent,
        segs: list[BaseMessageComponent],
        force_merge: bool,
    ) -> list[BaseMessageComponent]:
        """
        根据策略决定是否将消息段合并为转发节点

        合并后的消息结构：
        - 每个原始消息段成为一个 Node
        - 统一使用机器人自身身份
        """
        if not force_merge or not segs:
            return segs

        nodes = Nodes([])
        self_id = event.get_self_id()

        for seg in segs:
            nodes.nodes.append(Node(uin=self_id, name="解析器", content=[seg]))

        return [nodes]

    @staticmethod
    def _build_text_fallback(result: ParseResult) -> list[BaseMessageComponent]:
        lines: list[str] = []
        if result.header:
            lines.append(result.header)
        if result.text:
            lines.append(result.text)
        elif result.extra.get("info"):
            lines.append(str(result.extra["info"]))

        text = "\n".join(line for line in lines if line).strip()
        return [Plain(text)] if text else []

    def _resolve_groups(self, result: ParseResult) -> list[SendGroup]:
        if result.send_groups:
            return result.send_groups
        return [SendGroup(contents=list(MessageSender._iter_contents(result)))]

    async def _resend_as_file(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        plan: dict,
    ) -> bool:
        """语音发送失败后，改用文件形式重发一次"""
        segs = await self._build_segments(result, plan, force_audio_file=True)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])
        if not segs:
            return False
        try:
            await event.send(event.chain_result(segs))
            logger.info("语音发送失败，已改用文件形式发送音频")
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"音频改用文件重发仍失败： error={e}, segments={seg_meta}")
            return False

    async def _send_group(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        group: SendGroup,
        *,
        audio_mode: str | None = None,
    ) -> bool:
        plan = self._build_send_plan(
            result,
            group.contents,
            force_merge_override=group.force_merge,
            render_card_override=group.render_card,
        )

        await self._send_preview_card(event, result, plan)

        segs = await self._build_segments(result, plan, audio_mode=audio_mode)
        segs = self._merge_segments_if_needed(event, segs, plan["force_merge"])

        if not segs:
            return False

        try:
            await event.send(event.chain_result(segs))
            return True
        except Exception as e:
            seg_meta = self._collect_seg_meta(segs)
            logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            # 语音超出平台时长/体积限制时会发送失败，降级为文件重发
            if self._has_record(segs) and await self._resend_as_file(event, result, plan):
                return True
            return False

    @staticmethod
    def _collect_seg_meta(segs: list[BaseMessageComponent]) -> list[dict[str, str]]:
        """提取消息段元信息，用于失败日志定位。"""
        meta: list[dict[str, str]] = []

        for seg in segs:
            item = {"type": seg.__class__.__name__}
            for attr in ("file", "path", "url"):
                value = getattr(seg, attr, None)
                if value:
                    item["media"] = str(value)
                    break
            meta.append(item)

        return meta

    async def _send_cards(
        self,
        event: AstrMessageEvent,
        cards: list[ParseResult],
    ):
        """
        发送一组「卡片结果」

        每张卡片单独渲染为一张图片，并与该条目的链接文字放在同一个转发节点里，
        整体合并转发，避免刷屏。
        """
        nodes = Nodes([])
        self_id = event.get_self_id()

        for card in cards:
            content: list[BaseMessageComponent] = []
            if image_path := await self.renderer.render_card(card):
                content.append(self._image_from_path(image_path))
            if card.url:
                content.append(Plain(card.url))
            if not content:
                continue
            name = card.author.name if card.author else "解析器"
            nodes.nodes.append(Node(uin=self_id, name=name, content=content))

        if not nodes.nodes:
            logger.warning("作品卡片全部渲染失败，未发送任何内容")
            return

        try:
            await event.send(event.chain_result([nodes]))
        except Exception as e:
            logger.error(f"发送作品卡片失败： error={e}, nodes={len(nodes.nodes)}")

    async def send_parse_result(
        self,
        event: AstrMessageEvent,
        result: ParseResult,
        *,
        audio_mode: str | None = None,
    ):
        """
        发送解析结果的统一入口

        执行顺序固定：
        1. 构建发送计划
        2. 发送预览卡片（如有）
        3. 构建消息段
        4. 必要时合并转发
        5. 最终发送

        Args:
            audio_mode (str | None): 本次发送强制使用的音频模式（"file"/"voice"），
                由「/音乐 文件」「/全部音乐 文件」等命令传入，为空时用配置项
        """
        # 卡片列表（如「/全部」的主页作品）走独立的卡片发送流程
        if cards := result.extra.get("cards"):
            await self._send_cards(event, cards)
            return

        groups = self._resolve_groups(result)

        sent = False
        for group in groups:
            sent = await self._send_group(event, result, group, audio_mode=audio_mode) or sent

        if not sent:
            segs = self._build_text_fallback(result)
            if not segs:
                logger.warning("发送结果为空，不执行发送")
                return

            try:
                await event.send(event.chain_result(segs))
            except Exception as e:
                seg_meta = self._collect_seg_meta(segs)
                logger.error(f"发送解析结果失败： error={e}, segments={seg_meta}")
            return
