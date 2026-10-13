# 更新日志

## v1.8.2

> 修复 **「此项媒体下载失败」**：图片直链挑到坏 CDN 节点就整张失败，现在自动换节点重试。

### 修复

- **图片/视频直链改为「多候选 + 自动回退」**。抖音一次会给 3~4 条等价直链
  （不同 CDN 节点、`.jpeg` 原图与 `.webp` 两种格式），它们的可用性并不相同：
  个别节点在部分机房/线路恒定 403（日志里的
  `HTTP 403 Forbidden ... p3-sign.douyinpic.com/...~tplv-dy-aweme-images:q75.jpeg`），
  用 `<url>` 直接打开也是 403。此前是从 `url_list` 里 `random.choice` 随机挑一条，
  一旦挑到坏节点，那张图就变成「此项媒体下载失败」，而同一张图的其它节点其实是好的。
  - 新增 `core/parsers/douyin/urlpick.py`：对候选直链做**确定性排序**
    （原图 jpeg 优先于 webp；`*-pc-sign`、`*-ex-gddgtc-*` 等已知问题节点降到最后），
    首选之外的全部候选作为备用链路。
  - `Downloader.streamd()` 新增 `fallback_urls`：主选直链重试耗尽后**依次换备用直链**，
    落盘文件名仍按主选直链生成，不产生重复缓存；全部候选都失败才抛
    `DownloadException`（行为与旧版一致）。
  - 静态图、实况 mp4、视频、封面、头像全部接上备用链。
- **移动端协议改为只认精确命中**。该接口返回的其实是「推荐流」，被请求的作品通常排在
  首位，但**并非总是出现**（实测部分 slides 作品就不在列表里）。此前找不到目标时会退而
  取第一条，等于把一条毫不相干的视频当成用户发的链接发出去；现在匹配不到就返回空，
  交给分享页/web 通道处理。

### 实测

- 本地起 HTTP 服务模拟 403：主选 403 → 备用直链救回 ✅；全部候选都 403 →
  仍抛 `DownloadException` ✅；备用链与主选重复、不传备用链时行为与旧版一致 ✅。
- 真实数据（`v.douyin.com/4zhoptsRhGY` → 图文 7 张）：把每张图的首选**故意换成坏节点**，
  7 张全部靠备用直链下载成功 ✅。
- `urlpick.order()` 排序/去重/过滤 12 项、`pick_aweme` 严格匹配 4 项、相册上传回归 26 项，
  共 46 项全部通过。

## v1.8.1

> 新增 **`/相册少女集`**：解析出来的图片和视频直接存进 QQ 群相册，不再往群里刷屏。

### 新增

- **`/相册少女集 <链接>`**：先解析链接，再把图片和视频上传到群相册「少女集」
  （相册名见配置项「相册名称」），只在群里回一条上传结果。
  - 图片走 `upload_images_to_qun_album` 一次性上传（相册里显示为同一条）；
    旧版 NapCat 没有该接口时自动降级为逐张 `upload_image_to_qun_album`。
  - 视频走 `upload_video_to_qun_album`（封面自动抽首帧）；旧版 NapCat 没有该接口时
    自动降级为上传视频封面图，没有封面则明确报失败原因。
  - 抖音图文实况：静态图按图片上传，实况 mp4 按视频上传。
  - **文案写到相册评论**：群相册上传接口本身没有标题参数，改用
    `do_group_album_comment` 把作品文案写到相册里最新一条媒体上（可关）。
- 新增配置项：`album_enabled`、`album_name`（默认 少女集）、`album_title_comment`、
  `album_max_media`（默认 20）、`album_base64_fallback`。
- 新增 `core/album.py`（上传客户端）与 `_albumprobe/test_uploader.py`（离线自测）。

### 说明

- 走的是 **NapCat 扩展 action**（`get_qun_album_list` 等），经 AstrBot 已经建好的
  连接调用，**不需要额外填写 NapCat 地址与 token**。
- 相册需要**先在群里建好**，插件不会自动创建；找不到时会把群里已有的相册名一并回出来。
- `upload_images_to_qun_album` / `upload_video_to_qun_album` 需要 **NapCat v4.18.30+**；
  低版本会走降级链，不会整条失败。
