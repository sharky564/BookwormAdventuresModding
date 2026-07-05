"""modkit.builders - adapters for generative (builder) mods.

A builder computes its edits from game data (hp_scaling derives per-enemy HP factors;
randomizer shuffles rosters from a seed) instead of shipping a static recipe. We run the
byte-reproducing bwakit builder, then harvest its de-XOR'd files into the shared stage so
the single final repack composes them with the injection mods.
"""

import os
import shutil
import tempfile


def _harvest(builder_stage, stage, claim, mod_id):
    """Copy the builder's output into the shared stage, claiming each path exclusively
    (raises on conflict). Returns the staged relpaths."""
    rels = []
    for root, _, files in os.walk(builder_stage):
        for fn in files:
            sp = os.path.join(root, fn)
            rel = os.path.relpath(sp, builder_stage).replace(os.sep, "/")
            claim(rel, mod_id)
            dst = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(sp, dst)
            rels.append(rel)
    return rels


def stage_builder(
    mod, template_pak, originals_dir, stage, claim, overrides=None, game="ba1"
):
    """Run a builder mod, harvest its de-XOR'd output into `stage` (it does a throwaway
    repack internally that we discard). `overrides` maps mod id -> params, layered over
    the mod's defaults. Returns staged relpaths."""
    name = mod["builder"]
    params = dict(mod.get("params", {}))
    params.update((overrides or {}).get(mod["id"], {}))
    tmp = tempfile.mktemp(suffix=".pak")
    builder_stage = tmp + ".stage"
    try:
        # src = the user's own extracted files; template is only for the throwaway repack.
        if name == "bwakit.mods.hp_scaling":
            from bwakit.mods import hp_scaling

            hp_scaling.build(
                originals_dir, template_pak, tmp, keep_stage=True, game=game, **params
            )
        elif name == "bwakit.mods.randomizer":
            from bwakit.mods import randomizer

            randomizer.build(
                originals_dir, template_pak, tmp, keep_stage=True, **params
            )
        elif name == "bwakit.mods.dict_swap":
            from bwakit.mods import dict_swap

            dict_swap.build(originals_dir, template_pak, tmp, keep_stage=True, **params)
        else:
            raise ValueError("unknown builder %r" % name)
        return _harvest(builder_stage, stage, claim, mod["id"])
    finally:
        shutil.rmtree(builder_stage, ignore_errors=True)
        if os.path.exists(tmp):
            os.remove(tmp)
