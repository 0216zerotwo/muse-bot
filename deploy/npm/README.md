# musebot

一条命令把伙伴服务器装到自己的 Ubuntu / Debian 服务器上：伙伴本身、无界面浏览器（Chromium + Playwright）、语音（ffmpeg）、推送、HTTPS 证书（Caddy 自动申请），装好就常驻，服务器重启后会自动起来。

## 装

服务器：Ubuntu 22.04 / 24.04 或 Debian 12+，2 核 4G 内存，20G 硬盘，云服务商的安全组放行 TCP 80、443。有域名的话先把它解析到服务器 IP；没有也行，会用 `<IP>.sslip.io`。

```bash
sudo apt-get install -y nodejs npm        # 已经有 Node 就跳过
sudo npx -y musebot install 你的域名
```

脚本先列出要改动的东西，确认后才动手；第一次要几分钟（下载 Docker 镜像和浏览器）。最后打印**配对码**和**一键连接**链接，手机上填进去就连上了。

## 怎么常驻

- 伙伴和 Caddy 是两个 Docker 容器，`restart: unless-stopped`：进程崩了会自动重启。
- Docker 本身是 systemd 服务，安装时 `systemctl enable docker`：服务器重启后 Docker 起来，再把两个容器拉起来。
- 想确认：`sudo npx musebot status`，「开机自启」一行是「是」就对了。

## 日常

| 要做的事 | 命令 |
|---|---|
| 看状态 | `sudo npx musebot status` |
| 看日志 | `sudo npx musebot logs -f` |
| 重启 / 停下 / 启动 | `sudo npx musebot restart`（`stop` / `start`） |
| 再配对一台手机 | `sudo npx musebot pair` |
| 升级 | `sudo npx -y musebot@latest install`（数据保留） |
| 卸载 | `sudo npx musebot uninstall`，加 `--purge` 连数据一起删 |

## 把电脑上的数据搬过去

npm 上的包是公开的，里面**只有程序，没有任何数据和密钥**。要连记忆、对话和 API 密钥一起搬，在电脑上打私有的安装包，传到服务器后用 `--bundle` 装：

```bash
bash deploy/vps/pack.sh --with-data                       # 电脑上（Git Bash）
scp deploy/dist/musebot-server-*.tar.gz root@服务器:/root/
sudo npx -y musebot install 你的域名 --bundle /root/musebot-server-日期.tar.gz
```

装完删掉电脑和服务器上的这个压缩包（里面有密钥）。

## 发布新版本（维护者）

```bash
bash deploy/npm/build-bundle.sh      # 重新打安装包，检查没有数据和密钥
cd deploy/npm && npm version patch && npm publish
```