- AstrBot 与 NapCat 不在同一台机器时，本地路径传不过去，会再用 base64 试一次
  （仅对小于 24 MB 的文件生效）。

## v1.8.0

> **单条作品（视频 / 图文 / 实况）不再需要浏览器。**
> 只有 `/全部` 主页作品清单仍受平台风控限制、依赖浏览器内核。

### 修复

- **实况图只发静态图 / 普通视频取不到直链**：PC web 详情接口被 Argus 风控封死后，
  实况 mp4 直链曾只能靠浏览器代发获取。现改走**移动端 App 推荐流协议**：

  ```
  GET https://api5-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id=<id>&aid=6383
  User-Agent: com.ss.android.ugc.aweme/300500 (Linux; U; Android 13; zh_CN; ...)
  ```

  实测该通道**不需要 Cookie、不需要 ttwid / a_bogus / msToken / secsdk 签名**，
  HTTP 200 且 `aweme_list` 中恰好包含被请求的那条作品，字段完整：

  | 需要的字段 | 位置 |
  | --- | --- |
  | 实况（Live Photo）mp4 | `images[].video.bit_rate[].play_addr.url_list` |
  | 无水印视频 mp4 | `video.bit_rate[].play_addr.url_list`（按 `is_h265==0` + 码率挑档） |
  | 静态图原图 | `images[].url_list` 末项 |
  | 封面 / 作者 / 头像 / 音乐 / 发布时间 | `video.cover`、`author`、`music`、`create_time` |

  → 新增 `core/parsers/douyin/mobile.py`，`/全部` 之外的所有抖音解析链路都不再碰浏览器。

### 变更

- `_fetch_aweme_detail()` 改为**移动端协议优先**：命中即返回；移动端取不到时才回退
  PC web 详情接口（那条路才需要浏览器代发签名）。实况直链的来源随之从「浏览器」
  换成「移动端协议」。
- 图文 / 视频分享页解析失败（`_ROUTER_DATA` 空壳）时自动改走移动端协议，
  不再直接报「分享已删除或资源直链提取失败」。
- `slidesinfo` 未收录的作品（`filter_list` 里的普通图文/实况）同样改走移动端协议，
  不再直接抛「slidesinfo 接口未返回作品数据」。
- 浏览器启动失败后进入 **5 分钟冷却**，避免每条消息都白等一次启动。

### 修复（细节）

- 图集静态图优先避开 `p3-pc-sign.douyinpic.com`（实测该节点对部分资源返回 403，
  同路径的 `p9` / `p6` / `p11` 节点正常）。
- `mobile.video_url()` 会过滤掉背景音乐（`.mp3` / `ies-music`），避免图文作品
  误把原声 mp3 当成视频直链。

### 已知限制

- `/全部` / `/全部音乐` 需要作者主页作品清单，接口 `aweme/v1/web/aweme/post/` 与
  `iesdouyin web/api/v2/aweme/post/`（`shark_fail_1`）目前都取不到，
  **仍必须安装浏览器内核**（`python -m playwright install --with-deps chromium`）。
  未安装时会给出一条说明清楚的报错，而不是笼统的失败。

## v1.7.3

### 修复

- **Linux（Ubuntu / Debian）服务器不再白试一次 Edge**：`browser_channel` 默认值由 `msedge`
  改为 `auto`，`BrowserFetcher.candidates()` 按平台排序尝试顺序——Windows 试
  `msedge → chrome → 自带 chromium`，macOS 试 `chrome → msedge → 自带 chromium`，
  **Linux 试 `chrome → chromium → 自带 chromium`**。Linux 上没有 Edge，此前每次都先失败一次
  再回落，日志噪音大且首启更慢。
- **报错提示改成可直接照抄的命令**：内核缺失时提示
  `python -m playwright install --with-deps chromium`，并给出
  `PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright` 国内镜像与
  Ubuntu 的 `apt-get install libnss3 libnspr4 ...` 依赖清单。

### 新增

- `browser_channel` 新增 `executable:/usr/bin/google-chrome` 写法，可直接指定系统浏览器可执行文件；
  另支持环境变量 `WANNENG_BROWSER_CHANNEL` 覆盖配置（改配置不方便时用）。
- 自检脚本新增一键安装：`python tools/diagnose_douyin.py --install`
  （自动装 playwright 包 + Chromium 内核 + 系统依赖，默认走 npmmirror 镜像，装完继续自检）。
