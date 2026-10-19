#!/bin/bash
# 卸载 WireGuard 连接看门狗。以 root 运行。
#
#   sudo ./uninstall.sh <接口名> [Label]
#
# 例：
#   sudo ./uninstall.sh wg0
#   sudo ./uninstall.sh wg0 com.local.wireguard-watchdog

set -euo pipefail

IF="${1:-}"
DAEMON_LABEL="${2:-com.local.wireguard-watchdog}"
DAEMON_PLIST="/Library/LaunchDaemons/${DAEMON_LABEL}.plist"

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行：sudo $0 <接口名> [Label]" >&2
    exit 1
fi
if [ -z "$IF" ]; then
    echo "用法: sudo $0 <接口名> [Label]" >&2
    exit 1
fi

PREFIX=""
for d in /opt/homebrew /usr/local; do
    if [ -x "$d/bin/wg-quick" ]; then PREFIX="$d"; break; fi
done

echo "==> 停止并删除 Daemon"
launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
rm -f "$DAEMON_PLIST"

echo "==> 移除脚本"
rm -rf /usr/local/libexec/wireguard-watchdog

echo "==> 断开隧道"
if [ -n "$PREFIX" ]; then
    "$PREFIX/bin/wg-quick" down "$IF" 2>/dev/null || true
fi

echo "==> 清理运行状态"
rm -f "/var/run/wireguard-watchdog/status.${IF}" "/var/run/wireguard-watchdog/fails.${IF}"

echo
if [ -n "$PREFIX" ]; then
    echo "完成。配置文件 ${PREFIX}/etc/wireguard/${IF}.conf 保留未删（需要的话手工删）。"
else
    echo "完成。（未找到 wg-quick，配置文件请手工检查）"
fi
