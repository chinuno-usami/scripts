# wireguard-watchdog (macOS)

一个用 `wg-quick` 管隧道、按**握手时间**判断健康、挂了就自动重建的 WireGuard 看门狗。

## 为什么需要它

macOS 上常见的 WireGuard 用法是 App Store 版 App（NetworkExtension 隧道）。
它有两个覆盖不到的场景：

| 场景 | App 版 | App 版开 On-Demand | 本方案 |
|---|---|---|---|
| 对端掉线（IP 不变） | 自动恢复（协议自身重试握手） | 同左 | 自动恢复 + 主动重建 |
| **对端公网 IP 变更** | ❌ 不会恢复 | ❌ 不会恢复 | ✅ `wg-quick up` 重新解析域名 |
| 本机切网/休眠唤醒 | 自动恢复 | 自动恢复 | 自动恢复 + 主动重建 |

原因有两个：

1. **On-Demand 不是健康检查。** 它只是 `NEOnDemandRuleConnect`，在「网络路径变化 /
   VPN 处于未连接状态」时决定要不要连。当隧道名义上还连着（utun 还在、provider 还活着）
   但对端已经不可达时，系统认为它是 connected，规则根本不会被重新评估。
2. **macOS 版 WireGuard 不会重新解析 Endpoint 域名。** 解析只发生在隧道启动那一刻；
   之后网络变化时它只调用 `wgBumpSockets()`（重绑 UDP socket），
   不像 iOS 分支会 `wgSetConfig()` 重新下发解析结果。所以对端换 IP 后，
   客户端会一直往旧 IP 发，直到隧道被重启。

本方案用 `wg-quick` 重建隧道，每次 `up` 都会重新解析域名，因此能覆盖上面第 2 行。

## 文件

| 文件 | 作用 |
|---|---|
| `check.sh` | 看门狗本体，由 LaunchDaemon 以 root 调用 |
| `com.local.wireguard-watchdog.plist` | LaunchDaemon 模板（`__IF__` / `__LABEL__` 由安装脚本替换） |
| `install.sh` | 安装脚本（需要 root） |
| `uninstall.sh` | 卸载脚本（需要 root） |

## 依赖

```bash
brew install wireguard-tools     # 会自动带上 wireguard-go 和 bash
```

## 安装

```bash
sudo ./install.sh <配置文件>.conf [选项]
```

选项：

| 选项 | 说明 |
|---|---|
| `--if <name>` | 接口名 / 配置文件名（默认取 `.conf` 前的部分） |
| `--label <name>` | LaunchDaemon 标签（默认 `com.local.wireguard-watchdog`） |
| `--drop-dns` | 注释掉配置里的 `DNS =` 行，不让 wg-quick 改系统 DNS |
| `--no-keepalive` | 不自动补 `PersistentKeepalive` |

安装脚本会：

1. 定位 Homebrew 前缀（`/opt/homebrew` 或 `/usr/local`）并校验 `wg` / `wg-quick`
2. 把配置装到 `<prefix>/etc/wireguard/<if>.conf`（`root:wheel`，`600`）
3. 缺 `PersistentKeepalive` 就补 `= 25`；把 CRLF 转成 LF；可选注释掉 `DNS`
4. 试跑一次 `wg-quick up`，失败就中止（不会留下坏配置）
5. 装脚本到 `/usr/local/libexec/wireguard-watchdog/check.sh`
6. 装并加载 `/Library/LaunchDaemons/<label>.plist`

## ⚠️ 两个必须知道的坑

### 1. `wg show <配置名>` 是无效的

wg-quick 的「配置名」和「真实设备名」不是一回事：

```
/var/run/wireguard/wg0.name    -> 内容 "utun6"
/var/run/wireguard/utun6.sock  -> 真正的控制 socket

$ wg show wg0
Unable to access interface: No such file or directory   # 找不到 wg0.sock
$ wg show utun6
<正常输出>
```

所以排查问题时用 `wg show utun6`（或 `wg show` 不带参数）。
本项目两个脚本内部都正确处理了这层映射（读 `.name` 文件解析真实设备名）。

### 2. `DNS =` 会改系统 DNS

