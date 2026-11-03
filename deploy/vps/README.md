# 把伙伴装到 VPS

一个脚本装好全部功能：伙伴本身、它的浏览器（无界面 Chromium）、浏览器自动化（Node + Playwright）、语音（ffmpeg）、推送通知、HTTPS 证书、开机自启。用的是 Hermes 官方 Docker 镜像加我们的伙伴插件，前面放 Caddy 自动申请证书。

## 选 VPS

| | 建议 |
|---|---|
| 地区 | **香港、新加坡、日本**等境外机房。大陆机房要做 ICP 备案才能开放 80/443，而且拉 Docker 镜像、npm 包经常失败 |
| 配置 | 2 核 4G 内存，磁盘 20G 以上（最低 2G 内存，脚本会自动加交换空间） |
| 系统 | Debian 12 或 Ubuntu 22.04 / 24.04 |
| 端口 | 在云服务商的「安全组 / 防火墙」里放行 TCP 80 和 443 |

域名可有可无：有的话先把它解析到 VPS 的 IP；没有的话脚本会用 `<IP>.sslip.io`，同样能拿到 HTTPS 证书（iPhone 的通知和语音都要求 HTTPS）。

## 最省事：一条 npx 命令

在服务器上直接（见 `deploy/npm/README.md`）：

```bash
sudo apt-get install -y nodejs npm
sudo npx -y musebot-server install 你的域名
```

效果和下面的三步一样（装的是 GitHub Releases 里的同一种安装包），只是不用在电脑上打包、上传；以后升级 `sudo npx -y musebot-server update`。要带数据搬家还是用下面的 `pack.sh --with-data`，再 `npx musebot-server install --bundle 那个包`。

## 三步装好

**1. 在这台电脑上打包**（Git Bash）：

```bash
bash deploy/vps/pack.sh --with-data
```

`--with-data` 会把现在的记忆、对话、配置和 API 密钥一起带过去；不加就是全新安装，模型在 App 的 **设置 → 模型** 里重新添加。打好的包在 `deploy/dist/`。

**2. 上传并安装**：

```bash
scp deploy/dist/musebot-server-*.tar.gz root@你的服务器IP:/root/
ssh root@你的服务器IP
tar xzf musebot-server-*.tar.gz && bash musebot-server-*/install.sh 你的域名
```

不写域名就自动用 sslip.io。第一次安装要几分钟（下载 Docker 镜像和浏览器）。

**3. 手机连上**：安装最后会打印**配对码**和**一键连接链接**。
- iPhone：Safari 打开 `https://你的域名` → 分享 → 添加到主屏幕 → 从主屏幕打开，输入配对码
- 安卓：App 第一次打开时，把一键连接链接粘贴进去

配对一次就固定下来，之后不用再连。

## 之后

| 要做的事 | 怎么做 |
|---|---|
| 再配对一台设备 | `python3 /opt/companion/tools/companion-pair.py https://你的域名`，或在已连上的设备里 **设置 → 设备 → 生成配对码** |
| 升级 | `sudo npx -y musebot-server update`（下载最新 Release）；或电脑上重新 `pack.sh`，上传后再运行一次 `install.sh`。数据都保留 |
| 看日志 | `docker logs -f companion` |
| 重启 | `cd /opt/companion && docker compose restart` |
| 改 API 密钥 | App 的 **设置 → 模型**；或编辑 `/opt/companion/data/.env` 后重启 |
| 卸载 | `bash /opt/companion/uninstall.sh`（保留数据）或加 `--purge`（全删） |

## 安装脚本会改动什么

- 没有 Docker 时用官方脚本 `get.docker.com` 安装
- 内存小于 3G 且没有交换空间时，新建 2G 交换文件 `/swapfile-companion`
- `ufw` 防火墙开着时放行 80、443
- 程序和数据都在 `/opt/companion`；两个容器 `companion`（伙伴）和 `companion-caddy`（HTTPS）

伙伴只监听服务器本机的 8766 端口，外面只能经过 Caddy 的 HTTPS 访问；经过 Caddy 来的请求一律要配对过的设备才能用。
