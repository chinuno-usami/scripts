#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Webcam Motion Receiver — offline patch tool.

把 Webcam Motion Receiver 对 webcammotioncapture.info 的 4 个 HTTP 请求断开，
使其可以纯本地运行。只依赖标准库（Python 3.7+）。

用法（--src/--out 放在子命令前后都行）：
    python3 wmcr_patch.py info
    python3 wmcr_patch.py check    [--src Mocap] [--out out_patched]
    python3 wmcr_patch.py apply    [--strict] [--dry-run] [--force]
    python3 wmcr_patch.py verify   [--strict]
    python3 wmcr_patch.py revert   [--src Mocap] [--out out_patched]
    python3 wmcr_patch.py selftest [--strict]
    python3 wmcr_patch.py audit    [--script out/script.json]
    python3 wmcr_patch.py strings  --grep webcammotioncapture
    python3 wmcr_patch.py diff original.dll patched.dll

补丁分两类：
  1) GameAssembly.dll   —— 按 RVA 打；工具自己解析 PE 段，做 RVA→文件偏移映射
  2) global-metadata.dat —— GUPS 加密字符串 blob，按“明文→明文”描述，
     工具用 plain[i] = enc[i] ^ (i & 0xFF) ^ 0xAA 自动编码/解码
"""

import argparse
import bisect
import hashlib
import os
import shutil
import struct
import sys

# ---------------------------------------------------------------- 文件位置

GA_DLL = "GameAssembly.dll"
META = "global-metadata.dat"

FILEMAP = {
    GA_DLL: "GameAssembly.dll",
    META: "Webcam Motion Receiver_Data/il2cpp_data/Metadata/global-metadata.dat",
}

# ---------------------------------------------------------------- 补丁表

# (RVA, 原始字节, 新字节, 说明, 是否可选)
# 这是“当前构建”的内置表；换版本后可用 `locate --write-table` 生成新表，
# 再用 --table FILE 加载（见 dll_patches()）。
DEFAULT_DLL_PATCHES = [
    (0x6ACF10, "48895C", "33C0C3", False,
     "fi.fd.MoveNext -> return false      (POST /program_info.php)"),
    (0x6AC210, "48894C", "33C0C3", False,
     "fi.fc.MoveNext -> return false      (POST /check_unique_connection2.php)"),
    (0x6ADCB0, "48894C", "33C0C3", False,
     "fi.fe.MoveNext -> return false      (POST /login3.php)"),
    (0x6B02A8, "E853E41C00", "9090909090", False,
     "fl..ctor : nop Start<SetupOSCMessages>  (get_controllers.php)"),
    (0x6B1020, "E8DBD61C00", "9090909090", False,
     "fl.dld   : nop Start<SetupOSCMessages>  (get_controllers.php)"),
    (0x614EE0, "0F84F70000", "E9F8000000", False,
     "fi.Update : je -> jmp                (skip unique-connection polling)"),
    # --strict 才打：连 URL 都不再拼接（fi.Start 里的两处 call）
    (0x613ED5, "E8864A0200", "9090909090", True,
     "fi.Start : nop call fi.dju           (no program_info URL built)"),
    (0x613EE3, "E828FA2502", "9090909090", True,
     "fi.Start : nop StartCoroutine_Auto   (no coroutine object allocated)"),
]

# GUPS 加密字符串：这些 URL 是明文内容，跨版本稳定（服务端路径不会随便改）。
# 工具不用硬编码偏移，而是在 metadata 里搜它们的“加密形态”来定位：
#   enc[j+k] = plain[k] ^ (((p+k) & 0xFF) ^ 0xAA)   (p = 该字符串在 blob 内的偏移)
# 因为 key 周期是 256，只需枚举 q = p & 0xFF（256 次 find）即可唯一定位。
GUPS_BLOB_OFF = 0x6D4A38          # 本构建已知值（仅作 fallback / 参考）
GUPS_BLOB_LEN = 0x335EB
GUPS_URL_OFF = 0x1DCCC
BASE_URL_ORIG = "https://webcammotioncapture.info/"

META_TARGETS = [
    ("base_url", "https://webcammotioncapture.info/"),
    ("get_controllers",
     "https://webcammotioncapture.info/WMCTool/vrc-hand-tracker/get_controllers.php"),
]

# 定位用：这些字符串的 accessor 使用者，就是“构造 URL / 发请求”的代码
LOCATE_TARGETS = [
    ("program_info", "program_info.php"),
    ("login3", "login3.php"),
    ("check_unique", "check_unique_connection2.php"),
    ("get_controllers",
     "https://webcammotioncapture.info/WMCTool/vrc-hand-tracker/get_controllers.php"),
]

LOOPBACK = "http://127.0.0.1:1/"


def loopback_for(orig):
    """等长替换成本地地址，避免任何出网/DNS。"""
    if len(orig) <= len(LOOPBACK):
        return orig
    return LOOPBACK + "x" * (len(orig) - len(LOOPBACK))

# fallback：自动定位失败时才用（当前构建）
META_PATCHES = [
    (GUPS_BLOB_OFF + GUPS_URL_OFF, BASE_URL_ORIG, loopback_for(BASE_URL_ORIG), False,
     "base URL literal -> loopback (fallback offset)"),
]

# 反查用的 URL 访问器（RVA），来自 GuPS 字符串表
URL_ACCESSORS = {
    0x51F020: ("a.eyy", "https://webcammotioncapture.info/"),
    0x4F4780: ("a.dkq", "program_info.php"),
    0x4F8420: ("a.dof", "login3.php"),
    0x5191C0: ("a.eti", "check_unique_connection2.php"),
    0x534C40: ("a.ftn", "https://webcammotioncapture.info/WMCTool/vrc-hand-tracker/get_controllers.php"),
}

# 已改动的入口方法 RVA（audit 用来判断 xref 是否落在死代码里）
TOUCHED = {
    0x6ACF10: "fi.fd$$MoveNext",
    0x6AC210: "fi.fc$$MoveNext",
    0x6ADCB0: "fi.fe$$MoveNext",
    0x614EE0: "fi$$Update",
    0x6B02A8: "fl$$.ctor",
    0x6B1020: "fl$$dld",
    0x613ED5: "fi$$Start",
    0x613EE3: "fi$$Start",
}


_TABLE = None            # 由 --table 载入
_TABLE_PATH = None


def dll_patches():
    """当前生效的 DLL 补丁表（内置 or --table 载入）。"""
    return _TABLE if _TABLE is not None else DEFAULT_DLL_PATCHES


def load_table(path):
    """载入 locate --write-table 生成的 JSON 表。"""
    global _TABLE, _TABLE_PATH
    import json
    d = json.load(open(path))
    rows = []
    for e in d.get("dll", []):
        rows.append((int(e["rva"]), e["orig"], e["new"], bool(e.get("optional")),
                     e.get("desc", "")))
    if not rows:
        raise SystemExit("table %s has no dll entries" % path)
    _TABLE, _TABLE_PATH = rows, path
    print("loaded patch table: %s (%d dll entries)" % (path, len(rows)))
    return rows


def h(b):
    return bytes.fromhex(b) if isinstance(b, str) else b


def sha256(data):
    return hashlib.sha256(bytes(data)).hexdigest()


# ---------------------------------------------------------------- PE 辅助


def pe_info(data):
    """返回 (image_base, [(name, va, vsize, rawptr, rawsize), ...])"""
    if data[:2] != b"MZ":
        raise ValueError("not a PE file (missing MZ)")
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\0\0":
        raise ValueError("not a PE file (missing PE signature)")
    nsec = struct.unpack_from("<H", data, e_lfanew + 6)[0]
    optsz = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    opt = e_lfanew + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic == 0x20B:
        image_base = struct.unpack_from("<Q", data, opt + 24)[0]
    elif magic == 0x10B:
        image_base = struct.unpack_from("<I", data, opt + 28)[0]
    else:
        raise ValueError("unsupported optional header magic 0x%X" % magic)
    sec = opt + optsz
    sections = []
    for i in range(nsec):
        o = sec + i * 40
        name = data[o:o + 8].rstrip(b"\0").decode("latin1")
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
        sections.append((name, va, vsize, rawptr, rawsize))
    return image_base, sections


def rva_to_off(sections, rva):
    for name, va, vsize, rawptr, rawsize in sections:
        if va <= rva < va + max(vsize, rawsize):
            delta = rva - va
            if delta >= rawsize:
                raise ValueError("RVA 0x%X is in the uninitialised tail of %s" % (rva, name))
            return rawptr + delta
    raise ValueError("RVA 0x%X is not inside any section" % rva)


def off_to_rva(sections, off):
    for name, va, vsize, rawptr, rawsize in sections:
        if rawptr <= off < rawptr + rawsize:
            return va + (off - rawptr)
    return None


# ---------------------------------------------------------------- GUPS


def gups_decode(data):
    blob = bytearray(data[GUPS_BLOB_OFF:GUPS_BLOB_OFF + GUPS_BLOB_LEN])
    if len(blob) != GUPS_BLOB_LEN:
        raise ValueError("metadata too short for the GUPS blob")
    for i in range(len(blob)):
        blob[i] ^= (i & 0xFF) ^ 0xAA
    return bytes(blob)


def gups_encode(plain, abs_off):
    """把明文按 blob 内索引重新加密（写回 metadata 用）。"""
    if isinstance(plain, str):
        plain = plain.encode()
    idx0 = abs_off - GUPS_BLOB_OFF
    return bytes(plain[k] ^ ((idx0 + k) & 0xFF) ^ 0xAA for k in range(len(plain)))


def _enc(plain, q):
    return bytes(plain[k] ^ ((q + k) & 0xFF) ^ 0xAA for k in range(len(plain)))


def find_encrypted(data, plain):
    """在 metadata 里找 plain 的加密形态，返回 [(abs_off, q)]。

    不需要知道 blob 起点，也不依赖 RVA：对 256 个可能的 q 各做一次子串搜索，
    长度 >= 16 的模式误命中概率可以忽略。
    """
    hits = []
    for q in range(256):
        pat = _enc(plain, q)
        pos = 0
        while True:
            j = data.find(pat, pos)
            if j < 0:
                break
            hits.append((j, q))
            pos = j + 1
    return hits


def _dec_at(data, pos, q, n):
    """解密以 pos 为参考点、q 为起点相位的 n 个字节（q 平移）。"""
    return bytes(data[pos + k] ^ (((q + k) & 0xFF) ^ 0xAA) for k in range(n))


def _is_link_text(data, abs_off, q, ln):
    """判断命中处在源码里是 UI 文案（<link="..."> 里内嵌）而不是独立字面量。

    这类字符串只会被 TMP 富文本渲染、用户点击后交给 Application.OpenURL，
    程序自身不会发请求，因此保留原样（帮助链接仍可用）。
    """
    pre = _dec_at(data, abs_off - 16, (q - 16) & 0xFF, 16)
    post = _dec_at(data, abs_off + ln, q, 16)
    if pre.rstrip(b"\x00").endswith(b'"'):
        return True
    return post.startswith((b"manual.php", b"ja/manual.php", b"index.php",
                            b"ja/index.php", b"WMCTool/unitypackage"))


def resolve_meta(data):
    """返回 metadata 侧补丁：[(abs_off, q, orig_bytes, new_bytes, desc)]

    长串优先，并丢弃与已选区间重叠的短串命中（例如 get_controllers 的完整 URL
    本身就包含 base URL）。
    """
    out, used = [], []
    for name, text in sorted(META_TARGETS, key=lambda t: -len(t[1])):
        orig = text.encode()
        new = loopback_for(text).encode()
        hits = find_encrypted(data, orig) or find_encrypted(data, new)  # 后者=已打过补丁
        for abs_off, q in hits:
            if any(abs_off < b and a < abs_off + len(orig) for a, b in used):
                continue
            if _is_link_text(data, abs_off, q, len(orig)):
                continue
            used.append((abs_off, abs_off + len(orig)))
            out.append((abs_off, q, orig, new, "%s -> loopback" % name))
    return sorted(out)


def is_loopback(buf):
    return bytes(buf).startswith(LOOPBACK.encode())


# ---------------------------------------------------------------- 状态检查


def classify_dll(data):
    """[(rva, file_off, state, desc, cur, optional)]"""
    _, sections = pe_info(data)
    rows = []
    for rva, orig, new, optional, desc in dll_patches():
        off = rva_to_off(sections, rva)
        orig, new = h(orig), h(new)
        cur = bytes(data[off:off + len(new)])
        if cur == new:
            state = "patched"
        elif cur[:len(orig)] == orig:
            state = "original"
        else:
            state = "unknown"
        rows.append((rva, off, state, desc, cur, optional))
    return rows


def _fallback_meta_patches():
    """自动定位失败时的兜底：使用本构建已知偏移。"""
    out = []
    for abs_off, orig, new, optional, desc in META_PATCHES:
        orig_b = orig.encode() if isinstance(orig, str) else orig
        new_b = new.encode() if isinstance(new, str) else new
        q = (abs_off - GUPS_BLOB_OFF) & 0xFF
        out.append((abs_off, q, orig_b, new_b, desc))
    return out


def classify_meta(data):
    pats = resolve_meta(data) or _fallback_meta_patches()
    rows = []
    for abs_off, q, orig, new, desc in pats:
        cur = bytes(data[abs_off:abs_off + len(orig)])
        if cur == _enc(new, q):
            state = "patched"
        elif cur == _enc(orig, q):
            state = "original"
        else:
            state = "unknown"
        rows.append((abs_off, (abs_off - GUPS_BLOB_OFF) & 0xFFFFFF, state, desc, cur, False))
    return rows


def apply_dll(data, force=False, dry_run=False, strict=False):
    _, sections = pe_info(data)
    n = 0
    for rva, off, state, desc, cur, optional in classify_dll(data):
        if optional and not strict:
            continue
        orig, new = None, None
        for prva, porig, pnew, popt, _pd in dll_patches():
            if prva == rva:
                orig, new = h(porig), h(pnew)
        if state == "patched":
            print("  skip   RVA 0x%-8X %s" % (rva, desc))
            continue
        if state == "unknown" and not force:
            raise SystemExit(
                "ERROR: bytes at RVA 0x%X are neither original nor patched (%s).\n"
                "       Different game build? use --force to overwrite anyway."
                % (rva, cur.hex(" ")))
        if not dry_run:
            data[off:off + len(new)] = new
        n += 1
        print("  %-6s RVA 0x%-8X file+0x%-8X %s"
              % ("patch" if not dry_run else "would", rva, off, desc))
    return n


def apply_meta(data, force=False, dry_run=False, strict=False):
    pats = resolve_meta(data)
    if not pats:
        print("  WARN: could not auto-locate the GUPS strings, using fallback offsets")
        pats = _fallback_meta_patches()
    n = 0
    for abs_off, q, orig, new, desc in pats:
        cur = bytes(data[abs_off:abs_off + len(orig)])
        if cur == _enc(new, q):
            print("  skip   @0x%-8X %s" % (abs_off, desc))
            continue
        if cur != _enc(orig, q) and not force:
            raise SystemExit(
                "ERROR: metadata @0x%X neither original nor patched.\n"
                "       Use --force to overwrite anyway." % abs_off)
        if not dry_run:
            data[abs_off:abs_off + len(new)] = _enc(new, q)
        n += 1
        print("  %-6s @0x%-8X %s -> %r"
              % ("patch" if not dry_run else "would", abs_off, desc, new.decode("latin1")))
    return n


# ---------------------------------------------------------------- 子命令


def cmd_info(args):
    print("DLL patches (GameAssembly.dll, by RVA; this build: file_off = RVA - 0x1800):")
    for rva, orig, new, optional, desc in dll_patches():
        tag = "[strict]" if optional else "[always]"
        print("  %-8s RVA 0x%-8X %-16s -> %-16s %s"
              % (tag, rva, h(orig).hex(" "), h(new).hex(" "), desc))
    print("\nMetadata patches (global-metadata.dat, GUPS blob @0x%X):" % GUPS_BLOB_OFF)
    for abs_off, orig, new, optional, desc in META_PATCHES:
        print("  [always] @0x%-8X %s" % (abs_off, desc))
        print("           %r -> %r" % (orig, new))
    print("\nNeed 'always' only for offline use; '--strict' also removes dead URL building.")
    return 0


def summary(states):
    if all(s == "patched" for s in states):
        return "PATCHED (offline)"
    if all(s == "original" for s in states):
        return "ORIGINAL (untouched)"
    return "MIXED -> %s" % ", ".join(sorted(set(states)))


def resolve(args):
    return {
        GA_DLL: (os.path.join(args.src, FILEMAP[GA_DLL]),
                 os.path.join(args.out, GA_DLL)),
        META: (os.path.join(args.src, FILEMAP[META]),
               os.path.join(args.out, FILEMAP[META])),
    }


def cmd_check(args):
    paths = resolve(args)
    rc = 0
    for key in (GA_DLL, META):
        p = paths[key][0]
        if not os.path.exists(p):
            print("%s: MISSING %s" % (key, p))
            rc = 2
            continue
        data = open(p, "rb").read()
        print("%s  %s  %d bytes  sha256 %s" % (key, p, len(data), sha256(data)[:16]))
        try:
            rows = classify_dll(data) if key == GA_DLL else classify_meta(data)
        except ValueError as e:
            print("  ERROR: %s" % e)
            rc = 2
            continue
        for row in rows:
            addr, state, desc, optional = row[0], row[2], row[3], row[5]
            fmt = "  RVA 0x%-8X file+0x%-8X" if key == GA_DLL else "  @0x%-8X blob+0x%-6X"
            args2 = (addr, row[1]) if key == GA_DLL else (addr, row[1])
            print((fmt + " %-9s %s%s") % (args2 + (state, desc, "  [strict]" if optional else "")))
        required = [r[2] for r in rows if not r[5]]
        print("  => %s%s" % (summary(required),
                             "  (strict patches: %s)"
                             % summary([r[2] for r in rows if r[5]]) if any(r[5] for r in rows) else ""))
        print()
    return rc


def cmd_apply(args):
    paths = resolve(args)
    total = 0
    keys = (META,) if getattr(args, "meta_only", False) else (GA_DLL, META)
    for key in keys:
        src_path, out_path = paths[key]
        if not os.path.exists(src_path):
            print("%s: MISSING %s -- skipped" % (key, src_path))
            continue
        data = bytearray(open(src_path, "rb").read())
        print("%s:" % key)
        try:
            fn = apply_dll if key == GA_DLL else apply_meta
            total += fn(data, args.force, args.dry_run, args.strict)
        except ValueError as e:
            print("  ERROR: %s" % e)
            continue
        if args.dry_run:
            continue
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        print("  -> %s  sha256 %s" % (out_path, sha256(data)[:16]))
    print("\n%d patch(es) applied%s%s."
          % (total, " [strict]" if args.strict else "", " (dry-run)" if args.dry_run else ""))
    if not args.dry_run and total:
        print("\nCopy into the game folder (back it up first):")
        for key in keys:
            if os.path.exists(paths[key][1]):
                print("  %s\n    -> <game>/%s" % (paths[key][1], FILEMAP[key]))
    return 0


def cmd_verify(args):
    paths = resolve(args)
    bad = 0
    keys = (META,) if getattr(args, "meta_only", False) else (GA_DLL, META)
    for key in keys:
        p = paths[key][1]
        if not os.path.exists(p):
            print("%s: MISSING %s" % (key, p))
            bad += 1
            continue
        data = open(p, "rb").read()
        rows = classify_dll(data) if key == GA_DLL else classify_meta(data)
        want = [r for r in rows if args.strict or not r[5]]
        for row in want:
            if row[2] != "patched":
                print("  FAIL  RVA/@0x%X -> %s  (%s)" % (row[0], row[2], row[3]))
                bad += 1
        print("%s: %s  sha256 %s"
              % (key, "OK" if all(r[2] == "patched" for r in want) else "FAIL", sha256(data)[:16]))
    print("\nRESULT: %s" % ("PASS" if bad == 0 else "FAIL (%d)" % bad))
    return 0 if bad == 0 else 1


def cmd_revert(args):
    paths = resolve(args)
    for key in (GA_DLL, META):
        src_path, out_path = paths[key]
        if os.path.exists(out_path) and os.path.exists(src_path):
            shutil.copyfile(src_path, out_path)
            print("  restored %s  <- %s" % (out_path, src_path))
    return 0


def cmd_selftest(args):
    """内存里跑 apply -> verify -> 幂等 -> revert，不落盘。"""
    paths = resolve(args)
    ok = True
    for key in (GA_DLL, META):
        src_path = paths[key][0]
        if not os.path.exists(src_path):
            print("%s: MISSING, skipped" % key)
            continue
        raw = open(src_path, "rb").read()
        buf = bytearray(raw)
        fn = apply_dll if key == GA_DLL else apply_meta
        cls = classify_dll if key == GA_DLL else classify_meta
        print("%s: %d bytes, sha256 %s" % (key, len(raw), sha256(raw)[:16]))
        try:
            n1 = fn(buf, False, False, args.strict)
        except (ValueError, SystemExit) as e:
            print("  FAIL apply: %s" % e)
            ok = False
            continue
        want = [r for r in cls(buf) if args.strict or not r[5]]
        if any(r[2] != "patched" for r in want):
            print("  FAIL verify after apply")
            ok = False
        n2 = fn(buf, False, False, args.strict)
        if n2 != 0:
            print("  FAIL not idempotent (%d re-applied)" % n2)
            ok = False
        rev = bytearray(buf)
        _, sections = pe_info(rev) if key == GA_DLL else (None, None)
        if key == GA_DLL:
            for rva, orig, _new, optional, _d in dll_patches():
                o = rva_to_off(sections, rva)
                rev[o:o + len(h(orig))] = h(orig)
        else:
            for abs_off, q, orig, _new, _d in (resolve_meta(raw) or _fallback_meta_patches()):
                rev[abs_off:abs_off + len(orig)] = _enc(orig, q)
        same = bytes(rev) == raw
        if not same:
            print("  FAIL revert is not byte-identical")
            ok = False
        print("  patches=%d  sha256 patched=%s  revert_ok=%s" % (n1, sha256(buf)[:16], same))
    print("\nSELFTEST: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def cmd_strings(args):
    src = os.path.join(args.src, FILEMAP[META])
    if not os.path.exists(src):
        print("MISSING %s" % src)
        return 2
    plain = gups_decode(open(src, "rb").read())
    if not args.grep:
        with open(args.dump, "wb") as f:
            f.write(plain)
        print("wrote %s (%d bytes)" % (args.dump, len(plain)))
        return 0
    needle = args.grep.encode()
    hits, pos = 0, 0
    while True:
        k = plain.find(needle, pos)
        if k < 0:
            break
        s = k
        while s > 0 and 0x20 <= plain[s - 1] < 0x7F:
            s -= 1
        e = k
        while e < len(plain) and 0x20 <= plain[e] < 0x7F:
            e += 1
        print("blob+0x%-8X %r" % (s, plain[s:e].decode("latin1")))
        hits += 1
        pos = k + len(needle)
    print("%d hit(s)" % hits)
    return 0


def _call_sites(data, sections, targets):
    """扫描 E8 rel32，返回 {target_rva: [call_site_rva, ...]}（targets 为 RVA 集合）"""
    out = {}
    for name, va, vsize, rawptr, rawsize in sections:
        if name not in (".text", "il2cpp"):
            continue
        pos, end = rawptr, rawptr + rawsize
        while True:
            pos = data.find(b"\xe8", pos, end)
            if pos < 0:
                break
            rel = struct.unpack_from("<i", data, pos + 1)[0]
            tgt = off_to_rva(sections, pos + 5) + rel
            if tgt in targets:
                out.setdefault(tgt, []).append(off_to_rva(sections, pos))
            pos += 1
    return out


def _find_accessors(data, sections, q, cnt):
    """找 accessor 里 `mov edx, <blob 内偏移>` + `mov r8d, <字符串长度>` 的组合。"""
    hits = []
    for name, va, vsize, rawptr, rawsize in sections:
        if name not in (".text", "il2cpp"):
            continue
        pos, end = rawptr, rawptr + rawsize
        while True:
            pos = data.find(b"\xba", pos, end)
            if pos < 0 or pos + 5 > end:
                break
            imm = struct.unpack_from("<I", data, pos + 1)[0]
            if (imm & 0xFF) == q and imm < 0x2000000:
                lo, hi = max(rawptr, pos - 64), min(end, pos + 96)
                win = data[lo:hi]
                k = win.find(b"\x41\xb8")
                while k >= 0 and k + 6 <= len(win):
                    if struct.unpack_from("<I", win, k + 2)[0] == cnt:
                        hits.append(off_to_rva(sections, pos))
                        break
                    k = win.find(b"\x41\xb8", k + 1)
            pos += 1
    return hits


def _method_end(names_sorted, start):
    i = bisect.bisect_right(names_sorted, start)
    return names_sorted[i] if i < len(names_sorted) else start + 0x4000


def _rip_refs(data, sections, start_rva, end_rva):
    """方法区间内所有 rip-relative 引用的目标 RVA（近似 x86 解码）。"""
    try:
        off = rva_to_off(sections, start_rva)
        end = rva_to_off(sections, end_rva - 1) + 1
    except ValueError:
        return []
    out = []
    i = off
    while i < end - 7:
        if data[i] in (0x48, 0x49, 0x4C, 0x4D) and data[i + 1] in (0x8B, 0x8D):
            if (data[i + 2] & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", data, i + 3)[0]
                out.append(off_to_rva(sections, i + 7) + disp)
                i += 7
                continue
        i += 1
    return out


def _calls_in(data, sections, start_rva, end_rva):
    try:
        off = rva_to_off(sections, start_rva)
        end = rva_to_off(sections, end_rva - 1) + 1
    except ValueError:
        return []
    out = []
    pos = off
    while True:
        pos = data.find(b"\xe8", pos, end)
        if pos < 0:
            break
        rel = struct.unpack_from("<i", data, pos + 1)[0]
        out.append(off_to_rva(sections, pos + 5) + rel)
        pos += 1
    return out


def _stores_rax_off(data, sections, start_rva, end_rva):
    """方法内 `mov %rax, disp32(%reg)` 的 disp32 集合。"""
    try:
        off = rva_to_off(sections, start_rva)
        end = rva_to_off(sections, end_rva - 1) + 1
    except ValueError:
        return set()
    out = set()
    i = off
    while i < end - 7:
        if data[i] == 0x48 and data[i + 1] == 0x89 and (data[i + 2] & 0xC0) == 0x80 \
                and (data[i + 2] & 0x38) == 0x00:
            out.add(struct.unpack_from("<I", data, i + 3)[0])
            i += 7
            continue
        i += 1
    return out


def _reads_off(data, sections, start_rva, end_rva, want):
    try:
        off = rva_to_off(sections, start_rva)
        end = rva_to_off(sections, end_rva - 1) + 1
    except ValueError:
        return False
    i = off
    while i < end - 7:
        if data[i] == 0x48 and data[i + 1] == 0x8B and (data[i + 2] & 0xC0) == 0x80:
            if struct.unpack_from("<I", data, i + 3)[0] == want:
                return True
            i += 7
            continue
        i += 1
    return False


def _derive_table(args):
    """从 metadata 字符串 + 反汇编，自动推导出新版本的 DLL 补丁表。"""
    dpath = os.path.join(args.src, FILEMAP[GA_DLL])
    mpath = os.path.join(args.src, FILEMAP[META])
    data = open(dpath, "rb").read()
    meta = open(mpath, "rb").read()
    image_base, sections = pe_info(data)
    names = _load_script(args.script)
    slots = _load_slots(args.script)
    if not names:
        raise SystemExit("need --script out/script.json (run Il2CppDumper first)")
    sorted_starts = sorted(names)

    def mstart(rva):
        return _method_start(names, rva)

    def mname(rva):
        return (names.get(mstart(rva)) or "?").replace("$$", "::")

    # 1) 谁在调用 UnityWebRequest.Post / SendWebRequest
    web = {}
    for rva, nm in names.items():
        if nm and nm.startswith("UnityEngine.Networking.UnityWebRequest$$") \
                and nm.endswith(("$$Post", "$$SendWebRequest")):
            web[rva] = nm
    post_sites = _call_sites(data, sections, set(web))
    post_methods = set()
    for sites in post_sites.values():
        for c in sites:
            post_methods.add(mstart(c))

    entries, notes = [], []

    def add(rva, new_hex, desc, optional=False):
        ln = len(h(new_hex))
        try:
            off = rva_to_off(sections, rva)
        except ValueError as e:
            notes.append("skip %s: %s" % (desc, e))
            return
        orig = data[off:off + ln].hex().upper()
        entries.append((rva, orig, new_hex, optional, desc))

    def typeinfo_of_method(rva):
        end = _method_end(sorted_starts, rva)
        out = []
        for t in _rip_refs(data, sections, rva, end):
            nm = slots.get(t)
            if nm and nm.endswith("_TypeInfo"):
                out.append(nm[:-len("_TypeInfo")])
        return out

    by_name = {}
    for a, nm in names.items():
        by_name.setdefault(nm, a)

    def body_of(caller, depth=2):
        """从 caller 出发（深度 depth 跟随内部调用），找处于 post_methods 的 MoveNext。"""
        frontier, seen = {caller}, set()
        for _ in range(depth):
            nxt = set()
            for m in frontier:
                if m in seen:
                    continue
                seen.add(m)
                for cls in typeinfo_of_method(m):
                    mv = by_name.get(cls + "$$MoveNext")
                    if mv is not None and mv in post_methods:
                        return mv
                end = _method_end(sorted_starts, m)
                for c in _calls_in(data, sections, m, end):
                    cs = mstart(c)
                    if cs in names and cs != m:
                        nxt.add(cs)
            frontier = nxt
        return None

    # 2) 逐个 URL 字符串：accessor -> caller -> 请求体 / kickoff
    for tag, text in LOCATE_TARGETS:
        orig = text.encode()
        hits = find_encrypted(meta, orig)
        if not hits:
            notes.append("cannot locate string %r" % text[:32])
            continue
        for abs_off, q in hits:
            if _is_link_text(meta, abs_off, q, len(orig)):
                continue
            for acc_hit in _find_accessors(data, sections, q, len(orig)):
                acc = mstart(acc_hit)
                sites = _call_sites(data, sections, {acc}).get(acc, [])
                for site in sites:
                    caller = mstart(site)
                    cname = names.get(caller) or ""
                    if "MoveNext" in cname:
                        # 状态机：拦它的 kickoff（AsyncVoidMethodBuilder.Start<T>）
                        cls = cname.split("$$")[0]
                        kick = [a for a, nm in names.items()
                                if nm and "AsyncVoidMethodBuilder$$Start<" in nm
                                and _slot_generic(nm) == cls]
                        if not kick:
                            notes.append("%s: no kickoff for %s" % (tag, cls))
                            continue
                        ks = _call_sites(data, sections, set(kick))
                        for tgt, ksites in ks.items():
                            for c in sorted(set(ksites)):
                                add(c, "90" * 5,
                                    "%s: nop Start<%s>  (blocks %s)"
                                    % (mname(c), cls, tag))
                        continue
                    # 普通方法：BFS 找请求体 MoveNext
                    body = body_of(caller)
                    if body is not None:
                        add(body, "33C0C3", "%s -> return false  (POST /%s)"
                            % (mname(body), tag))
                        continue
                    # 字段追踪：URL 存字段，请求体在别处读它
                    end = _method_end(sorted_starts, caller)
                    for field in sorted(_stores_rax_off(data, sections, caller, end)):
                        for pm in sorted(post_methods):
                            pe_ = _method_end(sorted_starts, pm)
                            if _reads_off(data, sections, pm, pe_, field):
                                add(pm, "33C0C3", "%s -> return false  (POST /%s, field +0x%X)"
                                    % (mname(pm), tag, field))
                                break

    # 去重 + 排序
    uniq = {}
    for rva, orig, new, optional, desc in entries:
        uniq.setdefault(rva, (rva, orig, new, optional, desc))
    rows = [uniq[r] for r in sorted(uniq)]
    return rows, notes, data, meta


def cmd_locate(args):
    """重新定位：metadata 全自动，DLL 给出候选（版本更新后跑这个）。"""
    mpath = os.path.join(args.src, FILEMAP[META])
    dpath = os.path.join(args.src, FILEMAP[GA_DLL])

    print("== metadata (auto-located, no hardcoded offsets) ==")
    meta = open(mpath, "rb").read() if os.path.exists(mpath) else None
    if meta is None:
        print("  MISSING %s" % mpath)
    else:
        pats = resolve_meta(meta)
        if not pats:
            print("  FAILED: could not locate the encrypted URL strings")
            print("  (blob layout changed? try 'strings --grep webcammotioncapture')")
        for abs_off, q, orig, new, desc in pats:
            print("  %-16s @0x%-8X q=%3d len=%d  %r"
                  % (desc.split(" ->")[0], abs_off, q, len(orig), orig.decode("latin1")[:48]))

    if getattr(args, "write_table", None):
        print("\n== deriving a new patch table ==")
        rows, notes, _d, _m = _derive_table(args)
        for rva, orig, new, optional, desc in rows:
            print("  RVA 0x%-8X %-12s -> %-12s %s"
                  % (rva, orig, new, desc))
        for n in notes:
            print("  note: %s" % n)
        if not rows:
            print("  FAILED: nothing derived")
            return 1
        import json
        out = {
            "generated_by": "wmcr_patch.py locate --write-table",
            "gameassembly_sha256": sha256(_d),
            "metadata_sha256": sha256(_m) if _m else None,
            "dll": [{"rva": rva, "orig": orig, "new": new,
                     "optional": optional, "desc": desc}
                    for rva, orig, new, optional, desc in rows],
        }
        with open(args.write_table, "w") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print("\n  wrote %s (%d entries)" % (args.write_table, len(rows)))
        print("  use it with:  --table %s" % args.write_table)
        return 0

    if not os.path.exists(dpath):
        print("\nMISSING %s" % dpath)
        return 0
    data = open(dpath, "rb").read()
    image_base, sections = pe_info(data)
    names = _load_script(args.script)
    slots = _load_slots(args.script)

    print("\n== DLL candidates (RVA may change between builds) ==")
    for abs_off, q, orig, new, desc in (resolve_meta(meta) if meta else []):
        for hit in _find_accessors(data, sections, q, len(orig)):
            a = _method_start(names, hit)
            meth = names.get(a) or _nearest(names, a)
            print("  accessor  %-16s RVA 0x%-8X  %s"
                  % (desc.split(" ->")[0], a, (meth or "?").replace("$$", "::")))
            for c in sorted(_call_sites(data, sections, {a}).get(a, [])):
                cm = names.get(c) or _nearest(names, c)
                print("            called from RVA 0x%-8X  %s"
                      % (c, (cm or "?").replace("$$", "::")))

    web = {}
    for kind in ("Post", "SendWebRequest"):
        for rva, nm in names.items():
            if nm and nm.startswith("UnityEngine.Networking.UnityWebRequest$$") and nm.endswith("$$" + kind):
                web[rva] = kind
    if web:
        xr = _call_sites(data, sections, set(web))
        callers = {}
        for tgt, sites in xr.items():
            for c in sites:
                callers.setdefault(c, set()).add(web[tgt])
        print("\n  methods calling UnityWebRequest.Post/SendWebRequest (app side):")
        seen = set()
        for c in sorted(callers):
            m = names.get(c) or _nearest(names, c) or "?"
            if m.startswith(("UnityEngine", "Unity.", "System", "WebSocketSharp", "TwitchSDK", "Unity.Resource")):
                continue
            start = _method_start(names, c)
            if start in seen:
                continue
            seen.add(start)
            print("    RVA 0x%-8X  %-46s %s"
                  % (start, m.replace("$$", "::")[:46], ",".join(sorted(callers[c]))))
    else:
        print("\n  (no script.json -> cannot resolve engine method names)")

    kick = {rva: nm for rva, nm in names.items()
            if nm and "AsyncVoidMethodBuilder$$Start<" in nm
            and not nm.split("Start<")[-1].startswith(("System", "UnityEngine", "Unity.", "Microsoft"))}
    if kick:
        xr = _call_sites(data, sections, set(kick))
        print("\n  async kickoff sites (AsyncVoidMethodBuilder.Start<appType>):")
        for tgt, sites in sorted(xr.items()):
            print("    %s" % _slot_generic(kick[tgt]))
            for c in sorted(sites):
                start = _method_start(names, c)
                m = names.get(start) or _nearest(names, start) or "?"
                print("      kickoff RVA 0x%-8X   in %s" % (c, m.replace("$$", "::")[:46]))
    return 0


def _is_nop_hex(new):
    n = len(new) // 2
    return n > 0 and new.upper() == "90" * n


def _patch_points(data, names):
    """把当前补丁表按“打在哪”分类（含未打补丁的候选点）。"""
    state = {r[0]: r[2] for r in classify_dll(data)}
    stubs, nops, branches = set(), {}, {}
    for rva, orig, new, optional, desc in dll_patches():
        m = _method_start(names, rva) if names else rva
        nu = new.upper()
        patched = state.get(rva) == "patched"
        if nu in ("33C0C3",):
            if patched:
                stubs.add(m)
        elif nu.startswith("E9"):
            if patched:
                branches.setdefault(m, []).append(rva)
        else:
            if patched:
                nops.setdefault(m, []).append(rva)
    stub_all = {_method_start(names, rva) if names else rva
                for rva, orig, new, optional, desc in dll_patches()
                if new.upper() == "33C0C3"}
    nop_all = {_method_start(names, rva) if names else rva
               for rva, orig, new, optional, desc in dll_patches()
               if _is_nop_hex(new)}
    return state, stubs, nops, branches, stub_all, nop_all


def _slot_generic(nm):
    """'Method$...AsyncVoidMethodBuilder.Start<fl.X>()' -> 'fl.X'"""
    if "Start<" not in nm:
        return None
    return nm.split("Start<")[-1].split("(")[0].rstrip(">")


def _kick_types(data, sections, method_start, sorted_starts, slots, only=None):
    out = set()
    end = _method_end(sorted_starts, method_start)
    for t in _rip_refs(data, sections, method_start, end):
        t2 = _slot_generic(slots.get(t) or "")
        if t2:
            out.add(t2)
    return out


def _post_methods(data, sections, names):
    web = {rva for rva, nm in names.items()
           if nm and nm.startswith("UnityEngine.Networking.UnityWebRequest$$")
           and nm.endswith(("$$Post", "$$SendWebRequest"))}
    out = set()
    for sites in _call_sites(data, sections, web).values():
        for c in sites:
            out.add(_method_start(names, c))
    return out


def _downstream(data, sections, names, slots, start, depth=2):
    """start 的 depth 层下游（调用 + 通过 TypeInfo 关联到的 MoveNext）。"""
    sorted_starts = sorted(names)
    by_name = {}
    for a, nm in names.items():
        by_name.setdefault(nm, a)
    out, frontier, seen = set(), {start}, set()
    for _ in range(depth):
        nxt = set()
        for m in frontier:
            if m in seen:
                continue
            seen.add(m)
            end = _method_end(sorted_starts, m)
            for c in _calls_in(data, sections, m, end):
                cs = _method_start(names, c)
                if cs in names and cs != m:
                    out.add(cs)
                    nxt.add(cs)
            for t in _rip_refs(data, sections, m, end):
                nm = slots.get(t) or ""
                if nm.endswith("_TypeInfo"):
                    cls = nm[:-len("_TypeInfo")]
                    mv = by_name.get(cls + "$$MoveNext")
                    if mv is not None:
                        out.add(mv)
                        nxt.add(mv)
        frontier = nxt
    return out


def cmd_audit(args):
    """扫描 call 目标，判断每个 URL 访问器的调用点当前是否仍可达（数据驱动，跨版本可用）。"""
    p = os.path.join(args.src, FILEMAP[GA_DLL])
    meta = os.path.join(args.src, FILEMAP[META])
    meta_rows = classify_meta(open(meta, "rb").read()) if os.path.exists(meta) else []
    meta_loopback = bool(meta_rows) and all(r[2] == "patched" for r in meta_rows)

    if not os.path.exists(p):
        if meta_loopback:
            print("GameAssembly.dll not present; global-metadata.dat is fully loopback")
            print("=> external traffic to webcammotioncapture.info: NONE")
            return 0
        print("MISSING %s" % p)
        return 2

    data = open(p, "rb").read()
    image_base, sections = pe_info(data)
    names = _load_script(args.script)
    slots = _load_slots(args.script)
    sorted_starts = sorted(names)
    targets = {image_base + rva: (name, url) for rva, (name, url) in URL_ACCESSORS.items()}
    state, stubs, nops, branches, stub_all, nop_all = _patch_points(data, names)
    posts = _post_methods(data, sections, names)

    # 表里所有 kickoff 点所启动的 async 类型；其中已 patched 的算“已拦”
    kick_all, kick_patched = set(), set()
    for rva, orig, new, optional, desc in dll_patches():
        if not _is_nop_hex(new):
            continue
        m = _method_start(names, rva) if names else rva
        for t in _kick_types(data, sections, m, sorted_starts, slots):
            kick_all.add(t)
            if state.get(rva) == "patched":
                kick_patched.add(t)

    startcor = {rva for rva, nm in names.items()
                if nm and nm.startswith("UnityEngine.MonoBehaviour$$StartCoroutine")}

    def status(rva):
        m = _method_start(names, rva) if names else rva
        if m in stubs:
            return "DEAD (method stubbed)"
        if m in nops:
            return "DEAD (call nop'd)"
        if m in branches:
            return "DEAD (branch skipped)"
        if not names:
            return "LOOPBACK (URL rewritten)" if meta_loopback else "LIVE!"
        typ = (names.get(m) or "").split("$$")[0]
        if typ and typ in kick_patched:
            return "DEAD (kickoff nop'd)"
        down = _downstream(data, sections, names, slots, m, 2)
        if down & stubs:
            return "SAFE (downstream stubbed)"
        if m in stub_all and typ in kick_all:
            return "LIVE! (patch not applied)"
        if m in posts or (down & posts):
            return "LIVE!"
        if typ in kick_all:
            return "LIVE! (kickoff not patched)"
        if _calls_in(data, sections, m, _method_end(sorted_starts, m)) and \
                set(_method_start(names, c) for c in
                    _calls_in(data, sections, m, _method_end(sorted_starts, m))) & startcor:
            return "LIVE!"
        return "LOOPBACK (URL rewritten)" if meta_loopback else "build-only (no request)"

    found = []
    for name_, va, vsize, rawptr, rawsize in sections:
        if name_ not in (".text", "il2cpp"):
            continue
        pos, end = rawptr, rawptr + rawsize
        while True:
            pos = data.find(b"\xe8", pos, end)
            if pos < 0:
                break
            rel = struct.unpack_from("<i", data, pos + 1)[0]
            tgt = image_base + off_to_rva(sections, pos + 5) + rel
            if tgt in targets:
                found.append((off_to_rva(sections, pos), targets[tgt]))
            pos += 1

    if not found:
        print("no xref found (accessor calls already gone)")
        return 0

    print("file: %s" % p)
    print("patched points: %d/%d   metadata_loopback=%s"
          % (sum(1 for v in state.values() if v == "patched"), len(state), meta_loopback))
    print("\n%-8s %-34s %-10s %-26s %s"
          % ("accessor", "string", "xref RVA", "in method", "status"))
    live = 0
    for rva, (acc, url) in sorted(found):
        meth = names.get(rva) or _nearest(names, rva) or "?"
        shown = meth.replace("$$", "::")
        if len(shown) > 26:
            shown = shown[:23] + "..."
        s2 = status(rva)
        if s2.startswith("LIVE"):
            live += 1
        print("%-8s %-34s 0x%-8X %-26s %s" % (acc, url[:34], rva, shown, s2))
    print("\n%d reachable, %d neutralised" % (live, len(found) - live))
    return 1 if live else 0


def _load_script(path):
    """读取 Il2CppDumper 的 script.json（RVA -> "Class$$Method"）。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        import json
        d = json.load(open(path))
    except Exception:
        return {}
    out = {}
    for m in d.get("ScriptMethod", []):
        a = m.get("Address")
        if a:
            out.setdefault(a, m.get("Name"))
    return out


