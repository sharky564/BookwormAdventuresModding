"""bwa <command> -- thin dispatcher over the modding kit.

  bwa unpack <pak> <outdir>                 extract a .pak
  bwa repack <orig> <moddir> <out>          repack a moddir onto a base pak
  bwa mod <name> [opts...]                  run one mod builder (see bwakit.mods.<name>)
  bwa build --base B -o OUT MOD [MOD...]     apply several mods in sequence (compose)
  bwa decompile <pak|dir|.luc> -o OUT       decompile PopCap Lua to clean Lua 5.1 source
  bwa compile <.lua|dir> -o OUT             compile clean Lua 5.1 source back to PopCap .luc

A build MOD spec is `name[:k=v,k=v]`, e.g.  hp-scaling:src=pak_out  dict-swap:wordlist=words.txt
Each mod's output becomes the next mod's base, so the final pak has all of them layered.
"""

import argparse
import sys


def _coerce(v):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="bwa")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("unpack")
    sp.add_argument("pak")
    sp.add_argument("outdir")
    rp = sub.add_parser("repack")
    rp.add_argument("orig")
    rp.add_argument("moddir")
    rp.add_argument("out")
    mp = sub.add_parser("mod")
    mp.add_argument("name")
    mp.add_argument("rest", nargs=argparse.REMAINDER)
    bp = sub.add_parser(
        "build",
        help="apply several mods in sequence, chaining each output into the next base",
    )
    bp.add_argument("--base", required=True)
    bp.add_argument("-o", "--out", required=True)
    bp.add_argument(
        "mods",
        nargs="+",
        metavar="MOD",
        help="mod spec name[:k=v,k=v], e.g. hp-scaling:src=pak_out dict-swap:wordlist=w.txt",
    )
    dp = sub.add_parser(
        "decompile", help="PopCap .luc / directory / main.pak -> clean Lua 5.1 source"
    )
    dp.add_argument(
        "input", help="a .luc file, a directory of .luc files, or a main.pak"
    )
    dp.add_argument(
        "-o", "--out", help="output .lua (single file) or directory (tree/pak)"
    )
    dp.add_argument(
        "--jar", help="patched unluac jar (else auto-located / $BWA_UNLUAC_JAR)"
    )
    cp = sub.add_parser(
        "compile", help="clean Lua 5.1 source -> PopCap .luc (luac + transcode)"
    )
    cp.add_argument("input", help="a .lua file or a directory of .lua files")
    cp.add_argument("-o", "--out", help="output .luc (single file) or directory (tree)")
    cp.add_argument(
        "--luac", help="luac (Lua 5.1, LFIELDS_PER_FLUSH=32); else $BWA_LUAC / PATH"
    )
    cp.add_argument(
        "--ref",
        help="original PopCap .luc (file) or directory of originals (tree) "
        "to pin constant types and per-PC opcode variants",
    )
    a = p.parse_args(argv)

    if a.cmd == "unpack":
        import runpy

        sys.argv = ["popcap_pak", a.pak, "--extract", a.outdir]
        runpy.run_module("bwakit.popcap_pak", run_name="__main__")
    elif a.cmd == "repack":
        from bwakit import popcap_pak_repack as R

        print(R.repack(a.orig, a.moddir, a.out))
    elif a.cmd == "mod":
        from importlib import import_module

        import_module(f"bwakit.mods.{a.name.replace('-', '_')}").cli(a.rest)
    elif a.cmd == "build":
        from importlib import import_module
        import os

        cur, intermediates, n = a.base, [], len(a.mods)
        for i, spec in enumerate(a.mods):
            name, _, optstr = spec.partition(":")
            opts = {}
            for kv in optstr.split(",") if optstr else []:
                k, _, v = kv.partition("=")
                if k.strip():
                    opts[k.strip()] = _coerce(v.strip())
            mod = import_module(f"bwakit.mods.{name.replace('-', '_')}")
            out = a.out if i == n - 1 else f"{a.out}.step{i}"
            print(
                f"[{name}] {opts}  base={os.path.basename(cur)} -> {os.path.basename(out)}"
            )
            print("   ", mod.build(base_pak=cur, out_pak=out, **opts))
            if cur != a.base:
                intermediates.append(cur)
            cur = out
        for t in intermediates:
            try:
                os.remove(t)
            except OSError:
                pass
    elif a.cmd == "decompile":
        from bwakit.bytecode import decompile as D

        argv2 = [a.input]
        if a.out:
            argv2 += ["-o", a.out]
        if a.jar:
            argv2 += ["--jar", a.jar]
        D.main(argv2)
    elif a.cmd == "compile":
        from bwakit.bytecode import compile as C

        argv2 = [a.input]
        if a.out:
            argv2 += ["-o", a.out]
        if a.luac:
            argv2 += ["--luac", a.luac]
        if a.ref:
            argv2 += ["--ref", a.ref]
        return C.main(argv2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
