#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MemTest86 日志坏页 -> DIMM 插槽 归属分析

背景 / 重要前提
---------------
MemTest86 报出的 Address 是 CPU 视角的**物理地址**。你无法把物理地址直接当成
"第几条内存 / 哪个插槽"，因为 Intel 内存控制器会对物理地址做
channel / rank / bank 交织（interleave）与 XOR 哈希（channel hash）：

    DRAM(chan, rank, bank, row, col)  <--  物理地址
            ^                                    |
            +------- 一个线性(异或)函数 ----------+

  * channel 选择 = 物理地址若干位的异或(parity)
  * 具体参与异或的位由 MCHBAR 的 MAD_* 寄存器决定，Intel 未公开，
    需要逆向 (见 DRAMA / TRRespass 论文) 才能精确还原。

因此本脚本做两件可靠的事：
  1) 从日志里读出真实的内存拓扑（几个通道、每条 DIMM 在哪个槽、容量/rank）。
  2) 用**线性一致性检验**判断：是否存在任何一种(低位的)二通道交织模型，
     能让所有失败地址都落在同一个通道上。
       - 若存在      -> 故障大概率集中在一条 DIMM 上（给出掩码与槽位）
       - 若不存在    -> 从地址上无法归因到单条 DIMM（两条都坏 / 内存控制器问题）

用法
----
  python3 memtest86_dimm_map.py MemTest86-*.log
  python3 memtest86_dimm_map.py MemTest86-*.log --verbose

确定性定位的唯一可靠办法：
  只插一条 DIMM 跑 MemTest86，再换另一条，谁出错就是谁
  （或交换两条 DIMM 的插槽，看错误地址簇是否跟着走）。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import OrderedDict

# ---------------------------------------------------------------- 正则

RE_ERR_LINE = re.compile(r"\[MEM\s*(?:ERROR|WARNING)", re.IGNORECASE)
RE_ADDR = re.compile(r"Address\s*[:=]\s*(?:0x)?([0-9A-Fa-f]+)", re.IGNORECASE)

RE_SLOT_DEV = re.compile(
    r"\[Slot\s+(\d+)\]\s*DeviceLocator:\s*([^,]*),\s*BankLocator:\s*([^,]*),"
    r"\s*Manufacturer:\s*([^,]*),\s*S/N:\s*([^,]*),\s*AssetTag:\s*([^,]*),"
    r"\s*PartNumber:\s*(.*?)\s*$"
)
RE_SLOT_HANDLE = re.compile(r"\[Slot\s+(\d+)\]\s*Handle:")

RE_MAD_INTER = re.compile(r"MAD_INTER_CH\s*=\s*([0-9A-Fa-f]+)")
RE_CH_HASH = re.compile(r"CHANNEL_HASH\s*=\s*([0-9A-Fa-f]+)")
RE_MAD_DIMM = re.compile(r"MAD_DIMM_CH(\d)\s*=\s*([0-9A-Fa-f]+)")
RE_CH_DIMM = re.compile(
    r"Ch(\d)\s+DIMM\s+L\s+(size|ranks|width|map)\s*=\s*(\S+)")
RE_TOTAL = re.compile(r"total size\s*=\s*(\d+)")
RE_MCHBAR = re.compile(r"MCHBAR\s*=\s*([0-9A-Fa-f]+)")
RE_MEM_TYPE = re.compile(r"found\s+([A-Za-z0-9 ]+?)\s+\([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\)\s+at")


# ---------------------------------------------------------------- 数据结构

class MemoryConfig:
    def __init__(self):
        self.slots: "OrderedDict[int, dict]" = OrderedDict()
        self.channels: "OrderedDict[int, dict]" = OrderedDict()
        self.mad_inter = None
        self.channel_hash = None
        self.mchbar = None
        self.total = None
        self.controller = None

    def add_slot_line(self, slot, locator, bank, vendor, sn, asset, part):
        d = self.slots.setdefault(slot, {})
        d.update(locator=locator, bank=bank, vendor=vendor,
                 sn=sn, asset=asset, part=part)

    def add_slot_handle(self, slot, size_mb, speed, form):
        d = self.slots.setdefault(slot, {})
        d.update(size_mb=size_mb, speed=speed, form=form)

    @property
    def populated(self):
        return [d for d in self.slots.values() if d.get("size_mb")]

    def channel_of_slot(self, d) -> str | None:
        m = re.search(r"Channel\s*([A-Za-z])", d.get("locator", ""))
        return m.group(1).upper() if m else None

    def dimm_for_channel(self, ch: str) -> str:
        for d in self.populated:
            if self.channel_of_slot(d) == ch:
                return d["locator"]
        return "Channel%s-?" % ch


