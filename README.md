<div align="center">

![name](https://count.getloli.com/@astrbot_plugin_parser?name=万能解析&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# 万能解析

_✨ 发个链接，剩下的交给它 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)

</div>

---

## 📖 介绍

**万能解析** 是一个 AstrBot 链接解析插件。把受支持平台的分享链接丢进会话，机器人自动抓取 **视频 / 图集 / 音频**，下载回传，并用 PIL 渲染一张信息卡片（头像、昵称、标题、封面、数据）。无需任何前缀、无需注册账号、无需额外配置即可开箱使用。

在通用解析能力之上，本项目针对 **实况图（Live Photo）** 做了深度增强，并补齐了主页作品清单、主页音乐清单、短链服务、QQ空间登录态等一整套实用功能。

### 支持平台

| 平台 | 触发的消息形态 | 视频 | 图集 | 音频 |
| ---- | -------------- | :--: | :--: | :--: |
| A站 | 链接 / ac 号 | ✅ | ✅ | ✅ |
| B站 | av 号 / BV 号 / 链接 / 短链 / 卡片 / 小程序 | ✅ | ✅ | ✅ |
| 抖音 | 链接（分享链接，兼容电脑端链接） | ✅ | ✅ | ✅ |
| TikTok | 链接 | ✅ | ✅ | ❌ |
| 小红书 | 链接（含短链）/ 卡片 | ✅ | ✅ | ❌ |
| 快手 | 链接（标准链接与短链） | ✅ | ✅ | ❌ |
| 微博 | 链接（博文、视频、show、文章） | ✅ | ✅ | ❌ |
| 油管 | 链接（含短链） | ✅ | ❌ | ✅ |
| 网易云 | 链接 | ❌ | ❌ | ✅ |
| 推特 | 链接 | ✅ | ✅ | ❌ |
| Instagram | 链接 | ✅ | ✅ | ❌ |
| Iwara | 链接 | ✅ | ✅ | ❌ |
| Pixiv | 链接 / pid | ✅ | ✅ | ❌ |
| QQ空间 | 公开分享链接 / 卡片 | ✅ | ✅ | ❌ |
| 微信视频号 | 链接（含短链） | ✅ | ✅ | ❌ |
| 小黑盒 | 链接 / 卡片 | ✅ | ✅ | ❌ |
| 知乎 | 链接 / 卡片 | ✅ | ✅ | ❌ |
| NGA | 链接 | ✅ | ✅ | ❌ |
| CPP无差别同人站 | 链接 | ✅ | ✅ | ❌ |

> 平台是否生效取决于配置面板中对应解析器的 `启用` 开关；未启用的平台不会响应。
> 在会话里输入 **`/解析帮助`** 可以随时查看当前已启用的平台列表与全部命令。

---

## 🎞️ 实况图（Live Photo）

这是本项目的核心增强点。开启后：

- **抖音 / 小红书 / TikTok** 的实况图会以 **mp4** 形式发送；
- 同一张图的实况会 **替代** 其对应的静态图，不会重复发送；
- 分段作品的实况与静态图 **各归其位**，顺序不乱；
- **快手（含极速版）** 目前取不到实况视频，图集按静态图发送。

抖音的实况直链只能由作品详情接口给出，而 PC web 详情接口已被 Argus 风控锁死。插件因此内置了 **移动端 App 协议通道**（`aweme/v1/feed/?aweme_id=`，需带完整设备参数、并逐个 `aid` 试到拿到实况 mp4 为止），单条作品的**视频、图文与实况都不需要浏览器**；另有 **浏览器兜底**（懒启动、空闲自动关闭），只服务于 `/全部`、`/全部音乐` 这类主页清单。

---

## 🎨 效果图

插件默认启用 PIL 实现的通用媒体卡片渲染，效果图如下：

<div align="center">

<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/9_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/4_pic.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_video.png" width="160" />
<img src="https://raw.githubusercontent.com/fllesser/nonebot-plugin-parser/refs/heads/resources/resources/renderdamine/repost_2_pic.png" width="160" />

</div>

---

## 💿 安装

### 方式一：面板「从文件安装」（推荐）

1. 下载插件压缩包：
   - 直接下载：**https://github.com/shiqiufeng/wanneng-jiexi/releases/latest/download/wanneng-jiexi.zip**
   - 或在本地仓库执行 `python tools/package.py` 生成 `dist/万能解析.zip`
