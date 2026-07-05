"""Seeded randomizer for Bookworm Adventures.

Reorders creatures by repointing the AddEnemy name constants in books/Book{N}.luc (the
roster the engine fights). packs/Book{N}.luc only *preloads* each chapter's scripts, so any
cross-chapter move must resync it or `Name:new()` crashes on an unloaded creature.
Deterministic: a given (seed, options) always yields the same pak.

level = the 0-10 shuffle scale (see _REACH_CH). treasures = shuffle boss awards within each
book, as composable repoint_global transforms. Bosses + the Book 1 tutorial stay fixed by default.
"""

import random
import shutil
import pathlib
import argparse
import os

from bwakit.bytecode import luc_transcode as T, luc_inverse_transcode as INV
from bwakit import popcap_pak_repack as R
from bwakit.game import encounters as E
from bwakit.game.power_table import POWER_TABLE

BOOKS = (1, 2, 3)
_TUTORIAL = (1, 1)  # (book, chapter) of the scripted intro -- never shuffle
# Multi-phase/survival bosses have bespoke scripting; moving them desyncs it, so they stay put.
_SURVIVAL = {"Hydra", "SphinxPuzzle", "Codex"}


# ---- enemy roster -------------------------------------------------------------------


def _book_slots(src, b):
    """Parse one book and return (packs_chunk, books_chunk, packs_proto, books_proto,
    slots) where slots is a list of mutable rows
        [chapter, idx_in_chapter, is_boss, b_pc, b_kidx, p_pc, p_kidx, name, path]
    aligned 1:1 between the books/ roster and the packs/ preloader (verified)."""
    packs = T.parse_full("%s/scripts/packs/Book%d.luc" % (src, b))
    pproto = E.chapter_scripts_proto(packs)
    pchaps = E.chapter_slots(pproto)
    names = {p.split("/")[-1] for ch in pchaps for _, _, p in ch}
    books = T.parse_full("%s/scripts/books/Book%d.luc" % (src, b))
    bproto = E.book_roster_proto(books, names)
    broster = E.book_roster_slots(bproto, names)
    slots, bi = [], 0
    for ci, pch in enumerate(pchaps, start=1):
        n = len(pch)
        for j, (p_pc, p_kidx, path) in enumerate(pch):
            b_pc, b_kidx, bchap, bname = broster[bi]
            bi += 1
            assert bname == path.split("/")[-1] and bchap == ci, (
                "Book%d misalignment at flat %d: books(%s ch%d) vs packs(%s ch%d)"
                % (b, bi - 1, bname, bchap, path.split("/")[-1], ci)
            )
            slots.append([ci, j, j == n - 1, b_pc, b_kidx, p_pc, p_kidx, bname, path])
    return packs, books, pproto, bproto, slots


# ---- 0-10 shuffle scale --------------------------------------------------------------
# 0  = no shuffle
# 1  = within each chapter (strict; packs/ untouched)
# 2-5 = within each book, locality widening from ~2 chapters (2) to the whole book (5)
# 6-9 = across all books, locality widening from ~1.5 books (6) toward near-global (9)
# 10 = completely random (global)
# Higher = more random. Perturbed-key sort: key = index + Gaussian(sigma); sigma grows with level.
_REACH_CH = {
    2: 2.0,
    3: 4.0,
    4: 7.0,
    5: 12.0,
    6: 15.0,
    7: 25.0,
    8: 40.0,
    9: 70.0,
}  # sigma, in "chapters"


def _eligible(b, s, include_tutorial):
    if s[7] in _SURVIVAL:  # multi-phase survival fights never move
        return False
    if (b, s[0]) == _TUTORIAL and not include_tutorial:
        return False
    return True


