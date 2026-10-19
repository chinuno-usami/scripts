#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MemTest86 / MemTest86+ 日志 -> Windows (bcdedit) badmemorylist 命令生成器

原理:
  MemTest86 报告的 Address 是 CPU 视角的物理地址 (Physical Address)。
  bcdedit 的 badmemorylist 需要的是 4KB 页框号 PFN (Page Frame Number),
  PFN = 物理地址 >> 12   (等价于: 去掉地址最后 3 位十六进制, 向下取整到 4KB)

  参考: Microsoft "How to Manage the PFA Memory List"
        "This value contains the list of PFNs for the memory pages ..."
        > bcdedit /enum {badmemory}
        > badmemorylist 0xffe38 0x100f

用法示例:
  # 1) 直接打印命令
  python memtest86_to_badmemorylist.py MemTest86-*.log

  # 2) 每个坏页前后各多屏蔽 1 页 (更保险)
  python memtest86_to_badmemorylist.py MemTest86-*.log --expand 1

  # 3) 生成可直接右键"以管理员身份运行"的 .cmd
  python memtest86_to_badmemorylist.py MemTest86-*.log --cmd-file fix_badram.cmd

  # 4) 同时打印地址/页号对照表
  python memtest86_to_badmemorylist.py MemTest86-*.log --list

  # 5) 忽略 [MEM WARNING] 行, 只统计 [MEM ERROR]
  python memtest86_to_badmemorylist.py MemTest86-*.log --errors-only

注意:
  * 必须在管理员命令提示符中执行生成的命令。
  * badmemorylist 每次执行都是"整体覆盖", 必须在同一条命令里列全所有页。
  * 若日志是 INCOMPLETE FAIL / 出现 "Truncating due to too many errors",
    说明坏页清单不完整, 屏蔽只能算临时手段。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Iterable

# 一行的典型形式:
#   2026-10-18 14:24:32 - [MEM ERROR - Data] Test: 3, CPU: 2, Address: 144BBFEC8, ...
# 也兼容 "Address: 0x144BBFEC8" / HTML 报告里的写法
RE_ADDRESS = re.compile(r"Address\s*[:=]\s*(?:0x)?([0-9A-Fa-f]{4,16})", re.IGNORECASE)
# 只认 MEM ERROR / MEM WARNING 行, 否则会把
#   "hpet_init - HPET base address: 0xFED00000"
#   "mem_size - Max address: 0x86E000000"
# 这类日志误当成坏内存
RE_ERROR = re.compile(r"\[MEM\s*(?:ERROR|WARNING)", re.IGNORECASE)

# HTML 报告里的地址 (报告是 UTF-16 编码, 需先去标签)
RE_REPORT_ADDR = re.compile(r"Address\s*:\s*(?:0x)?([0-9A-Fa-f]{4,16})", re.IGNORECASE)
RE_REPORT_MIN = re.compile(r"Lowest\s+Error\s+Address\s*0x([0-9A-Fa-f]+)", re.IGNORECASE)
RE_REPORT_MAX = re.compile(r"Highest\s+Error\s+Address\s*0x([0-9A-Fa-f]+)", re.IGNORECASE)

BADMEMORY_OBJ = "{badmemory}"

# bcdedit 命令行长度上限约 8191 字符, 留点余量
MAX_CMD_LEN = 7500


def _read_text(path: str) -> tuple[str, bool]:
    """读取文件; 返回 (文本, 是否 HTML)。兼容: 日志(latin-1) / HTML 报告(UTF-16)"""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace"), True
    if data.count(b"\x00") > len(data) // 4:   # 无 BOM 的 UTF-16LE
        return data.decode("utf-16le", errors="replace"), True
    text = data.decode("latin-1", errors="replace")
    return text, "<html" in text.lower()


def _parse_report(text: str, addresses: set[int]) -> int:
    """解析 MemTest86 HTML 报告: Last N Errors + Lowest/Highest Error Address"""
    txt = re.sub(r"<[^>]+>", " ", text)
    n = 0
    for m in RE_REPORT_ADDR.finditer(txt):
        addresses.add(int(m.group(1), 16))
        n += 1
    for rx in (RE_REPORT_MIN, RE_REPORT_MAX):
        for m in rx.finditer(txt):
            addresses.add(int(m.group(1), 16))
    return n


