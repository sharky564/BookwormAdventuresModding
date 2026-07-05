"""modkit.build - compose selected mods onto a verified template main.pak.

Verify the template hash, resolve mods into a deterministic order, stage every edit
into one dir (chunk transforms compose in memory; replace_file/builder outputs claim a
file exclusively), then repack once. Inputs are the user's own de-XOR'd files plus the
recipes - no proto index is hardcoded, no .luc is prebuilt.
"""

import os
import json
import shutil
import hashlib
import tempfile

from . import transform as X
from .builders import stage_builder


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_mod(mod_dir):
    with open(os.path.join(mod_dir, "mod.json")) as f:
        m = json.load(f)
    m["_dir"] = mod_dir
    return m


def mod_games(mod):
    """Games a mod supports. Defaults to ['ba1'] for older mods without the field."""
    g = mod.get("games", ["ba1"])
    return g if isinstance(g, list) else [g]


def detect_game(originals_dir):
    """Identify which game an unpacked install is, from its book layout. Bookworm Adventures
    Deluxe (ba1) has scripts/books/Book1.luc; Volume 2 (ba2) numbers its books 0/4/5/6, so it
    has Book0.luc and no Book1.luc. Falls back to 'ba1'."""
    books = os.path.join(originals_dir, "scripts", "books")
    if os.path.exists(os.path.join(books, "Book1.luc")):
        return "ba1"
    if os.path.exists(os.path.join(books, "Book0.luc")):
        return "ba2"
    return "ba1"


def mod_transforms(mod, game):
    """The transform list to apply for `game`. `transforms` may be a flat list (applies to
    every game the mod declares -- valid only when the markers/structure are identical, e.g.
    the dictionary swap) or a dict keyed by game id (when BA1 and BA2 need different markers,
    e.g. disable_dialog). Unknown games yield an empty list."""
    t = mod.get("transforms", [])
    if isinstance(t, dict):
        return t.get(game, [])
    return t


def resolve_order(mods, game="ba1"):
    """Validate game support + requires/conflicts and return mods sorted by (apply_order, id)."""
    ids = {m["id"] for m in mods}
    for m in mods:
        if game not in mod_games(m):
            raise ValueError(
                "%s does not support game %r (supports %s)"
                % (m["id"], game, ", ".join(mod_games(m)))
            )
        for req in m.get("requires", []):
            if req not in ids:
                raise ValueError(
                    "%s requires %s, which is not selected" % (m["id"], req)
                )
        for con in m.get("conflicts", []):
            if con in ids:
                raise ValueError("%s conflicts with %s; both selected" % (m["id"], con))
    return sorted(mods, key=lambda m: (m.get("apply_order", 100), m["id"]))


def build(
    template_pak,
    originals_dir,
    mod_dirs,
    out_pak,
    luac=None,
    known_hashes=None,
    work_dir=None,
    overrides=None,
    game="ba1",
):
    """Apply the mods in `mod_dirs` onto `template_pak`, writing `out_pak`.

    originals_dir : directory of de-XOR'd original files.
    known_hashes  : optional iterable of accepted template sha256 hex digests.
    game          : 'ba1' (Bookworm Adventures Deluxe) or 'ba2' (Volume 2). Selects which mod
                    variants apply; the caller supplies the matching template_pak/originals_dir.
    Returns: out, mods (ordered ids), moddir, sha256, verified, files, applied.
    """
    digest = sha256(template_pak)
    verified = (known_hashes is None) or (digest in set(known_hashes))
    if known_hashes is not None and not verified:
        raise ValueError("template hash %s is not a recognized game version" % digest)

    mods = resolve_order([load_mod(d) for d in mod_dirs], game)

    if work_dir:
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir)
        os.makedirs(work_dir)
        stage = work_dir
    else:
        stage = tempfile.mkdtemp(prefix="bwamod_")

    # Conflict tracking. A file edited by chunk transforms may be co-edited by
    # several mods (they compose in memory). A file produced by an EXCLUSIVE op
    # (replace_file or a builder) may be touched by nothing else.
    compose = {}  # relpath -> [mod_id, ...]
    exclusive = {}  # relpath -> mod_id

    def claim_exclusive(rel, mod_id):
        if rel in exclusive or rel in compose:
            prev = exclusive.get(rel) or ", ".join(compose[rel])
            raise ValueError(
                "file conflict on %s: %s and %s both modify it" % (rel, prev, mod_id)
            )
        exclusive[rel] = mod_id

    applied = []

    # 1) declarative CHUNK transforms (append_bound_method / inject_*), grouped by
    #    file and composed in memory so multiple mods can patch one file.
    by_file = {}
    for m in mods:
        for t in mod_transforms(m, game):
            if t["op"] == "replace_file":
                continue
            by_file.setdefault(t["file"], []).append((m, t))
    # ... plus transforms a builder *generates* from its params (e.g. the randomizer's
    #     treasure shuffle): these compose in memory too, so they stack with code-inject
    #     mods that patch the same creature scripts.
    import importlib

    for m in mods:
        if not m.get("builder"):
            continue
        try:
            gen = getattr(importlib.import_module(m["builder"]), "gen_transforms", None)
        except Exception:
            gen = None
        if not gen:
            continue
        params = dict(m.get("params", {}))
        params.update((overrides or {}).get(m["id"], {}))
        params.setdefault("game", game)
        for t in gen(originals_dir, **params):
            by_file.setdefault(t["file"], []).append((m, t))
    for relpath, ops in by_file.items():
        chunk = X.load_chunk(os.path.join(originals_dir, relpath))
        for m, t in ops:
            X.apply_op(chunk, t, m["_dir"], luac)
            compose.setdefault(relpath, []).append(m["id"])
            applied.append((m["id"], t["op"], relpath))
        dst = os.path.join(stage, relpath)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        X.save_chunk(chunk, dst)

    # 2) replace_file transforms (exclusive): drop a bundled asset into the stage.
    for m in mods:
        for t in mod_transforms(m, game):
            if t["op"] != "replace_file":
                continue
            rel = t["file"]
            claim_exclusive(rel, m["id"])
            dst = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(m["_dir"], t["source"]), dst)
            applied.append((m["id"], "replace_file", rel))

    # 3) builder mods (exclusive): run the generative builder, harvest its files.
    for m in mods:
        if m.get("builder"):
            for rel in stage_builder(
                m, template_pak, originals_dir, stage, claim_exclusive, overrides, game
            ):
                applied.append((m["id"], "builder", rel))

    # 4) one repack over the template
    from bwakit import popcap_pak_repack as R

    R.repack(template_pak, stage, out_pak)

    return {
        "out": out_pak,
        "mods": [m["id"] for m in mods],
        "moddir": stage,
        "sha256": digest,
        "verified": verified,
        "files": sorted(set(list(compose) + list(exclusive))),
        "applied": applied,
    }
