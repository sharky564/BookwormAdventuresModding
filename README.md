# Bookworm Adventures Mod Builder

A fan-made tool for modding **Bookworm Adventures Deluxe** (BA1) and **Bookworm
Adventures Volume 2** (BA2). It builds a modded copy of the game **from your own
installed files** - pick some mods in a browser window, click **Build**, and play.

> ⚠️ **This project ships no game code, scripts, art, or the game's own dictionary.**
> You need your own copy of the game installed. *Bookworm Adventures*
> is © PopCap Games / Electronic Arts; this tool is unofficial and unaffiliated.

> This project has been made with extensive use of AI, but the main work has been done
> by me, AI was used more to set up various frameworks and GUIs that I have less
> experience with. AI read over a lot of my work and refactored it to make it less
> buggy, which has been a huge problem with this project.

## Play in one download (no Python)

The easy way - grab the ready-to-run app from the [**Releases**](../../releases) page.
One file, no install, no Python needed:

* **Windows:** download `BookwormModBuilder-windows.exe` and double-click.
* **macOS / Linux:** download `BookwormModBuilder-macos` / `-linux`, then `chmod +x` it and run.

It opens a page in your browser - point it at your game folder, tick some mods, hit Build,
and Launch. Because it's an unsigned community build, Windows may say "Windows protected your
PC"; click **More info -> Run anyway** (details in [BUILDING.md](BUILDING.md)).

These binaries are built automatically for each platform by GitHub Actions, so every release
has a current one.

## Run from source (Python 3.9+)

1. **Install Python 3.9+** (Windows, macOS, or Linux). Nothing else - the builder uses
only the Python standard library.
2. **Get the code** and open a terminal in the folder:

```
   git clone https://github.com/sharky564/BookwormAdventuresModding.git
   cd BookwormAdventuresModding
   ```

3. **Run it:**

```
   python bwa_mod_gui.py          # or:  ./run.sh   (Windows:  run.bat)
   ```

A page opens at `http://127.0.0.1:8765`.

4. **Point it at your game.** In the setup card, type the full path to your Bookworm
Adventures folder (the one containing `main.pak` and the game `.exe`). The tool makes
a **separate modded copy** of the install - your original stays untouched - and
unpacks it once (takes a minute).
5. **Pick mods, Build, Play.** Tick the mods you want, tweak their options, hit
**Build**, then **Launch**. **Restore** reverts to vanilla anytime.

Switching mods is just another Build - it always composes from the pristine backup,
never cumulatively, so you can't paint yourself into a corner.

## What you can do (the mods)

|Mod|What it does|BA1|BA2|
|-|-|:-:|:-:|
|**Custom / Expanded Dictionary**|Swap the word list - bundled ~300k words, or **point at your own `.txt`** (one word per line).|✅|✅|
|**Late-Game Enemy HP Scaling**|Scales late-game enemy HP up by how far ahead their attack power is; `d` dials intensity.|✅|✅|
|**Randomizer**|Shuffles which enemies you fight (and optionally each boss's treasure) from a seed. A **0–10** dial runs from within-chapter up to fully random; optional HP/XP rebalancing keeps moved enemies fair.|✅|-|
|**Enemy Resistance Scaling**|Later enemies take progressively less damage.|✅|-|
|**Misunderstanding Rack**|Every rack always spells MISUNDERSTANDING.|✅|-|
|**Disable Cutscene Dialog**|Skips the story dialog.|✅|-|
|**Skip Intro Tutorial**|Marks the intro tile tutorial done so chapter 1.1 is playable without it.|✅|-|
|**XP & Leveling From The Start**|Turns the level-up bar on from chapter 1.1.|✅|-|
|**Gems / Potions / Scramble From The Start**|Unlock the special tile mechanics immediately.|✅|-|

BA2 already unlocks gems/potions/XP/scramble by default, so those mods are BA1-only.
More BA2 coverage is in progress.

The builder resolves compatibility for you: mods that touch different things stack
automatically, and a genuine conflict is reported *before* it builds.

> **A note on `luac`:** most mods (dictionary, HP scaling, randomizer, the unlock mods,
> skip-tutorial) work with no extra setup. Two mods that inject *new* script code -
> **Enemy Resistance** and **Misunderstanding Rack** - need a PopCap-compatible `luac`
> compiler on your `PATH` (or set `BWA_LUAC`). Everything else is pure byte-editing.

## Custom dictionaries

The **Custom / Expanded Dictionary** mod takes any plain-text word list - one word per
line. Leave its **Word list file** box blank for the bundled ~300k list, or paste the
full path to your own file. Words are uppercased, filtered to A–Z, de-duplicated,
sorted, and length-gated to **3–16 letters** (the engine's limit), so whatever you feed
it loads cleanly. There's a tiny sample at [`examples/custom-words.txt`](examples/custom-words.txt).

## Command line (optional)

Everything the GUI does is on the CLI too (run from the repo root):

```
python -m modkit.client init  --game "C:\\PathToBookworm Adventures Deluxe"
python -m modkit.client mods
python -m modkit.client build randomizer dictionary_swap --set randomizer.level=7 --set randomizer.seed=42
python -m modkit.client build dictionary_swap --set dictionary_swap.wordlist=/path/to/mywords.txt
python -m modkit.client launch
python -m modkit.client restore
```

`--set ID.KEY=VALUE` overrides any mod option (repeatable). `build` always composes from
the pristine template, so a new selection is just another `build`.

## For developers / modders

* [**ARCHITECTURE.md**](ARCHITECTURE.md) - the composable transform engine, the three mod
kinds, the bytecode-injection rules, and the verification runs.
* [**BUILDING.md**](BUILDING.md) - package the app into a single executable with
PyInstaller so end users don't need Python installed.
* **Releases** are built by `.github/workflows/build.yml`: push a `v*` tag and GitHub
Actions compiles the Windows/macOS/Linux binaries and attaches them to that Release.
* Mods are folders under `mods/<id>/` with a `mod.json`; the core library
(unpack / repack / bytecode / builders) is the vendored `bwakit/` package.

## Legal & credits

Unofficial, non-commercial fan tool. It contains **no game content** and operates only on
files you already own. *Bookworm Adventures* and *Bookworm Adventures Volume 2* are
trademarks of PopCap Games / Electronic Arts. Tool code is MIT-licensed (see
[LICENSE](LICENSE)). The bundled word list is a large public-domain English word list owned
by @rtrb and can be replaced with your own at any time (see **Custom dictionaries**).



