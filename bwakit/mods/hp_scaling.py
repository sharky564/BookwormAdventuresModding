"""Late-game enemy-HP scaling (the shipped v3 mod), captured as a reproducible builder.

Scales only the EXCESS attack-power above the end of Book 1, so Book 1 stays fully
vanilla and Books 2-3 ramp up:

    HP_new = round(HP_base * (1 + max(0, power - P0) / 100) ** d)

with P0 = 118.6 (Lex's attack-power % at the end of Book 1) and d = 0.6. Per-enemy HP
lives in the Init SetHealth load (proto main.1, pc 2). The two multi-phase survival
bosses set each phase's HP inline in their head-transition protos, so Sphinx (2.4) and
Codex (3.10) get *every* SetHealth load repointed, not just Init -- otherwise only the
first phase scales.

Every edit appends a fresh constant and repoints the LOADK to it; shared constants are
never overwritten. P0 and d are the tuning dials.
"""

import os
import re
import shutil
import argparse
import pathlib

from bwakit.bytecode import luc_transcode as T, luc_inverse_transcode as INV
from bwakit.bytecode.edit import scale_loadks
from bwakit import popcap_pak_repack as R
from bwakit.game.power_table import POWER_TABLE

NAME_FIX = {"PharoahofOld": "PharoahOfOld"}  # roster spelling -> actual filename
SKIP_CH = {(1, 3), (2, 9)}  # Sphinx 2.4, Codex 3.10 -> handled as multi-phase
SPECIAL = {(0, 6), (2, 9)}  # chapters whose POWER row is one logical slot
ROSTERS = (
    pathlib.Path(__file__).resolve().parent.parent
    / "game"
    / "data"
    / "enemy_rosters.txt"
)

# exact SetHealth load sites per multi-phase boss: proto path -> [pc, ...]
SPHINX_SITES = [
    ("main.1", [2]),
    ("main.11", [177, 178, 218, 219, 259, 260, 300, 301]),
    ("main.9", [164, 165]),
    ("main.10", [0]),
]
CODEX_SITES = [
    ("main.1", [2]),
    ("main.11", [60, 61, 194, 195, 379, 380, 588, 589]),
    ("main.10", [264, 265]),
    ("main.12", [2, 5]),
]
SPHINX_POWER, CODEX_POWER = 159.3, 381.7


def _rosters():
    """Parse enemy_rosters.txt into names[book][chapter] = [creature, ...]."""
    names = [[], [], []]
    cb = -1
    for line in open(ROSTERS):
        mb = re.match(r"## Book (\d)", line)
        if mb:
            cb = int(mb.group(1)) - 1
            continue
        if re.match(r"\s*Chapter ", line):
            if cb >= 0:
                names[cb].append([])
            continue
        me = re.match(r"\s*-\s*(\S+)", line)
        if me and cb >= 0 and names[cb]:
            names[cb][-1].append(me.group(1))
    return names


def _proto(chunk, path):
    """Navigate a 'main.N.M' proto path."""
    cur = chunk
    for s in path.split(".")[1:]:
        cur = cur.protos[int(s)]
    return cur


# constant-repoint helpers now live in bwakit.bytecode.edit (scale_loadks), shared with enemy_swap


# ---- BA2 (Volume 2) support -------------------------------------------------
# BA2 reuses BA1's exact leveling curve, so Lex's attack-power growth is computed
# from cumulative enemy XP per chapter -> level -> +12.5% per offense level
# (levels 2,5,8,...). Books are 0 (tutorial)/4/5/6, each with 10 chapters. P0
# defaults to Lex's power at the end of Book 4 (the first full book), so Book 0/4
# stay vanilla and Books 5-6 ramp -- mirroring BA1's "scale only the excess above
# the first book" design. Treasure attack bonuses are NOT modeled (BA1's table was
# TAS-observed and treasure-inclusive), so this curve is gentler than BA1's; raise
# `d` to increase late-game intensity.
_GXP = [
    2,
    25,
    60,
    100,
    175,
    275,
    400,
    600,
    850,
    1175,
    1625,
    2225,
    3025,
    4075,
    5475,
    7325,
    9825,
    13150,
    17575,
    23475,
    31325,
    41725,
    55525,
    73875,
    98275,
    130725,
    173875,
    231275,
    307575,
    409075,
    544075,
    723575,
    963575,
    1288575,
    1700000,
    2250000,
    3000000,
    4000000,
    5250000,
    7000000,
    9250000,
    14376230,
]
_BA2_BOOKS = [0, 4, 5, 6]


