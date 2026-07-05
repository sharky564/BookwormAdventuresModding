#!/usr/bin/env bash
# Launch the mod builder (local web UI). Requires Python 3.9+.
cd "$(dirname "$0")"
exec python3 bwa_mod_gui.py "$@"
