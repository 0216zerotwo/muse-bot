# Muse Bot

一个自己托管的私人 AI 伙伴：像发消息一样交代事情，它替你查、比、记、提醒、动手做，需要你确认的事先问你。手机上有 iPhone 原生 App、安卓 App 和网页版，服务器跑在你自己的电脑或 VPS 上，模型用你自己的 API key 或会员订阅。

A self-hosted personal AI companion with native iPhone and Android apps and a web app. It runs on your own computer or server, powered by its own native Python runtime and your own model keys.

> 本项目与 Meta（Muse）、xAI（Grok）没有任何关系，名字和图标只是灵感来源。

## 能做什么

- **聊天和卡片**：回复短，结构化内容（天气、行程、比价、车次、表格、图表、地图）直接是卡片或可交互的小页面；文件（PDF、文档、表格、网页）在聊天里直接预览。
- **替你动手**：浏览器（无界面 Chromium，可随时接管）、搜索、读写文件、执行命令、生成图片和视频、定时任务和提醒。发邮件、付款、在网页上提交等对外操作一律先给你一张确认卡片，写清楚在哪个网站点什么。
- **记得你**：长期记忆（可查看、可删）、主动找你（早报、到点提醒）、目标和计划（`/goal` 多步自动推进）、帮手（不同角色的子助理）。
- **你的模型、你的数据**：在 App 里添加任意 OpenAI 兼容、Responses 或 Anthropic 协议的模型（DeepSeek、火山方舟、Claude、OpenAI、Gemini、xAI…），也支持导入 ChatGPT Plus/Pro（Codex）或 SuperGrok 订阅登录；所有数据都在你的服务器上。
- **手机端**：iPhone 原生 SwiftUI App；安卓 App（网页 + 原生能力：相机、相册、通知、分享、朗读）；任何浏览器也能直接用（可添加到主屏幕）。

## 结构

```
手机（iPhone App / 安卓 App / 浏览器）
        │  HTTPS + WebSocket，配对后的设备令牌
        ▼
Muse Bot 服务器 = muse/（自研异步智能体运行时）+ companion/（伙伴接口、安全与记忆服务）
        │  模型 API（你的 key / 订阅登录） · 浏览器 · 搜索 · 连接的服务（邮箱、日历、地图…）
```

| 目录 | 内容 |
|---|---|
| `muse/` | 自研原生智能体运行时（Python）：多服务商流式传输、工具与 MCP 编排、双轨安全审批、`muse.db` 存储与跨平台 `musebot` CLI |
| `companion/` | 伙伴业务与接口层（Python）：App 路由、设备配对、权限与隐私守卫、记忆、定时任务、帮手、创意与浏览器协作 |
| `web/` | 网页版（React + Vite），服务器直接提供；安卓 App 显示的也是它 |
| `ios/` | iPhone 原生 App（SwiftUI），GitHub Actions 云端编译 |
| `android/` | 安卓 App（WebView + 原生能力），不需要 Gradle |
| `deploy/` | 部署到 VPS：一条命令 `npx musebot-server install`（`deploy/npm`），或 Docker 安装包（`deploy/vps`） |
| `tools/` | 跨平台初始化（`tools/setup.py`）、配对（`tools/companion-pair.py`）与图标制作脚本 |

## 快速开始：在自己电脑上跑起来

