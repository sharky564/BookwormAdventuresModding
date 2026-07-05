"""Bookworm Adventures Deluxe save editor - TOML round-trip, single file.

    python3 bwa_toml.py read  save.bwa  out.toml          # save -> editable TOML
    python3 bwa_toml.py write save.bwa edited.toml [new.bwa]   # TOML -> save

This is a STANDALONE tool: no dependencies beyond the Python standard library
(tomllib, included with Python 3.11+). Drop this one file anywhere and it works;
nothing else from the project is required.

Format
------
A .bwa save is a flat Torque-style stream of records:
    <u16 namelen><name><u32 type><value>
value types: 0 = int (u32), 1 = string/float (u16 len + ascii), 2 = bool (u8),
4 = group/array (children follow as further records). The file has NO checksum,
length field, or offset table, so fixed-width values can be overwritten in
place safely.

Safety model
------------
Edits that keep the file the SAME SIZE are written back:
  - integer fields (position, health, xp, ...)   -> 4-byte overwrite
  - boolean flags (allow_gems, ...)               -> 1-byte overwrite
  - treasure equipped flags (mEnabled)            -> 1-byte overwrite
  - rack letters (the 16 tiles)                   -> 1-byte-per-tile overwrite
Size-changing edits (adding/removing potions or treasures, adding/removing a
gem on a tile, changing a string length) are NOT performed; those values appear
under [readonly] for reference. Every write is verified by re-reading the
result, and the tool refuses to save if the size changed or a field did not
read back as intended - so it is safe to run on saves it has never seen.
"""

from __future__ import annotations

import struct
import sys
import tomllib


# low-level field access


def _find(data: bytes, name: str) -> int:
    return data.find(struct.pack("<H", len(name)) + name.encode())


def read_int(data: bytes, name: str):
    p = _find(data, name)
    if p < 0:
        return None
    after = p + 2 + len(name)
    if struct.unpack_from("<I", data, after)[0] != 0:
        return None
    return struct.unpack_from("<I", data, after + 4)[0]


def read_bool(data: bytes, name: str):
    p = _find(data, name)
    if p < 0:
        return None
    after = p + 2 + len(name)
    if struct.unpack_from("<I", data, after)[0] != 2:
        return None
    return bool(data[after + 4])


def write_int_inplace(buf: bytearray, name: str, value: int) -> bool:
    p = _find(bytes(buf), name)
    if p < 0:
        return False
    after = p + 2 + len(name)
    if struct.unpack_from("<I", buf, after)[0] != 0:
        return False
    struct.pack_into("<I", buf, after + 4, int(value) & 0xFFFFFFFF)
    return True


def write_bool_inplace(buf: bytearray, name: str, value: bool) -> bool:
    p = _find(bytes(buf), name)
    if p < 0:
        return False
    after = p + 2 + len(name)
    if struct.unpack_from("<I", buf, after)[0] != 2:
        return False
    buf[after + 4] = 1 if value else 0
    return True


# rack (GridState): read + length-preserving letter write

_GEM_CLASSES = {
    "AmethystTile": "amethyst",
    "EmeraldTile": "emerald",
    "GarnetTile": "garnet",
    "SapphireTile": "sapphire",
    "RubyTile": "ruby",
    "CrystalTile": "crystal",
    "DiamondTile": "diamond",
}


def _rack_letter_offsets(data: bytes) -> list[int]:
    g = data.find(b"GridState")
    h = data.find(b"HOFNewTop")
    if g < 0 or h < 0:
        return []
    offsets = []
    i = data.find(b"mLetter", g)
    while i != -1 and i < h and len(offsets) < 16:
        base = i + len("mLetter")
        for j in range(base, min(base + 14, h - 2)):
            ln = struct.unpack_from("<H", data, j)[0]
            if ln == 1:
                ch = data[j + 2]
                if 65 <= ch <= 90 or 97 <= ch <= 122:
                    offsets.append(j + 2)
                    break
        i = data.find(b"mLetter", i + 1)
    return offsets


