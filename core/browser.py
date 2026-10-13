"""真实浏览器代发请求（抖音 Argus 风控绕过）

背景
----
抖音自 2026-09 起在边缘网关启用 `ArgusSecurityPlugin`，对
`/aweme/v1/web/aweme/post/`（主页作品）、`/aweme/v1/web/aweme/detail/`（作品详情）
等接口强制校验 `x-secsdk-web-signature` 请求头。该签名只能由抖音页面内建的
secsdk 在真实浏览器环境里算出，纯 HTTP 客户端（哪怕带上 a_bogus、ttwid、完整
Cookie）都会被拒，返回：

    403 Blocked by ArgusSecurityPlugin Uifid Not Found

本模块用 Playwright 驱动一个真实浏览器打开抖音首页，待 secsdk 就绪后，
**在页面上下文里用 XMLHttpRequest 发请求**——secsdk 会 hook XHR 并自动补上
签名，于是接口重新可用。

浏览器为懒加载：首次请求时启动，空闲 `idle_timeout` 秒后自动关闭，避免常驻
占用内存。未安装 playwright 时 `available` 为 False，调用方应回退到普通 HTTP。

通道选择
--------
`channel="auto"`（默认）会按平台给出尝试顺序：

* Windows：``msedge`` → ``chrome`` → 自带 chromium
* macOS：``chrome`` → ``msedge`` → 自带 chromium
* Linux（云服务器 / 容器）：``chrome`` → ``chromium`` → 自带 chromium

Linux 上没有 Edge，所以默认不再白试一次 ``msedge``。如果服务器上压根没装过
浏览器，**必须**先给 playwright 下载自带内核：

    python -m playwright install --with-deps chromium

也可用环境变量 ``WANNENG_BROWSER_CHANNEL`` 覆盖配置，例如指向系统 Chrome：

    WANNENG_BROWSER_CHANNEL=executable:/usr/bin/google-chrome
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from astrbot.api import logger

try:  # playwright 为可选依赖，未安装时不应影响插件启动
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)

# 在页面上下文里发 XHR 并等待完成；secsdk 会给它自动补签名
_XHR_SCRIPT = """
(args) => new Promise((resolve) => {
    try {
        const x = new XMLHttpRequest();
        x.open('GET', args.url, true);
        x.withCredentials = true;
        x.timeout = args.timeout * 1000;
        x.onreadystatechange = () => {
            if (x.readyState === 4) resolve({ status: x.status, text: x.responseText || '' });
        };
        x.onerror = () => resolve({ status: -1, text: '' });
        x.ontimeout = () => resolve({ status: -2, text: '' });
        x.send();
    } catch (e) {
        resolve({ status: -3, text: String(e) });
    }
})
"""


class BrowserFetcher:
    """用真实浏览器页面代发 GET 请求并返回 JSON

    Args:
        channel: 指定复用的系统浏览器通道（如 msedge、chrome），``auto`` 表示
            按平台自动排序；``executable:/绝对路径`` 可直接指定浏览器可执行文件
        idle_timeout: 空闲多少秒后自动关闭浏览器
        warmup_wait: 打开首页后等待 secsdk 就绪的毫秒数
    """

    HOME = "https://www.douyin.com/"

    def __init__(
        self,
        *,
        headless: bool = True,
        channel: str | None = "auto",
        idle_timeout: int = 300,
        warmup_wait: int = 6000,
        nav_timeout: int = 60,
    ) -> None:
        self.headless = headless
        # 环境变量优先级最高：不改配置也能在服务器上一行修好通道
        self.channel = (
            os.environ.get("WANNENG_BROWSER_CHANNEL", "").strip() or channel or "auto"
        )
        self.idle_timeout = idle_timeout
        self.warmup_wait = warmup_wait
        self.nav_timeout = nav_timeout

        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None
        # 最近一次启动/请求失败的原因，便于调用方把真实错误透给用户与日志
        self.last_error: str | None = None
        # 实际成功启动所用的通道（"msedge" / "chromium" / "自带 chromium"）
        self.launched_with: str | None = None

    @staticmethod
    def available() -> bool:
        """当前环境是否具备浏览器能力"""
        return async_playwright is not None

    # ---------------- 生命周期 ----------------

    @staticmethod
    def candidates(
        channel: str | None, platform: str | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        """按平台给出「通道标签 + launch 参数」的尝试顺序

        ``auto`` 时先试系统已装的浏览器，最后退回 playwright 自带 chromium。
        显式指定通道（含 ``executable:`` 前缀）时只试它，再退回自带 chromium，
        避免网络环境里静默换用另一个浏览器导致指纹不一致。
        """
        plat = (platform or sys.platform).lower()
        ch = (channel or "auto").strip()

        if ch.startswith("executable:"):
            path = ch.split(":", 1)[1].strip()
            if path:
                return [(f"executable:{path}", {"executable_path": path}), ("自带 chromium", {})]
            return [("自带 chromium", {})]

        if ch and ch != "auto":
            return [(ch, {"channel": ch}), ("自带 chromium", {})]

        if plat.startswith("win"):
            chain = ["msedge", "chrome"]
        elif plat == "darwin":
            chain = ["chrome", "msedge"]
        else:  # Linux 及其它 POSIX：系统 Chrome → 自带 chromium(新无头) → 自带 chromium
            chain = ["chrome", "chromium"]
        return [(c, {"channel": c}) for c in chain] + [("自带 chromium", {})]

    @staticmethod
    def launch_args() -> list[str]:
        """Chromium 启动参数

        Linux 云主机/容器通常以 root 运行，Chromium **必须**带 ``--no-sandbox``
        才肯启动（否则报 "Running as root without --no-sandbox is not supported"）；
        容器里 ``/dev/shm`` 往往只有 64 MB，``--disable-dev-shm-usage`` 可避免页面崩溃。
        """
        args = ["--disable-blink-features=AutomationControlled"]
        if os.name == "posix":
            args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        return args

    async def _ensure_page(self) -> Any:
        """确保浏览器与页面就绪（首次调用时启动）"""
        if self._page is not None and not self._page.is_closed():
            return self._page

        async with self._lock:
            # 双重检查，避免并发启动
            if self._page is not None and not self._page.is_closed():
                return self._page
            if async_playwright is None:
                self.last_error = "未安装 playwright（pip install playwright）"
                raise RuntimeError(self.last_error)

            self._pw = await async_playwright().start()

            # 按平台顺序试系统浏览器，最后退回自带 chromium
            last_err: Exception | None = None
            for label, extra in self.candidates(self.channel):
                try:
                    self._browser = await self._pw.chromium.launch(
                        headless=self.headless,
                        args=self.launch_args(),
                        **extra,
                    )
                    self.launched_with = label
                    logger.info(f"[browser] 浏览器已启动: {label}")
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.debug(f"[browser] 启动候选 {label} 失败: {e}")

            if self._browser is None:
                await self._shutdown()
                hint = self._launch_hint(last_err)
                self.last_error = f"浏览器启动失败: {last_err}{hint}"
                raise RuntimeError(self.last_error)

            try:
                self._context = await self._browser.new_context(
                    user_agent=UA,
                    locale="zh-CN",
                    viewport={"width": 1440, "height": 900},
                )
                await self._context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                )
                self._page = await self._context.new_page()
                await self._page.goto(
                    self.HOME,
                    wait_until="domcontentloaded",
                    timeout=self.nav_timeout * 1000,
                )
                # 等 secsdk 加载完成并把风控 cookie 种下
                await self._page.wait_for_timeout(self.warmup_wait)
            except Exception as e:  # noqa: BLE001
                await self._shutdown()
                self.last_error = f"打开抖音首页失败: {e}"
                raise RuntimeError(self.last_error) from e

            self.last_error = None
            logger.info(
                f"[browser] 抖音页面就绪（通道: {self.launched_with}），接口请求将经页面代发"
            )
            return self._page

    @staticmethod
    def _launch_hint(err: Exception | None) -> str:
        """根据底层报错给出可执行的修复提示"""
        text = str(err or "")
        if "Executable doesn't exist" in text or "playwright install" in text:
            return (
                "；未下载浏览器内核，请在 AstrBot 所在的虚拟环境里执行 "
                "`python -m playwright install --with-deps chromium`"
                "（Ubuntu/Debian 用 apt；下载慢可先 "
                "`export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright`）"
            )
        if "No such file or directory" in text and "chrome" in text.lower():
            return "；系统无可用 Chrome/Edge，请执行 `python -m playwright install --with-deps chromium`"
        if "error while loading shared libraries" in text or "Host system is missing" in text:
            return (
                "；缺少系统库，Ubuntu/Debian 执行 `python -m playwright install-deps chromium`；"
                "CentOS/阿里云需手动装 nss nspr atk at-spi2-atk cups-libs libdrm libXcomposite "
                "libXdamage libXrandr mesa-libgbm pango alsa-lib libxkbcommon"
            )
        if "root without --no-sandbox" in text:
            return "；检测到 root 未加沙箱豁免，本插件 v1.7.0+ 已内置 --no-sandbox，请确认已升级"
        return ""

    async def _shutdown(self) -> None:
        """关闭页面/浏览器并释放 playwright"""
        self.launched_with = None
        for obj in (self._page, self._context, self._browser):
            if obj is None:
                continue
            try:
                await obj.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[browser] 关闭资源失败: {e}")
        self._page = self._context = self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[browser] 停止 playwright 失败: {e}")
            self._pw = None

    async def close(self) -> None:
        """主动关闭（插件卸载时调用）"""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        async with self._lock:
            await self._shutdown()

    def _schedule_idle_close(self) -> None:
        """重置空闲计时，到期自动关浏览器"""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self.idle_timeout <= 0:
            return
        loop = asyncio.get_running_loop()
        self._idle_handle = loop.call_later(
            self.idle_timeout, lambda: asyncio.ensure_future(self.close())
        )

    # ---------------- 请求 ----------------

    async def get_json(
        self, url: str, *, timeout: int = 30, referer: str | None = None
    ) -> dict[str, Any] | None:
        """经浏览器页面代发 GET 并解析 JSON

        Args:
            url: 完整请求地址（无需自带 a_bogus，secsdk 会自动补签名）
            timeout: 页面内 XHR 的超时秒数
            referer: 预留参数，页面内 XHR 由浏览器自行带上来源

        Returns:
            dict | None: 解析后的 JSON 对象；失败返回 None
        """
        try:
            page = await self._ensure_page()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[browser] 页面不可用: {e}")
            return None

        try:
            result = await asyncio.wait_for(
                page.evaluate(_XHR_SCRIPT, {"url": url, "timeout": timeout}),
                timeout=timeout + 15,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[browser] 页面内请求异常: {e}")
            self.last_error = f"页面内请求异常: {e}"
            # 页面可能已崩溃，关掉让下次重建
            await self.close()
            return None
        finally:
            self._schedule_idle_close()

        status = int(result.get("status") or 0)
        text = result.get("text") or ""
        if status != 200:
            logger.warning(f"[browser] 页面内请求返回状态 {status}: {url[:90]}")
            self.last_error = f"页面内请求返回状态 {status}"
            return None

        import json

        try:
            body = json.loads(text)
        except ValueError:
            logger.warning(f"[browser] 响应不是合法 JSON: {text[:120]}")
            self.last_error = "响应不是合法 JSON"
            return None
        self.last_error = None
        return body if isinstance(body, dict) else None