- 自检脚本第 3 段现在同时报告 `chromium-*` 与 `chromium_headless_shell-*` 目录，
  并检测系统里是否已有 chrome/chromium/msedge 可直接复用，最后按发行版打印可复制的修复命令。
- 启动成功后日志会写明实际使用的通道（`[browser] 浏览器已启动: chrome`）。

## v1.7.2

### 修复

- **实况直链获取失败时不再静默**：`_fetch_live_map()` 过去把异常记在 `logger.debug` 后返回空集合，
  用户只会看到「图文作品的实况变成了静态图」而没有任何提示。现改为 `warning` 级并写明原因
  （详情接口 403 / 浏览器兜底不可用等），可在 AstrBot 面板日志里直接定位。

### 新增

- `tools/diagnose_douyin.py`：抖音浏览器兜底环境自检脚本。不依赖插件任何模块，可单独拷到服务器运行，
  依次检查 Python 环境 → playwright 可导入 → Chromium 内核是否已下载 → 能否启动（含 `--no-sandbox`）
  → 能否打开抖音首页并拿到风控 Cookie → 能否经页面代发 `aweme/detail` 并取到实况 mp4 直链。
  ```bash
  python tools/diagnose_douyin.py                 # 用内置样本作品
  python tools/diagnose_douyin.py <aweme_id>      # 指定作品
  ```

## v1.7.1

### 修复

- **修复 Linux 服务器（尤其以 root 运行的云主机 / 容器）上抖音浏览器兜底报
  「浏览器代发请求失败」的问题**：Chromium 在 root 下必须带 `--no-sandbox` 才能启动，
  容器 `/dev/shm` 过小时还需 `--disable-dev-shm-usage`。`core/browser.py` 新增
  `launch_args()`，在 POSIX 平台自动附加
  `--no-sandbox --disable-dev-shm-usage --disable-gpu`（Windows 行为不变）。

### 变更

- 浏览器兜底失败时**不再只抛笼统提示**：`BrowserFetcher` 记录 `last_error`，
  并针对「内核未下载」「缺系统库」「系统无 Chrome/Edge」分别给出可执行的修复提示，
  错误会直接显示在会话里，同时写入 AstrBot 日志的 `[browser]` 行。

### 新增

- 新增 `tools/setup_browser.sh`：Linux 服务器一键安装 playwright 的 Chromium 内核与系统依赖
  （自动识别 apt / yum / dnf），并做一次「真实启动 + 打开抖音首页」冒烟测试。

### 部署提示

- Linux / Docker 上，仅 `pip install playwright` 不够，必须再执行
  `python -m playwright install chromium`；Docker 请在容器内执行。

## v1.7.0

### 变更

- **插件更名为「万能解析」**：`metadata.yaml` 的 `name` 与 `display_name` 统一为 `万能解析`；
  `core/config.py` 的插件包名常量 `_plugin_name` 同步改为 `万能解析`，并新增路径回退逻辑——
  插件目录按包名定位失败时回退到代码所在目录，数据目录在新包名不存在时自动沿用旧目录
  `astrbot_plugin_parser_live`，升级不会丢失 Cookies 与缓存。
- **作者信息更新**为「流水，听雨的蛙，落雪，Deepseek-v4.1-Flash」。
- **重写 README 说明**：补充实况图说明、完整命令表、可选参数、依赖（含 Playwright）与常见问题。

### 新增

- 新增 **`/解析帮助`** 命令（别名 `解析说明`、`parserhelp`），输出当前已启用的平台列表、
  全部命令、可选参数用法与实况图说明；帮助文案集中在 `core/help.py` 维护。
- 新增打包脚本 `tools/package.py`，一键生成 `dist/万能解析.zip`，并同时输出 ASCII 名副本
  `dist/wanneng-jiexi.zip`（GitHub Release 附件名不接受中文，上传会返回 422）。
- 新增 `tools/verify_archive.py`，按 AstrBot 安装流程的校验逻辑（根目录推断、metadata 定位、
  必需字段、目录名合法性）预先校验压缩包，避免面板上传时报「压缩包不是合法的 AstrBot 插件」。
