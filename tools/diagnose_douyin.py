#!/usr/bin/env python3
"""抖音「浏览器兜底」环境自检（万能解析）

在 AstrBot 所在的机器上直接运行，逐步定位实况直链取不到的原因：

    python tools/diagnose_douyin.py
    python tools/diagnose_douyin.py 7695197082320078763   # 指定作品 ID
    python tools/diagnose_douyin.py --install              # 顺手把浏览器内核装上再自检

脚本不依赖本插件任何模块，只用标准库 + playwright，可单独拷到服务器上跑。
它会依次检查：Python 环境 → playwright 是否可导入 → Chromium 内核是否已下载
→ 能否启动（含 --no-sandbox）→ 能否打开抖音首页并拿到风控 Cookie
→ 能否经页面代发 aweme/detail 并取到实况 mp4 直链。
"""

from __future__ import annotations

import asyncio
import glob
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

# 默认用「呜呜说号卡」那条带实况的图文作品做样本
DEFAULT_AWEME_ID = "7695197082320078763"

DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
HOT_URL = "https://www.douyin.com/aweme/v1/web/hot/search/list/"

OK = "  [ 通过 ]"
NO = "  [ 失败 ]"
WA = "  [ 注意 ]"


def head(n: int, title: str) -> None:
    print(f"\n{'=' * 62}\n[{n}] {title}\n{'=' * 62}")


def browsers_dir() -> Path:
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom:
        return Path(custom)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def build_detail_params(aweme_id: str) -> dict[str, str]:
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
        "aweme_id": aweme_id,
    }


def build_launch_args() -> list[str]:
    """与 core/browser.py 的 launch_args() 保持一致"""
    args = ["--disable-blink-features=AutomationControlled"]
    if os.name == "posix":
        args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    return args


def build_candidates() -> list[tuple[str, dict]]:
    """与 core/browser.py 的 BrowserFetcher.candidates("auto") 保持一致"""
    if sys.platform.startswith("win"):
        chain = ["msedge", "chrome"]
    elif sys.platform == "darwin":
        chain = ["chrome", "msedge"]
    else:
        chain = ["chrome", "chromium"]
    return [(c, {"channel": c}) for c in chain] + [("自带 chromium", {})]


def pkg_manager() -> str:
    for name in ("apt-get", "dnf", "yum", "apk"):
        if shutil.which(name):
            return name
    return ""


def print_install_help() -> None:
    """按系统打印可复制的安装命令"""
    print("\n  可直接复制执行的修复命令（在 AstrBot 所在的 Python 环境里运行）:")
    py = sys.executable
    print(f"\n  # 0) 确认用的是 AstrBot 的解释器（不要用系统 python3）")
    print(f"      {py} -V")
    print(f"\n  # 1) 装浏览器内核 + 系统依赖（约 150 MB，首次慢）")
    print(f"      {py} -m playwright install --with-deps chromium")
    print(f"\n  # 2) 国内下载慢时先换镜像再重跑上一步")
    print(f"      export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright")
    print(f"      {py} -m playwright install chromium")
    pm = pkg_manager()
    if pm == "apt-get":
        print(f"\n  # 3) 若第 1 步 --with-deps 失败，Ubuntu/Debian 手动补依赖")
        print(f"      apt-get update && apt-get install -y libnss3 libnspr4 libatk1.0-0 \\")
        print(f"        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 \\")
        print(f"        libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 \\")
        print(f"        libasound2t64 fonts-liberation")
        print(f"\n  # 4) Docker 部署：要先进容器再执行上面的命令")
        print(f"      docker exec -it <AstrBot容器名> bash")
    elif pm in ("dnf", "yum"):
        print(f"\n  # 3) RHEL/CentOS/阿里云 {pm} 手动补依赖")
        print(f"      {pm} install -y nss nspr atk at-spi2-atk cups-libs libdrm libXcomposite \\")
        print(f"        libXdamage libXrandr mesa-libgbm pango alsa-lib libxkbcommon \\")
        print(f"        libX11 libXcursor libXi libXtst liberation-fonts")
    print(f"\n  # 5) 装完重跑本自检: {py} tools/diagnose_douyin.py\n")