2. 打开 AstrBot 面板 → **插件** → 右上角安装按钮 → 选 **从文件安装** → **选择文件** → 选中该 zip；
3. 点击 **安装**，等待依赖安装完成，插件会自动出现在列表中并处于启用状态。

> 两个文件名内容完全一致，安装结果没有区别，都可以直接用。zip 叫什么名字不影响识别，
> AstrBot 只读包内的 `metadata.yaml`。

### 方式二：解压到插件目录

把 zip 解压出的 `万能解析/` 整个文件夹放进 AstrBot 的 `data/plugins/` 目录，然后重启或在面板重载。

> AstrBot 会按 `metadata.yaml` 的 `name` 字段决定安装目录名，因此 zip 内层文件夹叫什么不影响结果；
> 插件包名与安装后的文件夹名均为 `万能解析`。若你从旧包名 `astrbot_plugin_parser_live` 升级而来，
> 数据目录会自动兼容，Cookies 与缓存不会丢失。

### 自行打包

```bash
python tools/package.py          # 生成 dist/万能解析.zip 与 dist/wanneng-jiexi.zip
python tools/verify_archive.py   # 按 AstrBot 的安装校验逻辑自检压缩包
```

`tools/verify_archive.py` 复刻了 AstrBot 安装流程中的真实校验（根目录推断、metadata 定位、
`name/desc/version/author` 必需字段、安装目录名是否为合法 Python 标识符），
可在上传前提前发现「压缩包不是合法的 AstrBot 插件」这类问题。

> 为什么要两份：GitHub Release 的附件名**不接受中文**（上传会返回 422），
> 所以发 Release 时用 ASCII 名的 `wanneng-jiexi.zip`；本地安装用哪个都行。

### 依赖

```bash
pip install -r requirements.txt
```

#### 抖音：需要浏览器的只有 `/全部`

抖音自 2026-09 起对 PC web 详情接口启用了 Argus 风控。插件现在分两条路：

| 场景 | 走的通道 | 要不要浏览器 |
| --- | --- | --- |
| 单条视频链接 | 分享页 SSR，失败则移动端 App 协议 | **不需要** |
| 图文 / 实况（Live Photo）链接 | 移动端 App 协议（`aweme/v1/feed/?aweme_id=`） | **不需要** |
| `/全部` `/全部音乐`（作者主页清单） | PC web `aweme/post` | 需要浏览器内核 |

移动端协议不需要 Cookie、ttwid、a_bogus、msToken 或 secsdk 签名，普通 HTTP 即可；但**必须带完整设备参数**
（`device_id` / `iid` / `device_platform` / `version_code` / `os_version` / `device_type` …），少带就会被接口
当成「随便给点推荐」，返回的列表里根本没有你要的那条作品（v1.8.3 修掉的正是这个静默失效）。细节见
`core/parsers/douyin/mobile.py` 顶部说明。所以**只用单条链接的话，服务器上完全不必装浏览器**。

只有在用 `/全部` 时才需要 `playwright` 的内核。Windows / macOS 上会优先复用系统已安装的 Edge / Chrome，
**无需再下载浏览器**；**Linux / Docker 上只 `pip install playwright` 是不够的**，还要装内核与系统库：

  ```bash
  # 一键脚本（自动识别 apt / yum / dnf，并做启动冒烟测试）
  # 在 AstrBot 服务器上执行，Docker 请先 docker exec 进容器
  bash tools/setup_browser.sh /root/AstrBot/venv

  # 或手动执行（推荐 --with-deps，一步装好内核 + 系统库）
  python -m playwright install --with-deps chromium

  # 下载慢就换国内镜像再重跑
  export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
  python -m playwright install chromium
  ```

  > **Ubuntu 服务器默认不带任何浏览器**，「没有浏览器」是正常现象，不需要装桌面版 Chrome ——
  > playwright 会下载自己独立的一份 Chromium（约 150 MB），装到 `~/.cache/ms-playwright`。
  > CentOS / 阿里云 Linux 等 RHEL 系系统上 `install-deps` 不支持 apt 流程，需手动 `yum install`。
  > `tools/setup_browser.sh` 已内置这些分支。
  配置项「浏览器通道」保持默认 `auto` 即可（会自动跳过 Linux 上不存在的 Edge）。

**排查**：若 `/全部` 报「浏览器代发请求失败」，AstrBot 日志里会有一行 `[browser] ...` 说明真实原因
（内核未下载 / 缺系统库 / 无法启动），按提示处理即可。单条链接已不再走浏览器，不会出现该报错。