def parse_files(paths: Iterable[str],
                include_warnings: bool = True) -> tuple[set[int], int, list[str]]:
    """返回 (唯一物理地址集合, 错误/警告行数, 实际解析的文件列表)。
    支持: MemTest86 .log 日志 和 MemTest86-Report-*.html 报告。"""
    addresses: set[int] = set()
    error_lines = 0
    seen_in: list[str] = []

    err_re = RE_ERROR if include_warnings else re.compile(r"\[MEM\s*ERROR", re.IGNORECASE)

    for path in paths:
        if not os.path.isfile(path):
            print(f"[!] 跳过(不是文件): {path}", file=sys.stderr)
            continue
        seen_in.append(path)
        text, is_html = _read_text(path)
        if is_html:
            error_lines += _parse_report(text, addresses)
            continue
        for line in text.splitlines():
            if not err_re.search(line):
                continue
            m = RE_ADDRESS.search(line)
            if not m:
                continue
            error_lines += 1
            addresses.add(int(m.group(1), 16))

    return addresses, error_lines, seen_in


def parse_existing_pfns(spec: str) -> list[int]:
    """从已有 badmemorylist 命令/文件/字符串中提取 PFN (所有 0x.... 数字)。"""
    raw = spec
    if os.path.isfile(spec):
        raw, _ = _read_text(spec)
        raw = re.sub(r"<[^>]+>", " ", raw)
    return sorted({int(t, 16) for t in re.findall(r"0x([0-9A-Fa-f]+)", raw)})


def to_pages(addresses: Iterable[int]) -> list[int]:
    """物理地址集合 -> 排序去重后的 4KB 页框号列表 (PFN)"""
    return sorted({addr >> 12 for addr in addresses})


def expand_pages(pages: Iterable[int], margin: int) -> list[int]:
    if margin <= 0:
        return sorted(set(pages))
    out: set[int] = set()
    for p in pages:
        for d in range(-margin, margin + 1):
            if p + d >= 0:
                out.add(p + d)
    return sorted(out)


def contiguous_runs(pages: list[int]) -> list[tuple[int, int]]:
    """把 PFN 列表压缩成连续区间 [(start_pfn, end_pfn), ...]"""
    if not pages:
        return []
    runs, start, prev = [], pages[0], pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        runs.append((start, prev))
        start = prev = p
    runs.append((start, prev))
    return runs


def build_command(pages: list[int], obj: str = BADMEMORY_OBJ) -> str:
    return "bcdedit /set %s badmemorylist %s" % (
        obj,
        " ".join("0x%x" % p for p in pages),
    )


def render_summary(addresses: set[int], pages: list[int], error_lines: int,
                   files: list[str], margin: int,
                   existing: list[int] | None = None,
                   added: list[int] | None = None) -> str:
    out = []
    out.append("=" * 68)
    out.append("MemTest86 -> badmemorylist 生成结果")
    out.append("=" * 68)
    for f in files:
        out.append("  输入文件      : %s" % f)
    out.append("  MEM ERROR/WARNING 行数: %d" % error_lines)
    if existing:
        out.append("  其中已屏蔽页  : %d" % len(set(existing)))
    if added is not None:
        out.append("  本次新增页    : %d" % len(added))
        for p in added:
            out.append("      0x%-8x  物理 0x%X000-0x%XFFF" % (p, p, p))
    out.append("  唯一物理地址  : %d" % len(addresses))
    out.append("  唯一 4KB 页   : %d%s" % (len(pages), "" if margin == 0 else "  (已含 ±%d 页扩展)" % margin))
    if addresses:
        out.append("  地址范围      : 0x%X - 0x%X" % (min(addresses), max(addresses)))
    low = min(pages)
    high = max(pages)
    out.append("  物理页范围    : 0x%X000 - 0x%XFFF" % (low, high))
    out.append("")
    out.append("  连续页区间 (%d 段):" % len(contiguous_runs(pages)))
    for a, b in contiguous_runs(pages):
        out.append("    pages 0x%-9X - 0x%-9X  物理 0x%X000 - 0x%XFFF  (%d 页)"
                   % (a, b, a, b, b - a + 1))
    out.append("")
    return "\n".join(out)


def render_table(addresses: set[int]) -> str:
    lines = ["  物理地址            页框号(PFN)"]
    for addr in sorted(addresses):
        lines.append("  0x%-16X  0x%X" % (addr, addr >> 12))
    lines.append("")
    return "\n".join(lines)


