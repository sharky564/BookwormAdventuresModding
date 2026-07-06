"""Dictionary swap, as a reproducible builder.

Front-codes a plain word list into the data/compressed.txt the engine loads at startup, and
repacks it onto a base pak. Pass your own file via `wordlist` (one word per line) to play with
any dictionary you like; leave it empty for the bundled default (bwakit/game/data/words.txt,
~300k words). The encoder uppercases, keeps A-Z only, dedups, sorts, and applies the game's
3..16 length gate, so the output is always loadable. BA1 and BA2 use the same format/path.
"""

import os
import shutil
import argparse
import pathlib

from bwakit.game import bwa_dictionary as D
from bwakit import popcap_pak_repack as R

_DEFAULT_WORDS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "game", "data", "words.txt")
)


def resolve_wordlist(wordlist):
    """User path if given and it exists, else the bundled default list. Tolerates a path
    pasted with surrounding quotes or stray whitespace, and expands ~ and env vars."""
    w = str(wordlist).strip() if wordlist else ""
    while len(w) >= 2 and w[0] == w[-1] and w[0] in ("'", '"'):
        w = w[1:-1].strip()
    if w:
        w = os.path.expandvars(os.path.expanduser(w))
    if w and os.path.exists(w):
        return w
    if not os.path.exists(_DEFAULT_WORDS):
        raise FileNotFoundError(
            "The bundled default word list is missing from this build (%s). This build "
            "didn't include bwakit's data files -- rebuild the exe, or supply your own "
            "word list path in the wordlist option." % _DEFAULT_WORDS
        )
    return _DEFAULT_WORDS


def build(src, base_pak, out_pak, *, wordlist=None, gate=True, keep_stage=False, **_):
    """Build a pak with a swapped dictionary.

    src        the user's extracted files (unused here; kept for the builder convention)
    base_pak   pak to repack onto (clean main.pak, or another mod's output to combine mods)
    out_pak    output pak path
    wordlist   path to a plain-text word list; empty/None => bundled default
    gate       apply the engine's 3..16 length gate (default True)
    """
    path = resolve_wordlist(wordlist)
    with open(path, encoding="utf-8", errors="ignore") as fh:
        words = fh.read().split()
    blob, kept = D.encode_compressed(words, gate=gate)

    stage = pathlib.Path(str(out_pak) + ".stage")
    shutil.rmtree(stage, ignore_errors=True)
    data = stage / "data"
    data.mkdir(parents=True, exist_ok=True)
    open(data / "compressed.txt", "wb").write(blob)

    subbed = R.repack(base_pak, str(stage), out_pak)[1]
    if not keep_stage:
        shutil.rmtree(stage, ignore_errors=True)
    return {
        "words": len(kept),
        "bytes": len(blob),
        "wordlist": os.path.basename(path),
        "custom": path != _DEFAULT_WORDS,
        "subbed": subbed,
        "out": str(out_pak),
    }


def cli(args):
    ap = argparse.ArgumentParser(prog="bwa mod dict-swap")
    ap.add_argument(
        "--wordlist",
        default=None,
        help="plain-text word list (one word per line); omit for the bundled ~300k default",
    )
    ap.add_argument(
        "--base",
        required=True,
        help="pak to repack onto (clean, or another mod's output)",
    )
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument(
        "--no-gate", action="store_true", help="skip the engine's 3..16 length gate"
    )
    a = ap.parse_args(args)
    print(build(None, a.base, a.out, wordlist=a.wordlist, gate=not a.no_gate))
