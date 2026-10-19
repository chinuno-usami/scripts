#!/bin/bash
# WireGuard 连接看门狗 (macOS / wg-quick)
#
# 由 LaunchDaemon 以 root 身份定期调用（见 com.local.wireguard-watchdog.plist）。
#
# 健康判据：wg 的 latest-handshake 时间。握手超时 -> 判定隧道实际已死，
# 然后 wg-quick down/up 重建；重建时会重新解析 Endpoint 域名，因此
# 「对端掉线」和「对端公网 IP 变更」两种 App 版客户端无法自愈的情况都能恢复。
#
# 依赖配置里的 PersistentKeepalive：空闲时也会刷新握手时间，否则长时间无流量会被误判。
#
# ⚠️ wg-quick 的「配置名」和「真实设备名」不是一回事：
#      /var/run/wireguard/<if>.name  内容 = "utunN"
#      /var/run/wireguard/utunN.sock 真正的控制 socket
#    所以必须 `wg show utunN`；`wg show <if>` 永远返回 "No such file or directory"。

set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

IF="${WG_WATCHDOG_IF:-}"
MAX_AGE="${WG_WATCHDOG_MAX_AGE:-180}"      # 最近握手超过多少秒算可疑
GRACE="${WG_WATCHDOG_GRACE:-5}"            # poke 之后等待重新握手的时间
FAILS_TO_RESTART="${WG_WATCHDOG_FAILS:-2}" # 连续多少次可疑才重启（防抖）
PROBE_OVERRIDE="${WG_WATCHDOG_PROBE:-}"    # 可选手工指定隧道内探测地址
LOG="${WG_WATCHDOG_LOG:-/var/log/wireguard-watchdog.log}"

if [ -z "$IF" ]; then
    echo "wireguard-watchdog: 必须设置 WG_WATCHDOG_IF（隧道配置名）" >&2
    exit 1
fi

# 定位 Homebrew 前缀下的 wg / wg-quick（Apple Silicon 与 Intel 都支持）
find_tool() {
    local d
    for d in "${WG_WATCHDOG_PREFIX:-}" /opt/homebrew /usr/local; do
        if [ -n "$d" ] && [ -x "$d/bin/$1" ]; then
            printf '%s\n' "$d/bin/$1"
            return 0
        fi
    done
    return 1
}

WG="$(find_tool wg || true)"
WGQUICK="$(find_tool wg-quick || true)"
PREFIX="$(dirname "$(dirname "${WG:-/opt/homebrew/bin/wg}")")"

CONF="$PREFIX/etc/wireguard/${IF}.conf"
NAMEFILE="/var/run/wireguard/${IF}.name"
STATE="/var/run/wireguard-watchdog"
FAILFILE="$STATE/fails.$IF"

mkdir -p "$STATE" 2>/dev/null

log() {
    printf '%s [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$IF" "$*" >>"$LOG" 2>/dev/null || true
}

rotate_log() {
    [ -f "$LOG" ] || return 0
    local size
    size=$(stat -f %z "$LOG" 2>/dev/null || echo 0)
    [ "$size" -gt 1048576 ] && mv -f "$LOG" "$LOG.1" 2>/dev/null
    return 0
}

# 配置名 -> 真实设备名（utunN）。失败返回非 0。
resolve_real_if() {
    local real
    [ -f "$NAMEFILE" ] || return 1
    real="$(cat "$NAMEFILE" 2>/dev/null || true)"
    [ -n "$real" ] || return 1
    [ -S "/var/run/wireguard/${real}.sock" ] || return 1
    printf '%s' "$real"
}

iface_up() {
    local real
    real="$(resolve_real_if)" || return 1
    [ -n "$WG" ] || return 1
    "$WG" show "$real" >/dev/null 2>&1
}

# 最近一次握手距今的秒数；无法判定时输出空
handshake_age() {
    local real hs now
    [ -n "$WG" ] || return 1
    real="$(resolve_real_if)" || return 1
    hs=$("$WG" show "$real" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -n | tail -1)
    case "$hs" in ''|*[!0-9]*) return 1 ;; esac
    if [ "$hs" -eq 0 ]; then printf '999999'; return 0; fi
    now=$(date +%s)
    printf '%s' "$(( now - hs ))"
}

