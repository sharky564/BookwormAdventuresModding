> **Design & internals.** This document explains how the engine works. A few specifics
> (the mod count, the randomizer level scale, the dictionary mod being "file-replace") describe
> earlier states of the project; current behavior is in [README.md](README.md) and each
> `mods/<id>/mod.json`.

# Bookworm Adventures - Modding Client

A small client that builds modded copies of *Bookworm Adventures Deluxe* **from the
player's own game files**. No PopCap data is ever hosted or shipped - the repo
carries only *mod recipes* + small assets, and the build happens locally on the
user's machine.

What's here now:
- **the transform engine** (`modkit/`) - makes independently authored mods stack
  correctly onto the same game files;
- **a 6-mod catalog** (`mods/`) - covering all three mod kinds;
- **a CLI** (`modkit/client.py`) and a **local web UI** (`modkit/webui.py`) - both
  thin layers over a shared `modkit/core.py`, so an install set up in one opens in
  the other.

---

## Why mods are recipes, not finished files

Most mods for this game are **byte-injections** into compiled scripts. The mid-2000s
PopCap Lua VM has a hard constraint we confirmed repeatedly: you **cannot recompile
an existing method body** and swap it in - the VM crashes on load. The only mutations
it tolerates keep every untouched body **byte-for-byte identical**: append a new
method, append constants, splice a small instruction block, repoint a constant load,
add a binding in the main chunk.

That rules out shipping finished `.luc` files. Two mods that both touch
`BattleEngine.luc` would clobber each other if the client just copied files - last
one wins. Applying each mod's **recipe in sequence** onto the unpacked script composes
them instead. Most compatibility then becomes automatic: mods touching different
files, or different methods in a file, always compose.

## Three mod kinds

1. **code-inject** - append a fresh method and splice calls to it into existing
   methods, targeting each method by the marker constants it contains (never by
   index). Mutations keep bodies identical. *(enemy_resistance, misunderstanding_rack,
   skip_intro_tutorial.)*
2. **file-replace** - swap a whole standalone data file (e.g. the dictionary at
   `data/compressed.txt`). No script overlap, so it composes with everything.
   *(dictionary_swap.)*
