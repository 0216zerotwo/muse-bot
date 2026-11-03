#!/usr/bin/env bash
# 在这台电脑上（Git Bash）打一个服务器安装包，上传到 VPS 后运行里面的 install.sh。
#
#   bash deploy/vps/pack.sh               # 全新安装：模型和连接在 App 里重新设置
#   bash deploy/vps/pack.sh --with-data   # 连同现在的数据一起搬过去（见下）
#
# --with-data 会带上：配置（服务器用的版本）、.env 里的 API 密钥、记忆、SOUL、技能、对话记录、
# 定时任务、伙伴的状态和创作、密码保管箱。不带：日志、缓存、浏览器登录数据、已配对的设备
# （服务器上重新配对）。包里有密钥，只上传到你自己的服务器，用完删掉本地这份。
#
# 输出：deploy/dist/musebot-server-<日期>.tar.gz
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WITH_DATA=0
[ "${1:-}" = "--with-data" ] && WITH_DATA=1
PY="$ROOT/hermes-env/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
WEB="$ROOT/web/dist"
[ -f "$WEB/companion.html" ] || { echo "先构建网页：cd web && npm install && npm run build" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
OUT_DIR="$ROOT/deploy/dist"
mkdir -p "$OUT_DIR"
NAME="musebot-server-$(date +%Y%m%d-%H%M)"
B="$STAGE/$NAME"
mkdir -p "$B/tools"

echo "==> Hermes 源码"
tar -C "$ROOT" -cf - \
  --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.egg-info' --exclude='.venv' --exclude='venv' --exclude='Lib' --exclude='Scripts' \
  --exclude='Include' --exclude='pyvenv.cfg' --exclude='activate*' --exclude='hermes_constants_scratch*.py' \
  hermes | tar -C "$B" -xf -

echo "==> 伙伴插件和网页"
tar -C "$ROOT" -cf - --exclude='__pycache__' --exclude='*.pyc' companion | tar -C "$B" -xf -
mkdir -p "$B/web" && cp -r "$WEB/." "$B/web/"
cp "$ROOT/tools/companion-pair.py" "$B/tools/"
cp "$ROOT/deploy/vps/install.sh" "$ROOT/deploy/vps/uninstall.sh" "$ROOT/deploy/vps/README.md" "$ROOT/deploy/vps/Dockerfile" "$B/"

if [ "$WITH_DATA" -eq 1 ]; then
  echo "==> 现在的数据（含 API 密钥）"
  H="$ROOT/hermes-home"
  D="$B/data"
  mkdir -p "$D"
  for item in .env SOUL.md auth.json memories skills sessions cron/jobs.json channel_directory.json projects.db vault; do
    [ -e "$H/$item" ] || continue
    mkdir -p "$D/$(dirname "$item")"
    cp -r "$H/$item" "$D/$item"
  done
  # 伙伴的数据：不带浏览器登录数据（cookie）、Playwright 的缓存和这台电脑配对过的设备
  tar -C "$H" -cf - --exclude='companion/mcp' --exclude='companion/devices.json' companion | tar -C "$D" -xf -
  # 对话数据库：用 SQLite 的备份接口，网关运行中也能拿到一致的一份
  "$PY" -I - "$H/state.db" "$D/state.db" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PYEOF
  # 配置：去掉只属于这台电脑的部分（Windows 路径、本机代理的假 IP 段、自定义网页目录）
  "$PY" -I - "$H/config.yaml" "$D/config.yaml" <<'PYEOF'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
security = cfg.get("security") or {}
security.pop("fake_ip_ranges", None)
if not security:
    cfg.pop("security", None)
browser = (cfg.get("mcp_servers") or {}).get("browser")
if isinstance(browser, dict):
    browser["cwd"] = "/opt/data/companion/mcp/browser"
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write("# 从电脑搬过来的配置（pack.sh --with-data）\n")
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
PYEOF
fi

tar -C "$STAGE" -czf "$OUT_DIR/$NAME.tar.gz" "$NAME"
SIZE=$(du -h "$OUT_DIR/$NAME.tar.gz" | cut -f1)
echo
echo "打好了：deploy/dist/$NAME.tar.gz（$SIZE）"
echo "上传并安装："
echo "  scp deploy/dist/$NAME.tar.gz root@你的服务器:/root/"
echo "  ssh root@你的服务器 'tar xzf $NAME.tar.gz && bash $NAME/install.sh 你的域名'"
[ "$WITH_DATA" -eq 1 ] && echo "（包里有 API 密钥：装好后删掉本地和服务器上的这个压缩包）"
exit 0
