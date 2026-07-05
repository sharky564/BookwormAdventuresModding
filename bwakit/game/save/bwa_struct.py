"""Bookworm Adventures - EXPERIMENTAL structural save editor (single file).

Adds/removes whole elements (potions, treasures) by byte-splicing. This changes
the file SIZE, unlike bwa_toml.py which only does safe in-place edits.

    python3 bwa_struct.py info   save.bwa
    python3 bwa_struct.py potion save.bwa add  health  [N]   [out.bwa]
    python3 bwa_struct.py potion save.bwa add  purify  [N]   [out.bwa]
    python3 bwa_struct.py potion save.bwa remove health [N]  [out.bwa]
    python3 bwa_struct.py treasure save.bwa remove <name>    [out.bwa]
    python3 bwa_struct.py treasure save.bwa clone  <name>    [out.bwa]

IMPORTANT - this is an EXPERIMENTAL instrument, not a proven-safe editor.
The .bwa group-element COUNT framing is not fully decoded: the game may or may
not require a count update that this tool does not perform. The container
appears self-delimiting (no count field was found), so pure byte-splicing may
work - but that must be CONFIRMED by loading an edited save in-game. The tool
therefore:
  * always writes a .bak backup of the original,
  * writes to a new file by default (never overwrites in place unless told),
  * re-parses its own output and reports the new element counts + file size,
so every experiment is recoverable and precisely characterised.

Method: ADD = clone an existing element of the same type (duplicate known-good
bytes); REMOVE = delete an element's exact byte span. Both rely on element
boundaries detected by walking the live structure (no hardcoded offsets), so it
adapts to arbitrary saves.
"""

from __future__ import annotations

import os
import shutil
import struct
import sys


# element boundary detection (structure-driven, no hardcoded offsets)


def _items_region(data: bytes) -> tuple[int, int]:
    """(start, end) byte range of the Items group's element area."""
    p = data.find(struct.pack("<H", 5) + b"Items")
    if p < 0:
        return (-1, -1)
    after = p + 2 + 5 + 4  # name + group type tag
    # the element area runs until the next sibling field; Last5HP follows Items
    end = data.find(b"Last5HP", after)
    if end < 0:
        end = len(data)
    else:
        end -= 2  # back up over Last5HP's u16 length prefix
    return after, end


def potion_elements(data: bytes) -> list[tuple[int, int, str]]:
    """[(start, end, kind), ...] for each potion element, in order."""
    start, end = _items_region(data)
    if start < 0:
        return []
    starts = []
    i = data.find(b"mChanceIfDupes", start)
    while i != -1 and i < end:
        starts.append(i - 2)  # record begins at the u16 length prefix
        i = data.find(b"mChanceIfDupes", i + 1)
    elems = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else end
        blk = data[s:e]
        kind = (
            "health"
            if b"HealthItem" in blk
            else "purify"
            if b"PurifyItem" in blk
            else "powerup"
            if b"PowerUpItem" in blk
            else "?"
        )
        elems.append((s, e, kind))
    return elems


def treasure_elements(data: bytes) -> list[tuple[int, int, str, bool]]:
    """[(start, end, name, equipped), ...] for each treasure element."""
    tre = data.find(struct.pack("<H", 9) + b"Treasures")
    if tre < 0:
        return []
    starts = []
    i = data.find(b"mScript", tre)
    while i != -1:
        rec = i - 2
        if starts and rec - starts[-1] > 256:  # left the treasure cluster
            break
        starts.append(rec)
        i = data.find(b"mScript", i + 1)
    elems = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else None
        if e is None:
            # last element: ends after its mEnabled bool value
            me = data.find(b"mEnabled", s)
            e = me + len("mEnabled") + 4 + 1 if me >= 0 else s + 60
        blk = data[s:e]
        name, eq = "", False
        mp = blk.find(b"mScript")
        if mp >= 0:
            j = mp + 7
            slen = struct.unpack_from("<H", blk, j + 4)[0]
            name = blk[j + 6 : j + 6 + slen].decode("latin-1")
        en = blk.find(b"mEnabled")
        if en >= 0:
            eq = bool(blk[en + len("mEnabled") + 4])
        elems.append((s, e, name, eq))
    return elems


# operations


def do_info(data: bytes) -> str:
    pe = potion_elements(data)
    te = treasure_elements(data)
    L = [f"File size: {len(data)} bytes"]
    counts = {}
    for _s, _e, k in pe:
        counts[k] = counts.get(k, 0) + 1
    L.append(f"Potions: {counts} (total {len(pe)} elements)")
    L.append(
        f"Treasures: {len(te)} ({sum(1 for *_, eq in [(t[2], t[3]) for t in te] if eq)} equipped)"
    )
    for s, e, name, eq in te:
        L.append(f"   [{'E' if eq else ' '}] {name}  ({e - s} bytes @ {s})")
    return "\n".join(L)


