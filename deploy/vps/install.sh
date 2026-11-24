#!/usr/bin/env bash
# 伙伴服务器：一键安装 / 升级（Debian 12+ / Ubuntu 22.04+，用 root 运行）
#
#   bash install.sh bot.example.com     # 用你的域名（先把域名解析到这台服务器）
#   bash install.sh                     # 没有域名：自动用 <服务器IP>.sslip.io
#   bash install.sh --yes bot.example.com
#
# 装好后，所有功能都在服务器上：伙伴（Muse 原生运行时，在 Docker 里）、它的浏览器（Chromium，无界面）、
# 浏览器自动化（Node + Playwright）、语音转码（ffmpeg）、HTTPS（Caddy 自动申请证书）、开机自启。
#
# 这台服务器上会改动的：
#   - 没有 Docker 时安装 Docker（官方脚本 get.docker.com）
#   - 内存小于 3G 且没有交换空间时，新建 2G 交换文件 /swapfile-companion
#   - ufw 防火墙开着时放行 80/tcp、443/tcp、443/udp 端口
#   - 新建 /opt/companion（程序、数据、配置都在这里），两个容器 companion 和 companion-caddy
# 再次运行就是升级：先构建新镜像，再原子替换程序；若新版本启动失败会自动回滚，数据保留。
# 跳过确认：--yes / -y 或 ASSUME_YES=1
set -euo pipefail

APP="${MUSEBOT_APP_DIR:-/opt/companion}"
DATA="$APP/data"
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${COMPANION_PORT:-8766}"
OS_RELEASE_FILE="${MUSEBOT_OS_RELEASE:-/etc/os-release}"
HEALTH_ATTEMPTS="${MUSEBOT_HEALTH_ATTEMPTS:-90}"
HTTPS_ATTEMPTS="${MUSEBOT_HTTPS_ATTEMPTS:-30}"
[[ "$HEALTH_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || HEALTH_ATTEMPTS=90
[[ "$HTTPS_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] || HTTPS_ATTEMPTS=30
CLI_DOMAIN=""

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[停止]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
用法：sudo bash install.sh [选项] [域名]

参数：
  域名          访问伙伴的域名（例如 bot.example.com）；不填则使用已保存域名或 <IP-横杠>.sslip.io

选项：
  -y, --yes     跳过确认提示，直接安装或升级
  -h, --help    显示本帮助并退出
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    -*)
      die "未知选项：$1（查看帮助：bash install.sh --help）"
      ;;
    *)
      [ -z "$CLI_DOMAIN" ] || die "多余的参数：$1（查看帮助：bash install.sh --help）"
      CLI_DOMAIN="$1"
      shift
      ;;
  esac
done

DOMAIN="${CLI_DOMAIN:-${DOMAIN:-}}"

valid_ipv4() {
  local ip="$1"
  [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  local IFS=.
  # shellcheck disable=SC2206
  local octets=($ip)
  local o
  for o in "${octets[@]}"; do
    [ "$o" -le 255 ] 2>/dev/null || return 1
  done
  return 0
}

valid_domain() {
  local d="$1"
  [ "${#d}" -le 253 ] || return 1
  [[ "$d" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$ ]]
}

read_os_field() {
  local key="$1"
  awk -F= -v k="$key" '
    $1 == k {
      val = substr($0, index($0, "=") + 1)
      gsub(/^["'\'' ]+|["'\'' \r]+$/, "", val)
      print val
      exit
    }
  ' "$OS_RELEASE_FILE"
}

# ---------- 检查（都通过才开始改动） ----------
[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash install.sh"
[ -d "$HERE/muse" ] && [ -d "$HERE/companion" ] && [ -f "$HERE/pyproject.toml" ] && [ -f "$HERE/uv.lock" ] && [ -f "$HERE/web/companion.html" ] && [ -f "$HERE/Dockerfile" ] && [ -f "$HERE/tools/companion-pair.py" ] \
  || die "安装包不完整：请在解压出来的目录里运行 install.sh"
[ -r "$OS_RELEASE_FILE" ] || die "找不到系统版本文件：$OS_RELEASE_FILE"
ID="$(read_os_field ID)"
ID_LIKE="$(read_os_field ID_LIKE)"
PRETTY_NAME="$(read_os_field PRETTY_NAME)"
case " ${ID:-} ${ID_LIKE:-} " in *" debian "*|*" ubuntu "*) ;; *) die "只支持 Debian / Ubuntu（这台是 ${PRETTY_NAME:-未知}）" ;; esac
case "$(uname -m)" in x86_64|aarch64) ;; *) die "不支持的 CPU 架构：$(uname -m)" ;; esac
command -v systemctl >/dev/null || die "这台服务器没有 systemd"

