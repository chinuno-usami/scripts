#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_resmap.py —— 解析 Windows 资源映射导出，核对 badmemorylist 是否真的生效

原理
----
`HKLM\\HARDWARE\\RESOURCEMAP\\System Resources` 是内核每次开机重建的内存资源映射：

    \\Physical Memory    内核当前 **可用** 的物理内存范围
    \\Loader Reserved    winload 保留、内核不会使用的范围
    \\Reserved           其它保留范围

如果 badmemorylist 生效，被屏蔽的 4KB 页会从 `Physical Memory` 里被挖掉，
并出现在 `Loader Reserved` 中。若两次开机导出完全一样，说明列表被忽略
（Win10 2004+ 的已知 bug）。

导出方法（管理员 CMD）:
    reg export "HKLM\\HARDWARE\\RESOURCEMAP\\System Resources" C:\\resmap.reg /y

用法
----
  # 1) 核对：把导出文件里每个坏页的状态列出来
  python3 check_resmap.py resmap.reg --bad MemTest86-*.log --bad MemTest86-Report-*.html
  python3 check_resmap.py resmap.reg --bad "bcdedit /set {badmemory} badmemorylist 0x144bb8 0x144bbe"

  # 2) 直接读 BCD 现状当“期望列表”（这两条要在 Windows 上先导出来）
  #    bcdedit /enum {badmemory} > enum_badmemory.txt
  python3 check_resmap.py resmap.reg --enum enum_badmemory.txt

  # 3) 期望 + 观测 一起核对，并自动生成合并命令（含未生效的页）
  python3 check_resmap.py resmap.reg --enum enum_badmemory.txt --bad MemTest86-*.log \
      --emit-cmd
  python3 check_resmap.py resmap.reg --enum enum_badmemory.txt --bad MemTest86-*.log \
      --emit-cmd-file fix_badram.cmd

  # 4) 只看映射本身
  python3 check_resmap.py resmap.reg --dump

  # 5) 前后两次导出做 diff（看新挖掉了哪些页）
  python3 check_resmap.py before.reg after.reg --diff

退出码: 0 = 全部生效; 1 = 有未生效的页; 2 = 解析/参数错误
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys

# ---------------------------------------------------------------- 常量

SECTION_ORDER = ["Physical Memory", "Loader Reserved", "Reserved"]

CM_TYPE_MEMORY = 3          # CmResourceTypeMemory
CM_TYPE_MEMORY_LARGE = 7    # CmResourceTypeMemoryLarge
CM_TYPE_PORT = 1

_MEMORY_LARGE_SHIFT = {0x0200: 8, 0x0400: 16, 0x0800: 32}

# MemTest86
RE_MEM_ERR = re.compile(r"\[MEM\s*(?:ERROR|WARNING)", re.IGNORECASE)
RE_LOG_ADDR = re.compile(r"Address\s*[:=]\s*(?:0x)?([0-9A-Fa-f]{4,16})", re.IGNORECASE)
RE_REPORT_MIN = re.compile(r"Lowest\s+Error\s+Address\s*0x([0-9A-Fa-f]+)", re.IGNORECASE)
RE_REPORT_MAX = re.compile(r"Highest\s+Error\s+Address\s*0x([0-9A-Fa-f]+)", re.IGNORECASE)


# ---------------------------------------------------------------- 读文件

def read_text(path: str) -> str:
    """读文本，兼容 UTF-16(带/不带 BOM) 与 ANSI/latin-1。"""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data.count(b"\x00") > len(data) // 4:
        return data.decode("utf-16le", errors="replace")
    return data.decode("latin-1", errors="replace")


# ---------------------------------------------------------------- 解析 .reg

def _hex_bytes(blob: str) -> bytes:
    return bytes(int(h, 16) for h in re.findall(r"[0-9a-fA-F]{2}", blob.replace("\\", " ")))