**一键自检 / 一键安装**（服务器上直接跑，不依赖插件模块）：

```bash
python tools/diagnose_douyin.py --install       # 先自动装好 playwright + Chromium 内核，再做全链路检查
python tools/diagnose_douyin.py                 # 仅检查，用内置样本作品
python tools/diagnose_douyin.py <aweme_id>      # 检查指定作品
```

它会依次验证：Python 环境 → playwright 可导入 → Chromium 内核 → 浏览器能否启动 →
抖音首页与风控 Cookie → 页面代发详情接口 → 实况直链是否真的取到，逐项给出通过/失败与修复命令。

> **现象对照**：图文作品「实况变成静态图」而群里没有任何报错，通常就是这条链路失败了 ——
> v1.7.2 起会在日志里以 `warning` 写明原因（旧版本是 `debug`，看不见）。

---

## ⚙️ 配置

所有配置项均在 AstrBot 插件配置面板中可视化编辑，主要分为以下几类：

| 分组 | 说明 |
| ---- | ---- |
| 全局开关 | 白名单 / 黑名单、是否必须 @ 机器人、引用解析、防抖窗口 |
| 媒体限制 | 最大时长、最大体积、并发与重试、下载超时、是否提示失败项 |
| 发送方式 | 音频发送方式（自动 / 语音 / 文件）、语音最长秒数、作品清单条数上限、转发合并阈值 |
| 短链服务 | 接口地址与密码，用于把直链转成短链 |
| 解析器模板 | 每个平台一个子模板，可单独启用、填 Cookie、设代理 |

### 抖音解析器

| 配置项 | 默认 | 说明 |
| ------ | ---- | ---- |
| `enable` | 开 | 是否启用 |
| `use_proxy` | 关 | 是否走全局代理 |
| `cookies` | 空 | 选填，登录态 Cookie |
| `browser_fallback` | 开 | 接口被风控拦截时启用浏览器兜底。**v1.8.0 起单条作品已不走浏览器**，此项实际只影响 `/全部` 主页清单 |
| `browser_channel` | `auto` | 浏览器通道。`auto`=按平台自动选（Linux: chrome→chromium→内置）；可填 `msedge`/`chrome`/`chromium`，或 `executable:/usr/bin/google-chrome`；也支持环境变量 `WANNENG_BROWSER_CHANNEL` |
| `browser_headless` | 开 | 无头模式运行浏览器 |
| `browser_idle_timeout` | 300 | 浏览器空闲多少秒后自动关闭 |

### QQ空间解析器

QQ空间解析器作为可选模板提供，支持 `h5.qzone.qq.com/ugc/share`、`mobile.qzone.qq.com/l` 等公开分享链接，并优先解析动态中的原图 / 原视频。未配置登录态或登录态不可用时，会自动回退到公开分享页可获取的媒体。

可选登录态来源：

- **SnowLuma 自动（推荐）**：调用 SnowLuma OneBot HTTP `get_credentials`，固定请求 `domain=qzone.qq.com`，短时缓存凭证；QQ空间接口出现登录态异常时会清缓存、重新获取并自动重试一次。
- **手动 Cookies**：可直接填写 `uin/p_uin`、`skey`、`p_skey` 等 Cookie；SnowLuma 获取失败时也会作为备用登录态。
- 两种登录态都不可用时，解析器仍保留公开 H5 fallback，不要求用户必须部署 SnowLuma。

SnowLuma HTTP 地址默认为 `http://127.0.0.1:3000`。这里填写的是 **OneBot HTTP API 地址，不是 WebSocket 地址**；如果 HTTP API 配置了 `access_token`，同时填写对应的 SnowLuma Access Token。QQ 本体已掉线或需要重新登录时，需要先恢复 QQ 登录，单独刷新 `p_skey` 无法恢复失效的 QQ 会话。

> QQ空间登录态只用于回查当前公开分享动态对应的媒体详情，不用于绕过动态自身的访问权限。

---

## 🎉 指令