UPGRADE=0
REINSTALL=0
[ -f "$APP/.installed" ] && UPGRADE=1
[ -f "$APP/.uninstalled" ] && REINSTALL=1
if [ "$UPGRADE" -eq 0 ] && [ "$REINSTALL" -eq 0 ] && [ -e "$APP" ]; then
  die "$APP 已存在，但不是本脚本建的。为避免覆盖，已停止"
fi
if [ "$UPGRADE" -eq 0 ] && command -v ss >/dev/null; then
  for p in 80 443 "$PORT" 9333; do
    ss -ltn "( sport = :$p )" | grep -q LISTEN && die "端口 $p 已被占用（伙伴需要 80、443、$PORT、9333）"
  done
fi

if [ -z "$DOMAIN" ] && [ -f "$APP/domain" ]; then
  DOMAIN="$(tr -d '\r\n[:space:]' < "$APP/domain")"
fi
if [ -z "$DOMAIN" ]; then
  IP=""
  for endpoint in "https://api.ipify.org" "https://icanhazip.com" "https://ifconfig.me/ip"; do
    candidate="$(curl -4 -fsS --max-time 8 "$endpoint" 2>/dev/null | tr -d '\r\n[:space:]' || true)"
    if valid_ipv4 "$candidate"; then
      IP="$candidate"
      break
    fi
  done
  [ -n "$IP" ] || die "查不到这台服务器的公网 IPv4。请直接给域名：bash install.sh 你的域名"
  DOMAIN="${IP//./-}.sslip.io"
  warn "没给域名，用 $DOMAIN（sslip.io 会把它解析到 $IP），同样能申请 HTTPS 证书"
fi