def _nearest(names, rva):
    if not names:
        return None
    best = None
    for a in names:
        if a <= rva and (best is None or a > best):
            best = a
    return names.get(best) if best is not None else None


def cmd_all(args):
    """apply -> verify -> audit 一条龙。"""
    rc = cmd_apply(args)
    if rc:
        return rc
    print()
    rc2 = cmd_verify(args)
    print()
    rc3 = cmd_audit(argparse.Namespace(src=args.out, script=args.script))
    print("\nALL DONE: verify=%s audit=%s"
          % ("PASS" if rc2 == 0 else "FAIL", "CLEAN" if rc3 == 0 else "LIVE XREFS"))
    return 0 if (rc2 == 0 and rc3 == 0) else 1


def _method_start(names, rva):
    """把某个指令地址对齐到它所属方法的起始 RVA。"""
    if not names:
        return rva
    best = None
    for a in names:
        if a <= rva and (best is None or a > best):
            best = a
    return best if best is not None else rva


def _load_slots(path):
    """script.json 的 ScriptMetadata: slot RVA -> 名字（如 a.b() / X_TypeInfo / Method$...）。"""
    if not path or not os.path.exists(path):
        return {}
    try:
        import json
        d = json.load(open(path))
    except Exception:
        return {}
    out = {}
    for key, val in (("ScriptMetadata", "Name"), ("ScriptMetadataMethod", "Name")):
        for e in d.get(key, []):
            a = e.get("Address")
            if a:
                out.setdefault(a, e.get(val))
    return out


