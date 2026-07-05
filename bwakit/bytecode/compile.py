"""Compile-direction front end: clean Lua 5.1 source -> PopCap .luc.

Pipeline:

    your.lua  --(luac, stock Lua 5.1)-->  stock .luc  --(stock_to_popcap)-->  PopCap .luc

The heavy lifting (opcode renumbering, operand bit-field repacking, numeric- and generic-for
lowering, SETLIST paging, int32 constant re-promotion, vararg-flag normalisation) lives in
luc_inverse_transcode.stock_to_popcap; this module just drives `luac` and wires up file I/O,
mirroring decompile.py on the other side.

`luac` MUST be stock Lua 5.1 built with LFIELDS_PER_FLUSH=32 (PopCap flushes table constructors
every 32 array elements, not the stock 50). Build one with tools/build_luac.sh in the kit root,
or from the bundled lua-5_1_5 source. The luac binary is located via, in order: the `luac=`
argument, $BWA_LUAC, `luac` on PATH, then `luac` next to this kit.

Passing the ORIGINAL PopCap .luc as `ref` (when recompiling an edited copy of a shipped script)
lets the converter pin exact constant types and adopt the original's per-PC opcode variants, so
unchanged regions come out byte-identical to PopCap's own output.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .luc_inverse_transcode import stock_to_popcap


def find_luac(luac=None):
    """Locate a Lua 5.1 luac binary. Raises FileNotFoundError with guidance if missing."""
    cands = []
    if luac:
        cands.append(luac)
    env = os.environ.get("BWA_LUAC")
    if env:
        cands.append(env)
    onpath = shutil.which("luac") or shutil.which("luac5.1")
    if onpath:
        cands.append(onpath)
    here = Path(__file__).resolve()
    for name in ("luac", "luac5.1"):
        cands.append(name)  # current working directory
        for p in here.parents:  # upward from this file (kit may ship one)
            cands.append(str(p / name))
            cands.append(str(p / "tools" / name))
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise FileNotFoundError(
        "luac (stock Lua 5.1, built with LFIELDS_PER_FLUSH=32) not found. Pass --luac <path>, "
        "set $BWA_LUAC, or put luac on PATH. Build one with tools/build_luac.sh in the kit root."
    )


def compile_source(src_path, luac=None, ref=None):
    """Compile one .lua source file and return PopCap .luc bytes.

    src_path : path to a Lua 5.1 source file
    luac     : luac binary (else auto-located / $BWA_LUAC)
    ref      : optional path to the original PopCap .luc this replaces (enables exact constant
               typing + per-PC opcode-variant adoption for byte-faithful unchanged regions)
    """
    luac = find_luac(luac)
    fd, tmp = tempfile.mkstemp(suffix=".luc")
    os.close(fd)
    try:
        proc = subprocess.run(
            [luac, "-o", tmp, str(src_path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()
            raise ValueError("luac failed on %s:\n%s" % (src_path, msg))
        return stock_to_popcap(tmp, ref_luc=ref)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def compile_file(src_path, out_path, luac=None, ref=None):
    """Compile one .lua file to a .luc file on disk. Returns out_path."""
    data = compile_source(src_path, luac=luac, ref=ref)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(data)
    return out_path


def compile_tree(src_dir, out_dir, luac=None, ref_dir=None, verbose=True):
    """Compile a directory tree of .lua files into a parallel tree of .luc files.

    If ref_dir is given, each source's same-relative-path .luc there is used as its reference.
    Returns (n_ok, [(relpath, error_str), ...]) so callers can report partial failures.
    """
    luac = find_luac(luac)
    src_dir = Path(src_dir)
    out_dir = Path(out_dir)
    ok, failed = 0, []
    for f in sorted(src_dir.rglob("*.lua")):
        rel = f.relative_to(src_dir)
        outf = out_dir / rel.with_suffix(".luc")
        ref = None
        if ref_dir:
            cand = Path(ref_dir) / rel.with_suffix(".luc")
            if cand.is_file():
                ref = str(cand)
        try:
            compile_file(f, outf, luac=luac, ref=ref)
            ok += 1
            if verbose:
                print("  ok   %s" % rel)
        except Exception as e:
            failed.append((str(rel), str(e)))
            if verbose:
                print("  FAIL %s: %s" % (rel, str(e).splitlines()[0]))
    if verbose:
        print("compiled %d file(s), %d failed" % (ok, len(failed)))
    return ok, failed


def main(argv=None):
    import argparse

    a = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="bwa compile",
        description="Compile clean Lua 5.1 source to PopCap .luc (luac + PopCap transcode).",
    )
    ap.add_argument("input", help="a .lua file or a directory of .lua files")
    ap.add_argument(
        "-o",
        "--out",
        help="output .luc (single file) or directory (tree); "
        "defaults to the input name with a .luc suffix",
    )
    ap.add_argument(
        "--luac",
        help="path to luac (Lua 5.1, LFIELDS_PER_FLUSH=32); else $BWA_LUAC / PATH",
    )
    ap.add_argument(
        "--ref",
        help="for a single file: the original PopCap .luc it replaces; "
        "for a directory: a directory of originals matched by path",
    )
    ns = ap.parse_args(a)
    inp = Path(ns.input)
    if inp.is_dir():
        out = ns.out or "compiled"
        ok, failed = compile_tree(inp, out, luac=ns.luac, ref_dir=ns.ref)
        return 0 if not failed else 1
    out = ns.out or str(inp.with_suffix(".luc"))
    compile_file(inp, out, luac=ns.luac, ref=ns.ref)
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
