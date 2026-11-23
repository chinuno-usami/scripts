# wmcr_patch.py — Webcam Motion Receiver 离线补丁工具

把 `GameAssembly.dll` / `global-metadata.dat` 里对 `webcammotioncapture.info` 的
4 个 HTTP 请求断掉，让 App 纯本地跑。纯标准库，Python 3.7+，Windows/macOS/Linux 都能用。

## 快速开始

```bash
# 目录长这样（--src 指向游戏根目录）
#   Mocap/
#   ├── GameAssembly.dll
#   └── Webcam Motion Receiver_Data/il2cpp_data/Metadata/global-metadata.dat

python3 wmcr_patch.py check            # 看当前是原版/已打补丁
python3 wmcr_patch.py all --strict                  # 离线补丁（干净，但登录相关面板不出现）
python3 wmcr_patch.py all --strict --auto-login     # ★ 实测可用：开机即"已登录"，直接点 Try for Free
# 产物在 out_patched/，按提示覆盖回游戏目录（先备份）
```

只想要最小改动（不 nop `fi.Start` 里那两行死代码）：

```bash
python3 wmcr_patch.py all              # 不带 --strict
```

## 命令

| 命令 | 作用 |
|---|---|
| `info` | 列出补丁集（RVA / 字节 / 说明），`[always]` 与 `[strict]` 分组 |
| `check` | 报告 `--src` 下每个补丁点的状态：original / patched / unknown |
| `apply` | 生成打过补丁的副本到 `--out`（默认 `out_patched`），保持原目录结构 |
| `verify` | 校验 `--out` 产物是否为目标状态 |
| `revert` | 用 `--src` 的原始文件覆盖 `--out` 产物 |
| `selftest` | 内存里跑 apply→verify→幂等→revert，不落盘，用来确认工具自身正确 |
| `audit` | 反查 5 个 URL 访问器的 `call` 点，判定「仍可达 / 已中和」（可达则退出码 1） |
| `strings` | 解密 GUPS 字符串表，`--grep` 查残留 URL 字面量 |
| `diff A B` | 字节级差异清单 |
| `locate` | **版本更新后跑这个**：自动重定位 metadata + 列出 DLL 候选；加 `--write-table` 直接推导出新补丁表 |
| `--table FILE` | 全局参数：加载 `locate --write-table` 生成的表，替代内置 `DEFAULT_DLL_PATCHES` |
| `all` | `apply` + `verify` + `audit` |

`apply` / `verify` / `all` 支持 `--meta-only`：只处理 `global-metadata.dat`，完全不依赖 RVA。

`apply --auto-login`（★ 推荐）：把 `fi.Update` 里的登录门控
（`cmpb $0,0x5b8; je ...`）换成 `movb $1,0x5b8` + 6×nop ——
**每帧强制"已登录"**，不需要点 Login。实测：启动后直接点 "Try for Free" 即可使用 VMC。

`apply --fake-login=mark`：把 `fi.fe.MoveNext` 换成"只置 `logged_in=1`"的短桩，
需要点一次 Login 才生效。

`apply --fake-login`（=call）：额外 `call fi.djk()` 立即刷新面板。
**不要用**：`fi.djk` 在未真正登录时会因 UI 字段为 null 抛 `NullReferenceException`，
而这段注入代码没有 unwind 记录 → 异常展开失败 → 进程崩溃。

通用参数：`--src`、`--out`（放子命令前后都行）；`apply/all` 另有 `--dry-run`、`--force`。

## 补丁内容

`[always]`（离线必需，6 处 / 22 字节）

| RVA | 改动 | 拦截的请求 |
|---|---|---|
| 0x6ACF10 | `48 89 5C` → `33 C0 C3`（`return false`） | `POST /program_info.php` |
| 0x6AC210 | `48 89 4C` → `33 C0 C3` | `POST /check_unique_connection2.php` |
| 0x6ADCB0 | `48 89 4C` → `33 C0 C3` | `POST /login3.php` |
| 0x6B02A8 | `call Start<SetupOSCMessages>` → `nop*5` | `get_controllers.php` |
| 0x6B1020 | 同上（`fl.dld`） | `get_controllers.php` |
| 0x614EE0 | `je` → `jmp`（跳过单端检测轮询） | 每秒一次的轮询 |

`[strict]`（可选，去掉死代码里的 URL 拼接）：`0x613ED5`、`0x613EE3` → `nop*5`

