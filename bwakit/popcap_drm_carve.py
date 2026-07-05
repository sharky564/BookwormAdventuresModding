#!/usr/bin/env python3
"""
popcap_drm_carve.py
===================

Extract the embedded, plaintext game executable from a PopCap
"!popcapdrmprotect!"-wrapped .exe (e.g. Bookworm Adventures Deluxe / Vol 2).

Background
----------
Some PopCap downloadable titles ship as a small loader-stub PE with the *real*
game appended as an overlay that begins with the ASCII marker
"!popcapdrmprotect!".  In builds where that DRM header is inert, the overlay is
simply the real game stored as an *uncompressed, unencrypted* PE image.  A
plain disassembler/decompiler (Ghidra, IDA, ...) never loads the overlay, so
all of the game's real code, strings and data tables look "missing".

This tool finds that embedded PE and carves it out into a standalone file you
can open directly in Ghidra.  It does NOT decrypt anything: if a particular
build genuinely encrypts its payload, the tool will detect the high entropy and
tell you, rather than producing a garbage file.

Usage
-----
    python3 popcap_drm_carve.py GAME.exe                 # writes GAME_real.exe
    python3 popcap_drm_carve.py GAME.exe -o out.exe      # choose output name
    python3 popcap_drm_carve.py GAME.exe --info          # analyse only, no write
    python3 popcap_drm_carve.py GAME.exe --force         # write even if it looks encrypted

Pure standard library; works anywhere Python 3 runs (Windows / macOS / Linux).
"""

import argparse
import math
import os
import struct
import sys

PROTECT_MAGIC = b"!popcapdrmprotect!"
KNOWN_MACHINES = {
    0x014C: "x86 (i386)",
    0x8664: "x86-64",
    0x01C0: "ARM",
    0xAA64: "ARM64",
}


def u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def shannon_entropy(b):
    if not b:
        return 0.0
    counts = [0] * 256
    for byte in b:
        counts[byte] += 1
    n = len(b)
    h = 0.0
    for c in counts:
        if c:
            p = c / n
            h -= p * math.log2(p)
    return h


def parse_pe(data, pe_off):
    """Parse a PE header at file offset `pe_off`. Returns a dict or None."""
    if (
        pe_off < 0
        or pe_off + 24 > len(data)
        or data[pe_off : pe_off + 4] != b"PE\x00\x00"
    ):
        return None
    machine = u16(data, pe_off + 4)
    nsec = u16(data, pe_off + 6)
    opt_sz = u16(data, pe_off + 20)
    opt = pe_off + 24
    if opt + 2 > len(data):
        return None
    opt_magic = u16(data, opt)
    if opt_magic == 0x10B:  # PE32
        if opt + 32 > len(data):
            return None
        image_base = u32(data, opt + 28)
    elif opt_magic == 0x20B:  # PE32+
        if opt + 32 > len(data):
            return None
        image_base = struct.unpack_from("<Q", data, opt + 24)[0]
    else:
        return None
    entry_rva = u32(data, opt + 16)
    sect_tab = opt + opt_sz
    sections = []
    for k in range(nsec):
        b = sect_tab + k * 40
        if b + 40 > len(data):
            break
        name = data[b : b + 8].rstrip(b"\x00").decode("latin1", "replace")
        vsize, va, rawsize, rawptr = struct.unpack_from("<IIII", data, b + 8)
        sections.append(
            {
                "name": name,
                "vsize": vsize,
                "va": va,
                "rawsize": rawsize,
                "rawptr": rawptr,
            }
        )
    if not sections:
        return None
    return {
        "pe_off": pe_off,
        "machine": machine,
        "opt_magic": opt_magic,
        "image_base": image_base,
        "entry_rva": entry_rva,
        "sections": sections,
    }


def host_overlay_offset(data):
    """End of the host PE's last raw section == start of any appended overlay."""
    if data[:2] != b"MZ" or len(data) < 0x40:
        return None, None
    e_lfanew = u32(data, 0x3C)
    pe = parse_pe(data, e_lfanew)
    if not pe:
        return None, None
    end = 0
    for s in pe["sections"]:
        if s["rawsize"] > 0:
            end = max(end, s["rawptr"] + s["rawsize"])
    return end, pe


def find_embedded_pe(data, start):
    """First valid MZ/PE image at or after `start`. Returns its file offset or None."""
    i = max(start, 0)
    n = len(data)
    while True:
        i = data.find(b"MZ", i)
        if i < 0:
            return None
        if i + 0x40 <= n:
            e = u32(data, i + 0x3C)
            pe_off = i + e
            if (
                0 < e < 0x1000
                and pe_off + 24 <= n
                and data[pe_off : pe_off + 4] == b"PE\x00\x00"
            ):
                nsec = u16(data, pe_off + 6)
                machine = u16(data, pe_off + 4)
                if 1 <= nsec <= 96 and machine in KNOWN_MACHINES:
                    return i
        i += 2


