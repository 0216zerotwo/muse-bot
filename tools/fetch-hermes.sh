#!/usr/bin/env bash
# Download Hermes Agent (the open-source engine Muse Bot runs on, MIT, by Nous Research) into ./hermes,
# at the version Muse Bot is built and tested against.
#
#   bash tools/fetch-hermes.sh
set -euo pipefail

COMMIT="8f5693af418256b89da17ddda18a9242f113dcf6"   # NousResearch/hermes-agent main, 2026-10-19
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$ROOT/hermes"

if [ -f "$TARGET/pyproject.toml" ]; then
  echo "hermes/ is already there (delete it to download again)"
  exit 0
fi
mkdir -p "$TARGET"
echo "==> Downloading Hermes Agent ${COMMIT:0:12}"
curl -fL --retry 3 "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/$COMMIT" \
  | tar -xz -C "$TARGET" --strip-components=1 --exclude='*/website' --exclude='*/tests' \
      --exclude="hermes-agent-$COMMIT/apps"   # the desktop and other apps: the server doesn't use them
echo "==> hermes/ ready"