def _shuffle_members(per_book, members, seed, level, scopekey, sigma):
    """Reassign creatures among `members` (list of (book, slot_index)) via a perturbed-key
    sort with the given sigma (sigma=None => a full random shuffle). Deterministic in seed."""
    n = len(members)
    if n < 2:
        return
    rng = random.Random("%s:enemies:L%s:%r" % (seed, level, scopekey))
    if sigma is None:
        order = list(range(n))
        rng.shuffle(order)
    else:
        order = sorted(range(n), key=lambda i: i + rng.gauss(0.0, sigma))
    creatures = [per_book[b][4][i][7] for (b, i) in members]
    for j, (b, i) in enumerate(members):
        per_book[b][4][i][7] = creatures[order[j]]


def _assign(src, seed, level, keep_bosses, include_tutorial=False):
    """Deterministic creature->slot assignment (no file writes). Returns
    (per_book, orig, name2path). `level` is the 0-10 shuffle scale (see _REACH_CH); shared by
    _roster_transforms and the HP/XP balancers so all three use the same permutation."""
    per_book = {b: _book_slots(src, b) for b in BOOKS}
    name2path = {s[7]: s[8] for b in BOOKS for s in per_book[b][4]}
    orig = {
        (b, i): per_book[b][4][i][7] for b in BOOKS for i in range(len(per_book[b][4]))
    }
    level = int(level)
    if level <= 0:
        return per_book, orig, name2path

    def chap_sigma(members, lvl):
        if lvl in (5, 10):
            return None  # 5 = full random within book, 10 = full random global
        chaps = len({per_book[b][4][i][0] for (b, i) in members}) or 1
        return _REACH_CH[lvl] * (
            len(members) / chaps
        )  # reach(chapters) * avg chapter size(slots)

    # --- non-boss enemies ---
    if level == 1:  # strict within-chapter
        groups = {}
        for b in BOOKS:
            for i, s in enumerate(per_book[b][4]):
                if not s[2] and _eligible(b, s, include_tutorial):
                    groups.setdefault((b, s[0]), []).append((b, i))
        for key, mem in groups.items():
            _shuffle_members(per_book, mem, seed, level, key, None)
    elif level <= 5:  # within each book, widening locality
        for b in BOOKS:
            mem = [
                (b, i)
                for i, s in enumerate(per_book[b][4])
                if not s[2] and _eligible(b, s, include_tutorial)
            ]
            _shuffle_members(
                per_book, mem, seed, level, ("book", b), chap_sigma(mem, level)
            )
    else:  # across all books
        mem = [
            (b, i)
            for b in BOOKS
            for i, s in enumerate(per_book[b][4])
            if not s[2] and _eligible(b, s, include_tutorial)
        ]
        _shuffle_members(
            per_book, mem, seed, level, ("global",), chap_sigma(mem, level)
        )

    # --- bosses: fixed by default, else shuffled among boss slots in the same scope ---
    if not keep_bosses:
        if level <= 5:
            for b in BOOKS:
                bm = [
                    (b, i)
                    for i, s in enumerate(per_book[b][4])
                    if s[2] and _eligible(b, s, include_tutorial)
                ]
                _shuffle_members(per_book, bm, seed, level, ("boss", b), None)
        else:
            bm = [
                (b, i)
                for b in BOOKS
                for i, s in enumerate(per_book[b][4])
                if s[2] and _eligible(b, s, include_tutorial)
            ]
            _shuffle_members(per_book, bm, seed, level, ("boss", "g"), None)
    return per_book, orig, name2path


def _randomize_enemies(src, stage, seed, level, keep_bosses, include_tutorial=False):
    per_book, orig, name2path = _assign(src, seed, level, keep_bosses, include_tutorial)
    moved = 0
    for b in BOOKS:
        packs, books, pproto, bproto, slots = per_book[b]
        changed = False
        for i, s in enumerate(slots):
            nm = s[7]
            if nm != orig[(b, i)]:
                E.repoint_path(bproto, s[3], s[4], nm)  # books/: bare name
                if level >= 2:
                    E.repoint_path(
                        pproto, s[5], s[6], name2path[nm]
                    )  # packs/: full path
                changed = True
                moved += 1
        if changed:
            od = stage / "scripts/books"
            od.mkdir(parents=True, exist_ok=True)
            open(od / ("Book%d.luc" % b), "wb").write(INV.emit_popcap(books))
            if level >= 2:
                od2 = stage / "scripts/packs"
                od2.mkdir(parents=True, exist_ok=True)
                open(od2 / ("Book%d.luc" % b), "wb").write(INV.emit_popcap(packs))
    return moved