# 猜一个「会被路由进隧道」的 IPv4 地址，用于主动触发一次握手。
# 不要求对方应答 —— 只要有出向流量，WireGuard 就会发起握手。
derive_probe() {
    [ -n "$PROBE_OVERRIDE" ] && { echo "$PROBE_OVERRIDE"; return 0; }
    [ -r "$CONF" ] || return 1

    local cidr
    # 1) 优先 AllowedIPs（它才决定哪些流量真的走隧道）
    while IFS= read -r cidr; do
        case "$cidr" in
            *:*) continue ;;                 # 跳过 IPv6
            0.0.0.0/0|0.0.0.0/1) continue ;; # 跳过 default，它们对推导没意义
        esac
        case "$cidr" in
            */32) printf '%s\n' "${cidr%/32}"; return 0 ;;
            */*)  printf '%s\n' "${cidr%%/*}" | awk -F. '{print $1"."$2"."$3".1"}'; return 0 ;;
            *)    printf '%s\n' "$cidr"; return 0 ;;
        esac
    done < <(sed -n 's/^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*//p' "$CONF" \
                 | tr ',' '\n' | tr -d ' ')

    # 2) 退一步用 DNS（通常也在隧道内）
    while IFS= read -r cidr; do
        case "$cidr" in
            *:*) continue ;;
        esac
        printf '%s\n' "$cidr"; return 0
    done < <(sed -n 's/^[[:space:]]*DNS[[:space:]]*=[[:space:]]*//p' "$CONF" \
                 | tr ',' '\n' | tr -d ' ' | grep -E '^[0-9]+(\.[0-9]+){3}$')

    # 3) 兜底：如果走默认路由，用一个文档地址（RFC5737），只会进隧道不会被真人收到
    if grep -qE '^[[:space:]]*AllowedIPs[[:space:]]*=[[:space:]]*0\.0\.0\.0/0' "$CONF"; then
        printf '192.0.2.1\n'; return 0
    fi
    return 1
}

poke() {
    local target
    target="$(derive_probe)" || return 1
    ping -c 1 -W 1000 -n "$target" >/dev/null 2>&1 || true
    return 0
}

restart() {
    local i
    [ -n "$WGQUICK" ] || { log "ERROR: wg-quick not found, cannot restart"; return 1; }
    log "restarting (wg-quick down/up $IF)"
    "$WGQUICK" down "$IF" >>"$LOG" 2>&1 || true

    for i in $(seq 1 20); do
        iface_up || break
        sleep 0.5
    done
    sleep 1

    # 放后台：wg-quick 在 launchd 环境下会前台等待它的 route monitor，不能同步等它返回
    ( "$WGQUICK" up "$IF" >>"$LOG" 2>&1 & )

    for i in $(seq 1 60); do
        if iface_up; then
            log "restart ok (real interface $(resolve_real_if))"
            return 0
        fi
        sleep 0.5
    done
    log "ERROR: $IF did not come up within 30s"
    return 1
}

write_status() {
    printf 'last_check=%s\nif=%s\nreal_if=%s\nhandshake_age=%s\nconsecutive_failures=%s\n' \
        "$(date '+%F %T')" "$IF" "$(resolve_real_if || echo -)" "${1:-unknown}" "${2:-0}" \
        >"$STATE/status.$IF"
}

# ---- main ----
rotate_log

if [ -z "$WG" ]; then
    log "ERROR: wg not found (brew install wireguard-tools)"
    write_status "wg-missing" 0
    exit 0
fi

if ! iface_up; then
    log "interface $IF not present (real device unresolved)"
    restart
    printf '0' >"$FAILFILE"
    write_status "interface-missing" 0
    exit 0
fi

age=$(handshake_age) || age=""
fails=$(cat "$FAILFILE" 2>/dev/null || echo 0)
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac

if [ -z "$age" ] || [ "$age" -gt "$MAX_AGE" ]; then
    # 先 poke 一次：如果是「休眠唤醒 / 空闲无流量」导致的假阳性，握手会立刻刷新
    poke && sleep "$GRACE"
    age=$(handshake_age) || age=""
fi

if [ -z "$age" ] || [ "$age" -gt "$MAX_AGE" ]; then
    fails=$(( fails + 1 ))
    printf '%s' "$fails" >"$FAILFILE"
    log "no recent handshake (age=${age:-unknown}s, limit=${MAX_AGE}s) consecutive_failures=$fails"
    if [ "$fails" -ge "$FAILS_TO_RESTART" ]; then
        restart
        printf '0' >"$FAILFILE"
        fails=0
    fi
else
    if [ "$fails" -ne 0 ]; then
        log "handshake recovered after poke (age=${age}s)"
        printf '0' >"$FAILFILE"
    fi
    fails=0
fi

write_status "${age:-unknown}" "$fails"
exit 0