def do_install() -> int:
    """--install：把 playwright 包 + Chromium 内核 + 系统依赖一次装齐"""
    py = sys.executable
    head(0, "安装浏览器内核（--install）")
    print(f"  使用解释器: {py}")
    env = dict(os.environ)
    if not env.get("PLAYWRIGHT_DOWNLOAD_HOST"):
        env["PLAYWRIGHT_DOWNLOAD_HOST"] = "https://npmmirror.com/mirrors/playwright"
        print("  已自动启用国内镜像: npmmirror.com")

    if importlib.util.find_spec("playwright") is None:
        print("  [1/2] 安装 playwright Python 包 ...")
        subprocess.run(
            [py, "-m", "pip", "install", "--disable-pip-version-check",
             "-i", "https://mirrors.aliyun.com/pypi/simple/", "playwright"],
            check=False,
        )
    else:
        print("  [1/2] playwright Python 包已存在，跳过")

    print("  [2/2] 下载 Chromium 内核 + 系统依赖（约 150 MB，首次较慢，请耐心等待）...")
    rc = subprocess.run(
        [py, "-m", "playwright", "install", "--with-deps", "chromium"],
        env=env, check=False,
    ).returncode
    if rc == 0:
        print(f"{OK} 安装命令执行完成，继续下面的自检")
    else:
        print(f"{WA} 安装命令退出码 {rc}；若是 --with-deps 报错，可去掉该参数只装内核：")
        print(f"      {py} -m playwright install chromium")
        print_install_help()
    return rc


