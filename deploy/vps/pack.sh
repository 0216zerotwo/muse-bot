#!/usr/bin/env bash
# 在这台电脑上（Git Bash / Linux / macOS）打一个服务器安装包，上传到 VPS 后运行里面的 install.sh。
#
#   bash deploy/vps/pack.sh               # 全新安装：模型和连接在 App 里重新设置
#   bash deploy/vps/pack.sh --with-data   # 连同现在的数据一起搬过去（见下）
#
# --with-data 会带上：配置（自动转换为服务器路径与浏览器参数）、.env 里的 API 密钥、记忆、SOUL、技能、
# 对话记录、定时任务、伙伴的状态和创作、密码保管箱。不带：日志、缓存、Windows 原生动态库、浏览器登录数据、
# 已配对的设备（服务器上重新配对）。包里有密钥，只上传到你自己的服务器，用完删掉本地这份。
#
# 输出：deploy/dist/musebot-server-<版本>.tar.gz（版本取 MUSEBOT_VERSION，比如发布流程里的 v1.2.0；没有就用日期）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WITH_DATA=0

usage() {
  cat <<'EOF'
用法：bash deploy/vps/pack.sh [--with-data]

选项：
  --with-data   将当前 HERMES_HOME 中的记忆、对话、配置与 API 密钥一并打入私有迁移包（权限 0600）
  -h, --help    显示本帮助并退出
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --with-data)
      WITH_DATA=1
      shift
      ;;
    *)
      echo "未知参数：$1（查看帮助：bash deploy/vps/pack.sh --help）" >&2
      exit 1
      ;;
  esac
done

PY="${MUSEBOT_PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "$ROOT/hermes-env/bin/python" "$ROOT/hermes-env/Scripts/python.exe" "$(command -v python3 2>/dev/null || true)" "$(command -v python 2>/dev/null || true)"; do
    [ -n "$cand" ] || continue
    if "$cand" -I -c "import sqlite3" >/dev/null 2>&1; then
      PY="$cand"
      if "$cand" -I -c "
try:
    import ruamel.yaml
except ImportError:
    import yaml
" >/dev/null 2>&1; then
        break
      fi
    fi
  done
fi
WEB="${MUSEBOT_WEB_DIST:-$ROOT/web/dist}"
HERMES_SRC="${MUSEBOT_HERMES_SRC:-$ROOT/hermes}"

[ -f "$HERMES_SRC/pyproject.toml" ] || { echo "先下载 Hermes：bash tools/fetch-hermes.sh" >&2; exit 1; }
[ -f "$WEB/companion.html" ] || { echo "先构建网页：cd web && npm ci && npm run build" >&2; exit 1; }

if [ "$WITH_DATA" -eq 1 ]; then
  umask 077
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
OUT_DIR="${MUSEBOT_OUT_DIR:-$ROOT/deploy/dist}"
mkdir -p "$OUT_DIR"
RELEASE="${MUSEBOT_VERSION:-dev-$(date +%Y%m%d-%H%M)}"
NAME="musebot-server-$RELEASE"
B="$STAGE/$NAME"
mkdir -p "$B/tools" "$B/hermes"

TAR_OWNER=()
if tar --version 2>/dev/null | grep -qi "gnu"; then
  TAR_OWNER=(--owner=0 --group=0 --force-local)
fi

echo "==> Hermes 源码"
tar -C "$HERMES_SRC" -cf - \
  --exclude='.git' --exclude='node_modules' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='*.egg-info' --exclude='.venv' --exclude='venv' --exclude='Lib' --exclude='Scripts' \
  --exclude='Include' --exclude='pyvenv.cfg' --exclude='activate*' --exclude='hermes_constants_scratch*.py' \
  --exclude='apps' --exclude='website' --exclude='tests' \
  . | tar -C "$B/hermes" -xf -

echo "==> 伙伴插件和网页"
tar -C "$ROOT" -cf - --exclude='__pycache__' --exclude='*.pyc' muse companion | tar -C "$B" -xf -
mkdir -p "$B/web" && cp -r "$WEB/." "$B/web/"
cp "$ROOT/tools/companion-pair.py" "$B/tools/"
printf '%s\n' "$RELEASE" > "$B/VERSION"
cp "$ROOT/deploy/vps/install.sh" "$ROOT/deploy/vps/uninstall.sh" "$ROOT/deploy/vps/README.md" "$ROOT/deploy/vps/Dockerfile" "$B/"
[ -f "$ROOT/LICENSE" ] && cp "$ROOT/LICENSE" "$B/LICENSE"
[ -f "$ROOT/NOTICE" ] && cp "$ROOT/NOTICE" "$B/NOTICE"