- `metadata.yaml` 补充 `short_desc`（插件市场卡片短描述）与 `repo`（仓库地址）字段。

### 涉及文件

- `metadata.yaml`
- `core/config.py`
- `core/help.py`（新增）
- `main.py`
- `README.md`
- `CHANGELOG.md`
- `tools/package.py`（新增）
- `tools/verify_archive.py`（新增）

## v1.6.7

### 修复

- 修复抖音「/全部」「/全部音乐」命令一律返回 `抖音接口状态码: 403` 的问题。
- 修复抖音实况图（Live Photo）退化为静态图的问题：实况直链取不到时只能回退静态图。

### 变更

- 抖音解析器新增**浏览器兜底**。抖音自 2026-09 起在边缘网关启用
  `ArgusSecurityPlugin`，对「主页作品（aweme/post）」「作品详情（aweme/detail）」
  等接口强制校验 `x-secsdk-web-signature`，纯 HTTP 客户端即便带上 a_bogus、ttwid、
  完整 Cookie 也会被拒：

  ```
  403 Blocked by ArgusSecurityPlugin Uifid Not Found
  ```

  该签名只能由抖音页面内建的 secsdk 在真实浏览器环境算出，因此这两个接口在检测到
  403 时会**自动改用真实浏览器页面代发请求**（页面内 XHR 会被 secsdk 自动补签名）。
  普通 HTTP 仍是首选路径，只有被拦截时才启动浏览器，且浏览器空闲后自动关闭。
- 新增依赖 `playwright`（复用系统 Edge/Chrome 时无需再下载浏览器）。
- 新增抖音解析器配置项：
  - `browser_fallback`：浏览器兜底开关（默认开启）
  - `browser_channel`：复用的系统浏览器，默认 `msedge`
  - `browser_headless`：无头模式（默认开启）
  - `browser_idle_timeout`：浏览器空闲自动关闭秒数（默认 300）

### 涉及文件

- `core/browser.py`（新增）
- `core/parsers/douyin/__init__.py`
- `core/config.py`
- `_conf_schema.json`
- `requirements.txt`

## v1.5.7

- 新增引用解析开关
- 新增 allcpp 解析器
- 修复更新插件时字体占用导致无法更新的问题

## v1.5.6

新功能：
- 引入 `require_at_in_group` 配置项，用于控制机器人在群聊中是否仅在被明确 @ 提及时才响应。
- 支持在群消息中同时识别来自 At 段和纯文本提及模式的 @ 提及。

错误修复：
- 阻止在缺少 `SESSDATA` 时保存或使用 B 站凭证，并提供警告及重新加载尝试。
- 在加载 B 站凭证时规范化 Cookie 键名，以处理小写形式的 `SESSDATA` 和 `DedeUserID`。

增强改进：
- 使媒体大小和时长限制的失败消息可根据配置选择是否显示。
- 优化群消息过滤逻辑，忽略仅提及其他机器人或用户而未提及当前机器人的消息。

## v1.5.5

### 新增

- 新增`引用解析`功能，可通过引用消息中的链接来触发解析

### 修复

- 当未安装 `curl_cffi` 时，为 Bilibili 解析器的初始化添加保护，并给出明确的错误提示。
- 当来源提供字符串或浮点数值时，将 Bilibili 的 dynamic 和 opus 发表时间戳字段规范化为整数。
- 修复缓存路径，将配置中路径的获取从 get_astrbot_data_path 更正为 get_astrbot_plugin_data_path。

## v1.5.4

- 新增 pixiv 解析器

## v1.5.2

### 新增

- 增加 iwara 视频 / 图片解析器 @sssysy
- 新增微信视频号(shipinhao)解析器 @anon0v0

### 优化

- 扩展 Twitter 解析器，使其能够同时处理 twitter.com 和 x.com URL，并规范化处理 @Zhalslar

- 基于 SSR 为抖音链接新增规范分享页解析能力 @piexian

### 修复

- 修复本地媒体发送时的文件路径问题 @piexian
- 无匹配视频流时优雅报错，避免 IndexError 中断解析 @anon0v0

## v1.5.1

变更:

- 允许为 B 站下载指定一个“首选视频编码格式列表”，而不再局限于单一编码格式。

修复:

