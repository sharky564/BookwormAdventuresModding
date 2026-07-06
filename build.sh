#!/usr/bin/env bash
# Build a single-file app on the CURRENT OS. PyInstaller does NOT cross-compile, so the
# shippable Windows .exe must be built on Windows (build.bat) -- this script is for
# sanity-testing the bundle on Linux/macOS. The build folder must contain
# bwa_mod_gui.py, modkit/, bwakit/, mods/, and optionally a `luac` binary.
set -e

luac_arg=()
if [ -f luac ]; then luac_arg=(--add-binary "luac:."); fi

pyinstaller --onefile --name BookwormModBuilder \
  --add-data "mods:mods" \
  --add-data "modkit/static:modkit/static" \
  --add-data "bwakit/game/data:bwakit/game/data" \
  --collect-data bwakit \
  --collect-submodules bwakit \
  "${luac_arg[@]}" \
  bwa_mod_gui.py

echo "Done -> dist/BookwormModBuilder"