# ---------------------------------------------------------------- 解析

def parse_log(path: str, cfg: MemoryConfig, addrs: set[int]) -> int:
    """解析单个日志；返回命中的错误行数。"""
    errs = 0
    pending_handle: dict[int, tuple] = {}
    if not os.path.isfile(path):
        print(f"[!] 跳过(不是文件): {path}", file=sys.stderr)
        return 0

    with open(path, "r", encoding="latin-1", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if RE_ERR_LINE.search(line):
                m = RE_ADDR.search(line)
                if m:
                    addrs.add(int(m.group(1), 16))
                    errs += 1
                continue

            if "[Slot " not in line:
                continue

            m = RE_SLOT_HANDLE.search(line)
            if m:
                slot = int(m.group(1))
                size = re.search(r"\bSize:\s*(\d+)", line)
                speed = re.search(r"Speed:\s*(\d+)", line)
                form = re.search(r"FormFactor:\s*(\d+)", line)
                pending_handle[slot] = (
                    int(size.group(1)) if size else None,
                    int(speed.group(1)) if speed else None,
                    int(form.group(1)) if form else None,
                )

            m = RE_SLOT_DEV.search(line)
            if m:
                slot = int(m.group(1))
                cfg.add_slot_line(slot, *m.groups()[1:])
                if slot in pending_handle:
                    cfg.add_slot_handle(slot, *pending_handle[slot])

    # ---- 内存控制器寄存器 ----
    with open(path, "r", encoding="latin-1", errors="replace") as fh:
        for line in fh:
            for rx, attr in ((RE_MAD_INTER, "mad_inter"),
                             (RE_CH_HASH, "channel_hash"),
                             (RE_MCHBAR, "mchbar")):
                m = rx.search(line)
                if m:
                    setattr(cfg, attr, m.group(1))

            m = RE_TOTAL.search(line)
            if m:
                cfg.total = int(m.group(1))

            m = RE_MAD_DIMM.search(line)
            if m:
                ch = int(m.group(1))
                cfg.channels.setdefault(ch, {})["mad_dimm"] = m.group(2)

            m = RE_CH_DIMM.search(line)
            if m:
                ch, key, val = int(m.group(1)), m.group(2), m.group(3)
                cfg.channels.setdefault(ch, {})[key] = val

            m = RE_MEM_TYPE.search(line)
            if m and cfg.controller is None:
                cfg.controller = m.group(1).strip()

    # 通道号 -> 槽位名
    for ch, info in cfg.channels.items():
        letter = "AB"[ch] if ch < 2 else str(ch)
        info["slot"] = cfg.dimm_for_channel(letter) if cfg.populated else "Channel%s-?" % letter

    return errs


# ---------------------------------------------------------------- 线性代数 (GF2)

def parity(x: int) -> int:
    return bin(x).count("1") & 1


def reduce_rows(rows: list[int], width: int):
    """返回 (RREF 基 dict pivot->row, free 列列表)"""
    basis: dict[int, int] = {}
    for r in rows:
        cur = r
        for p in sorted(basis, reverse=True):
            if (cur >> p) & 1:
                cur ^= basis[p]
        if cur:
            p = cur.bit_length() - 1
            for q in list(basis):
                if (basis[q] >> p) & 1:
                    basis[q] ^= cur
            basis[p] = cur
    free = [c for c in range(width) if c not in basis]
    return basis, free


def kernel_basis(basis: dict[int, int], free: list[int], width: int) -> list[int]:
    out = []
    for f in free:
        v = 1 << f
        for p, row in basis.items():
            if (row >> f) & 1:
                v |= 1 << p
        out.append(v)
    return out


def simplest_kernel_masks(kvecs: list[int], top_bit_limit: int = 34,
                          max_vecs: int = 18) -> list[int]:
    """从核空间基中枚举出『最简单』(位数少、位低)的恒定掩码。

    核空间的单个基向量往往含很高位; 需要做异或组合才能得到物理上
    可能的低位掩码 (如 通道选择函数)。
    """
    vecs = []
    for v in kvecs:
        if not v or v.bit_length() - 1 > top_bit_limit:
            continue
        # 丢弃只含单个高位(>=32)的平凡常量
        if v & (v - 1) == 0 and v.bit_length() - 1 >= 32:
            continue
        vecs.append(v)
    if len(vecs) > max_vecs:
        vecs = sorted(vecs, key=lambda v: v.bit_length())[:max_vecs]

    out: set[int] = set()
    n = len(vecs)
    for r in range(1, 1 << n):
        v = 0
        rr, i = r, 0
        while rr:
            if rr & 1:
                v ^= vecs[i]
            rr >>= 1
            i += 1
        out.add(v)
    return sorted(out, key=lambda v: (bin(v).count("1"), v.bit_length()))


def bits_of(v: int) -> list[int]:
    return [i for i in range(v.bit_length()) if (v >> i) & 1]


# ---------------------------------------------------------------- 通道模型

def model_channel(addr: int, mask: int) -> int:
    return parity(addr & mask)


def score_model(addrs: list[int], mask: int):
    c0 = sum(1 for a in addrs if model_channel(a, mask) == 0)
    c1 = len(addrs) - c0
    purity = max(c0, c1) / len(addrs)
    return purity, c0, c1


def plausible_masks(lo: int = 6, hi: int = 16, max_bits: int = 3):
    """低位、少量地址位参与的物理上可能的通道掩码"""
    masks = []
    bits = list(range(lo, hi + 1))
    import itertools
    for k in range(1, max_bits + 1):
        for combo in itertools.combinations(bits, k):
            m = 0
            for b in combo:
                m |= 1 << b
            masks.append(m)
    return masks


# ---------------------------------------------------------------- 输出

def fmt_bytes(n: int) -> str:
    return "%.0f GiB" % (n / 1024 ** 3)


def print_topology(cfg: MemoryConfig, files: list[str]):
    print("=" * 72)
    print("内存拓扑 (来自 MemTest86 日志)")
    print("=" * 72)
    print("  控制器       : %s" % (cfg.controller or "?"))
    if cfg.mchbar:
        print("  MCHBAR       : 0x%s" % cfg.mchbar)
    if cfg.total:
        print("  总容量       : %d bytes (%s)" % (cfg.total, fmt_bytes(cfg.total)))
    if cfg.mad_inter:
        print("  MAD_INTER_CH : 0x%s   (bit16=1 -> 通道交织开启)" % cfg.mad_inter)
    if cfg.channel_hash:
        print("  CHANNEL_HASH : 0x%s   (通道哈希寄存器, 算法未公开)" % cfg.channel_hash)
    print()
    print("  通道配置:")
    for ch, info in sorted(cfg.channels.items()):
        size = info.get("size")
        ranks = info.get("ranks")
        width = info.get("width")
        print("    通道%s (MAD_DIMM_CH%d=0x%s): %s%s%s  槽位 %s"
              % ("AB"[ch] if ch < 2 else ch, ch, info.get("mad_dimm", "?"),
                 ("%s " % fmt_bytes(int(size))) if size else "",
                 ("ranks=%s " % ranks) if ranks else "",
                 ("width=%s" % width) if width else "",
                 info.get("slot", "?")))
    print()
    print("  SMBIOS 已插内存条:")
    for slot, d in cfg.slots.items():
        if not d.get("size_mb"):
            continue
        print("    [Slot %s] %-18s %6d MB  %-20s %s"
              % (slot, d.get("locator", "?"), d["size_mb"],
                 d.get("part", ""), d.get("bank", "")))
    print()


def print_verdict(cfg, addrs, pages, topk=5):
    print("=" * 72)
    print("坏页 -> 通道/DIMM 归属分析")
    print("=" * 72)
    print("  唯一物理地址 %d 个, 归并为 %d 个 4KB 页" % (len(addrs), len(pages)))
    print()

    # ---- 1) 低位候选通道模型的纯度 ----
    masks = plausible_masks(lo=6, hi=16, max_bits=3)
    scored = []
    for m in masks:
        p, c0, c1 = score_model(addrs, m)
        scored.append((p, m, c0, c1))
    scored.sort(reverse=True)

    print("  低位(6..16)候选通道掩码的『单通道纯度』:")
    for p, m, c0, c1 in scored[:topk]:
        print("    mask=0x%-8X bits=%-18s 通道0=%4d 通道1=%4d  纯度=%.1f%%"
              % (m, bits_of(m), c0, c1, p * 100))
    best_p, best_m, _, _ = scored[0]
    print()

    # ---- 2) 线性一致性(GF2 kernel) ----
    width = max(addr.bit_length() for addr in addrs) + 1
    base = addrs[0]
    rows = [a ^ base for a in addrs[1:]]
    basis, free = reduce_rows(rows, width)
    kvecs = kernel_basis(basis, free, width)
    candidates = [v for v in simplest_kernel_masks(kvecs)
                  if v and any(b >= 6 for b in bits_of(v))]
    print("  线性一致性检验 (在全部失败地址上取值恒定的异或掩码):")
    if candidates:
        shown = candidates[:topk]
        for v in shown:
            vals = sorted({model_channel(a, v) for a in addrs})
            c0 = sum(1 for a in addrs if model_channel(a, v) == 0)
            print("    mask=0x%-10X bits=%-26s 恒定值=%s (通道0=%d,通道1=%d)"
                  % (v, bits_of(v), vals, c0, len(addrs) - c0))
        print("    说明: 真正的『通道选择函数』必然是上面之一;")
        print("          若最简掩码仍涉及 bit>=21 (>=2MB 粒度), 则不符合常规 Intel 交织,")
        print("          即不存在可解释为通道选择的低位低复杂度掩码。")
    else:
        print("    (无)")


def print_conclusion(cfg, addrs):
    print()
    print("  结论:")

    if len(cfg.channels) < 2 or not cfg.populated:
        print("    [-] 未解析到 2 个通道的插槽信息，无法判断。")
        return

    # 是否存在"低位掩码"让所有失败落在同一通道
    masks = plausible_masks(6, 16, 3)
    best = max(score_model(addrs, m)[0] for m in masks)
    # 是否所有失败地址在任何低位线性函数下都同通道
    low_consistent = False
    for m in plausible_masks(6, 14, 4):
        if score_model(addrs, m)[0] == 1.0:
            low_consistent = True
            break

    if best >= 0.98:
        print("    [+] 存在掩码(见上)使 %.1f%% 的坏页落在同一通道。" % (best * 100))
        print("        推测故障集中在单一通道的 DIMM 上，请对照上面的『通道配置』定位槽位。")
    else:
        print("    [x] 在 256B~64KB 粒度内的任何二通道交织模型下，")
        print("        两个通道都命中坏页 (最高纯度仅 %.1f%%)。" % (best * 100))
        print("        即：**无法**把故障归因到单条 DIMM。可能原因：")
        print("          - 两条 DIMM 都有问题")
        print("          - 内存控制器/IMC、主板走线或供电问题（地址簇跨通道）")
        print("          - 超频/时序不稳（本机 3000 条只跑 2133，暂不算超频）")
        print()
        print("    确定性定位方法（唯一可靠）:")
        print("      1) 只插 ChannelA 那条 DIMM，跑 MemTest86 记录是否报错；")
        print("      2) 换成只插 ChannelB 那条，再跑一次；")
        print("      3) 谁报错就是谁；若单条都不报错，则是双条同时工作时的时序/IMC 问题。")


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="从 MemTest86 日志分析坏页与 DIMM 插槽的归属（含不可判定性判断）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("logs", nargs="+", help="MemTest86 日志文件")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="打印更多候选掩码")
    args = ap.parse_args(argv)

    cfg = MemoryConfig()
    addrs: set[int] = set()
    files = []
    total_errs = 0
    for p in args.logs:
        n = parse_log(p, cfg, addrs)
        if n:
            files.append(p)
            total_errs += n

    if not addrs:
        print("[!] 没有从日志中解析到 [MEM ERROR]/[MEM WARNING] 的地址。", file=sys.stderr)
        return 2

    addrs_l = sorted(addrs)
    pages = sorted({a >> 12 for a in addrs})

    print_topology(cfg, files)
    print("  错误行数: %d   唯一地址: %d   唯一 4KB 页: %d"
          % (total_errs, len(addrs), len(pages)))
    print()
    print_verdict(cfg, addrs_l, pages, topk=40 if args.verbose else 5)
    print_conclusion(cfg, addrs_l)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