async def run(aweme_id: str) -> int:
    # ---------------- 1. 环境 ----------------
    head(1, "运行环境")
    print(f"  Python   : {sys.version.split()[0]}")
    print(f"  解释器   : {sys.executable}")
    print(f"  系统     : {platform.platform()}")
    print(f"  是否 root: {os.geteuid() == 0 if hasattr(os, 'geteuid') else 'N/A'}")
    print(f"  os.name  : {os.name}")

    # ---------------- 2. playwright ----------------
    head(2, "playwright Python 包")
    try:
        import playwright  # noqa: F401
        from playwright.async_api import async_playwright
        print(f"{OK} 已安装，可以 import")
    except Exception as e:
        print(f"{NO} 未安装或不可用: {e}")
        print("\n  修复: python -m pip install playwright")
        return 1

    # ---------------- 3. 浏览器内核 ----------------
    head(3, "Chromium 内核")
    d = browsers_dir()
    print(f"  内核目录: {d}")
    kernel_hits: list[str] = []
    shell_hits: list[str] = []
    if not d.exists():
        print(f"{WA} 目录不存在 —— 从未下载过浏览器内核目录")
    else:
        kernel_hits = sorted(glob.glob(str(d / "chromium-*")))
        shell_hits = sorted(glob.glob(str(d / "chromium_headless_shell-*")))
        if kernel_hits:
            print(f"{OK} 发现 {len(kernel_hits)} 个 chromium 版本目录")
            for h in kernel_hits[:3]:
                print(f"        {os.path.basename(h)}")
        else:
            print(f"{WA} 无 chromium-* 目录 —— 未下载自带内核")
        if shell_hits:
            print(f"{OK} 发现 {len(shell_hits)} 个 headless shell 目录")
        else:
            print(f"{WA} 无 chromium_headless_shell-* 目录 —— 无头模式会缺内核")

    # 顺手看看系统里有没有可以直接复用的浏览器
    found_sys = [p for p in ("google-chrome", "google-chrome-stable", "chromium",
                             "chromium-browser", "microsoft-edge") if shutil.which(p)]
    if found_sys:
        print(f"  系统已装浏览器: {', '.join(found_sys)}")
    elif os.name == "posix":
        print("  系统已装浏览器: 无 —— Ubuntu 服务器默认不带浏览器，属正常现象")

    if not (kernel_hits and shell_hits) or (os.name == "posix" and not found_sys and not kernel_hits):
        print_install_help()
    else:
        print("\n  内核齐备，无需额外安装。")

    # ---------------- 4. 启动浏览器 ----------------
    head(4, "启动浏览器")
    args = build_launch_args()
    print(f"  启动参数: {args}")

    pw = None
    browser = None
    try:
        pw = await async_playwright().start()
    except Exception as e:
        print(f"{NO} playwright 启动失败: {e}")
        return 1

    for label, extra in build_candidates():
        try:
            browser = await pw.chromium.launch(headless=True, args=args, **extra)
            print(f"{OK} 启动成功（{label}）")
            break
        except Exception as e:
            print(f"{WA} {label} 启动失败: {str(e)[:200]}")
    if browser is None:
        print(f"{NO} 所有通道都无法启动")
        print("\n  常见原因与修复:")
        print("   · 未下载内核  → python -m playwright install --with-deps chromium")
        print("   · 缺系统库    → python -m playwright install-deps chromium")
        print("   · root 运行缺 --no-sandbox → 本脚本已自动带上")
        print_install_help()
        await pw.stop()
        return 1

    # ---------------- 5. 打开抖音首页 ----------------
    head(5, "打开抖音首页（等 secsdk 种 Cookie）")
    try:
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
            ),
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await ctx.new_page()
        await page.goto(
            "https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000
        )
        await page.wait_for_timeout(6000)
        cookies = await ctx.cookies()
        names = {c["name"] for c in cookies}
        print(f"{OK} 页面标题: {await page.title()}")
        print(f"  Cookie 数: {len(cookies)}")
        risk = sorted(n for n in names if any(k in n.lower() for k in ("secsdk", "uifid", "s_v_web_id")))
        print(f"  风控相关 Cookie: {risk if risk else '（无）'}")
        if not risk:
            print(f"{WA} 没有拿到风控 Cookie，secsdk 可能没加载完")
    except Exception as e:
        print(f"{NO} 打开首页失败: {e}")
        await browser.close()
        await pw.stop()
        return 1

    # ---------------- 6. 页面代发接口 ----------------
    xhr = """
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
        } catch (e) { resolve({ status: -3, text: String(e) }); }
    })
    """

    head(6, "检查 1：热搜接口（验证「页面代发」链路本身）")
    hot = f"{HOT_URL}?{urlencode({'device_platform': 'webapp', 'aid': '6383', 'channel': 'channel_pc_web', 'detail_list': '1'}, safe='')}"
    r = await page.evaluate(xhr, {"url": hot, "timeout": 30})
    print(f"  页面内返回 HTTP 状态: {r['status']}")
    if r["status"] == 200:
        print(f"{OK} 浏览器代发链路正常（secsdk 工作）")
    else:
        print(f"{NO} 浏览器代发链路异常 —— 即使接口未被封也拿不到数据")

    head(7, "检查 2：作品详情接口（实况直链的来源）")
    print(f"  作品 ID: {aweme_id}")
    url = f"{DETAIL_URL}?{urlencode(build_detail_params(aweme_id), safe='')}"
    r = await page.evaluate(xhr, {"url": url, "timeout": 40})
    print(f"  页面内返回 HTTP 状态: {r['status']}")

    if r["status"] != 200:
        print(f"{NO} 详情接口未返回 200，实况直链必然取不到")
    else:
        import json as _json
        try:
            body = _json.loads(r["text"])
        except Exception as e:
            print(f"{NO} 响应不是合法 JSON: {e}")
            body = {}
        detail = body.get("aweme_detail") or {}
        images = detail.get("images") or []
        print(f"  status_code : {body.get('status_code')}")
        print(f"  作品描述    : {(detail.get('desc') or '')[:40]}")
        print(f"  images 数量 : {len(images)}")
        live = 0
        for i, img in enumerate(images):
            if not isinstance(img, dict):
                continue
            v = img.get("video")
            play = None
            if isinstance(v, dict):
                urls = (v.get("play_addr") or {}).get("url_list") or []
                play = urls[0] if urls else None
            if play:
                live += 1
                print(f"    img[{i}] 实况 ✓  {play[:90]}")
            else:
                print(f"    img[{i}] 无实况")
        if live:
            print(f"\n{OK} 取到 {live} 个实况直链 —— 服务器端环境完全正常")
        else:
            print(f"\n{WA} 未取到实况直链（这条作品可能本来就不带实况）")

    await browser.close()
    await pw.stop()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--install" in argv:
        do_install()
    rest = [a for a in argv if not a.startswith("-")]
    aweme_id = rest[0] if rest else DEFAULT_AWEME_ID
    try:
        return asyncio.run(run(aweme_id))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
