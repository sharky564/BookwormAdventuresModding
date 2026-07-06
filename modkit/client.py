"""bwa-mod -- CLI to build and launch a modded copy of Bookworm Adventures.

Thin presentation over modkit.core (shared with the web UI, so a CLI-built install
opens in the GUI and vice versa).

  bwa-mod init  --game <dir> [--modded <dir>] [--originals <dir>] [--no-rename] [--force]
  bwa-mod mods
  bwa-mod build <id> [<id> ...]
  bwa-mod status
  bwa-mod launch
  bwa-mod restore
"""

import os
import sys
import argparse

for cand in (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bwakit"),
):
    if cand not in sys.path and os.path.isdir(cand):
        sys.path.insert(0, cand)
from modkit import core  # noqa: E402
from modkit import debuglog  # noqa: E402


def cmd_init(a):
    try:
        st = core.init_install(
            a.game, a.modded, a.originals, a.no_rename, a.force, log=print
        )
    except Exception as e:
        debuglog.record("init failed")
        sys.exit("%s\n(full traceback: %s)" % (e, debuglog.PATH))
    print("Ready. exe '%s'." % st.get("exe"))
    print("Next:  bwa-mod mods   then   bwa-mod build <id> ...")


def cmd_mods(a):
    for m in core.catalog():
        print("%-22s  %s" % (m["id"], m["name"]))
        print("    %s" % m.get("description", "").strip())
        meta = ["kind=%s" % m["_kind"], "order=%s" % m.get("apply_order", 100)]
        if m.get("requires"):
            meta.append("requires=%s" % ",".join(m["requires"]))
        if m.get("conflicts"):
            meta.append("conflicts=%s" % ",".join(m["conflicts"]))
        print("    [%s]" % " | ".join(meta))
        if m.get("compat_note"):
            print("    note: %s" % m["compat_note"])
        spec = m.get("param_spec") or []
        defaults = m.get("params") or {}
        if spec:
            print("    options:")
            for pspec in spec:
                key = pspec["key"]
                dflt = pspec.get("default", defaults.get(key, ""))
                print(
                    "      --set %s.%s=<%s>   (default: %s)   %s"
                    % (
                        m["id"],
                        key,
                        pspec.get("type", "text"),
                        dflt,
                        pspec.get("label", ""),
                    )
                )
                if pspec.get("help"):
                    print("          %s" % pspec["help"].strip())
        print()


def _parse_set(items):
    ov = {}
    for it in items or []:
        idkey, _, val = it.partition("=")
        mid, _, key = idkey.partition(".")
        if mid and key:
            ov.setdefault(mid, {})[key] = val
    return ov


def cmd_build(a):
    try:
        r = core.build(a.mods, overrides=_parse_set(a.set), log=print)
    except Exception as e:
        debuglog.record("build failed")
        sys.exit("Build failed: %s\n(full traceback: %s)" % (e, debuglog.PATH))
    print("Built %s  (%d files; %s)" % (r["out"], r["files"], " -> ".join(r["order"])))
    print("Launch with:  bwa-mod launch")


def cmd_status(a):
    try:
        st = core.load_state()
    except Exception as e:
        sys.exit(str(e))
    print("game    : %s" % st["game_dir"])
    print("modded  : %s" % st["modded_dir"])
    print("exe     : %s" % st.get("exe"))
    print("template: %s  (sha256 %s...)" % (st["template"], st["template_sha256"][:16]))
    print("luac    : %s" % (core.luac() or "NOT FOUND (code-inject mods need it)"))
    print("installed: %s" % (", ".join(st["installed"]) or "(vanilla -- run build)"))


def cmd_launch(a):
    try:
        r = core.launch()
    except Exception as e:
        sys.exit(str(e))
    if not r["launched"]:
        print("Run the modded game:\n  %s" % r["path"])


def cmd_restore(a):
    core.restore()
    print("Restored modded main.pak to the unmodded template.")


def main(argv=None):
    debuglog.install("CLI: " + " ".join(argv if argv is not None else sys.argv[1:]))
    p = argparse.ArgumentParser(
        prog="bwa-mod", description="Build a modded copy of Bookworm Adventures."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    ip = sub.add_parser("init")
    ip.add_argument("--game", required=True)
    ip.add_argument("--modded")
    ip.add_argument("--originals")
    ip.add_argument("--no-rename", action="store_true")
    ip.add_argument("--force", action="store_true")
    ip.set_defaults(fn=cmd_init)
    sub.add_parser("mods").set_defaults(fn=cmd_mods)
    bp = sub.add_parser("build")
    bp.add_argument("mods", nargs="+")
    bp.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="ID.KEY=VAL",
        help="override a mod param, e.g. --set randomizer.seed=42",
    )
    bp.set_defaults(fn=cmd_build)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("launch").set_defaults(fn=cmd_launch)
    sub.add_parser("restore").set_defaults(fn=cmd_restore)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
