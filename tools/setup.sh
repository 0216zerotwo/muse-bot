#!/usr/bin/env bash
# Set up a Muse Bot server on this machine, from a fresh clone. Safe to run again (it keeps your data).
#
#   bash tools/setup.sh
#
# Needs: git bash on Windows (Hermes runs its tools through bash), uv (https://docs.astral.sh/uv),
# Node.js 18+ and npm. Downloads Hermes Agent, its Python environment (Python 3.14 via uv) and the
# web app's packages. Your data (keys, chats, memory) goes to ./hermes-home, which Git ignores;
# point HERMES_HOME somewhere else to keep it elsewhere.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_HOME="${HERMES_HOME:-$ROOT/hermes-home}"
EXTRAS=(--extra mcp --extra web --extra ddgs)

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[stop]\033[0m %s\n' "$*" >&2; exit 1; }

command -v uv >/dev/null || die "uv is missing: https://docs.astral.sh/uv/getting-started/installation/"
command -v npm >/dev/null || die "Node.js / npm is missing: https://nodejs.org (18 or later)"
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) WINDOWS=1 ;; *) WINDOWS=0 ;; esac

# 1. Hermes Agent, at the version Muse Bot is tested with.
bash "$ROOT/tools/fetch-hermes.sh"

# 2. Its Python environment, with the extras Muse Bot uses (MCP connections, web tools, search)
#    and the plugin's own dependencies (Web Push).
say "Python environment (hermes-env)"
(cd "$ROOT/hermes" && UV_PROJECT_ENVIRONMENT="$ROOT/hermes-env" uv sync --frozen --no-dev --python 3.14 "${EXTRAS[@]}")
PY="$ROOT/hermes-env/bin/python"
[ -x "$PY" ] || PY="$ROOT/hermes-env/Scripts/python.exe"
uv pip install --python "$PY" py-vapid==1.9.6 http-ece==1.2.1

# 3. The data folder: the plugin linked in, a starting config (never overwritten).
say "Data folder: $HERMES_HOME"
mkdir -p "$HERMES_HOME/plugins"
LINK="$HERMES_HOME/plugins/companion"
if [ ! -e "$LINK" ]; then
  if [ "$WINDOWS" -eq 1 ]; then
    cmd //c mklink /J "$(cygpath -w "$LINK")" "$(cygpath -w "$ROOT/companion")" >/dev/null
  else
    ln -s "$ROOT/companion" "$LINK"
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

sync_venv(["ddgs", "mcp", "web"], explicit=True, plugins=Candidates([Path(sys.argv[1])]))
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
