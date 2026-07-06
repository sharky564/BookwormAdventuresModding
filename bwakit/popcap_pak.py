"""PopCap .pak archive unpacker (Bookworm Adventures Deluxe `main.pak`).

CONFIRMED working on the real main.pak (27,432,983 bytes): the whole file is
XOR'd with the single byte 0xF7, the de-XOR'd magic is 0xBAC04AC0, and it
unpacks cleanly into 5158 files. The "encryption" that reportedly stopped
people is just this single-byte XOR; the harder-looking part is that the
extracted game logic is PopCap-CUSTOMIZED Lua bytecode (header "\\x1bLuaV ..."
with non-standard size fields) that off-the-shelf unluac/luadec reject -- but
the data tables (enemy stats, GEM_ALCHEMY_VALUES, OVERKILL_TABLE,
WORD_LENGTH_GEM_SPAWN_CHANCES, etc.) are embedded as recoverable Lua doubles,
and the properties/*.xml resource manifests are plaintext.

Format (after de-XOR):

  magic   : u32  0xBAC04AC0   (little-endian; appears as C0 4A C0 BA pre-XOR)
  version : u32  (usually 0)
  then a sequence of file records until a 0x80 flag byte:
    flag      : u8    (0x00 = file entry, 0x80 = end of table)
    namelen   : u8
    name      : namelen bytes (ascii, '\\'-separated path)
    filesize  : u32
    filetime  : 8 bytes (Windows FILETIME)
  after the table, the file payloads are concatenated in the same order.
  (The whole file -- header AND payloads -- is de-XOR'd in one pass at load.)

    python3 popcap_pak.py main.pak --diagnose       # confirm format
    python3 popcap_pak.py main.pak --list
    python3 popcap_pak.py main.pak --extract outdir
"""

from __future__ import annotations
import struct, sys, os

XOR = 0xF7
MAGIC = 0xBAC04AC0


_XTAB = bytes(b ^ XOR for b in range(256))


def dexor(b: bytes) -> bytes:
    return b.translate(_XTAB)


def parse(path: str):
    raw = open(path, "rb").read()
    data = dexor(raw)
    if struct.unpack_from("<I", data, 0)[0] != MAGIC:
        raise ValueError(
            f"Bad magic: got 0x{struct.unpack_from('<I', data, 0)[0]:08X}, "
            f"expected 0x{MAGIC:08X}. Format may differ; inspect header."
        )
    off = 8  # skip magic + version
    entries = []
    while off < len(data):
        flag = data[off]
        off += 1
        if flag == 0x80:
            break
        if flag != 0x00:
            # unexpected flag; stop to avoid garbage
            print(
                f"# warning: unexpected flag 0x{flag:02X} at {off - 1}, stopping table"
            )
            break
        namelen = data[off]
        off += 1
        name = data[off : off + namelen].decode("latin-1")
        off += namelen
        size = struct.unpack_from("<I", data, off)[0]
        off += 4
        off += 8  # filetime
        entries.append({"name": name, "size": size})
    # payloads follow, in order
    payload_off = off
    for e in entries:
        e["data_off"] = payload_off
        payload_off += e["size"]
    return data, entries


def diagnose(path: str):
    """Print the first bytes raw and de-XOR'd, to confirm the format before
    a full parse. Useful if --list/--extract reports a bad magic."""
    raw = open(path, "rb").read(64)
    dx = dexor(raw)
    print("# raw first 16 bytes:    ", raw[:16].hex(" "))
    print("# de-XOR'd first 16:     ", dx[:16].hex(" "))
    print(
        f"# de-XOR'd magic (u32):   0x{struct.unpack_from('<I', dx, 0)[0]:08X} "
        f"(expect 0x{MAGIC:08X})"
    )
    print(
        "# de-XOR'd as ascii:     ",
        "".join(chr(b) if 32 <= b < 127 else "." for b in dx[:32]),
    )


def extract(pak: str, outdir: str) -> int:
    """Unpack a .pak into outdir (mirrors internal '\\'-separated paths). Returns the
    number of files written. In-process equivalent of the `--extract` CLI, so callers
    (and a frozen app) don't have to spawn a Python subprocess."""
    data, entries = parse(pak)
    for e in entries:
        dst = os.path.join(outdir, e["name"].replace("\\", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data[e["data_off"] : e["data_off"] + e["size"]])
    return len(entries)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    if sys.argv[2] == "--diagnose":
        diagnose(path)
        return
    data, entries = parse(path)
    total = sum(e["size"] for e in entries)
    print(f"# {path}: {len(entries)} files, {total:,} bytes of content")
    if sys.argv[2] == "--list":
        for e in entries:
            print(f"  {e['size']:>10,}  {e['name']}")
    elif sys.argv[2] == "--extract":
        outdir = sys.argv[3] if len(sys.argv) > 3 else "pak_out"
        n = extract(path, outdir)
        print(f"# extracted {n} files to {outdir}/")


if __name__ == "__main__":
    main()
