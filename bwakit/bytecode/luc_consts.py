"""Extract the constant table(s) from a PopCap 0x56 Lua .luc file.

Validated format (Bookworm Adventures Deluxe):
  header: \x1bLua, ver 0x56, fmt 01, endian 01, sizes 04 04 04, opcode widths
          06 08 09 09, num_sz 08, then the pi*1e7 number-check double (8 bytes)
  then u32 source-name-length + name
  prototype: linedefined(i32), lastlinedefined(i32), nups/numparams/is_vararg/
             maxstacksize (4 bytes), sizecode(u32), line-info table (sizecode
             i32s) [debug-first ordering], then constants:
               sizek(u32), then each: <u8 type><value>
                 type 0 nil; 1 bool(u8); 3 number(double f64);
                 4 int(i32, PopCap custom); 5 string(u32 len + bytes incl \0)
Nested function prototypes follow; this tool scans ALL constant pools in the
file by walking every constant-table signature, which is robust enough to
recover the data tables without a full instruction decode.
"""

import struct, sys


def find_const_tables(data):
    """Scan for constant-table headers and decode each. A constant table is
    sizek(u32) followed by sizek valid <type><value> entries. We detect them
    by trying to decode at each plausible offset and keeping runs that consume
    cleanly into readable strings/numbers."""
    results = []
    n = len(data)
    off = 0
    while off < n - 5:
        # Heuristic anchor: a string constant entry is 05 <u32 len> <bytes\0>.
        if data[off] == 0x05:
            ln = struct.unpack_from("<I", data, off + 1)[0] if off + 5 <= n else 0
            if 1 <= ln <= 64 and off + 5 + ln <= n and data[off + 5 + ln - 1] == 0:
                s = data[off + 5 : off + 5 + ln - 1]
                if all(32 <= b < 127 for b in s):
                    # decode a run of constants starting here
                    run, end = decode_run(data, off)
                    if len(run) >= 2:
                        results.append((off, run))
                        off = end
                        continue
        off += 1
    return results


def decode_run(data, off):
    n = len(data)
    run = []
    while off < n:
        t = data[off]
        if t == 5:
            if off + 5 > n:
                break
            ln = struct.unpack_from("<I", data, off + 1)[0]
            if not (1 <= ln <= 200) or off + 5 + ln > n:
                break
            b = data[off + 5 : off + 5 + ln - 1]
            if not all(32 <= c < 127 or c in (9, 10, 13) for c in b):
                break
            run.append(b.decode("latin-1"))
            off += 5 + ln
        elif t == 4:
            if off + 5 > n:
                break
            run.append(struct.unpack_from("<i", data, off + 1)[0])
            off += 5
        elif t == 3:
            if off + 9 > n:
                break
            run.append(struct.unpack_from("<d", data, off + 1)[0])
            off += 9
        elif t == 1:
            run.append(bool(data[off + 1]))
            off += 2
        elif t == 0:
            run.append(None)
            off += 1
        else:
            break
    return run, off


if __name__ == "__main__":
    data = open(sys.argv[1], "rb").read()
    tables = find_const_tables(data)
    print(f"# {sys.argv[1]}: {len(tables)} constant run(s)")
    for off, run in tables:
        # show as name=value pairs where it looks like a table
        print(f"\n## run @ {off} ({len(run)} entries)")
        for v in run:
            print(f"   {v!r}")
