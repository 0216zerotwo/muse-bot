# musebot-server

一条命令把 [Muse Bot](https://github.com/0216zerotwo/muse-bot) 服务器装到自己的 Ubuntu / Debian 服务器上：`muse/` 原生智能体运行时、`companion/` 伙伴服务、无界面浏览器（Chromium + `@playwright/mcp`）、音频转码（`ffmpeg`）、Web Push 推送、HTTPS 证书（Caddy 自动申请），装好就常驻，服务器重启后会自动起来。

这个 npm 包只是安装器（几 KB）。服务器程序从 GitHub 仓库的 [Releases](https://github.com/0216zerotwo/muse-bot/releases) 下载并自动校验 `.sha256` 完整性，所以服务器端的更新不需要重新发布 npm 包。

## 安装

服务器：Ubuntu 22.04 / 24.04 或 Debian 12+，2 核 4G 内存，20G 硬盘，能访问 github.com，云服务商的安全组放行 TCP 80、443（建议同时放行 UDP 443）。有域名的话先把它解析到服务器 IP；没有也行，会自动使用 `<IP-横杠>.sslip.io`（例如 `203-0-113-10.sslip.io`）。

```bash
sudo apt-get install -y nodejs npm        # 已经有 Node 就跳过
sudo npx -y musebot-server install 你的域名
```

脚本先列出要改动的东西，确认后才动手；第一次要几分钟（构建镜像、下载浏览器和 Python 环境）。最后打印**配对码**和**一键连接**链接，在 iPhone 原生 App、安卓 App 或手机浏览器（Safari 添加到主屏幕）中填进去就连上了。

> **离线语音识别（可选）**：镜像已带 `ffmpeg`；如需开启服务器本机 SenseVoice-Small（`sherpa-onnx`）离线语音转文字，见 [deploy/vps/README.md](../vps/README.md) 中的两步配置说明。

## 怎么常驻

- 伙伴和 Caddy 是两个 Docker 容器，`restart: unless-stopped`：进程异常退出会自动重启。
- Docker 本身是 systemd 服务，安装时 `systemctl enable docker`：服务器重启后 Docker 起来，再把两个容器拉起来。
- 想确认：`sudo npx musebot-server status`，「开机自启」一行是「是」就对了。

## 日常管理

| 要做的事 | 命令 |
|---|---|
| 看版本和运行状态 | `sudo npx musebot-server status`（或 `npx musebot-server version`） |
| 看伙伴 / Caddy 日志 | `sudo npx musebot-server logs -f`（加 `--caddy` 看 HTTPS 代理日志） |
| 重启 / 停下 / 启动 | `sudo npx musebot-server restart`（`stop` / `start`） |
| 再配对一台手机 | `sudo npx musebot-server pair` |
| **升级到最新版（支持 v1.x → v2.0 无感升级）** | `sudo npx -y musebot-server update`（先构建后原子切换，已配对设备与历史数据完整保留，探活失败自动回滚） |
| 装指定版本 | `sudo npx -y musebot-server update --version v2.0.0` |
| 卸载 | `sudo npx musebot-server uninstall`（保留数据与 HTTPS 证书），加 `--purge` 全部删除 |

## 把电脑上的数据搬过去

Releases 里的公开安装包**只有程序，没有任何数据和密钥**。要连记忆、对话和 API 密钥一起搬，在电脑上打私有的安装包，传到服务器后用 `--bundle` 装（Windows PowerShell / macOS / Linux 通用，无需 Git Bash）：

```powershell
uv run musebot pack --with-data                           # 电脑上直接打包（先确保已构建网页：cd web && npm ci && npm run build）
scp deploy/dist/musebot-server-*.tar.gz root@服务器:/root/
sudo npx -y musebot-server install 你的域名 --bundle /root/musebot-server-dev-日期.tar.gz
```

> 在 Linux / macOS 下也可以运行 `bash deploy/vps/pack.sh --with-data`，效果完全一致。装完记得删掉电脑和服务器上的这个压缩包（里面有密钥）。

## 发布新版本（维护者）

**服务器程序**（`muse/`、`companion/`、`web/`、`deploy/vps/` 改了）：打一个版本标签推上去，GitHub Actions 的 Release 流程会跑通全套测试、构建网页、打包纯原生安装包（检查无任何私有数据或密钥、体积 `< 6 MB`，并生成 `.sha256`），发布成 Release。服务器上 `update` 就能拿到。

```bash
git tag v2.0.0 && git push origin v2.0.0
```

**这个安装器**（只有 `deploy/npm/` 改了才需要）：

```bash
cd deploy/npm && npm version patch && npm publish --registry https://registry.npmjs.org
```