def parse_reg(path: str) -> dict[str, bytes]:
    """把 .reg 文件解析成 {段名: 二进制}. 段名取 key 的最后一段。"""
    text = read_text(path)
    sections: dict[str, bytes] = {}
    cur: str | None = None
    buf: list[str] = []
    collecting = False

    def flush():
        nonlocal buf, collecting
        if cur is not None and buf:      # 空 buf 不能覆盖已解析好的段
            sections[cur] = _hex_bytes("".join(buf))
        buf, collecting = [], False

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            flush()
            key = s[1:-1]
            cur = key.rsplit("\\", 1)[-1]
            continue
        if "=hex(8):" in s:
            flush()
            buf = [s.split("=hex(8):", 1)[1]]
            collecting = True
            if not buf[0].rstrip().endswith("\\"):
                flush()
            continue
        if collecting:
            if s.endswith("\\"):
                buf.append(s[:-1])
            else:
                buf.append(s)
                flush()
    flush()
    return sections


def parse_resource_list(blob: bytes) -> list[tuple[int, int, int]]:
    """解析 CM_RESOURCE_LIST -> [(type, start, length), ...]"""
    if len(blob) < 20:
        return []
    count2 = struct.unpack_from("<I", blob, 16)[0]
    out = []
    off = 20
    for _ in range(count2):
        if off + 20 > len(blob):
            break
        d = blob[off:off + 20]
        off += 20
        t = d[0]
        flags = struct.unpack_from("<H", d, 2)[0]
        start = struct.unpack_from("<Q", d, 4)[0]
        length = struct.unpack_from("<I", d, 12)[0]
        if t == CM_TYPE_MEMORY_LARGE:
            length <<= _MEMORY_LARGE_SHIFT.get(flags & 0x0F00, 0)
        out.append((t, start, length))
    return out


def memory_descs(blob: bytes) -> list[tuple[int, int]]:
    """只保留内存类型描述符 -> [(start, length)]"""
    return [(st, ln) for (t, st, ln) in parse_resource_list(blob)
            if t in (CM_TYPE_MEMORY, CM_TYPE_MEMORY_LARGE) and ln > 0]


class ResMap:
    def __init__(self, path: str):
        self.path = path
        raw = parse_reg(path)
        self.raw = raw
        self.sections: dict[str, list[tuple[int, int]]] = {
            name: memory_descs(raw[name]) for name in raw if raw[name]
        }

    def names(self) -> list[str]:
        ordered = [n for n in SECTION_ORDER if n in self.sections]
        ordered += [n for n in self.sections if n not in ordered]
        return ordered

    def contains(self, section: str, addr: int) -> bool:
        for st, ln in self.sections.get(section, []):
            if st <= addr < st + ln:
                return True
        return False

    def containing(self, addr: int) -> list[str]:
        return [n for n in self.names() if self.contains(n, addr)]


# ---------------------------------------------------------------- 坏页来源

def collect_bad_pages(specs: list[str]) -> tuple[set[int], list[str]]:
    """支持: MemTest86 .log / .html 报告 / 文本文件(含 0x 的 PFN) / 直接传命令字符串"""
    pages: set[int] = set()
    notes: list[str] = []
    for spec in specs:
        if os.path.isfile(spec):
            text = read_text(spec)
            src = spec
        else:
            text = spec
            src = "(命令行字符串)"
        low = text.lower()
        if "<html" in low or ("memtest86" in low and "address:" in low and "[mem" not in low):
            n = 0
            plain = re.sub(r"<[^>]+>", " ", text)
            for m in re.finditer(r"Address\s*:\s*(?:0x)?([0-9A-Fa-f]{4,16})", plain, re.IGNORECASE):
                pages.add(int(m.group(1), 16) >> 12)
                n += 1
            for rx in (RE_REPORT_MIN, RE_REPORT_MAX):
                for m in rx.finditer(plain):
                    pages.add(int(m.group(1), 16) >> 12)
                    n += 1
            notes.append("%s -> HTML 报告, 取到 %d 条地址" % (src, n))
            continue
        if "[MEM" in text and RE_MEM_ERR.search(text):
            n = 0
            for line in text.splitlines():
                if not RE_MEM_ERR.search(line):
                    continue
                m = RE_LOG_ADDR.search(line)
                if m:
                    pages.add(int(m.group(1), 16) >> 12)
                    n += 1
            notes.append("%s -> MemTest86 日志, 取到 %d 条错误地址" % (src, n))
            continue
        # 否则当作 PFN 列表 / bcdedit 命令
        found = {int(t, 16) for t in re.findall(r"0x([0-9A-Fa-f]+)", text)}
        pages |= found
        notes.append("%s -> PFN 列表, 取到 %d 个 PFN" % (src, len(found)))
    return pages, notes