# ---- power-ratio HP balancing -------------------------------------------------------
# level -> (exponent on power ratio, min, max factor); clamps cap tutorial-era (~0) powers.
_BALANCE = {1: (0.5, 0.5, 2.0), 2: (1.0, 0.34, 3.0), 3: (1.25, 0.25, 4.0)}


def _power(b, c, s):
    """Lex's expected power at slot (book 1-3, chapter 1-10, stage 0-based), floored at 1.0
    so the near-zero tutorial powers never divide to infinity."""
    row = POWER_TABLE[b - 1][c - 1]
    v = row[s] if s < len(row) else (row[-1] if row else 1.0)
    return max(float(v), 1.0)


def _balance_transforms(src, seed, level, keep_bosses, include_tutorial, balance_level):
    """Emit `scale_health` ops that rescale each MOVED creature's HP by the ratio of Lex's
    power where it now sits vs. where it normally sits, shaped by the chosen difficulty.
    Uses the same permutation as _randomize_enemies."""
    if not balance_level or balance_level not in _BALANCE:
        return []
    exp, lo, hi = _BALANCE[balance_level]
    per_book, orig, name2path = _assign(src, seed, level, keep_bosses, include_tutorial)
    # original (book, chapter, stage) of every creature name (chapter=s[0], stage=s[1])
    orig_pos = {}
    for b in BOOKS:
        for i, s in enumerate(per_book[b][4]):
            orig_pos[orig[(b, i)]] = (b, s[0], s[1])
    ops, seen = [], set()
    for b in BOOKS:
        for i, s in enumerate(per_book[b][4]):
            nm = s[7]
            if nm == orig[(b, i)] or nm in seen:
                continue
            seen.add(nm)
            ob, oc, os_ = orig_pos[nm]
            factor = (_power(b, s[0], s[1]) / _power(ob, oc, os_)) ** exp
            factor = max(lo, min(hi, factor))
            if abs(factor - 1.0) < 1e-3:
                continue
            rel = _resolve(src, "scripts/" + name2path[nm] + ".luc")
            ops.append({"file": rel, "op": "scale_health", "factor": round(factor, 4)})
    return ops


# ---- power-ratio XP balancing -------------------------------------------------------
# Anchor a moved creature's awarded XP to what its NEW slot normally gives (the XP of the
# creature that originally sat there), then nudge it up a little for a creature that's tougher
# than that slot expects / down for an easier one. Deliberately gentle. Mirrors _BALANCE.
_BALANCE_XP = {1: (0.15, 0.90, 1.15), 2: (0.30, 0.80, 1.30), 3: (0.50, 0.70, 1.50)}


def _base_mxp(src, name2path):
    """Each creature's base awarded XP (mXP), read from its own Init script."""
    out = {}
    for nm, path in name2path.items():
        try:
            chunk = T.parse_full("%s/scripts/%s.luc" % (src, path))
        except Exception:
            continue
        p = chunk.protos[1] if len(chunk.protos) > 1 else chunk
        ki = {i for i, (t, v) in enumerate(p.consts) if E._s(v) == "mXP"}
        for w in p.code:
            if (w & 0x3F) == 9:  # SETTABLE A B C ; self.mXP = RK(C)
                B, C = (w >> 15) & 0x1FF, (w >> 6) & 0x1FF
                if B >= 250 and (B - 250) in ki and C >= 250:
                    cv = p.consts[C - 250][1]
                    if isinstance(cv, (int, float)) and not isinstance(cv, bool):
                        out[nm] = int(cv)
    return out