DOMAIN="${DOMAIN%.}"
DOMAIN="$(printf '%s' "$DOMAIN" | tr 'A-Z' 'a-z')"
case "$DOMAIN" in http*|*/*|*:*) die "域名只写主机名（不要带 https:// 或端口），比如 bot.example.com" ;; esac
valid_domain "$DOMAIN" || die "域名格式不正确：$DOMAIN（示例：bot.example.com）"

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
command -v ufw >/dev/null && ufw status | grep -q "Status: active" && echo "    防火墙 ufw 放行 80/tcp、443/tcp、443/udp"
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
  ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null
fi

# ---------- 先构建新镜像（升级时旧容器继续运行，构建失败不破坏现有服务） ----------
say "构建镜像（第一次要几分钟，会下载浏览器、Python 和语音组件）"
printf 'data\ncaddy\n' > "$HERE/.dockerignore"
docker build -t companion-hermes:next -f "$HERE/Dockerfile" "$HERE"

cleanup_backups() {
  rm -rf \
    "$APP/hermes.prev" \
    "$APP/muse.prev" \
    "$APP/companion.prev" \
    "$APP/pyproject.toml.prev" \
    "$APP/uv.lock.prev" \
    "$APP/Dockerfile.prev" \
    "$APP/docker-compose.yml.prev" \
    "$APP/VERSION.prev" \
    "$APP/LICENSE.prev" \
    "$APP/NOTICE.prev" \
    "$DATA/plugins/companion.prev" \
    "$DATA/companion-lib.prev" \
    "$DATA/companion-web.prev"
}

rollback_upgrade() {
  warn "新版本未能正常启动，正在回滚到上一版本..."
  if docker image inspect companion-hermes:prev >/dev/null 2>&1; then
    docker tag companion-hermes:prev companion-hermes:latest
  fi
  if [ -d "$APP/hermes.prev" ]; then
    rm -rf "$APP/hermes" && mv "$APP/hermes.prev" "$APP/hermes"
  fi
  if [ -d "$APP/muse.prev" ]; then
    rm -rf "$APP/muse" && mv "$APP/muse.prev" "$APP/muse"
  else
    rm -rf "$APP/muse"
  fi
  if [ -d "$APP/companion.prev" ]; then
    rm -rf "$APP/companion" && mv "$APP/companion.prev" "$APP/companion"
  else
    rm -rf "$APP/companion"
  fi
  if [ -f "$APP/pyproject.toml.prev" ]; then
    mv -f "$APP/pyproject.toml.prev" "$APP/pyproject.toml"
  else
    rm -f "$APP/pyproject.toml"
  fi
  if [ -f "$APP/uv.lock.prev" ]; then
    mv -f "$APP/uv.lock.prev" "$APP/uv.lock"
  else
    rm -f "$APP/uv.lock"
  fi
  if [ -f "$APP/Dockerfile.prev" ]; then
    mv -f "$APP/Dockerfile.prev" "$APP/Dockerfile"
  fi
  if [ -f "$APP/docker-compose.yml.prev" ]; then
    mv -f "$APP/docker-compose.yml.prev" "$APP/docker-compose.yml"
  fi
  if [ -f "$APP/VERSION.prev" ]; then
    mv -f "$APP/VERSION.prev" "$APP/VERSION"
  fi
  if [ -f "$APP/LICENSE.prev" ]; then
    mv -f "$APP/LICENSE.prev" "$APP/LICENSE"
  fi
  if [ -f "$APP/NOTICE.prev" ]; then
    mv -f "$APP/NOTICE.prev" "$APP/NOTICE"
  fi
  if [ -d "$DATA/plugins/companion.prev" ]; then
    mkdir -p "$DATA/plugins"
    rm -rf "$DATA/plugins/companion" && mv "$DATA/plugins/companion.prev" "$DATA/plugins/companion"
  fi
  if [ -d "$DATA/companion-lib.prev" ]; then
    rm -rf "$DATA/companion-lib" && mv "$DATA/companion-lib.prev" "$DATA/companion-lib"
  fi
  if [ -d "$DATA/companion-web.prev" ]; then
    rm -rf "$DATA/companion-web" && mv "$DATA/companion-web.prev" "$DATA/companion-web"
  fi
  chown -R 10000:10000 "$DATA"
  (cd "$APP" && docker compose up -d --no-build) || true
}

# ---------- 原子放置程序（数据目录保留） ----------
say "放置程序（数据目录不动）"
mkdir -p "$APP" "$DATA"
chmod 755 "$APP"
chmod 700 "$DATA"

if [ "$UPGRADE" -eq 1 ]; then
  cleanup_backups
  if docker image inspect companion-hermes:latest >/dev/null 2>&1; then
    docker tag companion-hermes:latest companion-hermes:prev || true
  fi
  if [ -d "$APP/hermes" ]; then mv "$APP/hermes" "$APP/hermes.prev"; fi
  if [ -d "$APP/muse" ]; then cp -a "$APP/muse" "$APP/muse.prev"; fi
  if [ -d "$APP/companion" ]; then cp -a "$APP/companion" "$APP/companion.prev"; fi
  if [ -f "$APP/pyproject.toml" ]; then cp -a "$APP/pyproject.toml" "$APP/pyproject.toml.prev"; fi
  if [ -f "$APP/uv.lock" ]; then cp -a "$APP/uv.lock" "$APP/uv.lock.prev"; fi
  if [ -f "$APP/Dockerfile" ]; then cp -a "$APP/Dockerfile" "$APP/Dockerfile.prev"; fi
  if [ -f "$APP/docker-compose.yml" ]; then cp -a "$APP/docker-compose.yml" "$APP/docker-compose.yml.prev"; fi
  if [ -f "$APP/VERSION" ]; then cp -a "$APP/VERSION" "$APP/VERSION.prev"; fi
  if [ -f "$APP/LICENSE" ]; then cp -a "$APP/LICENSE" "$APP/LICENSE.prev"; fi
  if [ -f "$APP/NOTICE" ]; then cp -a "$APP/NOTICE" "$APP/NOTICE.prev"; fi
  if [ -d "$DATA/plugins/companion" ]; then mv "$DATA/plugins/companion" "$DATA/plugins/companion.prev"; fi
  if [ -d "$DATA/companion-lib" ]; then mv "$DATA/companion-lib" "$DATA/companion-lib.prev"; fi
  if [ -d "$DATA/companion-web" ]; then cp -a "$DATA/companion-web" "$DATA/companion-web.prev"; fi
fi

docker tag companion-hermes:next companion-hermes:latest
docker rmi companion-hermes:next >/dev/null 2>&1 || true

rm -rf "$APP/muse.new" && cp -r "$HERE/muse" "$APP/muse.new" && rm -rf "$APP/muse" && mv "$APP/muse.new" "$APP/muse"
rm -rf "$APP/companion.new" && cp -r "$HERE/companion" "$APP/companion.new" && rm -rf "$APP/companion" && mv "$APP/companion.new" "$APP/companion"
cp "$HERE/pyproject.toml" "$APP/pyproject.toml.new" && mv -f "$APP/pyproject.toml.new" "$APP/pyproject.toml"
cp "$HERE/uv.lock" "$APP/uv.lock.new" && mv -f "$APP/uv.lock.new" "$APP/uv.lock"
rm -rf "$DATA/companion-web.new" && cp -r "$HERE/web" "$DATA/companion-web.new" && rm -rf "$DATA/companion-web" && mv "$DATA/companion-web.new" "$DATA/companion-web"
mkdir -p "$APP/tools" && cp "$HERE/tools/companion-pair.py" "$APP/tools/"
cp "$HERE/install.sh" "$HERE/uninstall.sh" "$HERE/README.md" "$HERE/Dockerfile" "$APP/"
if [ -f "$HERE/VERSION" ]; then cp "$HERE/VERSION" "$APP/VERSION"; fi
if [ -f "$HERE/LICENSE" ]; then cp "$HERE/LICENSE" "$APP/LICENSE"; fi
if [ -f "$HERE/NOTICE" ]; then cp "$HERE/NOTICE" "$APP/NOTICE"; fi
printf '%s\n' "$DOMAIN" > "$APP/domain"

if [ ! -f "$DATA/config.yaml" ]; then
  if [ -d "$HERE/data" ]; then
    say "导入数据"
    cp -a "$HERE/data/." "$DATA/"
  else
    say "写初始配置（模型在 App 的 设置 → 模型 里添加）"
    cat > "$DATA/config.yaml" <<'YAML'
# 伙伴服务器（install.sh 写的初始配置）。API 密钥放在同目录的 .env，不写在这里。
streaming:
  enabled: true
  cursor: ""
display:
  language: zh
agent:
  max_turns: 100
timezone: Asia/Shanghai
cron:
  wrap_response: false
mcp_servers:
  browser:
    command: playwright-mcp
    args: [--no-webmcp, --cdp-endpoint, "http://127.0.0.1:9333"]
    timeout: 120
    connect_timeout: 120
    cwd: /opt/data/companion/mcp/browser
YAML
  fi
fi

mkdir -p "$DATA/companion/mcp/browser"
chmod 700 "$DATA"
if [ -d "$DATA/vault" ]; then chmod 700 "$DATA/vault"; fi
for secret_file in \
  "$DATA/.env" \
  "$DATA/auth.json" \
  "$DATA/muse.db" \
  "$DATA/state.db" \
  "$DATA/projects.db" \
  "$DATA/config.yaml" \
  "$DATA/companion/devices.json" \
  "$DATA/companion/media.key" \
  "$DATA/companion/vapid_private.pem"; do
  if [ -f "$secret_file" ]; then chmod 600 "$secret_file"; fi
done
chown -R 10000:10000 "$DATA"  # the container runs as uid 10000 (see Dockerfile)

printf 'data\ncaddy\n' > "$APP/.dockerignore"

cat > "$APP/Caddyfile" <<CADDY
$DOMAIN {
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}
	request_body {
		max_size 25MB
	}
	reverse_proxy 127.0.0.1:$PORT {
		flush_interval -1
	}
}
CADDY

cat > "$APP/docker-compose.yml" <<COMPOSE
# install.sh 生成；改了要重新运行 install.sh 才会保留
services:
  companion:
    build: .
    image: companion-hermes:latest
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
    command: ["musebot", "serve"]
  caddy:
    image: caddy:2
    container_name: companion-caddy
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - ./caddy:/data
COMPOSE

# ---------- 启动并验证 ----------
say "启动"
cd "$APP"
if ! docker compose up -d --no-build; then
  docker logs companion --tail 80 >&2 || true
  if [ "$UPGRADE" -eq 1 ]; then
    rollback_upgrade
    die "新版本容器启动失败，已自动回滚到上一版本"
  fi
  die "伙伴容器启动失败。看日志：docker logs companion --tail 80"
fi
rm -f "$APP/.uninstalled"
touch "$APP/.installed"

say "等伙伴启动"
STARTED=0
for _ in $(seq 1 "$HEALTH_ATTEMPTS"); do
  boot_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/webui/bootstrap" || true)
  if [ "$boot_code" = "200" ] || [ "$boot_code" = "401" ]; then
    STARTED=1
    break
  fi
  sleep 2
done
if [ "$STARTED" -ne 1 ]; then
  docker logs companion --tail 80 >&2 || true
  if [ "$UPGRADE" -eq 1 ]; then
    rollback_upgrade
    die "新版本伙伴没有启动起来，已自动回滚到上一版本"
  fi
  die "伙伴没有启动起来。看日志：docker logs companion --tail 80"
fi
cleanup_backups
rm -rf "$DATA/companion-lib"
if [ -f "$DATA/muse.db" ]; then chmod 600 "$DATA/muse.db"; fi
for wal_file in "$DATA/muse.db-wal" "$DATA/muse.db-shm"; do
  if [ -f "$wal_file" ]; then chmod 600 "$wal_file"; fi
done

say "等 HTTPS 证书"
for i in $(seq 1 "$HTTPS_ATTEMPTS"); do
  # Through Caddy a request is not local, so an unpaired one gets 401 (or 200 if open): HTTPS works.
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$DOMAIN/webui/bootstrap" || true)
  if [ "$code" = "401" ] || [ "$code" = "200" ]; then
    break
  fi
  [ "$i" -eq "$HTTPS_ATTEMPTS" ] && warn "https://$DOMAIN 还打不开：确认域名已解析到这台服务器，云服务商的安全组放行了 80 和 443。证书申请好后会自动生效"
  sleep 4
done

echo
if [ "$UPGRADE" -eq 1 ]; then
  CURRENT_VER="$(tr -d '\r\n' < "$APP/VERSION" 2>/dev/null || echo "未知")"
  say "升级好了：https://$DOMAIN（版本：$CURRENT_VER，原有数据和已配对设备均保留）"
  printf '如需配对新设备：sudo npx musebot-server pair（或 python3 %s/tools/companion-pair.py https://%s %s）\n' "$APP" "$DOMAIN" "$PORT"
else
  say "装好了：https://$DOMAIN"
  python3 "$APP/tools/companion-pair.py" "https://$DOMAIN" "$PORT" || true
  printf '\n手机上：\n'
  printf '  iPhone  安装原生 App（或用 Safari 打开上面的地址 → 分享 → 添加到主屏幕），输入配对码\n'
  printf '  安卓    App 第一次打开时把「一键连接」链接粘贴进去\n'
  printf '再配对一台设备：python3 %s/tools/companion-pair.py https://%s %s\n' "$APP" "$DOMAIN" "$PORT"
  printf '（或在已连上的设备里：设置 → 设备 → 生成配对码）\n'
  printf '常用：docker logs -f companion（看日志） · cd %s && docker compose restart（重启）\n' "$APP"
  printf '升级：sudo npx -y musebot-server update（或用新安装包再运行 install.sh，数据都会保留）\n'
fi