if [ "$WITH_DATA" -eq 1 ]; then
  [ -n "$PY" ] || { echo "找不到 Python 环境：请先运行 bash tools/setup.sh" >&2; exit 1; }
  H="${HERMES_HOME:-$ROOT/hermes-home}"
  [ -d "$H" ] || { echo "找不到数据目录：$H（可通过 HERMES_HOME 指定）" >&2; exit 1; }
  echo "==> 现在的数据（含 API 密钥）"
  D="$B/data"
  mkdir -p "$D"
  chmod 700 "$D"
  for item in .env SOUL.md auth.json memories skills sessions cron/jobs.json channel_directory.json projects.db vault; do
    [ -e "$H/$item" ] || continue
    mkdir -p "$D/$(dirname "$item")"
    cp -r "$H/$item" "$D/$item"
  done
  # 伙伴的数据：不带浏览器登录数据（cookie）、Playwright 缓存、本机原生动态库（stt/pylib）、临时上传缓存和这台电脑配对过的设备
  if [ -d "$H/companion" ]; then
    tar -C "$H" -cf - \
      --exclude='companion/mcp' \
      --exclude='companion/devices.json' \
      --exclude='companion/stt/pylib' \
      --exclude='companion/attachments' \
      --exclude='companion/tmp' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      companion | tar -C "$D" -xf -
  fi
  # 对话数据库：用 SQLite 的备份接口，网关运行中也能拿到一致的一份
  if [ -f "$H/state.db" ]; then
    "$PY" -I - "$H/state.db" "$D/state.db" <<'PYEOF'
import sqlite3, sys
from pathlib import Path
src_uri = Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
src = sqlite3.connect(src_uri, uri=True)
dst = sqlite3.connect(str(Path(sys.argv[2]).resolve()))
src.backup(dst)
dst.close()
src.close()
PYEOF
  fi
  # 配置：去掉只属于这台电脑的部分（Windows 路径、本机代理的假 IP 段、桌面浏览器参数）
  if [ -f "$H/config.yaml" ]; then
    "$PY" -I - "$H/config.yaml" "$D/config.yaml" <<'PYEOF'
import re, sys

def load_yaml(path):
    try:
        from ruamel.yaml import YAML
        y = YAML(typ="safe")
        with open(path, encoding="utf-8") as fh:
            return y.load(fh) or {}
    except ImportError:
        import yaml
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

def dump_yaml(data, fh):
    try:
        from ruamel.yaml import YAML
        y = YAML()
        y.default_flow_style = False
        y.allow_unicode = True
        y.sort_base_mapping_type_on_output = False
        y.dump(data, fh)
    except ImportError:
        import yaml
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)

cfg = load_yaml(sys.argv[1])
if not isinstance(cfg, dict):
    cfg = {}

security = cfg.get("security")
if isinstance(security, dict):
    security.pop("fake_ip_ranges", None)
    if not security:
        cfg.pop("security", None)

servers = cfg.get("mcp_servers")
if not isinstance(servers, dict):
    servers = {}
    cfg["mcp_servers"] = servers

for name, srv in list(servers.items()):
    if not isinstance(srv, dict):
        continue
    cwd = srv.get("cwd")
    if isinstance(cwd, str) and (re.match(r"^[A-Za-z]:[/\\]", cwd) or "companion/mcp/" in cwd.replace("\\", "/") or name == "browser"):
        srv["cwd"] = f"/opt/data/companion/mcp/{name}"
    if name == "browser":
        cmd = srv.get("command")
        rewrite_to_builtin = cmd in ("npx", "npx.cmd", "playwright-mcp", None)
        if rewrite_to_builtin:
            srv["command"] = "playwright-mcp"
        raw_args = list(srv.get("args") or [])
        cleaned, skip = [], False
        for item in raw_args:
            s = str(item)
            if skip:
                skip = False
                continue
            if s == "--browser":
                skip = True
                continue
            if s.startswith("--browser="):
                continue
            if rewrite_to_builtin and (s in ("-y", "--yes") or s == "@playwright/mcp" or s.startswith("@playwright/mcp@")):
                continue
            cleaned.append(s)
        if "--cdp-endpoint" not in cleaned and not any(a.startswith("--cdp-endpoint=") for a in cleaned):
            cleaned.extend(["--cdp-endpoint", "http://127.0.0.1:9333"])
        if "--no-webmcp" not in cleaned:
            cleaned.append("--no-webmcp")
        srv["args"] = cleaned

if "browser" not in servers:
    servers["browser"] = {
        "command": "playwright-mcp",
        "args": ["--no-webmcp", "--cdp-endpoint", "http://127.0.0.1:9333"],
        "timeout": 120,
        "connect_timeout": 120,
        "cwd": "/opt/data/companion/mcp/browser",
    }

with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write("# 从电脑搬过来的配置（pack.sh --with-data）\n")
    dump_yaml(cfg, f)
PYEOF
  fi
fi

ARCHIVE="$OUT_DIR/$NAME.tar.gz"
rm -f "$ARCHIVE"
tar -C "$STAGE" "${TAR_OWNER[@]}" -czf "$ARCHIVE" "$NAME"
if [ "$WITH_DATA" -eq 1 ]; then
  chmod 600 "$ARCHIVE"
fi
SIZE=$(du -h "$ARCHIVE" | cut -f1)
echo
echo "打好了：$ARCHIVE（$SIZE）"
echo "上传并安装："
echo "  scp $ARCHIVE root@你的服务器:/root/"
echo "  ssh root@你的服务器 'tar xzf $NAME.tar.gz && bash $NAME/install.sh 你的域名'"
[ "$WITH_DATA" -eq 1 ] && echo "（包里有 API 密钥：装好后删掉本地和服务器上的这个压缩包）"
exit 0