`global-metadata.dat`：GUPS blob 内基址 `https://webcammotioncapture.info/`
（文件偏移 `0x6F2704`，33 字节）→ `http://127.0.0.1:1/##############`
（同长度；工具用 `plain[i] = enc[i] ^ (i & 0xFF) ^ 0xAA` 自动加/解密）

## 设计要点

* **不硬编码文件偏移**：解析 PE 段头，RVA→文件偏移自动算（本构建恰为 `-0x1800`）。
* **防打错**：每个点都校验原始字节；既不是 original 也不是 patched 就报 `unknown`
  并拒绝写入（`--force` 才强写）。换版本后 RVA 变了会立刻发现。
* **幂等**：已 patched 的点自动 skip，`selftest` 会检查第二次 apply 改动数为 0。
* **可还原**：`selftest` 验证 revert 后与原始文件逐字节一致；`revert` 直接从 `--src` 复制。
* **可审计**：`audit` 扫描全部 `E8 rel32` 调用，把目标换算成 RVA 与 URL 访问器比对，
  再结合当前补丁状态判断调用点是否还会执行（不需要 objdump / IDA）。
* **字符串按明文描述**：补丁表里写 `'https://...' -> 'http://127.0.0.1:1/...'`，
  加解密由工具负责，避免手算 XOR。

## 换游戏版本怎么办

分两层，抗变化能力不同：

| 层 | 依赖 | 版本更新后 |
|---|---|---|
| `global-metadata.dat`（URL 改 loopback） | 只在加密串里搜 `https://webcammotioncapture.info/` 等明文，枚举 256 个相位 q，**不依赖任何偏移/RVA/类名** | **自动可用** |
| `GameAssembly.dll`（精确 stub，无报错弹窗） | 依赖 RVA | 需要 `locate` 重定位（RVA 一定变） |

### 一条命令出表（推荐）

```bash
dotnet Il2CppDumper.dll GameAssembly.dll .../global-metadata.dat out
python3 wmcr_patch.py locate --script out/script.json --write-table wmcr_table.json
python3 wmcr_patch.py --table wmcr_table.json all --strict
```

`--write-table` 会**自动推导**补丁点（不需要你读反汇编），规则都是版本无关的：

| 补丁点 | 推导方法 |
|---|---|
| `POST /program_info.php` / `/check_unique_connection2.php` / `/login3.php` | 反查 URL 字符串的 accessor → 调用者方法；再沿「调用链 + `X_TypeInfo` 引用」两跳，找出落在 `UnityWebRequest.Post` 调用者集合里的 `MoveNext` |
| `POST /login3.php`（URL 存在字段里的情况） | 调用者里 `mov %rax, disp32(%reg)` 拿到字段偏移，再在 Post 方法里找读该偏移的那个 |
| `get_controllers.php`（`AsyncVoid` 状态机） | 从状态机类型名找 `AsyncVoidMethodBuilder.Start<T>`，回头扫它的 `call` 站点，即 kickoff（`fl..ctor` / `fl..dld`） |

在当前构建上验证：自动推导出的 5 条（3 个 `return false` + 2 个 kickoff NOP）与手工分析的表**逐字节一致**（`0x6AC210/0x6ACF10/0x6ADCB0/0x6B02A8/0x6B1020`）。`fi.Update` 的 `je→jmp`（可选优化）不参与自动推导——因为对应 `MoveNext` 被 stub 后已经不会再发请求。

### 三步 SOP

```bash
# 1) 重定位（metadata 会直接给出新偏移，DLL 给出候选清单）
dotnet Il2CppDumper.dll GameAssembly.dll .../global-metadata.dat out
python3 wmcr_patch.py locate --script out/script.json

# 2) 看 check：应报 original（未打）而不是 unknown（打到错误位置）
python3 wmcr_patch.py check

# 3) 打补丁；若 RVA 还没确认，先用只改 metadata 的降级模式
python3 wmcr_patch.py apply                 # 表已更新时
python3 wmcr_patch.py apply --meta-only     # 兜底：任何版本都能用
```

`locate` 会打印类似：