def _balance_xp_transforms(
    src, seed, level, keep_bosses, include_tutorial, balance_xp_level
):
    """Emit `set_xp` ops: set each MOVED creature's awarded XP to ~what its new slot normally
    gives, scaled by the power ratio between where it came from and where it now sits (a small
    buff for tougher-than-usual, a small nerf for easier). Same permutation as the shuffle."""
    if not balance_xp_level or balance_xp_level not in _BALANCE_XP:
        return []
    exp, lo, hi = _BALANCE_XP[balance_xp_level]
    per_book, orig, name2path = _assign(
        src, seed, int(level), keep_bosses, include_tutorial
    )
    base = _base_mxp(src, name2path)
    orig_pos = {}
    for b in BOOKS:
        for i, s in enumerate(per_book[b][4]):
            orig_pos[orig[(b, i)]] = (b, s[0], s[1])
    ops, seen = [], set()
    for b in BOOKS:
        for i, s in enumerate(per_book[b][4]):
            nm = s[7]
            if nm == orig[(b, i)] or nm in seen:
                continue
            seen.add(nm)
            expected = base.get(orig[(b, i)])  # XP this slot normally awards
            if not expected:
                continue
            ob, oc, os_ = orig_pos[nm]  # nm's intrinsic tier (where it came from)
            factor = (_power(ob, oc, os_) / _power(b, s[0], s[1])) ** exp
            factor = max(lo, min(hi, factor))
            target = max(1, round(expected * factor))
            if target == base.get(nm):
                continue
            rel = _resolve(src, "scripts/" + name2path[nm] + ".luc")
            ops.append({"file": rel, "op": "set_xp", "value": int(target)})
    return ops


# ---- treasure awards ----------------------------------------------------------------


def _treasure_names(src):
    names = set()
    for b in BOOKS:
        tp = E.treasure_proto(T.parse_full("%s/scripts/packs/Book%d.luc" % (src, b)))
        if tp:
            names.update(p.split("/")[-1] for _, _, p in E.treasure_slots(tp))
    return names


def _resolve(src, relpath):
    """Resolve `relpath` case-insensitively against `src`, returning the real (pak-case)
    relpath. packs/ preloader strings occasionally differ in case from the stored script
    file (e.g. 'PharoahofOld' vs 'PharoahOfOld.luc'); the engine is case-insensitive but
    extraction/repack on Linux is not, so the substitute must use the real case."""
    cur, real = src, []
    for part in relpath.split("/"):
        try:
            entries = os.listdir(cur)
        except OSError:
            real.append(part)
            cur = os.path.join(cur, part)
            continue
        match = next((e for e in entries if e.lower() == part.lower()), part)
        real.append(match)
        cur = os.path.join(cur, match)
    return "/".join(real)


def _treasure_plan(src, seed):
    """{boss_script_relpath: (old_treasure, new_treasure)} for bosses whose award changed,
    shuffled within each book."""
    tnames = _treasure_names(src)
    plan = {}
    for b in BOOKS:
        pchaps = E.chapter_slots(
            E.chapter_scripts_proto(
                T.parse_full("%s/scripts/packs/Book%d.luc" % (src, b))
            )
        )
        bosses = []
        for ch in pchaps:
            rel = _resolve(src, "scripts/%s.luc" % ch[-1][2])  # boss creature script
            bosses.append(
                (rel, E.boss_treasure(T.parse_full("%s/%s" % (src, rel)), tnames))
            )
        rng = random.Random("%s:treasures:book%d" % (seed, b))
        news = [o for _, o in bosses]
        rng.shuffle(news)
        for (rel, old), new in zip(bosses, news):
            if old and new and new != old:
                plan[rel] = (old, new)
    return plan


