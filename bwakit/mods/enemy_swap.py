"""Replace enemies at chapter slots (the enemy-swap mod), built on bwakit.game.encounters.

A swap is (book, chapter, slot, creature_path), all 1-based. For example
(1, 1, 4, "creatures/Book2/Djinn") puts the Book-2 Djinn into Book 1, Chapter 1, slot 4 -
bringing its attacks, HP, and animations. The chapter's script list is what the engine
loads, so the swapped-in creature loads from its path; exotic cross-book swaps can still hit
missing per-chapter resources (PAM/dialog), so playtest swaps as usual."""

import shutil
import pathlib
import argparse

from bwakit.bytecode import luc_transcode as T, luc_inverse_transcode as INV
from bwakit import popcap_pak_repack as R
from bwakit.game import encounters


def build(src, base_pak, out_pak, swaps, *, keep_stage=False):
    """src = extracted pak root (contains scripts/packs/); swaps = [(book,chapter,slot,path), ...] 1-based."""
    stage = pathlib.Path(str(out_pak) + ".stage")
    shutil.rmtree(stage, ignore_errors=True)
    by_book = {}
    for b, c, s, new in swaps:
        by_book.setdefault(int(b), []).append((int(c), int(s), new))
    applied = []
    for b, sw in sorted(by_book.items()):
        chunk = T.parse_full(f"{src}/scripts/packs/Book{b}.luc")
        p = encounters.chapter_scripts_proto(chunk)
        for c, s, new in sw:
            old = encounters.set_slot(p, c - 1, s - 1, new)
            applied.append({"book": b, "chapter": c, "slot": s, "from": old, "to": new})
        od = stage / "scripts/packs"
        od.mkdir(parents=True, exist_ok=True)
        open(od / f"Book{b}.luc", "wb").write(INV.emit_popcap(chunk))
    subbed = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return {"swaps": applied, "subbed": subbed, "out": str(out_pak)}


def _parse_spec(spec):
    loc, _, path = spec.partition("=")
    b, c, s = loc.split(":")
    return (int(b), int(c), int(s), path)


def cli(args):
    ap = argparse.ArgumentParser(prog="bwa mod enemy-swap")
    ap.add_argument(
        "--src", required=True, help="extracted pak root (contains scripts/packs/)"
    )
    ap.add_argument(
        "--list",
        type=int,
        metavar="BOOK",
        help="print Book<BOOK>'s roster with slot indices and exit",
    )
    ap.add_argument(
        "--base", help="clean main.pak to repack onto (required unless --list)"
    )
    ap.add_argument("-o", "--out", help="output pak (required unless --list)")
    ap.add_argument(
        "swaps",
        nargs="*",
        metavar="SWAP",
        help="book:chapter:slot=creature_path, e.g. 1:1:4=creatures/Book2/Djinn",
    )
    a = ap.parse_args(args)
    if a.list:
        for ci, ch in enumerate(
            encounters.read_roster(f"{a.src}/scripts/packs/Book{a.list}.luc"), 1
        ):
            print(f"Book {a.list} Chapter {ci}:")
            for si, path in enumerate(ch, 1):
                print(
                    f"  {a.list}:{ci}:{si}  {path}"
                    + ("   <- boss" if si == len(ch) else "")
                )
        return
    if not (a.base and a.out and a.swaps):
        ap.error("--base, -o, and at least one SWAP are required unless --list")
    print(build(a.src, a.base, a.out, [_parse_spec(s) for s in a.swaps]))
