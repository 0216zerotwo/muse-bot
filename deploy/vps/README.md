# 把伙伴装到 VPS

一个脚本装好全部核心功能：伙伴本身、它的浏览器（无界面 Chromium）、浏览器自动化（Node + `@playwright/mcp`）、音频转码（`ffmpeg`）、Web Push 推送通知、HTTPS 证书、开机自启。镜像基于 `debian:13-slim` 按 `uv.lock` 固定版本从源码构建 Hermes Agent 并挂载本仓库的 `companion` 插件，前面放 Caddy 自动申请和续期 HTTPS 证书。

## 选 VPS

| | 建议 |
|---|---|
| 地区 | **香港、新加坡、日本**等境外机房。大陆机房要做 ICP 备案才能开放 80/443，而且拉 Docker 镜像、npm 包经常失败 |
| 配置 | 2 核 4G 内存，磁盘 20G 以上（最低 2G 内存，脚本会自动加交换空间） |
| 系统 | Debian 12+ 或 Ubuntu 22.04 / 24.04 |
| 端口 | 在云服务商的「安全组 / 防火墙」里放行 TCP 80、443（建议同时放行 UDP 443 以启用 HTTP/3） |

域名可有可无：有的话先把它解析到 VPS 的 IP；没有的话脚本会自动用 `<IP-横杠>.sslip.io`（例如 `203-0-113-10.sslip.io`），同样能拿到正式 HTTPS 证书（iPhone 的通知和语音都要求 HTTPS）。

## 最省事：一条 npx 命令

在服务器上直接运行（详见 [deploy/npm/README.md](../npm/README.md)）：

```bash
sudo apt-get install -y nodejs npm
sudo npx -y musebot-server install 你的域名
```

效果和下面的三步一样（下载并校验 GitHub Releases 里的同一份安装包），只是不用在电脑上打包、上传；以后升级运行 `sudo npx -y musebot-server update`。要带着电脑上的数据搬家，用下面的 `pack.sh --with-data`，再 `sudo npx -y musebot-server install 你的域名 --bundle 那个包`。

## 三步装好（本地打包上传）

**1. 在电脑上打包**（Git Bash / Linux / macOS，先确保已运行过 `cd web && npm ci && npm run build`）：

```bash
bash deploy/vps/pack.sh --with-data
```

`--with-data` 会把当前 `HERMES_HOME` 里的记忆、对话、配置（自动重写为服务器路径与内置 `playwright-mcp`）和 API 密钥一起打进权限为 `0600` 的私有压缩包；不加就是全新安装，模型在 App 的 **设置 → 模型** 里添加。打好的包在 `deploy/dist/`。

**2. 上传并安装**：

```bash
scp deploy/dist/musebot-server-*.tar.gz root@你的服务器IP:/root/
ssh root@你的服务器IP
tar xzf musebot-server-*.tar.gz && bash musebot-server-*/install.sh 你的域名
```

不写域名就自动用 `<IP-横杠>.sslip.io`。第一次安装要几分钟（下载基础镜像、浏览器和 Python 运行时）。

**3. 手机连上**：首次安装最后会打印**配对码**和**一键连接链接**。
- **iPhone 原生 App**：按 [ios/README.md](../../ios/README.md) 安装 `.ipa` 后填入服务器地址与配对码（或直接打开一键连接链接）；也可直接用 Safari 打开 `https://你的域名` → 分享 → 添加到主屏幕。
- **安卓 App**：打开 App 后把一键连接链接粘贴进去（或填入地址与配对码）。

配对一次就固定下来，之后不用再连。

## 可选：开启服务器本机离线语音识别（SenseVoice）

镜像已内置 `ffmpeg` 用于语音音频转码。若希望语音按钮在服务器 CPU 上直接离线转文字（无需云端语音 API key，支持中英粤日韩），可在 `/opt/companion/data/companion/stt/` 下准备依赖和模型（容器 UID 为 `10000`）：

1. 将 `sherpa-onnx` 与 `numpy` 安装到 `/opt/companion/data/companion/stt/pylib`（与镜像 Python 3.14 匹配）：
   ```bash
   docker exec -u root companion /usr/local/bin/uv pip install --python /opt/venv/bin/python \
     --target /opt/data/companion/stt/pylib sherpa-onnx numpy
   chown -R 10000:10000 /opt/companion/data/companion/stt
   ```
2. 将 SenseVoice-Small int8 模型的 `model.int8.onnx` 和 `tokens.txt` 放入 `/opt/companion/data/companion/stt/sensevoice/`，重启伙伴（`sudo npx musebot-server restart`）后会自动检测并启用。未放置模型时点击语音输入会提示语音识别尚未配置。

## 日常管理

| 要做的事 | 怎么做 |
|---|---|
| 再配对一台设备 | `sudo npx musebot-server pair`（或 `python3 /opt/companion/tools/companion-pair.py https://你的域名`），或在已连上的设备里 **设置 → 设备 → 生成配对码** |
| 升级 | `sudo npx -y musebot-server update`（先构建新镜像再原子替换，新版本若启动失败会自动回滚，数据保留） |
| 看日志 | `sudo npx musebot-server logs -f`（加 `--caddy` 看 HTTPS 反向代理日志） |
| 重启 | `sudo npx musebot-server restart`（或 `cd /opt/companion && docker compose restart`） |
| 改 API 密钥 | App 的 **设置 → 模型**；或编辑 `/opt/companion/data/.env` 后重启 |
| 卸载 | `sudo npx musebot-server uninstall`（保留 `/opt/companion/data` 数据与 `/opt/companion/caddy` 证书）或加 `--purge`（全部删除） |

## 安装脚本会改动什么

- 没有 Docker 时用官方脚本 `get.docker.com` 安装
- 内存小于 3G 且没有交换空间时，新建 2G 交换文件 `/swapfile-companion`
- `ufw` 防火墙开着时放行 `80/tcp`、`443/tcp`、`443/udp`
- 程序和数据都在 `/opt/companion`（数据目录 `/opt/companion/data` 权限收紧为 `0700`，密钥文件 `0600`）；两个容器 `companion`（伙伴）和 `companion-caddy`（HTTPS）

伙伴只监听服务器本机的 8766 端口，外部流量全部经 Caddy 的 HTTPS 访问，且必须经过设备配对认证。