def _roster_transforms(src, seed, level, keep_bosses, include_tutorial):
    """Composable form of the enemy-roster shuffle. Instead of writing whole Book{N}.luc
    files (which would claim them exclusively and block other Book{N} mods), emit one
    repoint_roster op per book for books/ (bare AddEnemy names) and, at level>=2, packs/
    (full preloader paths). Reuses the exact deterministic permutation from _assign, so the
    composed result is byte-identical to the old file-writing path."""
    per_book, orig, name2path = _assign(
        src, seed, int(level), keep_bosses, include_tutorial
    )
    ops = []
    for b in BOOKS:
        packs, books, pproto, bproto, slots = per_book[b]
        brep, prep = [], []
        for i, s in enumerate(slots):
            nm = s[7]
            if nm != orig[(b, i)]:
                brep.append([s[3], s[4], nm])
                if int(level) >= 2:
                    prep.append([s[5], s[6], name2path[nm]])
        if brep:
            ops.append(
                {
                    "op": "repoint_roster",
                    "file": _resolve(src, "scripts/books/Book%d.luc" % b),
                    "proto_index": books.protos.index(bproto),
                    "repoints": brep,
                }
            )
        if prep:
            ops.append(
                {
                    "op": "repoint_roster",
                    "file": _resolve(src, "scripts/packs/Book%d.luc" % b),
                    "proto_index": packs.protos.index(pproto),
                    "repoints": prep,
                }
            )
    return ops


def gen_transforms(
    src,
    *,
    seed="0",
    treasures=False,
    balance_hp=0,
    balance_xp=0,
    level=2,
    keep_bosses=True,
    include_tutorial=False,
    enemies=True,
    **_,
):
    """Composable transforms (applied in the build's compose pass, so they stack with other
    mods): roster shuffle + optional treasure shuffle + optional HP/XP balancing, all from one
    deterministic permutation."""
    ops = []
    if enemies:
        ops += _roster_transforms(
            src, seed, int(level), bool(keep_bosses), bool(include_tutorial)
        )
    if treasures:
        ops += [
            {"op": "repoint_global", "file": rel, "from": old, "to": new}
            for rel, (old, new) in sorted(_treasure_plan(src, seed).items())
        ]
    if balance_hp:
        ops += _balance_transforms(
            src,
            seed,
            int(level),
            bool(keep_bosses),
            bool(include_tutorial),
            int(balance_hp),
        )
    if balance_xp:
        ops += _balance_xp_transforms(
            src,
            seed,
            int(level),
            bool(keep_bosses),
            bool(include_tutorial),
            int(balance_xp),
        )
    return ops


# ---- entry points -------------------------------------------------------------------


def build(
    src,
    base_pak,
    out_pak,
    *,
    seed=0,
    enemies=True,
    level=2,
    keep_bosses=True,
    include_tutorial=False,
    keep_stage=False,
    **_,
):
    """The enemy roster, treasure shuffle, and HP balancing are ALL delivered as composable
    transforms now (see gen_transforms / _roster_transforms), so they stack with code-inject
    mods that touch the same Book{N}.luc (e.g. disable_dialog, enable_scramble). This builder
    therefore writes no files of its own -- it produces a pass-through pak so the client's
    stage_builder harvest claims nothing exclusively. The CLI applies the transforms itself
    (see cli). Kept as a 'builder' purely so the build calls gen_transforms for it."""
    stage = pathlib.Path(str(out_pak) + ".stage")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    summary = {
        "seed": seed,
        "level": int(level),
        "enemies": bool(enemies),
        "keep_bosses": bool(keep_bosses),
        "include_tutorial": bool(include_tutorial),
        "composable": True,
    }
    summary["subbed"] = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return summary


