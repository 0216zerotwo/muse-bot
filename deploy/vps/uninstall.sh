#!/usr/bin/env bash
# 卸载伙伴服务器（install.sh 装的）。
#   bash uninstall.sh            # 停掉并删除容器和镜像，数据保留在 /opt/companion/data
#   bash uninstall.sh --purge    # 连数据一起删（记忆、对话、API 密钥、已配对的设备）
# 不会卸载 Docker，也不会删交换文件 /swapfile-companion 和防火墙规则。
set -euo pipefail
APP=/opt/companion
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行" >&2; exit 1; }
[ -f "$APP/.installed" ] || { echo "$APP 不是 install.sh 装的，已停止" >&2; exit 1; }

cd "$APP"
docker compose down --rmi local || true
if [ "${1:-}" = "--purge" ]; then
  read -r -p "确定删除 $APP 里的全部数据（记忆、对话、密钥）？输入 delete 确认：" ok
  [ "$ok" = "delete" ] || { echo "已取消，数据保留"; exit 0; }
  cd / && rm -rf "$APP"
  echo "已删除程序和数据"
else
  rm -rf "$APP/hermes" "$APP/caddy"
  echo "已停掉并删除程序；数据还在 $APP/data（再运行 install.sh 会接着用）"
fi