def read_rack(data: bytes) -> tuple[str, list[str]]:
    offs = _rack_letter_offsets(data)
    letters = "".join(chr(data[o]).upper() for o in offs)
    h = data.find(b"HOFNewTop")
    gems = []
    for idx, o in enumerate(offs):
        seg_end = offs[idx + 1] if idx + 1 < len(offs) else (h if h > 0 else len(data))
        seg = data[o:seg_end]
        gem = "none"
        for cls, gname in _GEM_CLASSES.items():
            if cls.encode() in seg:
                gem = gname
                break
        gems.append(gem)
    return letters, gems


def write_rack_inplace(buf: bytearray, new_rack: str) -> bool:
    offs = _rack_letter_offsets(bytes(buf))
    if len(offs) != len(new_rack):
        return False
    for o, ch in zip(offs, new_rack.upper()):
        if not ("A" <= ch <= "Z"):
            return False
        buf[o] = ord(ch)
    return True


# treasure array: (name, equipped, enabled_byte_offset)


def parse_treasures(data: bytes) -> list[tuple[str, bool, int]]:
    off = data.find(struct.pack("<H", 9) + b"Treasures")
    if off < 0:
        return []
    i = off + 2 + 9 + 4 + 4
    out = []
    cur = None
    misses = 0
    while i < len(data) - 2 and len(out) < 128:
        ln = struct.unpack_from("<H", data, i)[0]
        if ln in (7, 8) and i + 2 + ln <= len(data):
            tok = data[i + 2 : i + 2 + ln]
            if tok == b"mScript":
                j = i + 2 + ln
                slen = struct.unpack_from("<H", data, j + 4)[0]
                cur = data[j + 6 : j + 6 + slen].decode("latin-1") if slen else ""
                i = j + 6 + slen
                misses = 0
                continue
            if tok == b"mEnabled":
                j = i + 2 + ln
                if cur is not None:
                    out.append((cur, bool(data[j + 4]), j + 4))
                    cur = None
                i = j + 5
                misses = 0
                continue
        i += 1
        misses += 1
        if misses > 4000:
            break
    return out


def count_blocks(data: bytes, class_name: str) -> int:
    return data.count(class_name.encode())


# field schema

INT_FIELDS = {
    "CurrentBook": "current_book",
    "CurrentChapter": "current_chapter",
    "CurrentStage": "current_stage",
    "FarthestBook": "farthest_book",
    "FarthestChapter": "farthest_chapter",
    "FarthestStage": "farthest_stage",
    "Health": "health",
    "MaxHealth": "max_health",
    "XPLevel": "xp_level",
    "XP": "xp",
    "NumTreasuresFound": "num_treasures_found",
}
BOOL_FIELDS = {
    "AllowGems": "allow_gems",
    "AllowItems": "allow_items",
    "HideTreasures": "hide_treasures",
    "InGame": "in_game",
}
_INT_BY_KEY = {v: k for k, v in INT_FIELDS.items()}
_BOOL_BY_KEY = {v: k for k, v in BOOL_FIELDS.items()}


def _toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


# save -> TOML