- 当基于凭证（如 cookies）的请求失败时，回退为警告信息，从而避免在获取 B 站 AI 总结时发生崩溃。
- 通过正确读取使用 `rich` 或 `orig_text` 字段而非 `word` 字段的 `TEXT_NODE_TYPE_RICH` 节点中的文本，在解析 Bilibili 动态时保留标签。

## v1.5.0

## 概述

新增知乎链接解析能力，支持对知乎内容进行识别、请求、内容提取与卡片生成，并以模块化结构实现，便于后续维护和扩展。

## 变更内容

- 新增知乎解析器 [ZhihuParser](cci:2://file:///c:/mine/codes/astrbot_plugin_parser/core/parsers/zhihu/parser.py:14:0-84:9)
- 支持知乎常见页面类型解析
- 新增知乎请求处理逻辑
- 新增知乎内容提取逻辑
- 新增知乎卡片生成逻辑
- 将知乎解析实现拆分为多个职责清晰的模块，降低耦合度

## 支持范围

当前已支持以下知乎内容解析：

- 文章
- 回答
- 问题页面
- 想法

## 实现说明

本次实现采用 mixin 组合方式组织知乎解析器，主要拆分如下：

- [parser.py](cci:7://file:///c:/mine/codes/astrbot_plugin_parser/core/parsers/zhihu/parser.py:0:0-0:0)
  - 解析器主入口
- `request.py`
  - 请求相关逻辑
- `handlers.py`
  - 不同页面/数据类型的处理逻辑
- `content.py`
  - 内容提取与格式化
- `card.py`
  - 卡片摘要与展示内容生成
- `common.py`
  - 公共工具与共享逻辑

这样的拆分方式可以让不同职责独立维护，也方便后续继续补充更多知乎页面类型或优化展示效果。

## 变更动机

知乎链接在日常使用中较为常见，但此前缺少对应解析支持。该 PR 旨在补齐知乎平台解析能力，提升插件对主流内容平台的覆盖范围和使用体验。

## 测试情况

已完成以下方向的功能验证：

- 知乎链接识别是否正确
- 不同知乎页面类型是否能正确路由到对应解析逻辑
- 文章 / 回答 / 问题 / 想法内容是否能正常提取
- 卡片摘要与发送头部信息是否符合预期

## 兼容性与风险

- 本次改动为新增平台解析能力，对现有其他平台解析逻辑影响较小
- 主要风险集中在知乎页面结构或接口返回字段变化时的兼容性
- 后续如知乎页面结构调整，预计只需在对应模块中局部修正

## v1.4.0

### 新增

- 新增小黑盒平台支持，并同步更新 README 支持列表。
- 新增 `core/parsers/xiaoheihe.py`，支持：
  - 小黑盒帖子链接与分享卡片解析
  - 小黑盒游戏详情页链接与分享卡片解析
- 新增标准内容类型 `TextContent`，用于承载独立文本消息项。
- 新增 `create_video_content_by_task(...)`，支持基于自定义下载任务构造视频内容。
- 新增 `ytdlp_download_video_relaxed(...)`，用于宽松处理特定视频下载场景。
- 新增小黑盒配置项：
  - `show_body_text`
  - `video_send_mode`

### 变更

- 扩展配置结构，正式纳入小黑盒解析器配置节点。
- 扩展 sender，对 `TextContent` 提供标准发送支持。
- 调整小黑盒视频发送策略：视频内容从合并转发中分离，改为单独发送。
- 调整小黑盒视频输出策略，支持以下模式：
  - `不发送视频`
  - `发送第一条视频`
  - `发送全部视频`
- 调整小黑盒 `.m3u8` 视频处理路径，改为使用宽松 yt-dlp 下载逻辑。
- 增强小黑盒游戏页解析结果，纳入图片、结构化文本与视频提取能力。

### 涉及文件

- `README.md`
- `_conf_schema.json`
- `core/config.py`
- `core/data.py`
- `core/download.py`
- `core/parsers/__init__.py`
- `core/parsers/base.py`
- `core/parsers/xiaoheihe.py`
- `core/sender.py`
- `default_template.json`
- `metadata.yaml`

### 说明

本次升级（包括小黑盒网页逆向）由 GPT 5.4 驱动的 AI 智能体协助完成。
本次升级已通过人工验收，不会影响任何插件已有的功能。
