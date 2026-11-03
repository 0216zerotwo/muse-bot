#!/usr/bin/env bash
# 伙伴服务器：一键安装 / 升级（Debian 12+ / Ubuntu 22.04+，用 root 运行）
#
#   bash install.sh bot.example.com     # 用你的域名（先把域名解析到这台服务器）
#   bash install.sh                     # 没有域名：自动用 <服务器IP>.sslip.io
#
# 装好后，所有功能都在服务器上：伙伴（Hermes + 伙伴插件，在 Docker 里）、它的浏览器（Chromium，无界面）、
# 浏览器自动化（Node + Playwright）、语音（ffmpeg）、HTTPS（Caddy 自动申请证书）、开机自启。
#
# 这台服务器上会改动的：
#   - 没有 Docker 时安装 Docker（官方脚本 get.docker.com）
#   - 内存小于 3G 且没有交换空间时，新建 2G 交换文件 /swapfile-companion
#   - ufw 防火墙开着时放行 80、443 端口
#   - 新建 /opt/companion（程序、数据、配置都在这里），两个容器 companion 和 companion-caddy
# 再次运行就是升级：数据（记忆、对话、配置、已配对的设备）保留，只替换程序。
# 跳过确认：ASSUME_YES=1
set -euo pipefail

APP=/opt/companion
DATA=$APP/data
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT=8766
DOMAIN="${1:-${DOMAIN:-}}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[停止]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------- 检查（都通过才开始改动） ----------
[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"
[ -d "$HERE/hermes" ] && [ -d "$HERE/companion" ] && [ -f "$HERE/web/companion.html" ] \
  || die "安装包不完整：在解压出来的目录里运行 install.sh"
. /etc/os-release
case " ${ID:-} ${ID_LIKE:-} " in *" debian "*|*" ubuntu "*) ;; *) die "只支持 Debian / Ubuntu（这台是 ${PRETTY_NAME:-未知}）" ;; esac
case "$(uname -m)" in x86_64|aarch64) ;; *) die "不支持的 CPU 架构：$(uname -m)" ;; esac
command -v systemctl >/dev/null || die "这台服务器没有 systemd"

UPGRADE=0
[ -f "$APP/.installed" ] && UPGRADE=1
if [ "$UPGRADE" -eq 0 ] && [ -e "$APP" ]; then
  die "$APP 已存在，但不是本脚本建的。为避免覆盖，已停止"
fi
if [ "$UPGRADE" -eq 0 ] && command -v ss >/dev/null; then
  for p in 80 443 $PORT 9333; do
    ss -ltn "( sport = :$p )" | grep -q LISTEN && die "端口 $p 已被占用（伙伴需要 80、443、$PORT、9333）"
  done
fi

if [ -z "$DOMAIN" ] && [ -f "$APP/domain" ]; then
  DOMAIN="$(cat "$APP/domain")"
fi
if [ -z "$DOMAIN" ]; then
  IP="$(curl -4 -fsS --max-time 10 https://api.ipify.org || true)"
  [ -n "$IP" ] || die "查不到这台服务器的公网 IP。请直接给域名：bash install.sh 你的域名"
  DOMAIN="${IP//./-}.sslip.io"
  warn "没给域名，用 $DOMAIN（sslip.io 会把它解析到 $IP），同样能申请 HTTPS 证书"