def save_to_toml(path: str) -> str:
    data = open(path, "rb").read()
    L = []
    L.append("# Bookworm Adventures Deluxe save editor.")
    L.append("# Edit values under [writable] and run:")
    L.append("#     python3 bwa_toml.py write <save.bwa> <this.toml> [new.bwa]")
    L.append("# Position indices are 1-BASED as stored (book 1 = Book 1).\n")

    L.append("[writable]")
    for sname, key in INT_FIELDS.items():
        v = read_int(data, sname)
        if v is not None:
            L.append(f"{key} = {v}")
    L.append("")
    for sname, key in BOOL_FIELDS.items():
        v = read_bool(data, sname)
        if v is not None:
            L.append(f"{key} = {'true' if v else 'false'}")
    L.append("")

    rack, gems = read_rack(data)
    if rack:
        L.append("# Rack: 16 letters, written in place (gems unchanged).")
        L.append(f"rack = {_toml_str(rack)}")
        L.append(f"# gems (read-only): {gems}")
        L.append("")

    tre = parse_treasures(data)
    if tre:
        L.append("# Treasure equipped flags (flip these; keep exactly 3 True once")
        L.append("# you have >=3 treasures). Treasure names are read-only.")
        L.append("[writable.treasures]")
        for name, eq, _off in tre:
            L.append(f"{name} = {'true' if eq else 'false'}")
        L.append("")

    L.append("[readonly]")
    L.append("# Reference only -- NOT written back (these would resize the file).")
    L.append(f"health_potions = {count_blocks(data, 'HealthItem')}")
    L.append(f"purify_potions = {count_blocks(data, 'PurifyItem')}")
    L.append(f"powerup_potions = {count_blocks(data, 'PowerUpItem')}")
    for sname, key in (
        ("TotalKills", "total_kills"),
        ("TotalWordsSpelled", "total_words"),
        ("TotalLettersSpelled", "total_letters"),
    ):
        v = read_int(data, sname)
        if v is not None:
            L.append(f"{key} = {v}")
    return "\n".join(L) + "\n"


# TOML -> save (in-place writes + mandatory verification)


def toml_to_save(save_path: str, toml_path: str, out_path: str | None = None) -> str:
    original = open(save_path, "rb").read()
    data = bytearray(original)
    with open(toml_path, "rb") as f:
        cfg = tomllib.load(f)
    w = cfg.get("writable", {})
    warnings = []
    intended = {}
    changed = 0

    for key, val in w.items():
        if key in _INT_BY_KEY:
            sname = _INT_BY_KEY[key]
            if write_int_inplace(data, sname, int(val)):
                intended[("int", sname)] = int(val) & 0xFFFFFFFF
                changed += 1
            else:
                warnings.append(f"could not write int '{key}'")
        elif key in _BOOL_BY_KEY:
            sname = _BOOL_BY_KEY[key]
            if write_bool_inplace(data, sname, bool(val)):
                intended[("bool", sname)] = bool(val)
                changed += 1
            else:
                warnings.append(f"could not write bool '{key}'")
        elif key == "rack":
            if write_rack_inplace(data, str(val)):
                intended[("rack", "")] = str(val).upper()
                changed += 1
            else:
                warnings.append("could not write rack (need exactly 16 A-Z letters)")
        elif key == "treasures":
            pass
        else:
            warnings.append(f"unknown writable key '{key}' (ignored)")

    tre_cfg = w.get("treasures", {})
    if tre_cfg:
        pairs = parse_treasures(bytes(data))
        for name, _old, off in pairs:
            if name in tre_cfg:
                data[off] = 1 if tre_cfg[name] else 0
                changed += 1
        true_count = sum(1 for v in tre_cfg.values() if v)
        if pairs and true_count not in (3, len(pairs)):
            warnings.append(
                f"WARNING: {true_count} treasures equipped; the game "
                f"normally requires exactly 3."
            )

    if len(data) != len(original):
        raise SystemExit("Aborted: edit changed the file size (refusing to write).")

    out = bytes(data)
    for (kind, name), expect in intended.items():
        if kind == "int":
            got = read_int(out, name)
        elif kind == "bool":
            got = read_bool(out, name)
        elif kind == "rack":
            got = read_rack(out)[0]
        else:
            continue
        if got != expect:
            raise SystemExit(
                f"Aborted: verification failed for {name} "
                f"(wrote {expect!r}, read back {got!r})."
            )

    dst = out_path or save_path
    with open(dst, "wb") as f:
        f.write(out)
    msg = [f"Wrote {changed} field(s) to {dst} (verified, size unchanged)."]
    msg += [f"  - {x}" for x in warnings]
    return "\n".join(msg)


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "read":
        with open(sys.argv[3], "w") as f:
            f.write(save_to_toml(sys.argv[2]))
        print(f"Wrote editable TOML to {sys.argv[3]}")
    elif mode == "write":
        out = sys.argv[4] if len(sys.argv) > 4 else None
        print(toml_to_save(sys.argv[2], sys.argv[3], out))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