| 指令 | 权限 | 说明 |
| ---- | ---- | ---- |
| `/解析帮助` | 所有人 | 显示使用帮助（别名：`解析说明`） |
| `/音乐 <链接>` | 所有人 | 仅解析音频，只发音频不发视频 |
| `/全部 <主页链接>` | 所有人 | 抖音主页作品清单（卡片 + 作品直链） |
| `/全部音乐 <主页链接>` | 所有人 | 抖音主页音乐清单（音频直链文本） |
| `/相册少女集 <链接>` | 所有人 | 解析后把图片和视频存进群相册，只回一条结果（别名：`相册上传`） |
| `/开启解析` | ADMIN | 开启当前会话的解析功能 |
| `/关闭解析` | ADMIN | 关闭当前会话的解析功能 |
| `/短链验证` | ADMIN | 自检短链服务是否可用（别名：`短链测试`） |
| `/相册自检` | ADMIN | 检查群相册是否就绪：列相册、探接口（别名：`相册检测`） |
| `/登录B站` | ADMIN | 扫码获取 B 站凭证（别名：`blogin`） |

### 可选参数

清单类与音频类命令支持两个可选参数，**顺序不限**：

| 参数 | 取值范围 | 示例 |
| ---- | -------- | ---- |
| 条数 | 1 – 200，覆盖配置项中的条数上限 | `/全部 10 <链接>` |
| 发送方式 | `文件` 或 `语音` | `/音乐 文件 <链接>` |

可组合使用：

```
/全部音乐 语音 10 <主页链接>     取 10 首音乐以语音发送
/全部音乐 文件 10 <主页链接>     取 10 首音乐以文件发送，文件名形如「昵称_歌名.mp3」
```

### 关于直链与短链

清单中附带的直链，在配置了短链服务（配置项「短链服务接口地址」「短链服务密码」）时会 **自动转成短链**，短码按 36 进制由小到大递增；未配置则直接发送原始直链。配置完成后可用 `/短链验证` 自检：它会依次检查配置、创建短链、短链跳转，并分别测试经代理与不走代理两条网络路径。

### 群相册：`/相册少女集`

把解析出来的图片和视频直接存进 QQ 群相册，群里只回一条上传结果，不刷屏：

```
/相册少女集 2.33 一梦婺源 只此篁岭 # 婺源篁岭 # 晒秋女 https://v.douyin.com/4zhoptsRhGY/
```

上传内容与解析结果一一对应：

| 解析结果 | 上传方式 |
| --- | --- |
| 图集 / 图文 | `upload_images_to_qun_album` 一次性上传，相册里显示为同一条 |
| 视频 | `upload_video_to_qun_album`，封面自动抽首帧 |
| 抖音图文实况 | 静态图按图片上传，实况 mp4 按视频上传 |
| 作品文案 | 写到相册评论（`do_group_album_comment`）——上传接口没有标题参数 |

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `album_enabled` | 开 | 是否启用 `/相册少女集` |
| `album_name` | `少女集` | 目标群相册名，需**先在群里建好**（不会自动创建） |
| `album_title_comment` | 开 | 把作品文案写成相册评论 |
| `album_max_media` | `20` | 单次最多上传多少个媒体 |
| `album_base64_fallback` | 开 | 本地路径传不过去时改用 base64 再试（仅 < 24 MB） |

**依赖**：走 NapCat 扩展 action（`get_qun_album_list` 等），经 AstrBot 已经建好的连接调用，
**不用额外填 NapCat 地址与 token**。仅支持 QQ（NapCat）协议端。
`upload_images_to_qun_album` 与 `upload_video_to_qun_album` 需要 **NapCat v4.18.30+**；
低版本会自动降级为逐张图片上传 / 视频封面图，不会整条失败。相册名对不上时，
回复里会把群里已有的相册名一起列出来。

---

## 🧠 工作流程

插件运行后，每一条消息的处理流程如下：

1. **消息接收** —— 监听所有消息事件，获取消息链与原始文本；支持普通文本、链接、卡片（Json 组件）。
2. **基础过滤** —— 跳过被禁用的会话与空消息；消息首段 `@` 的目标不是本 Bot 时不解析。
3. **命令识别** —— 识别 `/音乐` `/全部` `/全部音乐` 前缀并剥离，解析随后的条数与发送方式参数。
4. **链接提取与匹配** —— 卡片消息先从 Json 中提取 URL；再用「关键词 + 正则」双重匹配定位解析器，未命中则退出。
5. **仲裁判定** —— 仅在 aiocqhttp 平台生效，通过固定贴表情在多个 Bot 之间仲裁，未胜出的 Bot 自动放弃。
6. **防抖判定** —— 对同一会话内相同链接做时间窗口限制；音频 / 清单类命令使用独立指纹，互不干扰。
7. **内容解析** —— 调用对应平台解析器获取媒体信息，统一收敛为 `ParseResult`；实况图在此阶段获取并绑定到对应静态图。
8. **媒体下载与消息构建** —— 下载视频 / 图片 / 音频 / 文件，按配置决定音频发送方式与失败提示。
9. **卡片渲染** —— 在非简洁模式或无直传媒体时用 PIL 渲染媒体卡片并缓存。
10. **消息合并与发送** —— 消息段超过阈值时自动合并为转发消息，最终发送到对应会话。

