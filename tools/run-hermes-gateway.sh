#!/usr/bin/env bash
# Thin compatibility wrapper delegating to `musebot serve`.
#
#   uv run musebot serve
#   bash tools/run-hermes-gateway.sh
#
# Run `python tools/setup.py` first if .venv or web/dist has not been initialized yet.
set -euo pipefail

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage: bash tools/run-hermes-gateway.sh [serve-args...]

Thin wrapper delegating to `musebot serve` in the foreground (Ctrl+C to stop).
Preferred cross-platform command: `uv run musebot serve`.

Environment:
  MUSE_HOME        Data directory (default: ./muse-home; honors legacy ./hermes-home)
  COMPANION_HOST   Listen address (default: 127.0.0.1; set 0.0.0.0 for LAN phones)
  COMPANION_PORT   Listen port (default: 8766)
EOF
  exit 0
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONIOENCODING=utf-8
export NO_PROXY="${NO_PROXY:-127.0.0.1,localhost}"

if [ -x "$ROOT/.venv/bin/musebot" ]; then
  exec "$ROOT/.venv/bin/musebot" serve "$@"
elif [ -x "$ROOT/.venv/Scripts/musebot.exe" ]; then
  exec "$ROOT/.venv/Scripts/musebot.exe" serve "$@"
elif command -v uv >/dev/null 2>&1; then
  cd "$ROOT"
  exec uv run musebot serve "$@"
else
  echo "No .venv or uv found: run python tools/setup.py first" >&2
  exit 1
fi
