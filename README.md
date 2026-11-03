# Muse Bot

一个自己托管的私人 AI 伙伴：像发消息一样交代事情，它替你查、比、记、提醒、动手做，需要你确认的事先问你。手机上有 iPhone 原生 App、安卓 App 和网页版，服务器跑在你自己的电脑或 VPS 上，模型用你自己的 API key（任意 OpenAI 兼容的服务）。

A self-hosted personal AI companion with native iPhone and Android apps and a web app. It runs on your own computer or server, with your own model API keys.

> 本项目与 Meta（Muse）、xAI（Grok）没有任何关系，名字和图标只是灵感来源。

## 能做什么

- **聊天和卡片**：回复短，结构化内容（天气、行程、比价、车次、表格、图表、地图）直接是卡片或可交互的小页面；文件（PDF、文档、表格、网页）在聊天里直接预览。
- **替你动手**：浏览器（无界面 Chromium，可随时接管）、搜索、写文件、生成图片和视频、定时任务和提醒。发邮件、付款、在网页上提交等对外操作一律先给你一张确认卡片，写清楚在哪个网站点什么。
- **记得你**：长期记忆（可查看、可删）、主动找你（早报、到点提醒）、目标和计划、帮手（不同角色的子助理）。
- **你的模型、你的数据**：在 App 里添加任意 OpenAI 兼容的模型（DeepSeek、火山方舟、OpenAI…），可自动按任务挑选；所有数据都在你的服务器上。
- **手机端**：iPhone 原生 SwiftUI App；安卓 App（网页 + 原生能力：相机、相册、通知、分享、朗读）；任何浏览器也能直接用（可添加到主屏幕）。

## 结构

```
手机（iPhone App / 安卓 App / 浏览器）
        │  HTTPS + WebSocket，配对后的设备令牌
        ▼
Muse Bot 服务器 = Hermes Agent（开源智能体引擎）+ companion 插件（本仓库）
        │  模型 API（你的 key） · 浏览器 · 搜索 · 连接的服务（邮箱、日历、地图…）
```

| 目录 | 内容 |
|---|---|
| `companion/` | 服务器：Hermes Agent 的插件（Python），提供 App 的接口、审批、记忆、定时任务、浏览器等 |
| `web/` | 网页版（React + Vite），服务器直接提供；安卓 App 显示的也是它 |
| `ios/` | iPhone 原生 App（SwiftUI），GitHub Actions 云端编译 |
| `android/` | 安卓 App（WebView + 原生能力），不需要 Gradle |
| `deploy/` | 部署到 VPS：一条命令 `npx musebot-server install`（`deploy/npm`），或 Docker 安装包（`deploy/vps`） |
| `tools/` | 安装、运行、配对等脚本 |

## 快速开始：在自己电脑上跑起来

