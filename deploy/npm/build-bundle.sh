#!/usr/bin/env bash
# 把服务器安装包放进这个 npm 包（bundle/musebot.tar.gz），之后 npm publish 就能
# 在服务器上 npx musebot install。在这台电脑上用 Git Bash 运行：
#
#   bash deploy/npm/build-bundle.sh
#
# 用的就是 deploy/vps/pack.sh（不带数据）：npm 上的包谁都能下载，所以绝不放记忆、对话和密钥；
# 搬数据用 pack.sh --with-data 打私有的包，再 npx musebot install --bundle 那个包。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
for arg in "$@"; do
  [ "$arg" = "--with-data" ] && { echo "npm 包是公开的，不能带数据" >&2; exit 1; }
done

before="$(ls -t "$ROOT"/deploy/dist/musebot-server-*.tar.gz 2>/dev/null | head -1 || true)"
bash "$ROOT/deploy/vps/pack.sh"
made="$(ls -t "$ROOT"/deploy/dist/musebot-server-*.tar.gz | head -1)"
[ -n "$made" ] && [ "$made" != "$before" ] || { echo "pack.sh 没有打出新的安装包" >&2; exit 1; }

mkdir -p "$HERE/bundle"
mv "$made" "$HERE/bundle/musebot.tar.gz"
node "$HERE/bin/musebot.js" check-bundle
echo "放好了：deploy/npm/bundle/musebot.tar.gz（$(du -h "$HERE/bundle/musebot.tar.gz" | cut -f1)）"
echo "发布：cd deploy/npm && npm version patch && npm publish"
