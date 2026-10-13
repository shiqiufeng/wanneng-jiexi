#!/usr/bin/env bash
# =============================================================================
# 万能解析 · 抖音「浏览器兜底」环境一键安装（Linux 服务器用）
#
# 作用：为 AstrBot 所在的 Python 环境装好 playwright 的 Chromium 内核与系统依赖，
#      并做一次真实启动 + 打开抖音页面的冒烟测试。
#
# 用法（在 AstrBot 服务器上执行，建议 root）：
#     bash setup_browser.sh                    # 自动探测 AstrBot 的 venv
#     bash setup_browser.sh /root/AstrBot/venv # 指定 venv 路径
#
# 说明：
#   - Docker 部署请先 `docker exec -it <容器名> bash` 进容器再执行本脚本；
#   - 脚本只做「安装 + 测试」，不修改 AstrBot 配置，可重复执行；
#   - 若公司网络访问不了 playwright 的 CDN，可先设置镜像：
#         export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
# =============================================================================
set -uo pipefail

RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; CYA=$'\033[36m'; RST=$'\033[0m'
info() { printf '%s[信息]%s %s\n' "$CYA" "$RST" "$*"; }
ok()   { printf '%s[成功]%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s[注意]%s %s\n' "$YEL" "$RST" "$*"; }
err()  { printf '%s[失败]%s %s\n' "$RED" "$RST" "$*"; }

# ---------------------------------------------------------------- 找 Python ---
PY=""
if [[ $# -ge 1 && -n "${1:-}" ]]; then
    if [[ -x "$1/bin/python" ]]; then PY="$1/bin/python"
    elif [[ -x "$1/Scripts/python.exe" ]]; then PY="$1/Scripts/python.exe"
    elif [[ -x "$1" ]]; then PY="$1"
    fi
    [[ -n "$PY" ]] && info "使用指定解释器: $PY" || { err "指定路径下找不到 python: $1"; exit 1; }
fi

if [[ -z "$PY" ]]; then
    # 常见 venv 位置（AstrBot 源码部署 / 启动器部署）
    for cand in \
        ./venv/bin/python ./venv/Scripts/python.exe \
        ./.venv/bin/python ../venv/bin/python \
        /root/AstrBot/venv/bin/python /opt/AstrBot/venv/bin/python \
        "$(dirname "$(dirname "$(command -v python3 2>/dev/null || true)")")/bin/python" ; do
        if [[ -x "$cand" ]]; then PY="$cand"; info "自动探测到 venv: $PY"; break; fi
    done
fi
if [[ -z "$PY" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PY="$(command -v python3)"; warn "未找到 venv，退回系统 python3: $PY"
        warn "若 AstrBot 用的是 venv，请显式传入路径，例如：bash setup_browser.sh /root/AstrBot/venv"
    else
        err "找不到任何 python3，请先安装 Python 或指定 venv 路径"; exit 1
    fi
fi

info "Python: $("$PY" -V 2>&1)  路径: $PY"

# --------------------------------------------------------------- 系统识别 ----
PKG=""
if command -v apt-get >/dev/null 2>&1; then PKG="apt"
elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
elif command -v yum >/dev/null 2>&1; then PKG="yum"
fi
OS_NAME="$( (. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-$ID}") || uname -s )"
info "系统: $OS_NAME   包管理器: ${PKG:-未识别}"

# --------------------------------------------------- 1. 安装 playwright 包 ----
if "$PY" -c "import playwright" >/dev/null 2>&1; then
    ok "playwright Python 包已安装"
else
    info "安装 playwright Python 包 ..."
    "$PY" -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --disable-pip-version-check playwright 2>&1 | tail -4
    if "$PY" -c "import playwright" >/dev/null 2>&1; then ok "playwright 包安装成功"
    else err "playwright 包安装失败，请检查网络或 pip 源"; exit 1
    fi
fi

# --------------------------------------------------- 2. 下载 Chromium 内核 ---
info "下载 Chromium 内核（约 150 MB，首次较慢）..."
if "$PY" -m playwright install chromium 2>&1 | tail -8; then
    ok "Chromium 内核就绪"
else
    err "Chromium 下载失败；可尝试设置镜像后重试："
    echo "    export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright"
    echo "    $PY -m playwright install chromium"
fi

# --------------------------------------------------- 3. 安装系统依赖库 -------
info "安装 Chromium 运行所需系统库 ..."
case "$PKG" in
    apt)
        "$PY" -m playwright install-deps chromium 2>&1 | tail -5 \
            || apt-get update -y && apt-get install -y \
                libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
                libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
                libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
                fonts-liberation libx11-xcb1 libxcursor1 libxi6 libxtst6
        ;;
    dnf|yum)
        # playwright 自带的 install-deps 仅支持 apt 系，RHEL 系需手动装
        "$PY" -m playwright install-deps chromium >/dev/null 2>&1 || true
        $PKG install -y \
            nss nspr atk at-spi2-atk cups-libs libdrm libXcomposite libXdamage \
            libXrandr mesa-libgbm pango alsa-lib libxkbcommon libX11 libXcursor \
            libXi libXtst liberation-fonts 2>&1 | tail -5
        ;;
    *)
        warn "未识别的包管理器，跳过系统库安装；若启动报缺库，请手动安装："
        echo "    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \\"
        echo "    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \\"
        echo "    libpango-1.0-0 libcairo2 libasound2 fonts-liberation"
        ;;
esac

# --------------------------------------------------- 4. 冒烟测试 ------------
info "冒烟测试：启动 Chromium 并打开抖音首页 ..."
"$PY" - <<'PYEOF'
import sys, asyncio
try:
    from playwright.async_api import async_playwright
except Exception as e:
    print("  [失败] 无法导入 playwright:", e); sys.exit(2)

async def main() -> int:
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
        except Exception as e:
            print("  [失败] Chromium 启动失败:", e); return 3
        try:
            page = await browser.new_page()
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            title = await page.title()
            cookies = await page.context.cookies()
            names = {c["name"] for c in cookies}
            print("  [成功] 页面标题:", title)
            print("  [信息] 已种 Cookie 数:", len(cookies),
                  "| 含 secsdk 相关:", sorted(n for n in names if "secsdk" in n.lower() or "uifid" in n.lower()))
            return 0
        finally:
            await browser.close()

sys.exit(asyncio.run(main()))
PYEOF
RC=$?

echo
if [[ $RC -eq 0 ]]; then
    ok "全部就绪 —— 回到 AstrBot 面板重发一次「/全部 + 主页链接」即可"
elif [[ $RC -eq 3 ]]; then
    err "Chromium 仍无法启动，请把上面的报错原文发出来"
    warn "root 环境必须带 --no-sandbox（本插件已内置）；若报缺 .so，说明系统库没装全"
else
    err "冒烟测试未通过（退出码 $RC）"
fi
exit $RC
