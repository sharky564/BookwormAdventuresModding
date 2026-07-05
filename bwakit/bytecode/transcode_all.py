#!/usr/bin/env python3
"""transcode_all.py - batch-transcode PopCap 0x56 .luc -> stock Lua 5.1 .luac.

Usage:
    python3 transcode_all.py <src_dir_with_.luc> <out_dir>

Walks src_dir, transcodes every .luc, writes a mirrored tree of .luac under out_dir,
validates each via the stock reader (lua51_stock), and writes _MANIFEST.json listing
which files are fully-correct vs. flagged (rk_overflow).

Then, to decompile to readable Lua source, run unluac on the output, e.g.:
    java -jar unluac.jar out_dir/scripts/tiles/EmeraldTile.luac > EmeraldTile.lua

(unluac: https://github.com/saucecode/unluac - any Lua 5.1-capable build works.
 luadec for 5.1 also accepts these files.)
"""

import sys, os, json
from bwakit.bytecode import luc_transcode as T

try:
    import lua51_stock as S

    HAVE_VALIDATOR = True
except Exception:
    HAVE_VALIDATOR = False


def main(src, out):
    files = []
    for dp, _, fs in os.walk(src):
        for f in fs:
            if f.endswith(".luc"):
                files.append(os.path.join(dp, f))
    files.sort()
    manifest = {"clean": [], "rk_overflow": [], "failed": []}
    for path in files:
        rel = os.path.relpath(path, src)
        dst = os.path.join(out, rel[:-4] + ".luac")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            chunk = T.parse_full(path)
            blob, stats = T.emit_standard(chunk)
            if HAVE_VALIDATOR:
                _, off, total = S.parse_stock(blob)
                assert off == total, f"round-trip partial {off}/{total}"
            open(dst, "wb").write(blob)
            if stats.get("rk_overflow"):
                manifest["rk_overflow"].append(
                    {"file": rel, "overflow_refs": stats["rk_overflow"]}
                )
            else:
                manifest["clean"].append(rel)
        except Exception as e:
            manifest["failed"].append({"file": rel, "error": repr(e)})
    json.dump(manifest, open(os.path.join(out, "_MANIFEST.json"), "w"), indent=1)
    print(
        f"clean={len(manifest['clean'])} "
        f"rk_overflow={len(manifest['rk_overflow'])} "
        f"failed={len(manifest['failed'])}"
    )
    for o in manifest["rk_overflow"]:
        print("  rk_overflow:", o["file"], o["overflow_refs"])
    for o in manifest["failed"]:
        print("  FAILED:", o["file"], o["error"])


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
