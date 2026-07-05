#!/usr/bin/env python3
"""Entry point for the packaged Bookworm Adventures Mod Builder (web UI).

Double-click the built executable, or run `python bwa_mod_gui.py`. Starts a local web
server bound to localhost and opens the browser. This is the PyInstaller target - see
BUILDING.md.
"""

import os
import sys

# Dev tree: make `modkit` and a sibling `bwakit` importable. When frozen, PyInstaller
# has already bundled both onto the path, so this loop is a no-op.
if not getattr(sys, "frozen", False):
    HERE = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        HERE,
        os.path.join(HERE, "bwakit"),
        os.path.join(os.path.dirname(HERE), "bwakit"),
    ):
        if os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)

from modkit import webui  # noqa: E402

if __name__ == "__main__":
    webui.main()