如果配置里的 `DNS =` 指向的是**局域网 DNS**（不在 `AllowedIPs` 里），
wg-quick 会通过 `networksetup` 把它设成系统 DNS。换到别的网络时这个地址不可达，
会直接断掉全部域名解析。

分片路由（`AllowedIPs` 里没有 `0.0.0.0/0`）的场景建议装的时候带 `--drop-dns`：

```bash
sudo ./install.sh wg0.conf --drop-dns
```

## 安装后

```bash
launchctl print system/com.local.wireguard-watchdog | grep -E 'state|runs|last exit'
cat /var/run/wireguard-watchdog/status.wg0
tail -f /var/log/wireguard-watchdog.log
```

`status.<if>` 里 `handshake_age` 会一直小于阈值，日志文件在正常情况下保持安静
（只在异常、恢复、重建时写）。

## 判断逻辑

```
每 60s：
  接口不存在                  -> 立即 wg-quick down/up 重建
  最近握手 > MAX_AGE          -> 先 ping 隧道内地址 poke 一次，等 GRACE 秒再看
    仍然 > MAX_AGE            -> 可疑计数 +1
      计数 >= FAILS_TO_RESTART -> 重建，计数归零
    恢复了                     -> 计数归零
```

- 用 `wg ... latest-handshakes` 而不是 ping，是因为握手时间才是隧道真实的存活证据。
- `PersistentKeepalive` 保证空闲隧道也有心跳，握手时间才有意义。
- 「先 poke 再判定」是为了避免休眠唤醒 / 长时间无流量造成的假阳性。

## 参数

LaunchDaemon plist 的 `EnvironmentVariables`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WG_WATCHDOG_IF` | 必填 | 接口名 / 配置文件名 |
| `WG_WATCHDOG_MAX_AGE` | `180` | 握手超时阈值（秒） |
| `WG_WATCHDOG_FAILS` | `2` | 连续多少次可疑才重建 |
| `WG_WATCHDOG_GRACE` | `5` | poke 后等待重新握手的秒数 |
| `WG_WATCHDOG_PROBE` | 自动推导 | 隧道内探测地址 |
| `WG_WATCHDOG_PREFIX` | 自动探测 | Homebrew 前缀 |
| `WG_WATCHDOG_LOG` | `/var/log/wireguard-watchdog.log` | 日志路径（超过 1MB 自动轮转） |

改完重新加载：

```bash
sudo launchctl bootout system/com.local.wireguard-watchdog
sudo launchctl bootstrap system /Library/LaunchDaemons/com.local.wireguard-watchdog.plist
```

### 探测地址推导顺序

1. `AllowedIPs` 里第一个非 `0.0.0.0/0` 的 IPv4 段（`/32` 用原值，其余取网络号 +1）
2. `DNS` 里的第一个 IPv4
3. 若有默认路由，退化为 `192.0.2.1`（RFC 5737 文档地址，只会进隧道，不会真打到别人）

不要求探测地址真的应答 —— 只要有出向流量被路由进隧道，WireGuard 就会发起握手。

## 卸载

```bash
sudo ./uninstall.sh <接口名> [Label]
```

会停掉并删除 Daemon、删掉脚本、断开隧道、清掉运行状态。配置文件保留。

## 设计取舍

- **用 LaunchDaemon（system 域）而不是 LaunchAgent（`~/Library/LaunchAgents`）**：
  `wg-quick` 需要 root 来建 utun、改路由、改 DNS。放用户域就得额外配
  `sudoers` NOPASSWD，白白多一个提权面。代价是隧道变成系统级、未登录也在跑。
- **`wg-quick up` 放后台执行**：wg-quick 在 launchd 环境下会前台等待它的 route monitor，
  所以脚本里用后台子 shell 启动 + 轮询接口是否就绪，不会被它阻塞。
- **`wg-quick` 的 DNS 还原**靠它自己的 route monitor 在接口消失时触发。
  极端情况下（强杀进程）可能残留，届时手工 `sudo networksetup -setdnsservers "Wi-Fi" Empty`。

## 发布前请确认

**绝对不要把这个目录里的 WireGuard 配置文件（`.conf`）和导出包提交上去 —— 里面有私钥。**
`.gitignore` 已经忽略了 `*.conf` / `*.zip`，但最好把配置放在仓库之外。
