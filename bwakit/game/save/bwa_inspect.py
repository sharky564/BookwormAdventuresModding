"""Bookworm Adventures .bwa save-file inspector.

The .bwa file is a Torque Game Engine SimObject serialization: a flat stream
of length-prefixed field names interleaved with typed values. This tool does
NOT assume fixed offsets. Instead it:

  1. Dumps every printable ASCII token (field names, class names, tile types)
     with its byte offset, so you can see the file's structure.
  2. Heuristically extracts named fields and the bytes that follow them, so
     you can spot where HP / position / score / items live.
  3. Lets you diff two saves (e.g. before/after taking damage) to localise a
     specific value to a specific byte range -- the fastest way to map an
     unknown field.

Usage:
    python3 bwa_inspect.py tokens   save.bwa
    python3 bwa_inspect.py fields   save.bwa
    python3 bwa_inspect.py hexdump  save.bwa [start] [length]
    python3 bwa_inspect.py diff     before.bwa after.bwa
"""

from __future__ import annotations

import sys
import struct


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def printable_tokens(data: bytes, min_len: int = 3):
    """Yield (offset, token) for runs of printable ASCII (the Torque field
    and class names appear as such runs)."""
    i = 0
    n = len(data)
    while i < n:
        if 32 <= data[i] < 127:
            start = i
            while i < n and 32 <= data[i] < 127:
                i += 1
            if i - start >= min_len:
                yield start, data[start:i].decode("ascii", "replace")
        else:
            i += 1


def cmd_tokens(path: str) -> None:
    data = read(path)
    print(f"# {path}: {len(data)} bytes")
    for off, tok in printable_tokens(data, min_len=3):
        # Skip the 16 single-letter rack tiles' noise by requiring length>=3.
        print(f"  {off:6d} (0x{off:05x})  {tok!r}")


# Torque length-prefixed string: 2-byte little-endian length, then the bytes.
def try_lp_string(data: bytes, off: int) -> tuple[str, int] | None:
    if off + 2 > len(data):
        return None
    (ln,) = struct.unpack_from("<H", data, off)
    if 0 < ln <= 64 and off + 2 + ln <= len(data):
        s = data[off + 2 : off + 2 + ln]
        if all(32 <= b < 127 for b in s):
            return s.decode("ascii"), off + 2 + ln
    return None


def cmd_fields(path: str) -> None:
    """Walk the file looking for `<len><name>` Torque field markers and print
    each name plus the next several bytes (the value blob) as hex + guessed
    int interpretations."""
    data = read(path)
    print(f"# {path}: {len(data)} bytes")
    print("# name @offset : next 12 bytes (hex) | u32@+namegap candidates")
    i = 0
    n = len(data)
    seen = 0
    while i < n - 2:
        got = try_lp_string(data, i)
        if got is None:
            i += 1
            continue
        name, after = got
        # Heuristic: real field names are identifier-ish.
        if not name[0].isalpha():
            i += 1
            continue
        blob = data[after : after + 12]
        hexs = blob.hex(" ")
        # Try to read a u32 right after the name (common for scalar fields).
        ints = []
        for delta in (0, 2, 4):
            if after + delta + 4 <= n:
                (v,) = struct.unpack_from("<I", data, after + delta)
                if v < 10_000_000:
                    ints.append(f"+{delta}={v}")
        print(
            f"  {name:<20} @{after - 2 - len(name):6d}: {hexs:<36} | {' '.join(ints)}"
        )
        seen += 1
        i = after
    if not seen:
        print("  (no length-prefixed field names found - format may differ)")


def cmd_hexdump(path: str, start: int = 0, length: int = 256) -> None:
    data = read(path)
    end = min(len(data), start + length)
    for base in range(start, end, 16):
        chunk = data[base : base + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"  {base:6d} (0x{base:05x})  {hexs:<48}  {asc}")


def cmd_diff(path_a: str, path_b: str) -> None:
    """Byte-diff two saves. The smallest changed regions usually correspond to
    the single value that changed between them (e.g. HP after one hit)."""
    a, b = read(path_a), read(path_b)
    print(f"# A={path_a} ({len(a)}B)  B={path_b} ({len(b)}B)")
    if len(a) != len(b):
        print(
            f"# NOTE: different lengths ({len(a)} vs {len(b)}); "
            f"diff aligns from the start and will desync after an insertion."
        )
    n = min(len(a), len(b))
    # Group contiguous differing bytes into ranges.
    runs = []
    i = 0
    while i < n:
        if a[i] != b[i]:
            start = i
            while i < n and a[i] != b[i]:
                i += 1
            runs.append((start, i))
        else:
            i += 1
    if not runs:
        print("  (no differing bytes in the overlapping region)")
        return
    # Find the nearest preceding printable token for context on each run.
    toks = list(printable_tokens(a, min_len=3))

    def preceding_token(off):
        best = None
        for t_off, t in toks:
            if t_off < off:
                best = (t_off, t)
            else:
                break
        return best

    print(f"  {len(runs)} changed region(s):")
    for start, end in runs:
        ctx = preceding_token(start)
        ctx_s = f"after {ctx[1]!r}@{ctx[0]} (+{start - ctx[0]}B)" if ctx else ""
        av = a[start:end].hex(" ")
        bv = b[start:end].hex(" ")
        # Interpret as little-endian int if 1-4 bytes.
        extra = ""
        if end - start <= 4:
            ai = int.from_bytes(a[start:end], "little")
            bi = int.from_bytes(b[start:end], "little")
            extra = f"  int: {ai} -> {bi}"
        print(
            f"    @{start:6d} (0x{start:05x}) len {end - start}: "
            f"{av} -> {bv}{extra}   {ctx_s}"
        )


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "tokens":
        cmd_tokens(sys.argv[2])
    elif cmd == "fields":
        cmd_fields(sys.argv[2])
    elif cmd == "hexdump":
        start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        length = int(sys.argv[4]) if len(sys.argv) > 4 else 256
        cmd_hexdump(sys.argv[2], start, length)
    elif cmd == "diff":
        cmd_diff(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