# ---------------------------------------------------------------- 区间运算

def merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    ivs = sorted(intervals)
    out = [list(ivs[0])]
    for st, ln in ivs[1:]:
        if st <= out[-1][0] + out[-1][1]:
            out[-1][1] = max(out[-1][1], st + ln - out[-1][0])
        else:
            out.append([st, ln])
    return [tuple(x) for x in out]


def subtract(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """a 中被 b 覆盖后剩下的部分"""
    res = []
    b = merge(b)
    for st, ln in a:
        cur, end = st, st + ln
        for bs, bl in b:
            be = bs + bl
            if be <= cur or bs >= end:
                continue
            if bs > cur:
                res.append((cur, bs - cur))
            cur = max(cur, be)
            if cur >= end:
                break
        if cur < end:
            res.append((cur, end - cur))
    return res


# ---------------------------------------------------------------- 坏页来源

def parse_bcd_enum(spec: str) -> tuple[set[int], str | None, str]:
    """解析 `bcdedit /enum {badmemory}` 的输出（也兼容纯 PFN 文件/命令）。

    返回 (PFN 集合, badmemoryaccess 值, 说明)
    """
    text = read_text(spec) if os.path.isfile(spec) else spec
    pfns: set[int] = set()
    access = None
    found = False
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"badmemorylist\b(.*)$", s, re.IGNORECASE)
        if m:
            pfns |= {int(t, 16) for t in re.findall(r"0x([0-9A-Fa-f]+)", m.group(1))}
            found = True
            continue
        m = re.match(r"badmemoryaccess\b\s*(\S+)", s, re.IGNORECASE)
        if m:
            access = m.group(1)
    if found:
        note = "bcdedit /enum {badmemory} 输出"
    else:
        pfns = {int(t, 16) for t in re.findall(r"0x([0-9A-Fa-f]+)", text)}
        note = "未发现 badmemorylist 行, 按 PFN 列表解析"
    return pfns, access, note


def build_cmd(pages: list[int], obj: str = "{badmemory}") -> str:
    return "bcdedit /set %s badmemorylist %s" % (
        obj, " ".join("0x%x" % p for p in pages))


def write_cmd_file(pages: list[int], obj: str, path: str) -> None:
    cmd = build_cmd(pages, obj)
    content = f"""@echo off
setlocal
title Fix bad RAM via bcdedit badmemorylist

net session >nul 2>&1
if errorlevel 1 (
    echo [!] 请以管理员身份运行本脚本。
    pause
    exit /b 1
)

echo [*] 当前 {obj} 设置:
bcdedit /enum {obj}
echo.

echo [*] 写入 {len(pages)} 个坏页 (PFN):
{cmd}
if errorlevel 1 (
    echo [!] 设置失败。
    pause
    exit /b 1
)

echo.
echo [*] 确认不使用被屏蔽的页 (默认即 No):
bcdedit /set {{current}} badmemoryaccess No

echo.
echo [*] 写入结果:
bcdedit /enum {obj}

echo.
echo [*] 完成。请重启后再用 check_resmap.py 验证。
pause
"""
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(content)


# ---------------------------------------------------------------- 输出