def build_cmd_file(pages: list[int], obj: str, path: str) -> str:
    cmd = build_command(pages, obj)
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
echo [*] 完成。请重启后再次运行 MemTest86 验证。
pause
"""
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(content)
    return cmd


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="从 MemTest86 日志提取坏内存页并生成 Windows bcdedit badmemorylist 命令",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("logs", nargs="+", help="MemTest86 日志(.log) 或 HTML 报告(.html), 可多个")
    ap.add_argument("--expand", type=int, default=0, metavar="N",
                    help="每个坏页前后各多屏蔽 N 页 (默认 0, 建议 0 或 1)")
    ap.add_argument("--existing", metavar="SPEC",
                    help="已有的 PFN 列表: 可传文件路径, 或直接传上次的 "
                         "'bcdedit /set {badmemory} badmemorylist 0x.. 0x..' 命令字符串。"
                         "会与本次结果合并 (badmemorylist 是覆盖语义, 必须合并)")
    ap.add_argument("--added-only", action="store_true",
                    help="额外单独输出本次新增的 PFN 列表 (仅供核对)")
    ap.add_argument("--obj", default=BADMEMORY_OBJ, metavar="GUID",
                    help="bcdedit 对象, 默认 {badmemory}")
    ap.add_argument("--cmd-file", metavar="PATH",
                    help="额外生成一个可双击(需管理员)的 .cmd 脚本")
    ap.add_argument("--list", action="store_true", help="打印 地址 -> PFN 对照表")
    ap.add_argument("--errors-only", action="store_true",
                    help="忽略 [MEM WARNING] 行, 只统计 [MEM ERROR] (默认两者都算)")
    ap.add_argument("--quiet", action="store_true", help="只输出 bcdedit 命令本身")
    args = ap.parse_args(argv)

    addresses, error_lines, files = parse_files(args.logs,
                                               include_warnings=not args.errors_only)
    if not addresses:
        print("[!] 未在任何文件中找到 [MEM ERROR]/[MEM WARNING] 错误地址。", file=sys.stderr)
        print("    请确认传入的是 MemTest86 运行日志 (MemTest86-*.log)。", file=sys.stderr)
        return 2

    base_pages = to_pages(addresses)
    pages = expand_pages(base_pages, max(0, args.expand))

    existing: list[int] | None = None
    added: list[int] | None = None
    if args.existing is not None:
        existing = parse_existing_pfns(args.existing)
        added = sorted(set(pages) - set(existing))
        pages = sorted(set(existing) | set(pages))   # 覆盖语义: 必须合并

    cmd = build_command(pages, args.obj)

    if not args.quiet:
        print(render_summary(addresses, pages, error_lines, files, max(0, args.expand),
                             existing=existing, added=added))
        if args.list:
            print("  地址 -> 页框号:")
            print(render_table(addresses))
        print("  生成的命令 (%d 个 PFN%s):" %
              (len(pages), "，= 已有 + 新增" if existing is not None else ""))
        print()

    print(cmd)

    if args.added_only and added is not None:
        print("\n# 本次新增 PFN (%d 个, 仅核对用):" % len(added))
        print(" ".join("0x%x" % p for p in added))

    if not args.quiet:
        print()
        print("  应用步骤 (管理员命令提示符):")
        print("    1) bcdedit /enum %s            :: 查看当前值" % args.obj)
        print("    2) 执行上面的命令")
        print("    3) bcdedit /enum %s            :: 验证" % args.obj)
        print("    4) bcdedit /set {current} badmemoryaccess No")
        print("    5) 重启生效")
        print()
        print("  清除屏蔽: bcdedit /deletevalue %s badmemorylist" % args.obj)

    if len(cmd) > MAX_CMD_LEN:
        print("\n[!] 警告: 命令长度 %d 字符, 可能超过 bcdedit 命令行上限 (~8191)。"
              % len(cmd), file=sys.stderr)
        print("    建议用 --cmd-file 生成脚本, 或减少屏蔽页数。", file=sys.stderr)

    if args.cmd_file:
        build_cmd_file(pages, args.obj, args.cmd_file)
        if not args.quiet:
            print("\n[*] 已生成脚本: %s" % args.cmd_file)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
