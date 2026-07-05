"""PopCap .pak REPACKER -- the exact inverse of popcap_pak.py's unpacker.

Rebuilds a Bookworm Adventures Deluxe `main.pak` from (a) the original pak as a reference
and (b) a directory of modified files. For every entry in the original archive, the modified
file is used if present in the directory, otherwise the original bytes are kept. This
guarantees a BYTE-EXACT result when nothing is changed (the foundational round-trip proof)
and a minimal diff when files are edited, and it preserves the original table order and the
per-file Windows FILETIME stamps -- neither of which can be reconstructed from the extracted
files alone.

Format (after de-XOR with 0xF7; see popcap_pak.py for the authoritative description):
  magic u32 0xBAC04AC0 | version u32 (0) | repeated entries | 0x80 terminator | payloads
  entry: flag 0x00 | namelen u8 | name (latin-1, '\\'-separated) | size u32 | filetime 8B
  payloads are concatenated in table order; no trailing bytes. The whole file is XOR'd
  with 0xF7 in one final pass.

Usage:
    python3 popcap_pak_repack.py ORIGINAL.pak MODIFIED_DIR OUTPUT.pak
    python3 popcap_pak_repack.py --selftest ORIGINAL.pak     # unpack->repack==original check
"""

from __future__ import annotations
import struct, sys, os
from bwakit import popcap_pak as P  # reuse XOR, MAGIC, dexor, and the format constants


def _read_table(path):
    """Parse the original pak, returning (data, entries) where each entry keeps name, size,
    the original payload bytes, and the 8-byte filetime (which popcap_pak.parse discards)."""
    raw = open(path, "rb").read()
    data = P.dexor(raw)
    if struct.unpack_from("<I", data, 0)[0] != P.MAGIC:
        raise ValueError(
            "Bad magic; not a PopCap pak (or already modified incorrectly)."
        )
    version = struct.unpack_from("<I", data, 4)[0]
    off = 8
    entries = []
    while off < len(data):
        flag = data[off]
        off += 1
        if flag == 0x80:
            break
        if flag != 0x00:
            raise ValueError(f"unexpected flag 0x{flag:02X} at {off - 1}")
        namelen = data[off]
        off += 1
        name = data[off : off + namelen]
        off += namelen  # raw bytes (latin-1 path)
        size = struct.unpack_from("<I", data, off)[0]
        off += 4
        filetime = data[off : off + 8]
        off += 8
        entries.append({"name": name, "size": size, "filetime": filetime})
    # attach the original payloads in table order
    p = off
    for e in entries:
        e["orig_payload"] = data[p : p + e["size"]]
        p += e["size"]
    if p != len(data):
        raise ValueError(f"payload length mismatch: consumed {p}, file is {len(data)}")
    return version, entries


def build_pak(version, entries):
    """Serialize entries (each with name:bytes, filetime:8B, and a 'payload' bytes field) into
    a complete pak byte string, then XOR. The table is written first (all entries, 0x80
    terminator), then payloads concatenated in the same order."""
    head = struct.pack("<II", P.MAGIC, version)
    table = bytearray()
    body = bytearray()
    for e in entries:
        name = e["name"]
        payload = e["payload"]
        if len(name) > 255:
            raise ValueError(f"name too long ({len(name)}B): {name!r}")
        table += bytes((0x00, len(name))) + name
        table += struct.pack("<I", len(payload))
        table += e["filetime"]
        body += payload
    table += b"\x80"
    return P.dexor(
        bytes(head) + bytes(table) + bytes(body)
    )  # dexor == re-XOR (involution)


def repack(original_pak, modified_dir, output_pak):
    """Rebuild output_pak from original_pak. A file present under modified_dir replaces its
    original entry; a file under modified_dir with no matching original entry is appended as a
    NEW archive entry (so mods can add creatures, scripts, etc). When modified_dir contains
    only replacements the result is unchanged, preserving the byte-exact round-trip. Returns
    (n_files, n_substituted, n_size_changed)."""
    version, entries = _read_table(original_pak)
    subbed = resized = 0
    have = set()
    for e in entries:
        rel = e["name"].decode("latin-1").replace("\\", os.sep)
        have.add(os.path.normpath(rel))
        cand = os.path.join(modified_dir, rel)
        if os.path.isfile(cand):
            new = open(cand, "rb").read()
            e["payload"] = new
            subbed += 1
            if len(new) != e["size"]:
                resized += 1
        else:
            e["payload"] = e["orig_payload"]
    # append files under modified_dir that have no original entry (added content)
    if entries:
        default_ft = entries[0]["filetime"]
        for root, _, files in os.walk(modified_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.normpath(os.path.relpath(full, modified_dir))
                if rel in have:
                    continue
                payload = open(full, "rb").read()
                entries.append(
                    {
                        "name": rel.replace(os.sep, "\\").encode("latin-1"),
                        "size": len(payload),
                        "filetime": default_ft,
                        "payload": payload,
                    }
                )
    blob = build_pak(version, entries)
    with open(output_pak, "wb") as f:
        f.write(blob)
    return len(entries), subbed, resized


def selftest(original_pak):
    """Round-trip proof: parse the original, rebuild WITHOUT any modification, and confirm the
    rebuilt bytes are identical to the original file. This validates the packer is the exact
    inverse of the unpacker (filetimes, ordering, and table layout all reproduced)."""
    original = open(original_pak, "rb").read()
    version, entries = _read_table(original_pak)
    for e in entries:
        e["payload"] = e["orig_payload"]
    rebuilt = build_pak(version, entries)
    if rebuilt == original:
        print(
            f"SELFTEST PASS: rebuilt pak is byte-identical to original "
            f"({len(entries)} files, {len(original):,} bytes)"
        )
        return 0
    n = min(len(original), len(rebuilt))
    i = 0
    while i < n and original[i] == rebuilt[i]:
        i += 1
    print(
        f"SELFTEST FAIL: diverge at byte {i} (orig {len(original)}B, rebuilt {len(rebuilt)}B)"
    )
    print("  orig   :", original[max(0, i - 4) : i + 8].hex(" "))
    print("  rebuilt:", rebuilt[max(0, i - 4) : i + 8].hex(" "))
    return 1


def main(argv):
    if len(argv) == 3 and argv[1] == "--selftest":
        return selftest(argv[2])
    if len(argv) == 4:
        n, s, r = repack(argv[1], argv[2], argv[3])
        print(
            f"repacked {argv[3]}: {n} files ({s} substituted from {argv[2]}, "
            f"{r} changed size)"
        )
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
