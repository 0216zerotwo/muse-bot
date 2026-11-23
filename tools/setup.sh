#!/usr/bin/env bash
# Set up a Muse Bot server on this machine, from a fresh clone. Safe to run again (it keeps your data).
#
#   bash tools/setup.sh
#
# Needs: Git Bash on Windows (Hermes runs its tools through bash), uv (https://docs.astral.sh/uv),
# Node.js 18+ and npm. Downloads Hermes Agent, its Python environment (Python 3.14 via uv) and the
# web app's packages. Your data (keys, chats, memory) goes to ./hermes-home, which Git ignores;
# point HERMES_HOME somewhere else to keep it elsewhere.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash tools/setup.sh

Downloads Hermes Agent at its pinned version, prepares the Python 3.14 environment (hermes-env
and Hermes's managed runtime), links ./companion into $HERMES_HOME/plugins/companion, creates
a starter config.yaml if absent, and builds the web app (web/dist).

Environment:
  HERMES_HOME   Data directory for keys, chats, and memory (default: ./hermes-home)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '\033[1;31m[stop]\033[0m Unknown option: %s (see: bash tools/setup.sh --help)\n' "$1" >&2
      exit 1
      ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_HOME="${HERMES_HOME:-$ROOT/hermes-home}"
EXTRAS=(--extra mcp --extra web --extra ddgs --extra doc-extract --extra computer-use --extra anthropic)

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[stop]\033[0m %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null || die "uv is missing: https://docs.astral.sh/uv/getting-started/installation/"
command -v npm >/dev/null || die "Node.js / npm is missing: https://nodejs.org (18 or later)"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) WINDOWS=1 ;; *) WINDOWS=0 ;; esac

# 1. Hermes Agent, at the version Muse Bot is tested with.
bash "$ROOT/tools/fetch-hermes.sh"

# 2. Its Python environment, with the extras Muse Bot uses (MCP connections, web tools, search,
#    document extraction, computer use, Anthropic SDK) and the plugin's Web Push dependencies.
say "Python environment (hermes-env)"
(cd "$ROOT/hermes" && UV_PROJECT_ENVIRONMENT="$ROOT/hermes-env" uv sync --frozen --no-dev --python 3.14 "${EXTRAS[@]}")
PY="$ROOT/hermes-env/bin/python"
[ -x "$PY" ] || PY="$ROOT/hermes-env/Scripts/python.exe"
[ -x "$PY" ] || die "Python interpreter not found in $ROOT/hermes-env"
uv pip install --python "$PY" py-vapid==1.9.6 http-ece==1.2.1

# 3. The data folder: the plugin linked in (repairing dangling links if moved), a starting config (never overwritten).
say "Data folder: $HERMES_HOME"
mkdir -p "$HERMES_HOME/plugins"
LINK="$HERMES_HOME/plugins/companion"
if [ -L "$LINK" ] && [ ! -e "$LINK" ]; then
  rm -f "$LINK"
fi
if [ ! -e "$LINK" ]; then
  if [ "$WINDOWS" -eq 1 ]; then
    SRC_WIN="$(cygpath -w "$ROOT/companion")"
    DST_WIN="$(cygpath -w "$LINK")"
    "$PY" -I -c "import _winapi, os, sys; src, dst = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2]); os.path.lexists(dst) or _winapi.CreateJunction(src, dst)" "$SRC_WIN" "$DST_WIN"
  else
    ln -sfn "$ROOT/companion" "$LINK"
  fi
fi
[ -f "$HERMES_HOME/config.yaml" ] || cp "$ROOT/tools/hermes-config.example.yaml" "$HERMES_HOME/config.yaml"

# 4. Hermes keeps the environment its gateway runs from itself: install the same extras and the
#    plugin's dependencies there too.
say "Hermes's managed environment"
(cd "$ROOT/hermes" && "$PY" hermes pm install venv "${EXTRAS[@]}")
(cd "$ROOT/hermes" && "$PY" - "$LINK" <<'PYEOF'
import sys
from pathlib import Path

sys.path.insert(0, ".")
from pm.client import sync_venv
from pm.plugin_inputs import Candidates

sync_venv(
    ["anthropic", "computer-use", "ddgs", "doc-extract", "mcp", "web"],
    explicit=True,
    plugins=Candidates([Path(sys.argv[1])]),
)
PYEOF
)

# 5. The web app the phones and browsers show.
say "Web app (web/dist)"
(cd "$ROOT/web" && npm ci && npm run build)

cat <<TIP

Ready. Start the server:

  bash tools/run-hermes-gateway.sh

then open http://127.0.0.1:8766 on this computer (no pairing needed here), and add a model in
设置 → 模型. Phones: see README.md (pair with python tools/companion-pair.py <address>).
TIP