fi
case "$DOMAIN" in http*|*/*) die "域名只写主机名，比如 bot.example.com" ;; esac

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)
DISK_GB=$(df -BG --output=avail / | tail -1 | tr -dc 0-9)
[ "$MEM_MB" -lt 1800 ] && warn "内存只有 ${MEM_MB}MB，建议至少 2G（推荐 4G），浏览器很吃内存"
[ "$DISK_GB" -lt 8 ] && die "磁盘剩余 ${DISK_GB}G，至少需要 8G（镜像里有浏览器和语音组件）"

echo
say "将在这台服务器上$( [ "$UPGRADE" -eq 1 ] && echo "升级" || echo "安装" )伙伴："
echo "    访问地址    https://$DOMAIN"
echo "    程序和数据  $APP"
command -v docker >/dev/null || echo "    安装 Docker（官方脚本）"
[ "$MEM_MB" -lt 3000 ] && [ "$SWAP_MB" -eq 0 ] && echo "    新建 2G 交换文件 /swapfile-companion"
command -v ufw >/dev/null && ufw status | grep -q "Status: active" && echo "    防火墙 ufw 放行 80、443"
[ -d "$HERE/data" ] && [ ! -f "$DATA/config.yaml" ] && echo "    导入安装包里带的数据（记忆、对话、配置、API 密钥）"
if [ "${ASSUME_YES:-0}" != "1" ]; then
  read -r -p "继续吗？[y/N] " ok
  case "$ok" in y|Y|yes) ;; *) die "已取消，没有做任何改动" ;; esac
fi

# ---------- 系统准备 ----------
if ! command -v docker >/dev/null; then
  say "安装 Docker"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null
docker compose version >/dev/null 2>&1 || die "Docker 缺少 compose 插件（apt install docker-compose-plugin）"
command -v python3 >/dev/null || { apt-get update -qq && apt-get install -y -qq python3; }

if [ "$MEM_MB" -lt 3000 ] && [ "$SWAP_MB" -eq 0 ] && [ ! -f /swapfile-companion ]; then
  say "新建 2G 交换文件"
  fallocate -l 2G /swapfile-companion && chmod 600 /swapfile-companion
  mkswap /swapfile-companion >/dev/null && swapon /swapfile-companion
  grep -q swapfile-companion /etc/fstab || echo "/swapfile-companion none swap sw 0 0" >> /etc/fstab
fi
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
fi

# ---------- 程序 ----------
say "放置程序（数据目录不动）"
mkdir -p "$APP" "$DATA/plugins"
rm -rf "$APP/hermes.new" && cp -r "$HERE/hermes" "$APP/hermes.new" && rm -rf "$APP/hermes" && mv "$APP/hermes.new" "$APP/hermes"
rm -rf "$DATA/plugins/companion" && cp -r "$HERE/companion" "$DATA/plugins/companion"
rm -rf "$DATA/companion-web" && cp -r "$HERE/web" "$DATA/companion-web"
mkdir -p "$APP/tools" && cp "$HERE/tools/companion-pair.py" "$APP/tools/"
cp "$HERE/install.sh" "$HERE/uninstall.sh" "$HERE/README.md" "$APP/"
[ -f "$HERE/VERSION" ] && cp "$HERE/VERSION" "$APP/VERSION"
echo "$DOMAIN" > "$APP/domain"

if [ ! -f "$DATA/config.yaml" ]; then
  if [ -d "$HERE/data" ]; then
    say "导入数据"
    cp -r "$HERE/data/." "$DATA/"
  else
    say "写初始配置（模型在 App 的 设置 → 模型 里添加）"
    cat > "$DATA/config.yaml" <<'YAML'
# 伙伴服务器（install.sh 写的初始配置）。API 密钥放在同目录的 .env，不写在这里。
plugins:
  enabled:
    - companion-platform
  disabled: []
streaming:
  enabled: true
  cursor: ""
display:
  language: zh
agent:
  max_turns: 20
timezone: Asia/Shanghai
cron:
  wrap_response: false
mcp_servers:
  browser:
    command: npx
    args: [-y, "@playwright/mcp@0.0.83", --no-webmcp, --cdp-endpoint, "http://127.0.0.1:9333"]
    timeout: 120
    connect_timeout: 120
    cwd: /opt/data/companion/mcp/browser
YAML
  fi
fi
mkdir -p "$DATA/companion/mcp/browser"
chown -R 10000:10000 "$DATA"  # the container runs as uid 10000 (see Dockerfile)

cp "$HERE/Dockerfile" "$APP/Dockerfile"
printf 'data\ncaddy\n' > "$APP/.dockerignore"

cat > "$APP/Caddyfile" <<CADDY
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
CADDY

cat > "$APP/docker-compose.yml" <<COMPOSE
# install.sh 生成；改了要重新运行 install.sh 才会保留
services:
  companion:
    build: .
    image: companion-hermes
    container_name: companion
    restart: unless-stopped
    network_mode: host
    init: true
    shm_size: "1gb"
    volumes:
      - ./data:/opt/data
    environment:
      - COMPANION_HOST=127.0.0.1
      - COMPANION_PORT=$PORT
      - COMPANION_WEB_DIST=/opt/data/companion-web
      - COMPANION_PUBLIC_URL=https://$DOMAIN
      - NO_PROXY=127.0.0.1,localhost
    command: ["gateway", "run"]
  caddy:
    image: caddy:2
    container_name: companion-caddy
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy:/data
COMPOSE

# ---------- 构建并启动 ----------
say "构建镜像（第一次要几分钟，会下载浏览器、Python 和语音组件）"
cd "$APP"
docker compose build
say "启动"
docker compose up -d
touch "$APP/.installed"

say "等伙伴启动"
for i in $(seq 1 90); do
  curl -fsS -o /dev/null "http://127.0.0.1:$PORT/webui/bootstrap" && break
  [ "$i" -eq 90 ] && die "伙伴没有启动起来。看日志：docker logs companion --tail 80"
  sleep 2
done
say "等 HTTPS 证书"
for i in $(seq 1 30); do
  # Through Caddy a request is not local, so an unpaired one gets 401: HTTPS works.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/webui/bootstrap" || true)
  [ "$code" = "401" ] && break
  [ "$i" -eq 30 ] && warn "https://$DOMAIN 还打不开：确认域名已解析到这台服务器，云服务商的安全组放行了 80 和 443。证书申请好后会自动生效"
  sleep 4
done

echo
say "装好了：https://$DOMAIN"
python3 "$APP/tools/companion-pair.py" "https://$DOMAIN" "$PORT" || true
cat <<TIP

手机上：
  iPhone  用 Safari 打开上面的地址 → 分享 → 添加到主屏幕 → 从主屏幕打开，输入配对码
  安卓    App 第一次打开时把「一键连接」链接粘贴进去
再配对一台设备：python3 $APP/tools/companion-pair.py https://$DOMAIN
（或在已连上的设备里：设置 → 设备 → 生成配对码）
常用：docker logs -f companion（看日志） · cd $APP && docker compose restart（重启）
升级：用新的安装包再运行一次 install.sh，数据都会保留
TIP