需要：**git**、**[uv](https://docs.astral.sh/uv/getting-started/installation/)**（管理 Python）、**Node.js 18+**；Windows 上用 **Git Bash** 运行下面的命令（Hermes 需要 bash）。

```bash
git clone https://github.com/0216zerotwo/muse-bot.git
cd muse-bot
bash tools/setup.sh                 # 下载 Hermes、建 Python 环境、建数据目录、构建网页
bash tools/run-hermes-gateway.sh    # 启动服务器（Ctrl+C 停止）
```

然后在这台电脑的浏览器打开 <http://127.0.0.1:8766>（本机访问不用配对）：

1. 第一次打开会引导你给伙伴起名字、选形象和性格。
2. **设置 → 模型 → 添加模型**：填服务的地址（base_url）、API key 和模型名，比如 DeepSeek `https://api.deepseek.com` + `deepseek-chat`。key 只存在服务器的 `hermes-home/.env`。
3. 想让它用浏览器、邮箱、日历、地图等：**设置 → 已连接的服务**。

你的数据都在 `hermes-home/`（API key、聊天记录、记忆、已配对的设备），Git 不会提交它。想放在别处，运行脚本前设置 `HERMES_HOME`。

## 连上手机

| | 怎么装 | 说明 |
|---|---|---|
| **iPhone** | Fork 本仓库后 **Actions → Build iOS** 生成未签名 `.ipa`，在 Windows 上用 Sideloadly 和免费 Apple ID 安装 | 详见 [ios/README.md](ios/README.md) |
| **安卓** | **Actions → Build Android** 下载 APK 安装；或 `bash android/build.sh` 自己打包 | 详见 [android/README.md](android/README.md) |
| **浏览器** | 直接打开服务器地址；iPhone 用 Safari「添加到主屏幕」 | 推送通知需要 HTTPS |

服务器默认只让本机访问。要让同一 Wi-Fi 下的手机连上，启动时加上 `COMPANION_HOST=0.0.0.0`（别人连进来也必须先配对），并在电脑防火墙里放行 8766 端口：

```bash
COMPANION_HOST=0.0.0.0 bash tools/run-hermes-gateway.sh
```

第一次连接时，在服务器上生成一次性配对码：

```bash
python tools/companion-pair.py http://你的电脑局域网IP:8766      # 或 https://你的域名
```

在 App 里填地址和配对码（或直接粘贴它给的「一键连接」链接）。同一 Wi-Fi 下用电脑的局域网 IP；手机用数据线连电脑时，安卓可以用 `adb reverse` 直接连 `127.0.0.1`（见安卓说明）。已连上的设备也能在 **设置 → 设备** 里生成新的配对码。

## 部署到服务器（常驻、HTTPS）

Ubuntu 22.04+ / Debian 12+，2 核 4G，放行 80 / 443 端口：

```bash
sudo apt-get install -y nodejs npm
sudo npx -y musebot-server install 你的域名     # 没有域名就不写，会用 <IP>.sslip.io
```

它会装好 Docker、构建镜像（伙伴 + 无界面浏览器 + 语音组件）、用 Caddy 自动申请 HTTPS 证书，并设为开机自启，最后打印配对码。常用：`sudo npx musebot-server status | logs -f | restart | pair | uninstall`。详见 [deploy/npm/README.md](deploy/npm/README.md) 和 [deploy/vps/README.md](deploy/vps/README.md)（也可以不用 npm，打包上传后运行 `install.sh`）。

## 隐私和安全

- 所有数据（聊天、记忆、文件、API key）只在你的服务器上；手机上只缓存聊天副本，不存密钥。
- 手机要先用一次性配对码配对；配对码猜错多次会锁定。设备令牌只存哈希，可在 **设置 → 设备** 随时移除。
- 对外的操作（发邮件、付款、在网页上提交、上传文件）默认先问你；每类操作能放到多大权限可在 **设置 → 权限** 调整。读过你的私人数据后，往其他网站发送内容也会先问你。
- 日志只记录消息长度，不记录内容。

## 开发

```bash
# 服务器插件的测试（需要先 bash tools/setup.sh）
cd companion/tests && ../../hermes-env/bin/python -B -m unittest discover -s . -p "test_*.py"
# Windows: ../../hermes-env/Scripts/python.exe

# 网页
cd web && npm run lint && npm test && npm run build    # 开发时 npm run dev（连本机 8766 的服务器）

# iPhone（Windows 上的静态检查；编译在 CI）
python ios/scripts/validate.py
```

改了图标：把 1024×1024 的图放到 `tools/icon-runs/chosen/muse-bot-icon.png`，运行 `python tools/make_app_icons.py tools/icon-runs/chosen/muse-bot-icon.png`，一次更新 iOS、安卓和网页的所有尺寸。

## 致谢与许可

本项目以 [MIT 许可](LICENSE) 发布。它建立在这些开源项目上（详见 [NOTICE](NOTICE)）：

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)（Nous Research，MIT）：智能体引擎，`tools/fetch-hermes.sh` 下载固定版本
- [nanobot](https://github.com/HKUDS/nanobot)（HKUDS，MIT）：网页版最初来自它的 WebUI
- iPhone 端的 [MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui)，互动卡片里的 [Chart.js](https://www.chartjs.org/) 和 [Leaflet](https://leafletjs.com/)，模型图标来自 [lobe-icons](https://github.com/lobehub/lobe-icons)
