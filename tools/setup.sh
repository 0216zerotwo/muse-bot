#!/usr/bin/env bash
# Thin compatibility wrapper delegating to cross-platform `tools/setup.py` + root `pyproject.toml`.
#
#   bash tools/setup.sh
#
# Safe to run again (keeps your existing data in $MUSE_HOME / $HERMES_HOME).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash tools/setup.sh [options]

Thin wrapper delegating to `python tools/setup.py` using the root `pyproject.toml`
and `uv.lock` (no Hermes checkout or Git Bash required on Windows).

Options:
  --skip-web          Skip building web/dist
  --python <version>  Python version passed to uv sync (default: 3.14)
  --home <path>       Override data directory (default: $MUSE_HOME or ./muse-home)
  -h, --help          Show this help message

Environment:
  MUSE_HOME     Data directory for keys, chats, and memory (default: ./muse-home)
  HERMES_HOME   Legacy fallback data directory (honored in-place if present)
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if command -v python3 >/dev/null 2>&1; then
  exec python3 "$ROOT/tools/setup.py" "$@"
elif command -v python >/dev/null 2>&1; then
  exec python "$ROOT/tools/setup.py" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run --no-project python "$ROOT/tools/setup.py" "$@"
else
  printf '\033[1;31m[stop]\033[0m Neither python3, python, nor uv was found in PATH.\n' >&2
  exit 1
fi
