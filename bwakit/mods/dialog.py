"""Rewrite enemy (or any) dialog by swapping text constants in a .luc -- no recompile.

Dialog scripts are linear call-sequences (SetInitialIntroText / AddIntroText / AddAttackText /
AddDeathText, ...), so the safe, lossless edit is to replace the *string constants* in place and
re-emit the bytecode untouched -- byte-identical except for the strings you change. This sidesteps
the decompile->recompile round-trip, which isn't byte-reversible on larger scripts and can silently
corrupt control flow. Only constant values change (never count/order), so every instruction still
points at the right slot.

Adding/removing lines can't be a constant swap: write new .lua + `bwa compile NEW.lua --ref ORIG.luc`
(recompile is only safe on branch-free scripts; dialog.is_linear() confirms). See cli() for usage.
"""

import os
import json
import shutil
import argparse
import pathlib

from bwakit.bytecode import luc_transcode as T, luc_inverse_transcode as INV
from bwakit import popcap_pak_repack as R

LOADK = 1  # PopCap opcode for LOADK (0-15 match stock); Bx = (ins >> 6) & 0x3FFFF
_CONTROL_FLOW = {
    22,
    30,
    31,
    32,
}  # JMP / FORLOOP / TFORLOOP / generic-for entry -> presence means branches


def _walk(proto, path="main"):
    """Yield (proto_path, proto) for the chunk and every nested proto, depth-first."""
    yield path, proto
    for i, sub in enumerate(proto.protos):
        yield from _walk(sub, "%s.%d" % (path, i))


def _chunk(luc):
    """Accept a path or an already-parsed chunk."""
    return luc if hasattr(luc, "protos") else T.parse_full(luc)


def _to_bytes(s):
    return s.encode("latin1") if isinstance(s, str) else s


def dialog_lines(luc):
    """Return [(proto_path, const_index, text), ...] for every string that is LOADK'd as an
    argument -- i.e. exactly the lines you can edit. Structural strings (method names and
    global names, reached via SELF / GETGLOBAL rather than LOADK) are excluded. `luc` is a
    path or a parsed chunk. Lines come back in code order, grouped by function."""
    chunk = _chunk(luc)
    out = []
    for path, p in _walk(chunk):
        idxs = sorted({(w >> 6) & 0x3FFFF for w in p.code if (w & 0x3F) == LOADK})
        for i in idxs:
            if i < len(p.consts) and p.consts[i][0] == "str":
                out.append((path, i, p.consts[i][1].rstrip(b"\x00").decode("latin1")))
    return out


def replace_dialog_text(luc, replacements, out_path=None):
    """Swap exact dialog strings, leaving all bytecode byte-for-byte intact.

    `replacements` : {old_text: new_text} (str or bytes). A string constant is changed only
                     when its value equals a key, so structural names are safe as long as you
                     don't map them. New text may be any length; the bytecode is re-emitted
                     with corrected string headers but identical instructions.
    Returns (new_luc_bytes, num_replaced). Writes out_path if given.
    """
    chunk = _chunk(luc)
    repl = {
        _to_bytes(k).rstrip(b"\x00"): _to_bytes(v).rstrip(b"\x00")
        for k, v in replacements.items()
    }
    n = 0
    for _, p in _walk(chunk):
        new_consts = []
        for typ, val in p.consts:
            if typ == "str" and val.rstrip(b"\x00") in repl:
                new_consts.append(("str", repl[val.rstrip(b"\x00")] + b"\x00"))
                n += 1
            else:
                new_consts.append((typ, val))
        p.consts = new_consts
    data = INV.emit_popcap(chunk)
    if out_path:
        pathlib.Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
    return data, n


def is_linear(luc):
    """True if the script contains no control-flow ops (JMP/FORLOOP/TFORLOOP/for-in). Dialog
    files are linear, which is what makes recompiling them safe; use this to confirm a file
    you produced with `bwa compile` carries no branches before shipping it."""
    chunk = _chunk(luc)
    return not any((w & 0x3F) in _CONTROL_FLOW for _, p in _walk(chunk) for w in p.code)


def build(src, base_pak, out_pak, edits, keep_stage=False):
    """Apply dialog text edits and repack onto a clean pak.

    `src`   : extracted (de-XOR'd) pak root that contains scripts/...
    `edits` : {luc_relpath: {old_text: new_text}}, e.g.
              {"scripts/creatures/Book1/dialog/Alexander.luc": {old: new, ...}}.
    Returns {"files": [{file, replaced, requested, unmatched}], "subbed": N, "out": path}.
    """
    stage = pathlib.Path(str(out_pak) + ".stage")
    if stage.exists():
        shutil.rmtree(stage)
    report = []
    for rel, repl in edits.items():
        src_luc = os.path.join(src, rel)
        present = {t for _, _, t in dialog_lines(src_luc)}
        unmatched = sorted(k for k in repl if k not in present)
        _, n = replace_dialog_text(src_luc, repl, str(stage / rel))
        report.append(
            {"file": rel, "replaced": n, "requested": len(repl), "unmatched": unmatched}
        )
    subbed = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return {"files": report, "subbed": subbed, "out": str(out_pak)}


def cli(args):
    ap = argparse.ArgumentParser(
        prog="bwa mod dialog",
        description="Swap enemy dialog text in place (no recompile).",
    )
    ap.add_argument(
        "--src", required=True, help="extracted pak root (contains scripts/)"
    )
    ap.add_argument(
        "--list",
        metavar="LUC",
        help="print the editable dialog lines of one .luc (relpath under --src) and exit",
    )
    ap.add_argument(
        "--base", help="clean main.pak to repack onto (required unless --list)"
    )
    ap.add_argument("-o", "--out", help="output pak (required unless --list)")
    ap.add_argument(
        "--edits", help='JSON file: {"<luc relpath>": {"<old>": "<new>", ...}, ...}'
    )
    a = ap.parse_args(args)

    if a.list:
        cur = None
        for path, idx, text in dialog_lines(os.path.join(a.src, a.list)):
            if path != cur:
                print(path + ":")
                cur = path
            print("  #%-3d %r" % (idx, text))
        return
    if not (a.base and a.out and a.edits):
        ap.error("--base, -o, and --edits are required unless --list")
    with open(a.edits) as f:
        edits = json.load(f)
    result = build(a.src, a.base, a.out, edits)
    for r in result["files"]:
        flag = (
            ""
            if r["replaced"] == r["requested"]
            else "  (!) unmatched: %r" % (r["unmatched"],)
        )
        print(
            "  %s: replaced %d/%d%s" % (r["file"], r["replaced"], r["requested"], flag)
        )
    print(
        "subbed %d entr%s -> %s"
        % (result["subbed"], "y" if result["subbed"] == 1 else "ies", result["out"])
    )
