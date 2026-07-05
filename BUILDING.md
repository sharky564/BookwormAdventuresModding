# Packaging the Mod Builder into a single executable

The app bundles into one file with [PyInstaller](https://pyinstaller.org). **PyInstaller
does not cross-compile**, so build the Windows `.exe` *on Windows*. (`build.sh` builds
for the current OS - handy for a Linux/macOS sanity check of the bundle.)

## 1. The build folder is `bwa-mod-client/` itself

Everything needed is already here - `bwakit/` is vendored alongside `modkit/`:

```
bwa-mod-client/
  bwa_mod_gui.py        # entry point
  modkit/               # engine + CLI + web UI
  bwakit/               # the unpack/compile/repack/bytecode toolkit (vendored)
  mods/                 # the mod catalog
  tools/build_luac.sh   # builds the custom luac (step 2)
  build.bat             # the build command
  luac.exe              # you add this (optional; see step 2)
```

The engine calls `bwakit` in-process (no Python subprocess is spawned), so it freezes
cleanly. Just build from inside this folder.

## 2. Get a luac (only for code-inject mods)

The code-inject mods (`enemy_resistance`, `misunderstanding_rack`, `skip_intro_tutorial`)
compile a small Lua method, which needs the game's flavor of `luac`: **Lua 5.1.5 built
with `LFIELDS_PER_FLUSH = 32`**. The file-replace (`dictionary_swap`) and builder
(`enemy_hp_scaling`, `randomizer`) mods do **not** need it.

`tools/build_luac.sh <lua-5.1.5 source dir or zip>` builds it on Unix (it patches
`src/lopcodes.h` and compiles); on Windows do the same edit and build with your C
toolchain. Drop the resulting `luac.exe` in this folder. The app finds a bundled
`luac.exe`/`luac` automatically (it also honours a `BWA_LUAC` env var, then `PATH`).
Without it, the three code-inject mods fail with a clear "luac not found" message and
everything else still builds.

## 3. Build

```
pip install pyinstaller
build.bat            # Windows      -> dist\BookwormModBuilder.exe
./build.sh           # current OS   (sanity check)
```

The flags bundle `mods/`, `modkit/static/`, all of `bwakit` (code + its data files), and
`luac` into one file; `--windowed` means no console window appears.

## 4. Run

Double-click `BookwormModBuilder.exe`. It starts a local web server on `127.0.0.1`
(falling back to a free port if the default is busy) and opens your browser to the mod
builder. First run: point it at your game folder; then pick mods and Build.

## 5. Distributing it (SmartScreen / antivirus)

An unsigned executable downloaded from the internet trips Windows SmartScreen ("Windows
protected your PC") and may alarm antivirus - expected for any unsigned hobby build, not
a sign of malware. Ship a short note with the download:

> This is an unsigned community tool. Windows may warn you because it isn't code-signed
> (signing certificates cost money). The full source is at https://github.com/sharky564/BookwormAdventuresModding
> - read it or build it yourself with BUILDING.md. Click **More info → Run anyway** to launch.

Code-signing with an OV/EV certificate removes the warning but isn't free; for a hobby
mod tool the note above is the usual approach.
