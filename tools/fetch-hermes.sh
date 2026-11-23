#!/usr/bin/env bash
# Download Hermes Agent (the open-source engine Muse Bot runs on, MIT, by Nous Research) into ./hermes,
# at the version Muse Bot is built and tested against.
#
#   bash tools/fetch-hermes.sh [--force]
set -euo pipefail

COMMIT="8f5693af418256b89da17ddda18a9242f113dcf6"   # NousResearch/hermes-agent main, 2026-10-19
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${MUSEBOT_HERMES_TARGET:-$ROOT/hermes}"
URL="${MUSEBOT_HERMES_URL:-https://codeload.github.com/NousResearch/hermes-agent/tar.gz/$COMMIT}"
EXPECTED_SHA256="${HERMES_TARBALL_SHA256:-}"
FORCE=0

usage() {
  cat <<'EOF'
Usage: bash tools/fetch-hermes.sh [--force]

Downloads Hermes Agent at its pinned commit into ./hermes via an atomic staging directory
so an interrupted download never leaves a partial tree behind.

Options:
  --force       Re-download and replace ./hermes even if already present
  -h, --help    Show this help message and exit
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --force)
      FORCE=1
      shift
      ;;
    *)
      echo "Unknown option: $1 (see: bash tools/fetch-hermes.sh --help)" >&2
      exit 1
      ;;
  esac
done

tree_complete() {
  local dir="$1"
  [ -f "$dir/pyproject.toml" ] && [ -f "$dir/uv.lock" ] && [ -f "$dir/hermes" ] || return 1
  if [ -f "$dir/.musebot-hermes-commit" ]; then
    [ "$(tr -d '\r\n[:space:]' < "$dir/.musebot-hermes-commit")" = "$COMMIT" ] || return 1
  fi
  return 0
}

if [ "$FORCE" -eq 0 ] && tree_complete "$TARGET"; then
  echo "hermes/ is already ready at ${COMMIT:0:12} (pass --force to download again)"
  exit 0
fi

if [ -e "$TARGET" ] && [ "$FORCE" -eq 0 ]; then
  echo "==> Incomplete or outdated hermes/ detected; re-downloading cleanly"
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
ARCHIVE="$STAGE/hermes.tar.gz"
EXTRACTED="$STAGE/hermes"
mkdir -p "$EXTRACTED"

echo "==> Downloading Hermes Agent ${COMMIT:0:12}"
curl -fL --retry 3 -o "$ARCHIVE" "$URL"

if [ -n "$EXPECTED_SHA256" ]; then
  ACTUAL_SHA256=""
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(sha256sum "$ARCHIVE" | awk '{print tolower($1)}')"
  elif command -v shasum >/dev/null 2>&1; then
    ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print tolower($1)}')"
  elif command -v python3 >/dev/null 2>&1; then
    ACTUAL_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$ARCHIVE")"
  else
    echo "Cannot verify HERMES_TARBALL_SHA256: no sha256sum, shasum, or python3 available" >&2
    exit 1
  fi
  EXPECTED_LOWER="$(printf '%s' "$EXPECTED_SHA256" | tr 'A-Z' 'a-z')"
  if [ "$ACTUAL_SHA256" != "$EXPECTED_LOWER" ]; then
    echo "Hermes archive SHA-256 mismatch (expected $EXPECTED_LOWER, got $ACTUAL_SHA256)" >&2
    exit 1
  fi
fi

TAR_FLAGS=()
if tar --version 2>/dev/null | grep -qi "gnu"; then
  TAR_FLAGS=(--force-local --no-same-owner)
fi

tar "${TAR_FLAGS[@]}" -xzf "$ARCHIVE" -C "$EXTRACTED" --strip-components=1 \
  --exclude='*/website' --exclude='*/tests' \
  --exclude="hermes-agent-$COMMIT/apps"   # the desktop and other apps: the server doesn't use them

if [ ! -f "$EXTRACTED/pyproject.toml" ] || [ ! -f "$EXTRACTED/uv.lock" ] || [ ! -f "$EXTRACTED/hermes" ]; then
  echo "Downloaded Hermes archive is incomplete (missing pyproject.toml, uv.lock, or hermes launcher)" >&2
  exit 1
fi
printf '%s\n' "$COMMIT" > "$EXTRACTED/.musebot-hermes-commit"

mkdir -p "$(dirname "$TARGET")"
rm -rf "$TARGET"
mv "$EXTRACTED" "$TARGET"
echo "==> hermes/ ready (${COMMIT:0:12})"