def _level_for(xp):
    L = 0
    for thr in _GXP:
        if xp >= thr:
            L += 1
        else:
            break
    return L


def _offense_levels(L):
    return sum(1 for k in range(1, L + 1) if k % 3 == 2)


def _ba2_roster(src, bnum):
    """[(chapter, name), ...] in AddEnemy order for books/Book{bnum}.luc."""
    from bwakit.game import encounters as _E

    chunk = T.parse_full(os.path.join(src, "scripts", "books", f"Book{bnum}.luc"))

    def _walk(p):
        yield p
        for s in p.protos:
            yield from _walk(s)

    rp = next(p for p in _walk(chunk) if "AddEnemy" in [_E._s(v) for _, v in p.consts])
    s = lambda i: _E._s(rp.consts[i][1])
    out, code, pc = [], rp.code, 0
    while pc < len(code):
        w = code[pc]
        if (w & 0x3F) == 11:  # SELF
            a = (w >> 24) & 0xFF
            C = (w >> 6) & 0x1FF
            if C >= 250 and s(C - 250) == "AddEnemy":
                chap = name = None
                j = pc + 1
                while j < len(code):
                    wj = code[j]
                    oj = wj & 0x3F
                    if oj == 1:  # LOADK
                        aj = (wj >> 24) & 0xFF
                        val = rp.consts[(wj >> 6) & 0x3FFFF][1]
                        if aj >= a + 2:
                            if (
                                isinstance(val, (int, float))
                                and not isinstance(val, bool)
                                and chap is None
                            ):
                                chap = int(val)
                            elif isinstance(val, (bytes, str)) and name is None:
                                vv = _E._s(val)
                                if vv and vv[0].isupper() and " " not in vv:
                                    name = vv
                    if oj == 27 and ((wj >> 24) & 0xFF) == a:
                        break  # CALL a
                    j += 1
                if name:
                    out.append((chap, name))
        pc += 1
    return out


def _ba2_creature_hp_xp(src, bnum, name):
    """(hp, mXP) for a creature, or (None, None) if not found."""
    from bwakit.game import encounters as _E

    f = os.path.join(src, "scripts", "creatures", f"Book{bnum}", f"{name}.luc")
    if not os.path.exists(f):
        return None, None
    chunk = T.parse_full(f)
    p = chunk.protos[1] if len(chunk.protos) > 1 else chunk
    hp = None
    if len(p.code) > 2 and (p.code[2] & 0x3F) == 1:
        v = p.consts[(p.code[2] >> 6) & 0x3FFFF][1]
        hp = int(v) if isinstance(v, (int, float)) else None
    mxp = None
    ki = [i for i, (t, v) in enumerate(p.consts) if _E._s(v) == "mXP"]
    for w in p.code:
        if (w & 0x3F) == 9:  # SETTABLE A B C
            B = (w >> 15) & 0x1FF
            Cc = (w >> 6) & 0x1FF
            if B >= 250 and (B - 250) in ki and Cc >= 250:
                v = p.consts[Cc - 250][1]
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    mxp = int(v)
    return hp, mxp


def _ba2_power_by_chapter(src):
    """({(book,chap): power%}, {(book,chap): [names]}) from the leveling curve."""
    from collections import OrderedDict

    power, rosters = {}, OrderedDict()
    cum = 0
    for b in _BA2_BOOKS:
        chs = OrderedDict()
        for chap, nm in _ba2_roster(src, b):
            chs.setdefault(chap, []).append(nm)
        for chap, nms in chs.items():
            rosters[(b, chap)] = nms
            power[(b, chap)] = 12.5 * _offense_levels(_level_for(cum))
            for nm in nms:
                _, mxp = _ba2_creature_hp_xp(src, b, nm)
                cum += mxp or 0
    return power, rosters


