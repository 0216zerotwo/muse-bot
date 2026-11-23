#!/usr/bin/env bash
# Run the Muse Bot server (the Hermes gateway with the companion plugin) in the foreground.
# Set it up first with tools/setup.sh. Stop it with Ctrl+C.
#
#   bash tools/run-hermes-gateway.sh
#
# HERMES_HOME (your data: keys, chats, memory) defaults to ./hermes-home in this repository.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: bash tools/run-hermes-gateway.sh [gateway-args...]

Runs the Muse Bot server (Hermes gateway + companion plugin) in the foreground.
Run `bash tools/setup.sh` first if ./hermes or ./hermes-env has not been initialized yet.

Environment:
  HERMES_HOME      Data directory (default: ./hermes-home)
  COMPANION_HOST   Listen address (default: 127.0.0.1; set 0.0.0.0 for LAN phones)
  COMPANION_PORT   Listen port (default: 8766)
EOF
  exit 0
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_HOME="${HERMES_HOME:-$ROOT/hermes-home}"
export PYTHONIOENCODING=utf-8
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"
PY="$ROOT/hermes-env/bin/python"
[ -x "$PY" ] || PY="$ROOT/hermes-env/Scripts/python.exe"
[ -x "$PY" ] || { echo "No Python environment yet: run bash tools/setup.sh first" >&2; exit 1; }
[ -f "$ROOT/hermes/hermes" ] || { echo "Hermes is not downloaded yet: run bash tools/setup.sh first" >&2; exit 1; }
cd "$ROOT/hermes"
exec "$PY" hermes gateway run "$@"
