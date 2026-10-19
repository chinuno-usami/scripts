# memtest86-badram-toolkit

Small, dependency-free Python tools for dealing with **faulty RAM on Windows**:

| Tool | What it does |
|---|---|
| `memtest86_to_badmemorylist.py` | Turn **MemTest86** logs/reports into a `bcdedit` `badmemorylist` (Windows bad-page blacklist), with merging and `.cmd` generation |
| `memtest86_dimm_map.py` | Read the DRAM topology from the MemTest86 log and decide whether the errors can be blamed on a **single DIMM** |
| `check_resmap.py` | **Prove that `badmemorylist` really took effect** by decoding `HKLM\HARDWARE\RESOURCEMAP`, and auto-generate the next merged command |

> 中文速览见文末 → [中文说明](#中文说明)

## Why these tools exist

- MemTest86 reports **CPU physical addresses**; `bcdedit` wants **4 KB page frame numbers (PFN)**:
  `PFN = physical_address >> 12` (drop the last 3 hex digits). Getting this wrong is the
  #1 reason `badmemorylist` "doesn't work".
- `badmemorylist` is **overwrite-only**: every `bcdedit /set` replaces the entire list.
  Newly discovered pages must be merged with the previous list manually.
- Since **Windows 10 2004** the list is sometimes **silently ignored** by the kernel
  (Microsoft Q&A thread *"Starting with Windows 10 2004 the PFA memory list is no longer recognized"*),
  and Windows Update / Memory Diagnostics may delete it. You need a way to *prove* effectiveness.
- MemTest86 **Free** truncates the detailed error record list after roughly 500 errors per
  session (`Truncating due to too many errors`), and the config file (`mt86.cfg`,
  `MAXERRCOUNT`, `REPORTNUMERRS`, …) is **Pro/Site only**. The workaround is documented below.

## Requirements

- Python **3.8+**, standard library only — no `pip install` needed.
- Windows (Administrator) only for the `bcdedit` / `reg export` steps.
  The scripts themselves run on any OS.

## Scripts

### 1. `memtest86_to_badmemorylist.py` — generate the blacklist

Parses MemTest86 `.log` files **and** `MemTest86-Report-*.html` reports, extracts every
error address, converts to PFNs, merges duplicate pages, and prints a ready-to-run command.

```console
$ python memtest86_to_badmemorylist.py MemTest86-*.log
====================================================================
MemTest86 -> badmemorylist 生成结果
====================================================================
  MEM ERROR/WARNING 行数: 864
  唯一物理地址  : 387
  唯一 4KB 页   : 35
  地址范围      : 0x144BB97F0 - 0x52FFFFAE8
  物理页范围    : 0x144BB9000 - 0x52FFFFFFF

  连续页区间 (15 段):
    pages 0x144BB9    - 0x144BB9     物理 0x144BB9000 - 0x144BB9FFF  (1 页)
    pages 0x144BBE    - 0x144BC0     物理 0x144BBE000 - 0x144BC0FFF  (3 页)
    ...

  生成的命令 (35 个 PFN):

bcdedit /set {badmemory} badmemorylist 0x144bb9 0x144bbe 0x144bbf ...
```

Useful options:

```bash
# Merge several runs / reports (union of all bad pages)
python memtest86_to_badmemorylist.py old.log new.log MemTest86-Report-*.html

# Only show what is new compared to an already applied command (badmemorylist is overwrite-only)
python memtest86_to_badmemorylist.py new.log --added-only \
    --existing "bcdedit /set {badmemory} badmemorylist 0x144bb9 0x144bbe ... 0x52ffff"

# Block ±1 page around every bad page (extra margin)
python memtest86_to_badmemorylist.py MemTest86-*.log --expand 1

# Produce a .cmd you can right-click -> Run as administrator
python memtest86_to_badmemorylist.py MemTest86-*.log --cmd-file fix_badram.cmd
```

| Option | Description |
|---|---|
| `--expand N` | additionally block N pages before/after each bad page |
| `--existing SPEC` | merge with a previous PFN list (file path, `bcdedit` command string, or `bcdedit /enum` output) |
| `--added-only` | also print only the newly added PFNs |
| `--cmd-file PATH` | write a self-checking `.cmd` (admin check, write, verify) |
| `--list` | print an address → PFN table |
| `--errors-only` | ignore `[MEM WARNING]` lines (Hammer test) |
| `--quiet` | print only the `bcdedit` command |
| `--obj GUID` | bcdedit object, default `{badmemory}` |

Notes:

- Only `[MEM ERROR]` / `[MEM WARNING]` lines are used, so unrelated log lines such as
  `HPET base address: 0xFED00000` are never mistaken for bad memory.
- `MemTest86-Report-*.html` is UTF-16 encoded and contains only the *last 10* errors, but its
  `Lowest / Highest Error Address` values are still useful to catch addresses lost to truncation.
- Multiple input files are automatically unioned.

### 2. `memtest86_dimm_map.py` — can the errors be blamed on one DIMM?

Intel memory controllers hash/interleave physical addresses across channels, ranks and banks
(`channel/rank/bank` are XOR functions of address bits). MemTest86 does **not** tell you which
DIMM is faulty (that's a Pro/Site feature). This script:

1. Extracts the real topology from the log (`SMBIOS` slots, `MAD_DIMM_CH*`, `MAD_INTER_CH`,
   `CHANNEL_HASH`).
2. Scores every *plausible* 2-channel interleave model by "single-channel purity".
3. Runs a **GF(2) linear-consistency test**: is there *any* XOR mask that puts all failing
   addresses on the same channel?

```console
$ python memtest86_dimm_map.py MemTest86-*.log
  控制器       : Intel Coffee Lake
  总容量       : 34359738368 bytes (32 GiB)
  MAD_INTER_CH : 0x00010000   (bit16=1 -> 通道交织开启)
  CHANNEL_HASH : 0x120C3200   (通道哈希寄存器, 算法未公开)

  通道配置:
    通道A (MAD_DIMM_CH0=0x00000410): 16 GiB ranks=2 width=x8  槽位 ChannelA-DIMM2
    通道B (MAD_DIMM_CH1=0x00000410): 16 GiB ranks=2 width=x8  槽位 ChannelB-DIMM2

  低位(6..16)候选通道掩码的『单通道纯度』:
    mask=0x8000     bits=[15]               通道0=  21 通道1= 366  纯度=94.6%
    ...

  结论:
    [x] 在 256B~64KB 粒度内的任何二通道交织模型下，两个通道都命中坏页。
        即：**无法**把故障归因到单条 DIMM。
```

**Honest caveat:** exact channel decoding requires Intel's unpublished `MAD_*` hash
(see the DRAMA / TRRespass research). The only reliable way to identify a failing module is to
**test one DIMM at a time** (or swap them and re-run); the tool tells you when the data does
not support a single-DIMM conclusion instead of guessing.

### 3. `check_resmap.py` — prove that `badmemorylist` took effect

`HKLM\HARDWARE\RESOURCEMAP\System Resources` is rebuilt on every boot and contains:

```
\Physical Memory    physical ranges the kernel may use
\Loader Reserved    ranges reserved by winload (this is where blocked pages land)
\Reserved           other reserved ranges
```

If `badmemorylist` works, each blocked 4 KB page is **carved out of `Physical Memory`** and
appears under `Loader Reserved`. If a before/after export is identical, the list was ignored.

The script fully decodes the exported `CM_RESOURCE_LIST` (including `CmResourceTypeMemoryLarge`),
so you don't have to eyeball hex bytes.

```cmd
:: On Windows (Administrator) — export the two inputs
reg export "HKLM\HARDWARE\RESOURCEMAP\System Resources" C:\resmap.reg /y
bcdedit /enum {badmemory} > C:\enum_badmemory.txt
```

```bash
# Check the pages declared in BCD against the live memory map
python check_resmap.py resmap.reg --enum enum_badmemory.txt

# Check BCD list + observed MemTest86 errors, and emit the next merged command
python check_resmap.py resmap.reg --enum enum_badmemory.txt --bad MemTest86-*.log --emit-cmd

# ... write it as a runnable .cmd instead
python check_resmap.py resmap.reg --enum enum_badmemory.txt --bad MemTest86-*.log \
    --emit-cmd-file fix_badram.cmd

# Just dump the decoded map
python check_resmap.py resmap.reg --dump

# Compare two exports (e.g. before / after applying the list)
python check_resmap.py before.reg after.reg --diff
```

```console
$ python check_resmap.py resmap.reg --enum enum_badmemory.txt --bad MemTest86-*.log --emit-cmd
  [期望] enum_badmemory.txt -> 35 个 PFN, badmemoryaccess=No
  [观测] MemTest86-20261018-163914_850136.log -> 426 条错误地址
  待核对: 42 个 4KB 页 (期望 35 / 观测 36 / 交集 29)

  逐页状态:
    0x144bb8   phys 0x0144BB8000  Physical=True  LoaderReserved=False [O] -> !! 未屏蔽 (仍可用)
    0x144bbd   phys 0x0144BBD000  Physical=True  LoaderReserved=False [O] -> !! 未屏蔽 (仍可用)
    ...

  小结: 已屏蔽 35 / 未屏蔽 7 / 其它 0
  观测到但尚未屏蔽的页 (7):
    0x144bb8 0x144bbd 0x146bbd 0x3750ab 0x3850ad 0x4a6257 0x83f067

  建议下发的合并命令 (42 个 PFN, = 期望 ∪ 观测; 覆盖语义必须一次列全):
    bcdedit /set {badmemory} badmemorylist 0x144bb8 0x144bb9 ... 0x83f067
    bcdedit /set {current} badmemoryaccess No
```

Exit codes: `0` = everything is blocked, `1` = some pages are not in effect, `2` = usage/parse error.
`--json` returns the full per-page state (`expected` / `observed` / `physical` / `loader_reserved` / `reserved`).

## End-to-end workflow

1. **Collect** — boot MemTest86 (Free is fine) and let it save the log/report to the USB stick.
2. **Generate** — `python memtest86_to_badmemorylist.py MemTest86-*.log --cmd-file fix_badram.cmd`
3. **Apply** — on Windows, right-click the `.cmd` → *Run as administrator*, then reboot:
   ```cmd
   bcdedit /set {badmemory} badmemoryaccess no
   bcdedit /enum {badmemory}
   ```
4. **Verify** — `python check_resmap.py resmap.reg --enum enum_badmemory.txt --emit-cmd`.
   A new hole in `Physical Memory` at the blocked pages = it works. No change = the kernel is
   ignoring the list (Windows 10 2004+ bug), and the blacklist is not a viable workaround.
5. **Repeat** — after the next MemTest86 run, pass `--existing`/`--enum` so the list is merged
   instead of overwritten.

## Getting all error addresses on MemTest86 **Free**

The detailed error list is capped at roughly 500 records per session, so a full 32 GB scan with
all tests enabled will truncate. Workaround, entirely within the Free edition:

- In the main menu → `Test Selection`, enable **one test at a time** and set **1 pass**.
  Individual tests produced 1–285 errors on the machine this was developed against, i.e. below
  the cap; running all of them together (930) truncates.
- If a single test still exceeds the cap, restrict `Address Range` per run.
- Every session writes a uniquely named `MemTest86-<timestamp>.log`, so nothing is overwritten —
  copy them off the stick and let the scripts union them.

## Caveats

- Blacklisting pages is a **temporary workaround**, not a repair. Replace faulty RAM when possible.
- `badmemorylist` stores **PFNs** (page numbers), 4 KB granularity — a whole page is lost per entry.
- Windows may delete the list (Windows Memory Diagnostics, some updates). Re-check with
  `check_resmap.py` after updates.
- MemTest86 Free's HTML report only contains the last 10 errors; `check_resmap.py` and
  `memtest86_to_badmemorylist.py` also read its `Lowest/Highest Error Address` for extra coverage.
- **Privacy:** real MemTest86 logs contain SMBIOS baseboard/system/RAM serial numbers and part
  numbers. Sanitize them before publishing or attaching them to issues — see `.gitignore`.

## License

MIT. Add a `LICENSE` file before publishing if you want it to be explicit.

---

## 中文说明

三个零依赖的 Python 小工具，用来处理 Windows 上的内存坏块：

1. **`memtest86_to_badmemorylist.py`** — 读 MemTest86 的 `.log` / HTML 报告，把物理地址换算成
   PFN（`PFN = 物理地址 >> 12`），合并去重后生成 `bcdedit` 的 `badmemorylist` 命令；支持
   `--existing` 合并旧列表、`--expand` 多屏蔽邻近页、`--cmd-file` 生成可运行的 `.cmd`。
2. **`memtest86_dimm_map.py`** — 从日志里还原内存拓扑，并用 GF(2) 线性一致性检验判断
   "能不能把故障归因到某一条 DIMM"。注意：物理地址到 DIMM 的映射受 Intel 未公开的
   channel hash 影响，多数情况下结论是"无法判定"，脚本会如实说明而不会瞎猜。
3. **`check_resmap.py`** — 解析 `HKLM\HARDWARE\RESOURCEMAP\System Resources` 的导出，
   逐页核对 badmemorylist 是否**真的生效**（看 `Physical Memory` 里有没有被挖出洞、
   对应地址是否落到 `Loader Reserved`），并能自动生成下一版合并命令或 `.cmd` 脚本。

典型流程：

```
MemTest86 采集日志
   -> memtest86_to_badmemorylist.py  生成/合并 badmemorylist 命令
   -> Windows 管理员执行 + 重启
   -> check_resmap.py --enum ... 核对是否生效, 顺便产出下一版命令
```

提取坏地址时的坑：MemTest86 Free 每轮会话对详细错误记录有约 500 条上限
（`Truncating due to too many errors`），且配置文件 `mt86.cfg` 是 Pro/Site 专属。
Free 版可用"一次只跑一个测试、1 pass"的方式绕开（各测试错误数都低于上限），
再把每次会话的日志一起丢给脚本取并集。
