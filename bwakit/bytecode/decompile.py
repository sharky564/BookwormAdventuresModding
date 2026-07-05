#!/usr/bin/env python3
"""decompile.py -- one command: PopCap .luc  ->  clean, luac-valid Lua 5.1 source.

Per file the pipeline is:
    transcode (PopCap 0x56 -> stock Lua 5.1)        [bwakit.bytecode.luc_transcode]
    -> unluac  (patched: 5.0-style ForBlock50 + BOTTOM_CONDITION dialect)   [Java jar]
    -> fix_decompiled post-passes (self-ref, FLOOR, generic-for, SETLIST, ...)  [fix_decompiled]

Accepts a single .luc file, a directory tree of .luc files, or a whole main.pak.

unluac is a Java program, so this shells out to `java -jar`. The patched jar is located via,
in order: the `jar=` argument, $BWA_UNLUAC_JAR, `unluac_built.jar`/`unluac.jar` in the current
directory, or the same names searched upward from this file (the kit ships one at its root).
A Java runtime (JRE) must be on PATH.

Rebuild the jar from the bundled source any time with:
    rm -rf out && mkdir out
    cd unluac && java --module jdk.compiler/com.sun.tools.javac.Main -d ../out $(find src -name '*.java')
    java --module jdk.jartool/sun.tools.jar.Main cfe ../unluac_built.jar unluac.Main -C ../out .
"""

import os
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path

from bwakit.bytecode import luc_transcode as _T
from bwakit.bytecode import fix_decompiled as _FD

_JAR_NAMES = ("unluac_built.jar", "unluac.jar")


def find_unluac_jar(jar=None):
    """Locate the patched unluac jar. Raises FileNotFoundError with guidance if missing."""
    cands = []
    if jar:
        cands.append(jar)
    env = os.environ.get("BWA_UNLUAC_JAR")
    if env:
        cands.append(env)
    here = Path(__file__).resolve()
    for name in _JAR_NAMES:
        cands.append(name)  # current working directory
        for p in here.parents:  # upward from this file (kit root ships one)
            cands.append(str(p / name))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "patched unluac jar not found. Pass jar=<path>, set $BWA_UNLUAC_JAR, or put "
        "unluac_built.jar in the current directory. Build it from the bundled unluac/ "
        "source -- see this module's docstring."
    )


def _java():
    j = shutil.which("java")
    if not j:
        raise RuntimeError(
            "`java` not found on PATH; a Java runtime is required to run unluac."
        )
    return j


def decompile_luc(luc_path, jar=None):
    """Decompile one PopCap .luc file and return the Lua source as a string."""
    jar = find_unluac_jar(jar)
    fd, tmp = tempfile.mkstemp(suffix=".luc")
    os.close(fd)
    try:
        _T.transcode(str(luc_path), tmp)
        proc = subprocess.run(
            [_java(), "-jar", jar, tmp], capture_output=True, text=True, timeout=180
        )
        if "Exception" in proc.stderr:
            first = proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else "?"
            raise RuntimeError("unluac failed on %s: %s" % (luc_path, first))
        return _FD.fix_text(proc.stdout)[0]
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def decompile_tree(src_dir, out_dir, jar=None, verbose=True):
    """Decompile every .luc under src_dir into a mirrored .lua tree under out_dir.

    Returns {"ok": [rel, ...], "failed": [(rel, error), ...]}."""
    jar = find_unluac_jar(jar)
    src_dir, out_dir = Path(src_dir), Path(out_dir)
    files = sorted(src_dir.rglob("*.luc"))
    ok, failed = [], []
    for f in files:
        rel = f.relative_to(src_dir)
        out = out_dir / rel.with_suffix(".lua")
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            out.write_text(decompile_luc(f, jar))
            ok.append(str(rel))
        except Exception as e:  # noqa: BLE001 - report and continue
            failed.append((str(rel), str(e)))
            if verbose:
                print("  FAIL %s: %s" % (rel, e))
    if verbose:
        print("decompiled %d/%d files -> %s" % (len(ok), len(files), out_dir))
        if failed:
            print("  %d failed" % len(failed))
    return {"ok": ok, "failed": failed}


def decompile_pak(pak_path, out_dir, jar=None, verbose=True):
    """Extract a main.pak and decompile its scripts into out_dir."""
    jar = find_unluac_jar(jar)
    tmp = tempfile.mkdtemp(prefix="bwa_pak_")
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "bwakit.popcap_pak",
                str(pak_path),
                "--extract",
                tmp,
            ],
            check=True,
        )
        scripts = Path(tmp) / "scripts"
        src = scripts if scripts.is_dir() else Path(tmp)
        return decompile_tree(src, out_dir, jar, verbose)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    import argparse

    argv = argv if argv is not None else sys.argv[1:]
    ap = argparse.ArgumentParser(
        prog="bwa decompile",
        description="PopCap .luc / directory / main.pak -> clean Lua 5.1 source",
    )
    ap.add_argument(
        "input", help="a .luc file, a directory of .luc files, or a main.pak"
    )
    ap.add_argument(
        "-o",
        "--out",
        help="output .lua (single file) or directory (tree/pak); "
        "default 'decompiled/' for trees and stdout for a single file",
    )
    ap.add_argument(
        "--jar", help="patched unluac jar (else auto-located / $BWA_UNLUAC_JAR)"
    )
    a = ap.parse_args(argv)
    inp = Path(a.input)
    if inp.is_file() and inp.suffix == ".luc":
        src = decompile_luc(inp, a.jar)
        if a.out:
            Path(a.out).write_text(src)
            print("wrote", a.out)
        else:
            sys.stdout.write(src)
    elif inp.is_file():  # treat any other file as a pak
        decompile_pak(inp, a.out or "decompiled", a.jar)
    elif inp.is_dir():
        decompile_tree(inp, a.out or "decompiled", a.jar)
    else:
        ap.error("input not found: %s" % inp)


if __name__ == "__main__":
    main()