def fmt_range(st: int, ln: int) -> str:
    return "0x%010X - 0x%010X (%d 页)" % (st, st + ln, ln // 0x1000)


def dump(rm: ResMap):
    print("=" * 78)
    print("资源映射: %s" % rm.path)
    print("=" * 78)
    for name in rm.names():
        print("\n[%s]  共 %d 段" % (name, len(rm.sections[name])))
        for st, ln in rm.sections[name]:
            print("   " + fmt_range(st, ln))


def check(rm: ResMap, observed: set[int], expected: set[int],
          bad_notes: list[str], enum_notes: list[str],
          show_all: bool, emit_cmd: bool, obj: str,
          cmd_file: str | None = None):
    pages = sorted(expected | observed)
    print("=" * 78)
    print("资源映射: %s" % rm.path)
    print("=" * 78)
    for n in enum_notes:
        print("  [期望] %s" % n)
    for n in bad_notes:
        print("  [观测] %s" % n)
    print("  待核对: %d 个 4KB 页 (期望 %d / 观测 %d / 交集 %d)"
          % (len(pages), len(expected), len(observed), len(expected & observed)))
    print()

    blocked, unblocked, other = [], [], []
    for p in pages:
        a = p << 12
        in_phys = rm.contains("Physical Memory", a)
        if in_phys:
            unblocked.append(p)
        elif rm.contains("Loader Reserved", a) or rm.contains("Reserved", a):
            blocked.append(p)
        else:
            other.append(p)

    print("  逐页状态:")
    for p in pages:
        a = p << 12
        in_phys = rm.contains("Physical Memory", a)
        in_lr = rm.contains("Loader Reserved", a)
        tag = ("E" if p in expected else "") + ("O" if p in observed else "") or "-"
        if not in_phys and (in_lr or rm.contains("Reserved", a)):
            mark = "已屏蔽"
        elif in_phys:
            mark = "!! 未屏蔽 (仍可用)"
        else:
            mark = "?? 不在任何段"
        if show_all or mark != "已屏蔽":
            print("    0x%-8x phys 0x%010X  Physical=%-5s LoaderReserved=%-5s [%s] -> %s"
                  % (p, a, in_phys, in_lr, tag, mark))
    print("    (标记: E=在期望列表 / O=在观测坏页 / EO=两者都有)")
    print()

    unblocked_set = set(unblocked)
    print("  小结: 已屏蔽 %d / 未屏蔽 %d / 其它 %d" % (len(blocked), len(unblocked), len(other)))
    exp_bad = sorted(expected & unblocked_set)
    new_bad = sorted(observed & unblocked_set - expected)
    if exp_bad:
        print("  期望列表(--enum)里但未生效 (%d):" % len(exp_bad))
        print("    " + " ".join("0x%x" % p for p in exp_bad))
    if new_bad:
        print("  观测到但尚未屏蔽的页 (%d):" % len(new_bad))
        print("    " + " ".join("0x%x" % p for p in new_bad))

    if emit_cmd:
        merged = pages
        print()
        print("  建议下发的合并命令 (%d 个 PFN, = 期望 ∪ 观测; 覆盖语义必须一次列全):" % len(merged))
        print("    " + build_cmd(merged, obj))
        print("    bcdedit /set {current} badmemoryaccess No")
        if cmd_file:
            write_cmd_file(merged, obj, cmd_file)
            print("  已生成脚本: %s" % cmd_file)

    if unblocked:
        print()
        print("  => 有 %d 页还没生效。请检查:" % len(unblocked))
        print("     - bcdedit /enum {badmemory} 是否包含这些 PFN")
        print("     - bcdedit /enum {current} 是否被覆盖")
        print("     - 是否已重启；Win10 2004+ 存在忽略该列表的已知问题")
        return 1
    print("  => 全部页都已从可用内存剔除，badmemorylist 生效。")
    return 0


def diff(rm_a: ResMap, rm_b: ResMap):
    print("=" * 78)
    print("资源映射 diff")
    print("   before: %s" % rm_a.path)
    print("   after : %s" % rm_b.path)
    print("=" * 78)
    rc = 0
    for name in SECTION_ORDER:
        a = merge(rm_a.sections.get(name, []))
        b = merge(rm_b.sections.get(name, []))
        removed = subtract(a, b)   # before 有, after 没有
        added = subtract(b, a)     # after 新出现
        if not removed and not added:
            print("\n[%s] 无变化" % name)
            continue
        print("\n[%s]" % name)
        for st, ln in removed:
            print("   - 移出: " + fmt_range(st, ln))
        for st, ln in added:
            print("   + 新增: " + fmt_range(st, ln))
    # 结论
    phys_removed = subtract(merge(rm_a.sections.get("Physical Memory", [])),
                            merge(rm_b.sections.get("Physical Memory", [])))
    if phys_removed:
        print("\n结论: 可用内存(Physical Memory)中出现了新空洞 -> 说明这次修改生效了:")
        for st, ln in phys_removed:
            print("   " + fmt_range(st, ln))
    else:
        print("\n结论: Physical Memory 没有任何变化 -> 这次修改 **没有生效**"
              "（或两次导出之间没有重启）。")
        rc = 1
    return rc


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="解析 HKLM\\HARDWARE\\RESOURCEMAP\\System Resources 导出，核对 badmemorylist 是否生效",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("regs", nargs="+", help="资源映射导出的 .reg 文件 (1 个=核对; 2 个+--diff=对比)")
    ap.add_argument("--bad", action="append", default=[], metavar="SPEC",
                    help="坏页来源: MemTest86 .log / .html 报告 / 含 0x 的 PFN 文件 / "
                         "直接的 'bcdedit ... badmemorylist 0x.. 0x..' 字符串。可重复。")
    ap.add_argument("--enum", action="append", default=[], metavar="FILE",
                    help="期望列表: 'bcdedit /enum {badmemory}' 的输出文件 (或直接传输出文本)。"
                         "会逐页核对 BCD 里声明的页是否真的被内核剔除。可重复。")
    ap.add_argument("--emit-cmd", action="store_true",
                    help="输出一条合并后的 bcdedit badmemorylist 命令 (期望 ∪ 观测)")
    ap.add_argument("--emit-cmd-file", metavar="PATH",
                    help="同 --emit-cmd, 并把命令写成可右键以管理员运行的 .cmd")
    ap.add_argument("--obj", default="{badmemory}", metavar="GUID",
                    help="bcdedit 对象, 默认 {badmemory}")
    ap.add_argument("--dump", action="store_true", help="只打印资源映射内容")
    ap.add_argument("--diff", action="store_true", help="对两个导出做前后对比")
    ap.add_argument("--show-all", action="store_true", help="连已屏蔽的页也逐条打印")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出核对结果")
    args = ap.parse_args(argv)

    for p in args.regs:
        if not os.path.isfile(p):
            print("[!] 文件不存在: %s" % p, file=sys.stderr)
            return 2

    if args.diff:
        if len(args.regs) != 2:
            print("[!] --diff 需要正好两个 .reg 文件", file=sys.stderr)
            return 2
        return diff(ResMap(args.regs[0]), ResMap(args.regs[1]))

    if len(args.regs) > 1:
        print("[!] 给出了多个 .reg 但没有 --diff，是否想用 --diff 做前后对比？", file=sys.stderr)
        return 2

    rm = ResMap(args.regs[0])
    if args.dump or (not args.bad and not args.enum):
        dump(rm)
        if not args.bad and not args.enum:
            print("\n提示: 加 --bad MemTest86-*.log 核对坏页是否被屏蔽;")
            print("      加 --enum enum.txt 核对 BCD 里声明的列表是否真的生效。")
        return 0

    observed, bad_notes = collect_bad_pages(args.bad)
    expected: set[int] = set()
    enum_notes: list[str] = []
    for spec in args.enum:
        pfns, access, note = parse_bcd_enum(spec)
        expected |= pfns
        extra = ""
        if access is not None:
            extra = ", badmemoryaccess=%s%s" % (
                access, "  <== 必须为 No, 否则坏页会被允许使用!" if access.lower() in ("yes", "1") else "")
        enum_notes.append("%s -> %d 个 PFN%s" % (spec, len(pfns), extra))

    if not observed and not expected:
        print("[!] 没有从 --bad / --enum 里解析出任何页。", file=sys.stderr)
        return 2

    if args.json:
        result = {}
        for p in sorted(expected | observed):
            a = p << 12
            result["0x%x" % p] = {
                "phys": "0x%X" % a,
                "expected": p in expected,
                "observed": p in observed,
                "physical": rm.contains("Physical Memory", a),
                "loader_reserved": rm.contains("Loader Reserved", a),
                "reserved": rm.contains("Reserved", a),
            }
        print(json.dumps(result, indent=2))
        bad = [v for v in result.values()
               if v["physical"] or not (v["loader_reserved"] or v["reserved"])]
        return 0 if not bad else 1

    return check(rm, observed, expected, bad_notes, enum_notes,
                 args.show_all, args.emit_cmd or bool(args.emit_cmd_file),
                 args.obj, args.emit_cmd_file)


if __name__ == "__main__":
    raise SystemExit(main())