```
== metadata (auto-located, no hardcoded offsets) ==
  base_url         @0x6F2704   q=204 len=33  'https://webcammotioncapture.info/'
  get_controllers  @0x6F7EDB   q=163 len=77  'https://webcammotioncapture.info/WMCTool/...'

== DLL candidates (RVA may change between builds) ==
  accessor  base_url         RVA 0x51F020    ...a::eyy
            called from RVA 0x615A81    fi::.cctor
  accessor  get_controllers  RVA 0x534C40    ...a::ftn
            called from RVA 0x6A73A3    fl.<SetupOSCMessages>d__25::MoveNext

  methods calling UnityWebRequest.Post/SendWebRequest (app side):
    RVA 0x6AC210    fi.fc::MoveNext     Post
    RVA 0x6ACF10    fi.fd::MoveNext     Post
    RVA 0x6ADCB0    fi.fe::MoveNext     Post
    RVA 0x6A5010    fl.<GetHashesAsync>d__35::MoveNext   Post
    ...

  async kickoff sites (AsyncVoidMethodBuilder.Start<appType>):
    fl.<SetupOSCMessages>d__25
      kickoff RVA 0x6B02A8   in fl::.ctor
      kickoff RVA 0x6B1020   in fl::dld
```

照着把新 RVA 填回 `DLL_PATCHES` 即可（`check` 会校验每个 RVA 的原始字节，填错会直接 `unknown` 报错，不会打坏文件）。

### 什么时候必须改代码

| 变化 | 需要改 |
|---|---|
| RVA / 类名 / 字符串索引变了 | 只改 `DLL_PATCHES` 的 RVA（`locate` 给） |
| 服务端路径改名（`program_info.php` 等） | 改 `META_TARGETS` 里的字符串（一行） |
| GUPS 换了 XOR key / 算法 | 改 `_enc()` 与 `resolve_meta()` 的 key（一处常量） |
| 混淆器改到"方法体加密/自校验" | hex patch 可能失效，改用运行时 hook（BepInEx/Harmony、MinHook） |

## 实测结论（1.12.2 / Unity 6000.0.68f1 构建）

| 操作 | 结果 |
|---|---|
| `all --strict` | 无面板、无网络、不崩（纯离线） |
| `all --strict --auto-login` | **启动 → 直接点 "Try for Free" → VMC 正常** ✅ |
| `all --strict --fake-login=mark` | 点 Login（随便填）→ 点 "Try for Free" → VMC 正常 ✅ |
| `all --strict --fake-login`（call） | 点 Login 后**崩溃**（djk 抛 null + 无 unwind）❌ |

## 「登录后才显示」的面板

`fi.djk()`（RVA `0x637D40`）是"按登录标志刷新面板"的函数，里面有 4 处
`movzbl 0x5b8(%rbx),%edx` + `GameObject.SetActive(edx)`（登录后才显示），
以及 2 处 `SetActive(false)`（隐藏登录面板）。它的**唯一调用点**是
`fi.fe.MoveNext` 的成功分支（`movb $1,0x5b8` 之后 `call fi.djk`）。

默认补丁把 `fe.MoveNext` 换成 `return false`，因此 `djk` 永不执行。

**注意**：不要在注入代码里直接 `call fi.djk`。`djk` 里每个 UI 字段前都有
`test/je → il2cpp_codegen_raise_null_reference_exception`；未真正登录时某些字段为 null，
一抛异常就要走 `.pdata` 展开，而注入代码没有 unwind 记录 → **进程崩溃**（实测复现）。

正确做法是 `--auto-login`：只改 `fi.Update` 里的登录门控（函数体内、有 unwind）：

```
原: 80 be b8 05 00 00 00   cmpb $0, 0x5b8(%rsi)
    0f 84 2d 01 00 00      je   0x18061446c
新: c6 86 b8 05 00 00 01   movb $1, 0x5b8(%rsi)     ; 每帧强制已登录
    90 90 90 90 90 90      nop ×6                    ; 取消门控
```

`0x5b8` 这个偏移是自动从 `fe.MoveNext` 里的 `movb $1,<off>` 提取的；
定位时优先用补丁表里 `fi.Update` 的那个锚点，给了 `--script` 时还会用
「该处 0x100 字节内出现 `Input.GetKeyDown` 调用」这一特征排除 GUPS 复制出来的假方法。

**"Try for Free" 仍需要点一次**：它是普通 UI 按钮（`level0` 场景里文本为
"Try for Free"），走 UI 事件绑定，不是登录校验，因此不受 `--auto-login` 影响。

## 两种补丁模式对比

| | `apply`（DLL+meta） | `apply --meta-only` |
|---|---|---|
| 是否需要 RVA | 是 | 否 |
| 版本更新后 | 需 `locate` 重定位 | 直接可用 |
| 出网 | 无 | 无（全部指向 127.0.0.1:1） |
| 副作用 | 干净，无请求、无报错弹窗 | App 逻辑照跑，但请求会被拒；可能有 `Server unstable / Need internet` 类提示或日志 |
| 手部追踪 | `get_controllers` 不请求 | 同上（请求被拒绝） |