---

## 🧩 扩展

插件支持自定义解析器：继承 `BaseParser`，声明 `platform` 并用 `@handle(...)` 标注关键词与正则即可自动注册，无需改动主流程。

```python
from .base import BaseParser, Platform, handle


class ExampleParser(BaseParser):
    platform = Platform(name="example", display_name="示例网站")

    @handle("example.com", r"example\.com/item/(?P<id>\w+)")
    async def _parse(self, searched):
        ...
```

示例解析器请看 [example.py](https://github.com/Zhalslar/astrbot_plugin_parser/blob/main/core/parsers/example.py)。

---

## ❓ 常见问题

**Q：某个平台发了链接没反应？**
A：先确认该平台的解析器已启用（`/解析帮助` 会列出当前已启用的平台），再看是否需要 Cookie 登录态。

**Q：抖音的 `/全部`、`/全部音乐` 报接口错误？**
A：确认抖音解析器的「浏览器兜底」为开启状态，并确保运行环境已装好 `playwright` 及其浏览器内核。
平台风控策略会持续变化，浏览器兜底是当前最稳的路径。

**Q：会话里报「浏览器代发请求失败」？**
A：这是浏览器兜底启动失败。看 AstrBot 日志里的 `[browser]` 行，会直接写明原因：
`未下载浏览器内核` → 执行 `python -m playwright install --with-deps chromium`；
`缺少系统库` → 执行 `python -m playwright install-deps chromium`（RHEL 系需手动 yum）；
`root 环境` → v1.7.1 起已自动附加 `--no-sandbox`，升级到该版本即可。
Linux 服务器上直接跑 `python tools/diagnose_douyin.py --install`（一条命令装齐并自检），
或 `bash tools/setup_browser.sh`。
**Ubuntu 服务器本来就没有浏览器**，不需要装桌面 Chrome；playwright 会下载自己的 Chromium。

**Q：实况图只发了静态图？**
A：v1.8.0 起实况直链改由移动端 App 协议下发（不再依赖浏览器），v1.8.3 修掉了该协议「少带设备参数就静默退化成推荐流」的问题。
先确认作品**本身**带实况（纯图文作品没有实况，日志里现在只会有一行 debug，不再报 warning）；快手（含极速版）暂不支持取实况视频。
若确实带实况却仍只有静态图，看日志里有没有 `[抖音] 实况直链获取失败` 的 warning —— 那是真的没取到。

**Q：会话里出现「此项媒体下载失败」？**
A：这是某个媒体的直链下载失败。抖音一次会给同一条内容的 3~4 条等价直链（不同 CDN 节点 + `.jpeg`/`.webp` 两种格式），个别节点在部分线路/机房恒定 403。
v1.8.2 起插件会**自动换成备用节点重试**，不再随机挑一条就认命；只有整组候选都失败才放弃。日志里会有：

```
[下载] 主选直链失败（HTTP 403 Forbidden），改用备用直链第 1 条
[下载] 备用直链第 1 条成功 | https://...
```

若仍失败，说明该资源所有节点都取不到（多为直链过期），重新发一次链接即可。

**Q：`/相册少女集` 说「没有找到名为『少女集』的群相册」，但群里明明有这个相册？**
A：这是 v1.8.4 修掉的 bug。`bot.call_action()` 返回的是响应里的 `data` 字段（aiocqhttp 官方约定），
旧代码却按「完整信封」去取 `resp["data"]`，于是在真实环境里恒为 `None`，相册列表被解析成空。
升级到 v1.8.4 即可；管理员也可以用 `/相册自检` 直接看到插件读到的群相册名。
另外确认协议端是 **NapCat 且版本 ≥ v4.18.30**（群相册是 NapCat 扩展接口）。

**Q：多个机器人在同一个群里重复解析？**
A：插件内置贴表情仲裁，会在多个 Bot 之间自动选出唯一一个来解析。

---

## 🎉 致谢

本项目核心代码来自 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser)，经 [astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser) 移植到 AstrBot，再由本项目做本地化增强。请前往原仓库给作者点个 Star！

<div align="center">

**作者：** 流水，听雨的蛙，落雪，Deepseek-v4.1-Flash

</div>