支持 **Windows PowerShell**、**macOS** 与 **Linux** 原生运行（无需 Git Bash）。需要：**git**、**[uv](https://docs.astral.sh/uv/getting-started/installation/)**（自动管理 Python 3.14 与虚拟环境）、**Node.js 18+**。

```powershell
git clone https://github.com/0216zerotwo/muse-bot.git
cd muse-bot
python tools/setup.py          # 创建 .venv、生成默认 config.yaml（预置浏览器 MCP）、构建网页
uv run musebot serve           # 启动服务器（Ctrl+C 停止）
```

然后在这台电脑的浏览器打开 <http://127.0.0.1:8766>（本机访问不用配对）：

1. 第一次打开会引导你给伙伴起名字、选形象和性格，并添加模型。
2. **设置 → 模型 → 添加模型**：填服务的地址（base_url）、API key 和模型名，比如 DeepSeek `https://api.deepseek.com` + `deepseek-chat`（或导入订阅登录）。密钥只存在服务器的 `muse-home/.env`（权限 `0600`）。
3. 想让它用浏览器、邮箱、日历、地图等：**设置 → 已连接的服务**（新装已默认预置浏览器连接）。

你的数据都在 `muse-home/`（API key、聊天记录、`muse.db`、记忆、已配对的设备），Git 不会提交它。想放在别处，可设置环境变量 `MUSE_HOME` 或传入 `--home <路径>`。

常用 CLI 命令：

```powershell
uv run musebot serve [--host 0.0.0.0] [--port 8766]   # 启动服务器
uv run musebot setup                                  # 初始化或更新本地环境与网页构建
uv run musebot pair 192.168.1.20:8766                 # 生成手机一次性配对码
uv run musebot pack [--with-data]                     # 纯 Python 打包 VPS 安装包（Windows 亦无需 Git Bash）
uv run musebot doctor [--json] [--clean-legacy]       # 检查运行环境健康度 / 清理 v1.x 遗留目录
```

### 从 v1.x 无感升级到 v2.0

如果你之前运行过 v1.x（目录下已有 `hermes-home/` 或设置了 `HERMES_HOME`）：

1. 拉取最新代码后运行 `python tools/setup.py` 与 `uv run musebot serve`。
2. `v2.0` 会自动**原位沿用**原有的 `hermes-home/`：已配对的手机无需重新配对，`.env` 密钥、`config.yaml`、长期记忆、技能与定时任务全部保留；启动时会自动以**只读拷贝**方式把旧版 `state.db` 中的历史会话、目标与用量增量同步进 `muse.db`（原 `state.db` 字节零修改，可随时无损回退）。
3. 确认一切正常后，可运行 `uv run musebot doctor --clean-legacy` 一键安全清理旧版 `hermes/`、`hermes-env/` 目录及未修改过的旧内置技能。

## 连上手机

| | 怎么装 | 说明 |
|---|---|---|
| **iPhone** | Fork 本仓库后 **Actions → Build iOS** 生成未签名 `.ipa`，在电脑上用 Sideloadly 和免费 Apple ID 安装 | 详见 [ios/README.md](ios/README.md) |
| **安卓** | **Actions → Build Android** 下载 APK 安装；或 `bash android/build.sh` 自己打包 | 详见 [android/README.md](android/README.md) |
| **浏览器** | 直接打开服务器地址；iPhone 也可在 Safari「添加到主屏幕」作为网页应用使用 | 推送通知需要 HTTPS |

服务器默认只让本机访问。要让同一 Wi-Fi 下的手机连上，启动时绑定 `0.0.0.0`（别人连进来也必须先配对），并在电脑防火墙里放行 8766 端口：

```powershell
uv run musebot serve --host 0.0.0.0
```

第一次连接时，在服务器上生成一次性配对码（支持直接写域名、局域网 `IP:端口` 或完整 URL）：

```powershell
uv run musebot pair 192.168.1.20:8766                  # 局域网自动用 http://；公网域名自动用 https://
# 或直接运行标准库脚本：python tools/companion-pair.py 192.168.1.20:8766
```

在 App 里填地址和配对码（或直接粘贴它给的「一键连接」链接）。同一 Wi-Fi 下用电脑的局域网 IP；手机用数据线连电脑时，安卓可以用 `adb reverse` 直接连 `127.0.0.1`（见安卓说明）。已连上的设备也能在 **设置 → 设备** 里生成新的配对码。

## 部署到服务器（常驻、HTTPS）

Ubuntu 22.04+ / Debian 12+，2 核 4G，放行 80 / 443 端口：

```bash
sudo apt-get install -y nodejs npm
sudo npx -y musebot-server install 你的域名     # 没有域名就不写，会自动用 <IP-横杠>.sslip.io
```

它会装好 Docker、基于 `debian:13-slim` 与锁定版本的 `uv.lock` 构建自包含镜像（`muse/` 原生运行时 + `companion/` 伙伴服务 + 无界面 Chromium + Playwright MCP + `ffmpeg` 音频转码 + Web Push）、用 Caddy 自动申请 HTTPS 证书，并设为开机自启，最后打印配对码。

- **在线无感升级（含 `v1.x -> v2.0`）**：`sudo npx -y musebot-server update`（先构建新镜像再原子切换，保留已配对设备并自动同步历史数据库；若探活失败会自动回滚镜像与配置）。
- **常用命令**：`sudo npx musebot-server status | logs [-f] [--caddy] | restart | pair | version | uninstall`。
- **带数据搬家上云**：在电脑上运行 `uv run musebot pack --with-data` 生成私有安装包上传即可（Windows PowerShell 直接可用）。详见 [deploy/npm/README.md](deploy/npm/README.md) 和 [deploy/vps/README.md](deploy/vps/README.md)。

## 隐私和安全

- 所有数据（聊天、记忆、文件、API key）只在你的服务器上；手机上只缓存聊天副本，不存密钥。
- 手机要先用一次性配对码配对；配对码猜错多次会按 IP 限流并锁定。设备令牌只存哈希，可在 **设置 → 设备** 随时移除。
- 对外的操作（发邮件、付款、在网页上提交、上传文件）默认先问你；高危终端命令会在后台异步挂起等你拍板；每类操作能放到多大权限可在 **设置 → 权限** 调整。读过你的私人数据后，往其他网站发送内容也会先问你。
- 服务端实施四重日志脱敏：只记录消息与参数长度（`<N 字>`），并对 URL 查询参数与子进程环境变量自动打码剥离。

## 开发

```powershell
# 运行时与伙伴接口单元测试（先运行 python tools/setup.py）
uv run python -B -W error -m unittest discover -s muse/tests -t . -p "test_*.py"
cd companion/tests
uv run python -B -W error -m unittest discover -s . -p "test_*.py"
cd ../..

# 部署与工具脚本测试
node --test deploy/tests/*.test.js

# 网页
cd web && npm run lint && npm test && npm run build    # 开发时 npm run dev（连本机 8766 的服务器）

# iPhone 与安卓静态契约检查（Windows / Linux / macOS 通用；iOS 编译在 macOS CI）
uv run python ios/scripts/validate.py
uv run python android/scripts/validate.py
```

发布新版本：`git tag v2.0.0 && git push origin v2.0.0`，GitHub Actions 会运行测试门禁、构建纯原生服务器安装包（`< 6 MB`）并附带 `.sha256` 校验文件发到 Releases，服务器上 `sudo npx -y musebot-server update` 就能校验升级（npm 安装器本身很少需要重新发布）。

改了图标：把 1024×1024 的图放到 `tools/icon-runs/chosen/muse-bot-icon.png`，运行 `uv run python tools/make_app_icons.py`，一次更新 iOS、安卓和网页的所有尺寸。

## 致谢与许可

本项目以 [MIT 许可](LICENSE) 发布。它建立在这些开源项目与设计灵感之上（详见 [NOTICE](NOTICE)）：

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)（Nous Research，MIT）：早期 v1.x 架构参考与本地数据目录格式兼容来源（v2.0 起已全面采用本仓库内置的自研 `muse/` 原生智能体运行时）
- [nanobot](https://github.com/HKUDS/nanobot)（HKUDS，MIT）：网页版最初来自它的 WebUI
- iPhone 端的 [MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui)，互动卡片里的 [Chart.js](https://www.chartjs.org/) 和 [Leaflet](https://leafletjs.com/)，模型图标来自 [lobe-icons](https://github.com/lobehub/lobe-icons)，可选本机离线语音识别支持 [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) 与 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
