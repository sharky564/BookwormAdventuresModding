"""Give a specific enemy custom stats: clone its creature into a new self-contained class and
point a chapter slot at the clone. Uses the compile pipeline (game.creatures), so it needs the
Lua toolchain (unluac + luac); the surgical mods (hp_scaling, enemy_swap) do not.

A clone spec is `book:chapter:slot=source_creature,new_name[,hp]` (1-based book/chapter/slot).
source_creature is a creature path ("creatures/Book2/Dracula") or a bare name resolved under the
slot's book. The clone .luc is written to scripts/creatures/Book{book}/{new_name}.luc and the
slot is repointed to it. Example: 1:1:1=Cerberus,CerberusElite,40 puts a 40-HP Cerberus clone at
Book 1, Chapter 1, slot 1."""

import shutil
import pathlib
import argparse

from bwakit.bytecode import luc_transcode as T, luc_inverse_transcode as INV
from bwakit import popcap_pak_repack as R
from bwakit.game import encounters, creatures


def build(src, base_pak, out_pak, clones, *, unluac_jar, luac, keep_stage=False):
    """clones = [(book, chapter, slot, source_creature, new_name, hp), ...] (1-based)."""
    stage = pathlib.Path(str(out_pak) + ".stage")
    shutil.rmtree(stage, ignore_errors=True)
    by_book, made = {}, []
    for book, chap, slot, source_creature, new_name, hp in clones:
        book = int(book)
        sc = (
            source_creature
            if "/" in source_creature
            else f"creatures/Book{book}/{source_creature}"
        )
        clone_bytes = creatures.clone(
            f"{src}/scripts/{sc}.luc",
            new_name,
            hp=(None if hp is None else int(hp)),
            unluac_jar=unluac_jar,
            luac=luac,
        )
        outp = stage / "scripts" / "creatures" / f"Book{book}" / f"{new_name}.luc"
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_bytes(clone_bytes)
        by_book.setdefault(book, []).append(
            (int(chap), int(slot), f"creatures/Book{book}/{new_name}")
        )
        made.append(
            {
                "book": book,
                "chapter": int(chap),
                "slot": int(slot),
                "source": sc,
                "clone": f"creatures/Book{book}/{new_name}",
                "hp": hp,
                "bytes": len(clone_bytes),
            }
        )
    for book, slots in sorted(by_book.items()):
        chunk = T.parse_full(f"{src}/scripts/packs/Book{book}.luc")
        p = encounters.chapter_scripts_proto(chunk)
        for chap, slot, path in slots:
            encounters.set_slot(p, chap - 1, slot - 1, path)
        od = stage / "scripts" / "packs"
        od.mkdir(parents=True, exist_ok=True)
        (od / f"Book{book}.luc").write_bytes(INV.emit_popcap(chunk))
    subbed = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return {"clones": made, "subbed": subbed, "out": str(out_pak)}


def _parse_spec(spec):
    loc, _, rest = spec.partition("=")
    b, c, s = (int(x) for x in loc.split(":"))
    parts = rest.split(",")
    hp = int(parts[2]) if len(parts) > 2 and parts[2].strip() else None
    return (b, c, s, parts[0], parts[1], hp)


def cli(args):
    ap = argparse.ArgumentParser(prog="bwa mod enemy-clone")
    ap.add_argument("--src", required=True, help="extracted pak root")
    ap.add_argument("--base", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--unluac", required=True, help="path to unluac.jar")
    ap.add_argument("--luac", required=True, help="path to luac 5.1 binary")
    ap.add_argument(
        "clones",
        nargs="+",
        metavar="CLONE",
        help="book:chapter:slot=source_creature,new_name[,hp], e.g. 1:1:1=Cerberus,CerberusElite,40",
    )
    a = ap.parse_args(args)
    print(
        build(
            a.src,
            a.base,
            a.out,
            [_parse_spec(c) for c in a.clones],
            unluac_jar=a.unluac,
            luac=a.luac,
        )
    )
