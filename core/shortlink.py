from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import ClientError, ClientTimeout
from astrbot.api import logger

if TYPE_CHECKING:
    from aiohttp import ClientSession

    from .config import PluginConfig

BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
"""短码字符表，顺序即「由小到大」的递增顺序"""

MAX_RETRY = 5
"""短码被占用时的最大顺延次数"""

REQUEST_TIMEOUT = ClientTimeout(total=30)
"""短链服务的请求超时，避免自检时长时间挂起"""

CHECK_KEY = "pycheck"
"""自检用的短码；检查开始与结束都会清理它，避免误判「短链已存在」"""

CHECK_TARGET = "https://example.com/"
"""自检用的跳转目标"""


def encode_base36(number: int) -> str:
    """把正整数编码成 36 进制短码：1 -> "1"、36 -> "10" """
    if number <= 0:
        return BASE36_ALPHABET[0]

    chars: list[str] = []
    while number > 0:
        number, remainder = divmod(number, 36)
        chars.append(BASE36_ALPHABET[remainder])
    return "".join(reversed(chars))


class ShortLinkClient:
    """自建短链服务(Url-Shorten-Worker)客户端

    短码按 36 进制在本地递增（1、2、…、9、a、…、z、10、…），
    冲突时自动顺延，保证「由小到大、越短越好」。
    """

    def __init__(self, config: PluginConfig, session: ClientSession):
        self.session = session
        self.api = (config.shortlink_api or "").strip()
        self.password = (config.shortlink_password or "").strip()
        # 短链服务在 Cloudflare 上，境内直连不通，复用插件配置的代理
        self.proxy = config.proxy or None
        self.seq_file: Path = config.data_dir / "shortlink_seq.json"
        self.base = self._derive_base()

    @property
    def enabled(self) -> bool:
        return bool(self.api and self.password)

    def _derive_base(self) -> str:
        """短链前缀：接口地址末段就是密码时（如 https://dl.example.com/你的密码），前缀取其上级"""
        if not self.api:
            return ""
        parsed = urlparse(self.api)
        if parsed.path.strip("/") == self.password:
            return f"{parsed.scheme}://{parsed.netloc}"
        return self.api.rstrip("/")

    def _load_seq(self) -> int:
        try:
            return max(int(json.loads(self.seq_file.read_text("utf-8"))["next"]), 1)
        except (OSError, ValueError, KeyError, TypeError):
            return 1

    def _save_seq(self, next_seq: int) -> None:
        try:
            self.seq_file.write_text(json.dumps({"next": next_seq}), "utf-8")
        except OSError as e:
            logger.warning(f"[短链] 计数器写入失败: {e}")

    async def _cmd(self, payload: dict, *, proxy: str | None) -> dict | None:
        """调用短链服务的命令接口

        Args:
            payload (dict): 命令体，如 {"cmd": "add", ...}
            proxy (str | None): 本次请求使用的代理，None 表示直连

        Returns:
            dict | None: 服务返回的响应体，网络异常或响应不是 JSON 对象时返回 None
        """
        try:
            async with self.session.post(
                self.api, json=payload, proxy=proxy, timeout=REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"[短链] 请求失败，状态码: {resp.status}")
                    return None
                body = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError) as e:
            logger.warning(f"[短链] 请求失败: {e}")
            return None

        if not isinstance(body, dict):
            logger.warning(f"[短链] 响应格式异常: {body}")
            return None
        return body

    async def _add(self, url: str, key: str) -> bool:
        """调用短链服务创建一个自定义短码"""
        body = await self._cmd(
            {"cmd": "add", "url": url, "key": key, "password": self.password},
            proxy=self.proxy,
        )
        if body and body.get("status") == 200:
            return True
        logger.warning(f"[短链] 创建被拒绝: {body}")
        return False

    async def _delete(self, key: str, *, proxy: str | None) -> None:
        """删除指定短码，自检前后用它清理测试记录"""
        await self._cmd(
            {"cmd": "del", "key": key, "password": self.password}, proxy=proxy
        )

    async def shorten(self, url: str) -> str | None:
        """把长链接换成短链，失败返回 None"""
        if not self.enabled:
            return None

        seq = self._load_seq()
        for _ in range(MAX_RETRY):
            key = encode_base36(seq)
            seq += 1
            if await self._add(url, key):
                # 成功后才推进计数器，失败顺延时不会占用编号
                self._save_seq(seq)
                return f"{self.base}/{key}"

        logger.warning(f"[短链] 连续 {MAX_RETRY} 次短码均被占用，放弃创建")
        return None

    async def _check_redirect(
        self, name: str, short_url: str, *, proxy: str | None
    ) -> tuple[str, bool, str]:
        """检查短链能否跳转到目标地址"""
        try:
            async with self.session.get(
                short_url, allow_redirects=False, proxy=proxy, timeout=REQUEST_TIMEOUT
            ) as resp:
                status = resp.status
                location = resp.headers.get("Location", "")
        except (ClientError, TimeoutError) as e:
            return (name, False, f"请求失败：{e}")

        if location == CHECK_TARGET:
            return (name, True, f"正常跳转到 {CHECK_TARGET}")
        return (name, False, f"状态码 {status}，未跳转到 {CHECK_TARGET}")

    async def verify(self) -> list[tuple[str, bool, str]]:
        """自检短链服务是否可用

        依次检查配置、创建短链、短链跳转；跳转分别走代理与不走代理各测一次，
        后者对应「手机/浏览器不挂代理时能否打开」，短链被墙时只有这一项失败。

        Returns:
            list[tuple[str, bool, str]]: 每项为（检查项, 是否通过, 说明）
        """
        if not self.api or not self.password:
            return [("配置", False, "接口地址或密码未填写，短链功能处于停用状态")]

        results: list[tuple[str, bool, str]] = []

        # 先清掉上次自检可能残留的短码，避免「短链已存在」被误判为不可用
        await self._delete(CHECK_KEY, proxy=self.proxy)

        body = await self._cmd(
            {
                "cmd": "add",
                "url": CHECK_TARGET,
                "key": CHECK_KEY,
                "password": self.password,
            },
            proxy=self.proxy,
        )
        if body is None:
            results.append(
                ("创建短链", False, f"服务无响应，请检查接口地址与代理设置（{self.api}）")
            )
            return results
        if body.get("status") != 200:
            results.append(("创建短链", False, f"被拒绝：{body.get('error') or body}"))
            return results

        short_url = f"{self.base}/{CHECK_KEY}"
        results.append(("创建短链", True, short_url))
        results.append(
            await self._check_redirect("短链跳转（经代理）", short_url, proxy=self.proxy)
        )
        results.append(
            await self._check_redirect("短链跳转（不走代理）", short_url, proxy=None)
        )

        await self._delete(CHECK_KEY, proxy=self.proxy)
        return results
