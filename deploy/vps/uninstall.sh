#!/usr/bin/env bash
# 卸载伙伴服务器（install.sh 装的）。
#   bash uninstall.sh            # 停掉并删除容器、镜像和程序；保留 /opt/companion/data 与 Caddy 证书
#   bash uninstall.sh --purge    # 连数据一起删（记忆、对话、API 密钥、已配对的设备、证书）
# 不会卸载 Docker，也不会删交换文件 /swapfile-companion 和防火墙规则。
set -euo pipefail

APP="${MUSEBOT_APP_DIR:-/opt/companion}"
PURGE=0
ASSUME_YES="${ASSUME_YES:-0}"

usage() {
  cat <<'EOF'
用法：sudo bash uninstall.sh [选项]

选项：
  --purge       连同 /opt/companion 中的全部数据（记忆、对话、密钥、证书）一起删除
  -y, --yes     与 --purge 配合时跳过交互确认
  -h, --help    显示本帮助并退出
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --purge)
      PURGE=1
      shift
      ;;
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    *)
      echo "未知选项：$1（查看帮助：bash uninstall.sh --help）" >&2
      exit 1
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行：sudo bash uninstall.sh" >&2; exit 1; }
[ -f "$APP/.installed" ] || { echo "$APP 不是 install.sh 装的（或已经卸载），已停止" >&2; exit 1; }

cd "$APP"
if [ -f "$APP/docker-compose.yml" ]; then
  docker compose down --rmi local || true
fi
docker rmi companion-hermes:prev >/dev/null 2>&1 || true

if [ "$PURGE" -eq 1 ]; then
  if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "确定删除 $APP 里的全部数据（记忆、对话、密钥、证书）？输入 delete 确认：" ok
    [ "$ok" = "delete" ] || { echo "已取消，数据保留"; exit 0; }
  fi
  cd / && rm -rf "$APP"
  echo "已删除程序、证书和全部数据"
else
  # 保留 $APP/data、$APP/caddy（避免重装时触发 Let's Encrypt 证书频率限制）和 $APP/domain
  rm -rf "$APP/hermes" "$APP/hermes.prev" "$APP/muse" "$APP/muse.prev" "$APP/tools"
  rm -f "$APP/.installed" "$APP/Dockerfile" "$APP/.dockerignore" "$APP/docker-compose.yml" "$APP/Caddyfile" "$APP/VERSION" "$APP/VERSION.prev"
  touch "$APP/.uninstalled"
  echo "已停掉并删除程序；数据保留在 $APP/data，HTTPS 证书保留在 $APP/caddy（再运行 install.sh 会接着用）"
fi