def _build_ba2(src, base_pak, out_pak, stage, p0, d, keep_stage):
    power, rosters = _ba2_power_by_chapter(src)
    if p0 is None:
        p0 = max((pw for (b, c), pw in power.items() if b == 4), default=0.0)
    fac = lambda pw: (1 + max(0.0, pw - p0) / 100.0) ** d
    res, scaled = {}, 0
    for (b, c), nms in rosters.items():
        pw = power[(b, c)]
        for nm in nms:
            f = os.path.join(src, "scripts", "creatures", f"Book{b}", f"{nm}.luc")
            if not os.path.exists(f):
                res["MISSING"] = res.get("MISSING", 0) + 1
                continue
            chunk = T.parse_full(f)
            p = chunk.protos[1]
            if (p.code[2] & 0x3F) != 1:
                res["NO_PC2"] = res.get("NO_PC2", 0) + 1
                continue
            base = float(p.consts[(p.code[2] >> 6) & 0x3FFFF][1])
            if max(1, round(base * fac(pw))) == base:
                res["UNCHANGED"] = res.get("UNCHANGED", 0) + 1
                continue
            scale_loadks(p, [2], fac(pw))
            od = stage / f"scripts/creatures/Book{b}"
            od.mkdir(parents=True, exist_ok=True)
            open(od / f"{nm}.luc", "wb").write(INV.emit_popcap(chunk))
            scaled += 1
            res["OK"] = res.get("OK", 0) + 1
    subbed = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return {
        "scaled": scaled,
        "status": res,
        "p0": round(p0, 2),
        "d": d,
        "game": "ba2",
        "subbed": subbed,
        "out": str(out_pak),
    }


def build(src, base_pak, out_pak, *, p0=None, d=0.6, game="ba1", keep_stage=False):
    """Build the scaled pak.

    src       extracted pak root (must contain scripts/creatures/Book*/)
    base_pak  clean main.pak to repack onto
    out_pak   output pak path
    p0, d     scaling dials (defaults reproduce the shipped v3)
    """
    creatures = os.path.join(src, "scripts", "creatures")
    stage = pathlib.Path(str(out_pak) + ".stage")
    shutil.rmtree(stage, ignore_errors=True)
    if game == "ba2":
        return _build_ba2(src, base_pak, out_pak, stage, p0, d, keep_stage)
    if p0 is None:
        p0 = 118.6
    fac = lambda power: (1 + max(0.0, power - p0) / 100.0) ** d
    names = _rosters()

    def write(b, chunk, fn):
        od = stage / f"scripts/creatures/Book{b + 1}"
        od.mkdir(parents=True, exist_ok=True)
        open(od / f"{fn}.luc", "wb").write(INV.emit_popcap(chunk))

    def scale_generic(b, name, power):
        fn = NAME_FIX.get(name, name)
        path = os.path.join(creatures, f"Book{b + 1}", f"{fn}.luc")
        if not os.path.exists(path):
            return "MISSING"
        chunk = T.parse_full(path)
        p = chunk.protos[1]
        w = p.code[2]
        if (w & 0x3F) != 1:
            return "NO_PC2"
        bx = (w >> 6) & 0x3FFFF
        k = p.consts[bx]
        base = float(k[1])
        new = max(1, round(base * fac(power)))
        if new == base:
            return "UNCHANGED"
        scale_loadks(p, [2], fac(power))
        write(b, chunk, fn)
        return "OK"

    def fix_boss(b, fn, power, sites):
        f = fac(power)
        chunk = T.parse_full(os.path.join(creatures, f"Book{b + 1}", f"{fn}.luc"))
        for pp, pcs in sites:
            scale_loadks(_proto(chunk, pp), pcs, f)
        write(b, chunk, fn)
        return f

    res, scaled = {}, 0
    for b in range(3):
        for c in range(len(POWER_TABLE[b])):
            if (b, c) in SKIP_CH:
                continue
            slots = [0] if (b, c) in SPECIAL else range(len(POWER_TABLE[b][c]))
            for e in slots:
                row = names[b][c] if c < len(names[b]) else []
                nm = row[e] if e < len(row) else None
                if not nm:
                    continue
                st = scale_generic(b, nm, POWER_TABLE[b][c][e])
                res[st] = res.get(st, 0) + 1
                if st == "OK":
                    scaled += 1

    sf = fix_boss(1, "SphinxPuzzle", SPHINX_POWER, SPHINX_SITES)
    cf = fix_boss(2, "Codex", CODEX_POWER, CODEX_SITES)
    subbed = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return {
        "scaled": scaled,
        "status": res,
        "p0": p0,
        "d": d,
        "sphinx_factor": round(sf, 4),
        "codex_factor": round(cf, 4),
        "subbed": subbed,
        "out": str(out_pak),
    }


def cli(args):
    ap = argparse.ArgumentParser(prog="bwa mod hp-scaling")
    ap.add_argument(
        "--src", required=True, help="extracted pak root (contains scripts/creatures/)"
    )
    ap.add_argument("--base", required=True, help="clean main.pak to repack onto")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument(
        "--p0",
        type=float,
        default=118.6,
        help="power threshold below which HP is untouched",
    )
    ap.add_argument("--d", type=float, default=0.6, help="scaling exponent (steepness)")
    a = ap.parse_args(args)
    print(build(a.src, a.base, a.out, p0=a.p0, d=a.d))