def add_potion(data: bytes, kind: str, n: int) -> tuple[bytes, str]:
    pe = potion_elements(data)
    src = next(((s, e) for s, e, k in pe if k == kind), None)
    if src is None:
        return data, (
            f"cannot add '{kind}': no existing {kind} element to clone "
            f"(clone-and-splice needs at least one of the same type)."
        )
    s, e = src
    block = data[s:e]
    # splice n copies right after the source element
    out = data[:e] + block * n + data[e:]
    return (
        out,
        f"added {n} x {kind} potion ({len(block)} bytes each, cloned from @ {s})",
    )


def remove_potion(data: bytes, kind: str, n: int) -> tuple[bytes, str]:
    pe = potion_elements(data)
    targets = [(s, e) for s, e, k in pe if k == kind]
    if not targets:
        return data, f"no {kind} potion to remove."
    n = min(n, len(targets))
    # remove the LAST n of that kind (splice them out from the end to keep
    # earlier offsets valid)
    removed = 0
    out = bytearray(data)
    for s, e in reversed(targets[-n:]):
        del out[s:e]
        removed += 1
    return bytes(out), f"removed {removed} x {kind} potion"


def remove_treasure(data: bytes, name: str) -> tuple[bytes, str]:
    te = treasure_elements(data)
    tgt = next(((s, e) for s, e, nm, _eq in te if nm == name), None)
    if tgt is None:
        return data, f"treasure '{name}' not found (have: {[t[2] for t in te]})"
    s, e = tgt
    out = data[:s] + data[e:]
    return out, f"removed treasure '{name}' ({e - s} bytes)"


def clone_treasure(data: bytes, name: str) -> tuple[bytes, str]:
    te = treasure_elements(data)
    tgt = next(((s, e) for s, e, nm, _eq in te if nm == name), None)
    if tgt is None:
        return data, f"treasure '{name}' not found (have: {[t[2] for t in te]})"
    s, e = tgt
    block = data[s:e]
    out = data[:e] + block + data[e:]
    return out, f"cloned treasure '{name}' (+{len(block)} bytes; now duplicated)"


# driver with backup + verification


def _report(orig: bytes, out: bytes, msg: str) -> str:
    pe0, te0 = potion_elements(orig), treasure_elements(orig)
    pe1, te1 = potion_elements(out), treasure_elements(out)
    return (
        f"{msg}\n"
        f"  size: {len(orig)} -> {len(out)} ({len(out) - len(orig):+d} bytes)\n"
        f"  potion elements: {len(pe0)} -> {len(pe1)}\n"
        f"  treasure elements: {len(te0)} -> {len(te1)}\n"
        f"  (re-parsed output cleanly; load in-game to confirm the game "
        f"accepts the new structure.)"
    )


def _save(orig_path: str, out: bytes, out_path: str | None) -> str:
    # always back up the original
    bak = orig_path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(orig_path, bak)
    dst = out_path or (orig_path.rsplit(".", 1)[0] + ".edited.bwa")
    with open(dst, "wb") as f:
        f.write(out)
    return dst


def main():
    a = sys.argv
    if len(a) < 3:
        print(__doc__)
        sys.exit(1)
    cmd, save = a[1], a[2]
    data = open(save, "rb").read()

    if cmd == "info":
        print(do_info(data))
        return

    if cmd == "potion":
        action, kind = a[3], a[4]
        n = int(a[5]) if len(a) > 5 and a[5].isdigit() else 1
        out_path = (
            a[6]
            if len(a) > 6
            else (a[5] if len(a) > 5 and not a[5].isdigit() else None)
        )
        if action == "add":
            out, msg = add_potion(data, kind, n)
        elif action == "remove":
            out, msg = remove_potion(data, kind, n)
        else:
            print(f"unknown potion action '{action}'")
            sys.exit(1)
    elif cmd == "treasure":
        action, name = a[3], a[4]
        out_path = a[5] if len(a) > 5 else None
        if action == "remove":
            out, msg = remove_treasure(data, name)
        elif action == "clone":
            out, msg = clone_treasure(data, name)
        else:
            print(f"unknown treasure action '{action}'")
            sys.exit(1)
    else:
        print(__doc__)
        sys.exit(1)

    if out == data:
        print(f"No change: {msg}")
        return
    dst = _save(save, out, out_path)
    print(_report(data, out, msg))
    print(f"  backup: {save}.bak\n  wrote:  {dst}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
