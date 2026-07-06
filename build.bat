@echo off
setlocal
REM Build BookwormModBuilder.exe (single-file, windowed). Run this ON WINDOWS.
REM Prereqs: Python 3 + `pip install pyinstaller`, and an optional Windows luac.exe in
REM this folder (Lua 5.1.5 built with LFIELDS_PER_FLUSH=32 -- see BUILDING.md).
REM The build folder must contain: bwa_mod_gui.py, modkit\, bwakit\, mods\, (luac.exe).

set LUAC=
if exist luac.exe set LUAC=--add-binary "luac.exe;."

pyinstaller --onefile --windowed --name BookwormModBuilder ^
  --add-data "mods;mods" ^
  --add-data "modkit\static;modkit\static" ^
  --add-data "bwakit\game\data;bwakit\game\data" ^
  --collect-data bwakit ^
  --collect-submodules bwakit ^
  %LUAC% ^
  bwa_mod_gui.py

echo.
echo Done. The app is dist\BookwormModBuilder.exe
endlocal