3. **builder** - a generative recipe whose edits are *computed* from game data and
   can't be a static list. It names a `bwakit` builder that produces the modified
   files. *(enemy_hp_scaling: derives a per-enemy HP factor from the power table and
   repoints each creature's SetHealth constant. randomizer: from a seed, reorders the
   enemies by repointing the AddEnemy name constants in `books/Book*.luc` - the roster
   the engine actually fights - at a chosen `level` (1 within a chapter, 2 within a book,
   3 across books; levels 2-3 also resync the `packs/` preloader so no creature is named
   before it's loaded), and optionally shuffles each boss's treasure award via composable
   `repoint_global` transforms.)*

All three stage into one directory and are sealed by a **single repack**.

## The three rules that make code-inject mods composable

1. **Target by content, never by index** (`find_proto` matches marker constants).
2. **Compute indices dynamically** (a new method's proto index is `len(protos)` at
   append time).
3. **Keep bodies byte-identical** (only prepend/splice and append; inserting at
   instruction 0 never disturbs the original body).

## mod.json

A code-inject / file-replace mod lists `transforms`:

```jsonc
{
  "id": "misunderstanding_rack",
  "name": "Misunderstanding Rack",
  "version": "1.0.0",
  "description": "...",
  "apply_order": 50,                    // lower applies first; sort key (apply_order, id)
  "requires": [], "conflicts": [],
  "transforms": [
    { "file": "scripts/TileEngine.luc", "op": "append_bound_method",
      "class": "TileEngine", "method": "NextNeeded", "source": "NextNeeded.lua" },
    { "file": "scripts/TileEngine.luc", "op": "inject_self_call",
      "markers": ["mOldBaseAnim"], "method": "NextNeeded", "args": [1,2], "ret": 2, "at_pc": 0 }
  ]
}
```

A builder mod names a generative builder instead:

```jsonc
{
  "id": "enemy_hp_scaling", "name": "Late-Game Enemy HP Scaling",
  "apply_order": 40, "kind": "builder",
  "builder": "bwakit.mods.hp_scaling",
  "params": { "p0": 118.6, "d": 0.6 },
  "compat_note": "Stacks in effect with enemy_resistance."
}
```

### Transform ops

- **`append_bound_method`** `{class, method, source}` - compile `source`
  (`CLASS = {}; function CLASS:M(...) ... end`) and bind `CLASS.M = closure` in the
  script's main chunk. Use for any new logic.
- **`inject_self_call`** `{markers, method, args, ret, at_pc}` - splice
  `R[ret] = self:method(R[args...])`. Registers are by index; at `at_pc: 0` they are
  the target's parameters.
- **`inject_global_call`** `{markers, global, method, args, at_pc}` - splice
  `GLOBAL.method(args...)`; each arg is `{"str":…}`, `{"bool":…}`, or `{"num":…}`.
- **`replace_file`** `{file, source}` - stage a bundled asset in place of a game file.

> `markers` must match **exactly one** proto, or the build fails loudly. Builder mods
> carry a `builder` (a `bwakit.mods.*` module) + `params`.

## How a pak is built (`modkit.build.build`)

1. **Verify** the pristine template `main.pak` by sha256 against known-good versions.
2. **Resolve order**: check `requires`/`conflicts`, sort by `(apply_order, id)`.
3. **Stage everyone into one dir**, tracking conflicts:
   - code-inject transforms grouped by file, **composed in memory** (several mods can
     patch one file);
   - file-replace assets copied in (**exclusive** - that path may be touched by nothing
     else);
   - builder mods run and their output files harvested (**exclusive**).
   A file claimed exclusively that any other mod also touches → a clear conflict error.
4. **One repack** of the staging dir over the template into the output pak.

Nothing hardcodes a proto index or ships a prebuilt `.luc`; inputs are the user's own
files plus the recipes/assets.

## Using it - CLI

```
bwa-mod init  --game "C:\\...\Bookworm Adventures Deluxe"
        # duplicates the install to a sibling "... Modded" folder, backs up + hashes
        # the pristine main.pak as the build template, caches the unpacked originals,
        # and renames the copy's executable. (Pass --originals DIR to reuse an already
        # extracted copy and skip the one-time unpack.)
bwa-mod mods                                 # list the catalog
bwa-mod build enemy_hp_scaling dictionary_swap
        # verify template -> compose -> repack into the modded main.pak
bwa-mod status
bwa-mod launch                               # run the modded executable
bwa-mod restore                              # revert the modded main.pak to the template
```

`build` always composes from the **pristine template**, so switching mod selections is
just another `build` - never cumulative. The vanilla install is never touched.

## Using it - web UI

```
python -m modkit.webui            # serves http://127.0.0.1:8765 and opens the browser
```

A single-page app over the same `core`:
- first run shows a **setup** card (point it at the game folder; advanced options for a
  pre-extracted originals dir and overwrite);
- then a **mod checklist** with a kind badge and the `compat_note` shown inline;
- a live **plan** panel - resolved apply order, hard issues (`requires`/`conflicts`),
  and compat notes - updating as you toggle mods; Build is disabled while issues exist;
- **Build / Launch / Restore** buttons; long actions run as background jobs and stream
  their log into the page.

The server is bound to localhost. Init in the browser needs the game path **typed**
(browsers can't pick a server-side folder), but it's a one-time step.

## Compatibility

- **Automatic** when mods touch different files or different methods. Two mods editing
  the same file compose; an exclusive (file-replace/builder) file that overlaps anything
  is rejected with a conflict error.
- **`apply_order`** sequences edits when order matters.
- **`requires` / `conflicts`** are hard constraints the engine enforces.
- **`compat_note`** flags *semantic* overlaps the engine can't infer (e.g.
  `enemy_hp_scaling` + `enemy_resistance` both harden the late game - fine together,
  just harder). The web UI surfaces these inline and in the plan panel.

## Verification status

The engine reproduces every shipped, hand-verified pak from recipes:

| Run | What it proves | Result |
|-----|----------------|--------|
| **A** | `misunderstanding_rack + skip_intro_tutorial` → output `.pak` **byte-identical** to the shipped, user-confirmed misunderstanding pak. | ✅ |
| **B** | `enemy_resistance` builds a structurally valid `CreatureBaseClass.luc` (method appended, DecHealth found by marker, round-trips). | ✅¹ |
| **C** | The rack mod's transforms split across **two independent mods on the same file** → identical to applying them as one. | ✅ |
| **D** | `enemy_hp_scaling` scales 89 creature scripts → **byte-identical to the shipped v3 pak**. | ✅ |
| **E** | `enemy_hp_scaling + dictionary_swap` → **byte-identical to the shipped combined pak** (one-shot stage→repack equals the original sequential build). | ✅ |
| **F** | The code-inject + builder mods compose with **zero conflicts** (treasures + enemy_hp_scaling is the one intentional collision, on shared boss scripts). | ✅ |
| **F2** | `enable_gems` / `enable_potions` inject `profile.Set("AllowGems"/"AllowItems", true)` at `Book:LoadChapterResources` (verified the flag const lands in that proto); `enable_scramble` repoints the `chapter >= 4` scramble gate (op 25 LE, operand B) to `>= 0` (verified exactly one match, emitted bytecode compares against 0). All three compose on `books/Book.luc`, and with `randomizer include_tutorial` (different files) they make chapter 1.1 randomizable. | ✅ |
| **G** | `randomizer` is **deterministic** (same seed+options → byte-identical pak). It reorders enemies in `books/Book*.luc` at `level` 1/2/3 (within chapter / within book / across books); for levels 2-3 it rewrites `packs/Book*.luc` so each chapter's preload set still **equals** its roster (verified per chapter, all levels - the no-crash invariant), with no cross-book name collisions. Boss/solo-boss slots (incl. Sphinx 2.4, Codex 3.10) and the Book 1 tutorial stay fixed. `treasures` shuffles boss awards within each book as `repoint_global` transforms that compose with `enemy_resistance`. | ✅ |

The CLI was then exercised end-to-end on a simulated install: `build` produced the
byte-identical combined pak, a rebuild with a different selection produced the
byte-identical misunderstanding pak, and `restore` returned `main.pak` to the template.

The **web UI** was verified the same way, headless: driving the server exactly as the
browser does (`/api/status`, `/api/mods`, `/api/plan`, a background `/api/build` job,
`/api/launch`, `/api/restore`) produced a pak byte-identical to the shipped combined
pak. The sandbox has no Tk and no browser, so the *rendered window* itself wasn't run
here - but all logic lives in `core` (verified) and the page is plain HTML/JS.

¹ Run B differs from the old hand-built resistance pak only in the scratch-register
base of one injection (the engine uses always-safe registers above `maxstack`); same
opcode/constants otherwise, same pattern proven in Run A. Worth one playtest, like any pak.

## Repo layout

```
bwa-mod-client/
  bwa_mod_gui.py   # entry point for the packaged web-UI app (PyInstaller target)
  build.bat        # one-file Windows build  (build.sh = current-OS sanity build)
  BUILDING.md      # how to package into a single executable
  modkit/
    transform.py   # code-inject primitives (the engine)
    builders.py    # adapters for generative bwakit builders
    build.py       # verify -> order -> stage (3 kinds, conflict-checked) -> repack
    core.py        # shared API: state, catalog, plan, init, build, launch, restore
    client.py      # CLI over core
    webui.py       # local HTTP server + JSON API over core (background jobs)
    static/
      index.html   # single-page web UI (vanilla JS/CSS, no build step)
  mods/
    enemy_resistance/      mod.json + ApplyResistance.lua      (code-inject)
    misunderstanding_rack/ mod.json + NextNeeded.lua           (code-inject)
    skip_intro_tutorial/   mod.json                            (code-inject)
    dictionary_swap/       mod.json + compressed.txt           (file-replace)
    enemy_hp_scaling/      mod.json                            (builder)
    randomizer/            mod.json                            (builder)
  README.md
```

`client.py` and `webui.py` are thin shells over `core.py`, sharing one state format.
The engine calls `bwakit` (unpack / compile / repack / bytecode + the mod builders)
**in-process** - no Python subprocess - so it freezes cleanly. It also needs the custom
`luac` (`BWA_LUAC`, a bundled `luac.exe`, or `PATH`; only for code-inject mods).

---

## Roadmap - what's left

The hard parts are done: a composable engine, a CLI, a verified web-UI GUI, and a
freezable build (engine is subprocess-free; resources + a bundled `luac` resolve from
the PyInstaller bundle). Remaining:

1. **Produce the release build.** `BUILDING.md` + `build.bat` package everything into one
   `BookwormModBuilder.exe`; that final step runs on Windows (PyInstaller doesn't
   cross-compile) and needs a Windows `luac.exe`. Optionally code-sign to avoid the
   SmartScreen warning.
2. **Mod fetch** from the repo (pull the selected mod folders on demand).
3. **A native window** is an easy alternative if preferred: a Tkinter shell over the
   same `core` (the sandbox here had no Tk, which is why the GUI is a local web app -
   fully verifiable headless and packaged the same way).
4. **More atomic mods** to grow the catalog.