def carved_end(data, mz_off, pe):
    """End offset (in container) of the embedded image, using its section table."""
    end = 0
    for s in pe["sections"]:
        if s["rawsize"] > 0:
            end = max(end, s["rawptr"] + s["rawsize"])
    return min(mz_off + end, len(data)) if end else len(data)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return (
                f"{n:.0f} {unit}"
                if unit == "B"
                else f"{n / 1024:.2f} {unit}"
                if False
                else f"{n} B ({n / 1048576:.2f} MB)"
            )
        n /= 1024


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Carve the embedded plaintext game PE out of a PopCap "
        "'!popcapdrmprotect!' executable."
    )
    ap.add_argument("input", help="the wrapped game .exe")
    ap.add_argument("-o", "--output", help="output file (default: <input>_real.exe)")
    ap.add_argument(
        "--info", action="store_true", help="analyse only; do not write a file"
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="write the carve even if the payload looks encrypted",
    )
    args = ap.parse_args(argv)

    try:
        with open(args.input, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"error: cannot read {args.input}: {e}", file=sys.stderr)
        return 2

    print(f"[*] input            : {args.input}")
    print(f"[*] file size        : {len(data)} bytes ({len(data) / 1048576:.2f} MB)")

    if data[:2] != b"MZ":
        print("error: not an MZ/PE executable.", file=sys.stderr)
        return 2

    overlay_off, host_pe = host_overlay_offset(data)
    if host_pe is None:
        print("[!] could not parse the host PE header; falling back to a raw scan.")
        overlay_off = 0x400
    else:
        s = host_pe["sections"]
        print(
            f"[*] host PE          : image_base={hex(host_pe['image_base'])}, "
            f"{len(s)} sections, machine={KNOWN_MACHINES.get(host_pe['machine'], hex(host_pe['machine']))}"
        )
        print(
            f"[*] overlay starts at: {hex(overlay_off)}  "
            f"(overlay size {len(data) - overlay_off} bytes)"
        )

    # DRM marker?
    magic_at = data.find(PROTECT_MAGIC)
    if magic_at >= 0:
        print(
            f"[*] DRM marker       : '{PROTECT_MAGIC.decode()}' found at {hex(magic_at)}"
        )
        search_from = magic_at
    else:
        print(
            "[!] DRM marker '!popcapdrmprotect!' not found "
            "(this may not be a PopCap-DRM build, but trying anyway)."
        )
        search_from = overlay_off if overlay_off else 0x400

    # Locate the embedded real-game PE.
    mz = find_embedded_pe(data, search_from)
    if mz is None:
        # last resort: scan from the very start (after the host stub's own MZ)
        mz = find_embedded_pe(data, 0x400)
    if mz is None or (host_pe and mz == 0):
        print(
            "error: no embedded PE image found. This build does not appear to "
            "contain a plaintext appended executable.",
            file=sys.stderr,
        )
        return 1

    emb = parse_pe(data, mz + u32(data, mz + 0x3C))
    if emb is None:
        print(
            f"error: found 'MZ' at {hex(mz)} but its PE header is invalid.",
            file=sys.stderr,
        )
        return 1

    print(f"[+] embedded PE      : MZ at {hex(mz)}")
    print(f"    image_base       : {hex(emb['image_base'])}")
    print(
        f"    entry (RVA)      : {hex(emb['entry_rva'])}  -> VA {hex(emb['image_base'] + emb['entry_rva'])}"
    )
    print(
        f"    machine          : {KNOWN_MACHINES.get(emb['machine'], hex(emb['machine']))}"
    )
    print(f"    sections ({len(emb['sections'])}):")
    for s in emb["sections"]:
        print(
            f"      {s['name']:8} VA={hex(emb['image_base'] + s['va']):>10}  "
            f"vsize={hex(s['vsize']):>9}  raw={hex(s['rawsize']):>9}"
        )

    end = carved_end(data, mz, emb)
    size = end - mz
    print(
        f"[*] carve range      : {hex(mz)}..{hex(end)}  ({size} bytes, {size / 1048576:.2f} MB)"
    )

    # Plaintext check: sample the embedded .text. Real code ~6.0-6.7;
    # encrypted/compressed payloads sit near ~7.9-8.0.
    text = next(
        (s for s in emb["sections"] if s["name"].lower().startswith(".text")), None
    )
    sample_src = text if text else emb["sections"][0]
    s_off = mz + sample_src["rawptr"]
    s_len = min(256 * 1024, sample_src["rawsize"])
    H = shannon_entropy(data[s_off : s_off + s_len])
    print(
        f"[*] payload entropy  : {H:.2f} bits/byte (sampled {s_len} bytes of {sample_src['name']})"
    )
    looks_encrypted = H > 7.2
    if looks_encrypted:
        print(
            "[!] WARNING: high entropy -- this build's payload appears to be "
            "ENCRYPTED or COMPRESSED, not plaintext."
        )
        print(
            "    The carved file will not be directly analysable. You would need a "
            "runtime memory dump instead."
        )
    else:
        print(
            "[+] payload looks like plaintext code -- carve should be analysable in Ghidra."
        )

    if args.info:
        print("[*] --info: no file written.")
        return 0

    if looks_encrypted and not args.force:
        print(
            "error: refusing to write a likely-encrypted carve. Re-run with "
            "--force to write anyway.",
            file=sys.stderr,
        )
        return 1

    out = args.output
    if not out:
        base, ext = os.path.splitext(args.input)
        out = base + "_real" + (ext or ".exe")
    try:
        with open(out, "wb") as f:
            f.write(data[mz:end])
    except OSError as e:
        print(f"error: cannot write {out}: {e}", file=sys.stderr)
        return 2

    print(f"[+] wrote            : {out}  ({end - mz} bytes)")
    print("[+] done. Open it in Ghidra (PE / x86 defaults) and run the usual analysis;")
    print("    the strings and functions that were missing will now resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
