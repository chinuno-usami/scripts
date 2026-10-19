#!/bin/bash
# 安装 WireGuard 连接看门狗（wg-quick 版）。
# 必须以 root 运行（需要写 /opt|/usr/local、/Library/LaunchDaemons）。
#
#   sudo ./install.sh <配置文件名.conf> [选项]
#
# 选项：
#   --if <name>        接口名 / 配置文件名（默认取 .conf 前的部分）
#   --label <name>     LaunchDaemon 标签（默认 com.local.wireguard-watchdog）
#   --drop-dns         注释掉配置里的 DNS 行，不让 wg-quick 改系统 DNS
#   --no-keepalive     不自动补 PersistentKeepalive
#
# 例：
#   sudo ./install.sh wg0.conf
#   sudo ./install.sh /path/to/some.conf --if wg0 --drop-dns

set -euo pipefail

IF=""
ADD_KEEPALIVE=1
DROP_DNS=0
DAEMON_LABEL=com.local.wireguard-watchdog
CONF_SRC=""

while [ $# -gt 0 ]; do
    case "$1" in
        --if) IF="${2:-}"; shift 2 ;;
        --label) DAEMON_LABEL="${2:-}"; shift 2 ;;
        --no-keepalive) ADD_KEEPALIVE=0; shift ;;
        --drop-dns) DROP_DNS=1; shift ;;
        -*) echo "未知参数: $1" >&2; exit 1 ;;
        *) CONF_SRC="$1"; shift ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 sudo 运行：sudo $0 <配置文件名.conf>" >&2
    exit 1
fi

[ -n "$CONF_SRC" ] || {
    echo "用法: sudo $0 <配置文件名.conf> [--if name] [--label name] [--drop-dns] [--no-keepalive]" >&2
    exit 1
}
[ -f "$CONF_SRC" ] || { echo "找不到配置文件: $CONF_SRC" >&2; exit 1; }

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
LIBEXEC=/usr/local/libexec/wireguard-watchdog
DAEMON_PLIST="/Library/LaunchDaemons/${DAEMON_LABEL}.plist"

echo "==> 1/5 检查依赖"
WG=""; WGQUICK=""; PREFIX=""
for d in /opt/homebrew /usr/local; do
    if [ -x "$d/bin/wg" ] && [ -x "$d/bin/wg-quick" ]; then
        WG="$d/bin/wg"; WGQUICK="$d/bin/wg-quick"; PREFIX="$d"; break
    fi
done
if [ -z "$WG" ]; then
    cat >&2 <<'EOF'
缺少 wireguard-tools。请先在普通终端执行：

    brew install wireguard-tools

（会自动带上 wireguard-go 和 bash）
EOF
    exit 1
fi
CONF_DIR="$PREFIX/etc/wireguard"
echo "    Homebrew 前缀: $PREFIX"
"$WG" --version || true

if [ -z "$IF" ]; then
    IF="$(basename "$CONF_SRC")"
    IF="${IF%.conf}"
fi
CONF="$CONF_DIR/${IF}.conf"

echo "==> 2/5 安装配置 $CONF"
mkdir -p "$CONF_DIR"
install -m 600 -o root -g wheel "$CONF_SRC" "$CONF"

if [ "$ADD_KEEPALIVE" -eq 1 ] && ! grep -qiE '^[[:space:]]*PersistentKeepalive' "$CONF"; then
    echo "    [注意] 配置里没有 PersistentKeepalive，补成 25（看门狗靠握手时间判断健康，空闲时也必须有心跳）"
    echo "PersistentKeepalive = 25" >> "$CONF"
fi

if LC_ALL=C grep -q $'\r' "$CONF"; then
    echo "    [注意] 配置含 CRLF，已转换为 LF"
    tr -d '\r' < "$CONF" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
fi

if [ "$DROP_DNS" -eq 1 ] && grep -qiE '^[[:space:]]*DNS[[:space:]]*=' "$CONF"; then
    echo "    [注意] 按 --drop-dns 注释掉 DNS 行，wg-quick 不会改系统 DNS"
    sed -i '' -E 's/^([[:space:]]*)(DNS[[:space:]]*=)/\1#\2/I' "$CONF"
fi

chmod 600 "$CONF"; chown root:wheel "$CONF"

echo "==> 3/5 试运行一次 wg-quick up（验证配置可用）"
"$WGQUICK" down "$IF" >/dev/null 2>&1 || true
sleep 1
"$WGQUICK" up "$IF" >/dev/null 2>&1 &

# 注意：不能判断 `wg show $IF` —— wg-quick 的配置名不是设备名。
# /var/run/wireguard/<name>.name 里存的是真实设备名（utunN），socket 也是 utunN.sock。
REAL_IF=""
for _ in $(seq 1 80); do
    if [ -f "/var/run/wireguard/${IF}.name" ]; then
        REAL_IF="$(cat "/var/run/wireguard/${IF}.name" 2>/dev/null || true)"
        if [ -n "$REAL_IF" ] && [ -S "/var/run/wireguard/${REAL_IF}.sock" ]; then
            break
        fi
        REAL_IF=""
    fi
    sleep 0.5
done

if [ -n "$REAL_IF" ] && "$WG" show "$REAL_IF" >/dev/null 2>&1; then
    echo "    OK：${IF} 已建立，真实设备 = ${REAL_IF}"
    "$WG" show "$REAL_IF"
else
    echo "    [错误] ${IF} 没能起来，先手工排查： sudo $WGQUICK up $IF" >&2
    echo "    （排查时注意：wg show 要用真实设备名，例如 wg show utun6；wg show ${IF} 永远会失败）" >&2
    exit 1
fi

echo "==> 4/5 安装看门狗脚本到 ${LIBEXEC}"
mkdir -p "$LIBEXEC"
install -m 755 -o root -g wheel "$SRC_DIR/check.sh" "$LIBEXEC/check.sh"

echo "==> 5/5 安装并启动 LaunchDaemon"
sed -e "s/__IF__/${IF}/g" -e "s/__LABEL__/${DAEMON_LABEL}/g" \
    "$SRC_DIR/com.local.wireguard-watchdog.plist" > "$DAEMON_PLIST"
chmod 644 "$DAEMON_PLIST"; chown root:wheel "$DAEMON_PLIST"
plutil -lint "$DAEMON_PLIST" >/dev/null

launchctl bootout "system/${DAEMON_LABEL}" 2>/dev/null || true
launchctl bootstrap system "$DAEMON_PLIST"
launchctl enable "system/${DAEMON_LABEL}"

echo
echo "完成。状态速查："
echo "  launchctl print system/${DAEMON_LABEL} | grep -E 'state|runs|last exit'"
echo "  cat /var/run/wireguard-watchdog/status.${IF}"
echo "  tail -f /var/log/wireguard-watchdog.log"
echo
echo "注意：如果 WireGuard App / 系统设置 > VPN 里有同名隧道，请断开它并关掉 On-Demand，"
echo "      否则 App 和 wg-quick 会抢同一个隧道。"
