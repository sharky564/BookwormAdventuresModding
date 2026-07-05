@echo off
REM Launch the mod builder (local web UI). Requires Python 3.9+.
cd /d "%~dp0"
python bwa_mod_gui.py %*