def _apply_transforms_to_stage(src, stage, ops):
    """Apply repoint_global / scale_health ops to scripts, emitting them into `stage`.
    Ops are grouped by file so a creature that is both rescaled and treasure-repointed
    lands on a single chunk; each file is read from the stage if a prior pass wrote it."""
    from collections import OrderedDict
    from bwakit.bytecode.edit import scale_loadks

    by_file = OrderedDict()
    for op in ops:
        by_file.setdefault(op["file"], []).append(op)
    for rel, fops in by_file.items():
        spath = stage / rel
        chunk = T.parse_full(str(spath) if spath.exists() else "%s/%s" % (src, rel))
        for op in fops:
            if op["op"] == "repoint_global":
                E.repoint_global(chunk, op["from"], op["to"])
            elif op["op"] == "scale_health":
                p = chunk.protos[1]
                if (p.code[2] & 0x3F) == 1:
                    scale_loadks(p, [2], op["factor"])
            elif op["op"] == "repoint_roster":
                p = chunk.protos[op["proto_index"]]
                for pc, kidx, val in op["repoints"]:
                    E.repoint_path(p, pc, kidx, val)
            elif op["op"] == "set_xp":
                from bwakit.bytecode.edit import set_xp

                set_xp(chunk.protos[1], op["value"])
        spath.parent.mkdir(parents=True, exist_ok=True)
        open(spath, "wb").write(INV.emit_popcap(chunk))


def cli(args):
    ap = argparse.ArgumentParser(prog="bwa mod randomizer")
    ap.add_argument(
        "--src", required=True, help="extracted pak root (contains scripts/)"
    )
    ap.add_argument("--base", required=True, help="clean main.pak to repack onto")
    ap.add_argument("-o", "--out", required=True, help="output pak")
    ap.add_argument("--seed", default="1", help="randomizer seed (any text or number)")
    ap.add_argument(
        "--level",
        type=int,
        default=2,
        choices=tuple(range(11)),
        help="0-10 shuffle scale: 0=off, 1=within chapter, 2-5=within book (widening), 6-10=across books (10=fully random)",
    )
    ap.add_argument(
        "--no-enemies", action="store_true", help="don't shuffle enemy order"
    )
    ap.add_argument(
        "--treasures", action="store_true", help="also shuffle treasure awards"
    )
    ap.add_argument(
        "--include-tutorial",
        action="store_true",
        help="also shuffle the Book 1 tutorial (1.1); use with the mechanic mods",
    )
    ap.add_argument(
        "--randomize-bosses",
        action="store_true",
        help="also shuffle each chapter's boss slot (riskier)",
    )
    ap.add_argument(
        "--balance-hp",
        type=int,
        default=0,
        choices=(0, 1, 2, 3),
        help="rescale moved-enemy HP by Lex's power ratio: 0=off,1=gentle,2=match,3=harsh",
    )
    ap.add_argument(
        "--balance-xp",
        type=int,
        default=0,
        choices=(0, 1, 2, 3),
        help="rescale moved-enemy XP toward the slot's expected value: 0=off,1=gentle,2=match,3=harsh",
    )
    a = ap.parse_args(args)
    # The roster shuffle is now composable too, so apply EVERYTHING (roster + treasures +
    # balance) as transforms in one pass. build() no longer writes files itself.
    ops = gen_transforms(
        a.src,
        seed=a.seed,
        treasures=a.treasures,
        balance_hp=a.balance_hp,
        balance_xp=a.balance_xp,
        level=a.level,
        keep_bosses=not a.randomize_bosses,
        include_tutorial=a.include_tutorial,
        enemies=not a.no_enemies,
    )
    stage = pathlib.Path(a.out + ".stage")
    shutil.rmtree(stage, ignore_errors=True)
    _apply_transforms_to_stage(a.src, stage, ops)
    res = {
        "seed": a.seed,
        "level": a.level,
        "enemies": not a.no_enemies,
        "keep_bosses": not a.randomize_bosses,
        "include_tutorial": a.include_tutorial,
        "treasures": a.treasures,
        "balance_hp": a.balance_hp,
        "balance_xp": a.balance_xp,
        "transforms": len(ops),
    }
    res["subbed"] = R.repack(a.base, str(stage), a.out)[1]
    shutil.rmtree(stage, ignore_errors=True)
    print(res)