def cmd_diff(args):
    a = open(args.original, "rb").read()
    b = open(args.patched, "rb").read()
    if len(a) != len(b):
        print("size differs: %d vs %d" % (len(a), len(b)))
        return 1
    runs, i, n = [], 0, len(a)
    while i < n:
        if a[i] != b[i]:
            j = i
            while j < n and a[j] != b[j]:
                j += 1
            runs.append((i, a[i:j].hex(" "), b[i:j].hex(" ")))
            i = j
        else:
            i += 1
    for off, old, new in runs:
        print("file+0x%-8X %-26s -> %s" % (off, old, new))
    print("%d run(s), %d byte(s) changed"
          % (len(runs), sum(len(o.split()) for _, o, _ in runs)))
    return 0


# ---------------------------------------------------------------- CLI


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--src", default=argparse.SUPPRESS)
    common.add_argument("--out", default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(
        description="Patch Webcam Motion Receiver to run fully offline "
                    "(no webcammotioncapture.info traffic).")
    ap.add_argument("--src", default="Mocap", help="dir with untouched game files (default: Mocap)")
    ap.add_argument("--out", default="out_patched", help="patched output dir (default: out_patched)")
    ap.add_argument("--table", default=None,
                    help="patch table JSON from 'locate --write-table' (for a new game build)")
    sub = ap.add_subparsers(dest="cmd")

    def add(name, func, help_text, **kw):
        p = sub.add_parser(name, parents=[common], help=help_text, **kw)
        p.set_defaults(func=func)
        return p

    add("info", cmd_info, "list the patch set")
    add("check", cmd_check, "show patch state of the --src files")
    p = add("apply", cmd_apply, "write patched copies into --out")
    p.add_argument("--strict", action="store_true", help="also nop the dead URL building in fi.Start")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="write bytes even if they look unexpected")
    p.add_argument("--meta-only", action="store_true",
                   help="only patch global-metadata.dat (no RVA needed, works across builds)")
    p = add("verify", cmd_verify, "verify patched files under --out")
    p.add_argument("--strict", action="store_true", help="require the strict patches too")
    p.add_argument("--meta-only", action="store_true", help="ignore the DLL patches")
    add("revert", cmd_revert, "restore --out files from --src")
    p = add("selftest", cmd_selftest, "apply/verify/revert in memory (no files written)")
    p.add_argument("--strict", action="store_true")
    p = add("audit", cmd_audit, "list remaining xrefs to the URL accessors")
    p.add_argument("--script", default="out/script.json", help="Il2CppDumper script.json (optional)")
    p = add("strings", cmd_strings, "decode / grep the GUPS string table")
    p.add_argument("--grep")
    p.add_argument("--dump", default="gups_strings.txt")
    p = add("all", cmd_all, "apply + verify + audit in one go")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--meta-only", action="store_true")
    p.add_argument("--script", default="out/script.json")
    p = add("locate", cmd_locate, "re-locate patch points (run after a game update)")
    p.add_argument("--script", default="out/script.json")
    p.add_argument("--write-table", metavar="FILE", default=None,
                   help="derive a new patch table and write it as JSON")
    p = add("diff", cmd_diff, "byte-diff two files")
    p.add_argument("original")
    p.add_argument("patched")
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    if getattr(args, "table", None):
        load_table(args.table)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
