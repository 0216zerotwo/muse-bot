# musebot-server

一条命令把 [Muse Bot](https://github.com/0216zerotwo/muse-bot) 服务器装到自己的 Ubuntu / Debian 服务器上：伙伴本身、无界面浏览器（Chromium + Playwright）、语音（ffmpeg）、推送、HTTPS 证书（Caddy 自动申请），装好就常驻，服务器重启后会自动起来。

这个 npm 包只是安装器（几 KB）。服务器程序从 GitHub 仓库的 [Releases](https://github.com/0216zerotwo/muse-bot/releases) 下载，所以服务器端的更新不需要重新发布 npm 包。

## 装

服务器：Ubuntu 22.04 / 24.04 或 Debian 12+，2 核 4G 内存，20G 硬盘，能访问 github.com，云服务商的安全组放行 TCP 80、443。有域名的话先把它解析到服务器 IP；没有也行，会用 `<IP>.sslip.io`。

```bash
sudo apt-get install -y nodejs npm        # 已经有 Node 就跳过
sudo npx -y musebot-server install 你的域名
```

脚本先列出要改动的东西，确认后才动手；第一次要几分钟（下载 Docker 镜像和浏览器）。最后打印**配对码**和**一键连接**链接，手机上填进去就连上了。

## 怎么常驻

- 伙伴和 Caddy 是两个 Docker 容器，`restart: unless-stopped`：进程崩了会自动重启。
- Docker 本身是 systemd 服务，安装时 `systemctl enable docker`：服务器重启后 Docker 起来，再把两个容器拉起来。
- 想确认：`sudo npx musebot-server status`，「开机自启」一行是「是」就对了。

## 日常

| 要做的事 | 命令 |
|---|---|
| 看版本和状态 | `sudo npx musebot-server status` |
| 看日志 | `sudo npx musebot-server logs -f` |
| 重启 / 停下 / 启动 | `sudo npx musebot-server restart`（`stop` / `start`） |
| 再配对一台手机 | `sudo npx musebot-server pair` |
| **升级到最新版** | `sudo npx -y musebot-server update`（数据保留） |
| 装指定版本 | `sudo npx -y musebot-server update --version v1.2.0` |
| 卸载 | `sudo npx musebot-server uninstall`，加 `--purge` 连数据一起删 |

## 把电脑上的数据搬过去

Releases 里的安装包是公开的，里面**只有程序，没有任何数据和密钥**。要连记忆、对话和 API 密钥一起搬，在电脑上打私有的安装包，传到服务器后用 `--bundle` 装：

```bash
bash deploy/vps/pack.sh --with-data                       # 电脑上（Git Bash），先构建网页：cd web && npm run build
scp deploy/dist/musebot-server-*.tar.gz root@服务器:/root/
sudo npx -y musebot-server install 你的域名 --bundle /root/musebot-server-dev-日期.tar.gz
```

装完删掉电脑和服务器上的这个压缩包（里面有密钥）。

## 发布新版本（维护者）

**服务器程序**（`companion/`、`web/`、`deploy/vps/` 改了）：打一个版本标签推上去，GitHub Actions 的 Release 流程会构建网页、下载固定版本的 Hermes、打包（检查没有数据和密钥），发布成 Release。服务器上 `update` 就能拿到。

```bash
git tag v1.2.0 && git push origin v1.2.0
```

**这个安装器**（只有 `deploy/npm/` 改了才需要）：

```bash
cd deploy/npm && npm version patch && npm publish --registry https://registry.npmjs.org
```
