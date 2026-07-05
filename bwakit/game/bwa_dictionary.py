#!/usr/bin/env python3
"""
bwa_dictionary.py - native-accurate word validator for Bookworm Adventures Deluxe (BWA1).

Faithful reimplementation of the game's native dictionary path:
    LoadDictionaryFiles (0x436b70) -> Dict_LoadCompressed (0x491f60)
        -> per line: front-coding decode -> Dict_ToUpper (0x491e00)
        -> length gate 3..16 -> Dict_Insert (0x492270)
    check: gTileEngine:ValidateWord -> Dict_ToUpper -> Dict_Find (0x492aa0) -> bool

INPUT FILE
    data/compressed.txt   (extracted from main.pak; see BWA_LUC_FORMAT.md for unpacking)
    A prefix-compressed ("front-coded"), sorted, lowercase word list.

COMPRESSED.TXT FORMAT  (verified against FUN_00491f60, char-by-char reader)
    Each word is encoded as  <count?><suffix><CR|LF> where:
      * <count> is an optional leading decimal integer = number of leading chars to copy
        from the PREVIOUS word.  The count PERSISTS across lines: a line with no leading
        digit reuses the previous line's count (the native code never resets the count
        accumulator at end-of-line; only a per-line "have we started the count" flag resets).
      * <suffix> is the new trailing letters to append after the copied prefix.
    Example (the real file starts):
        aah        -> count 0,  "aah"
        3ed        -> count 3,  "aah"[:3]+"ed"   = "aahed"
        ing        -> reuse 3,  "aah"[:3]+"ing"  = "aahing"
        s          -> reuse 3,  "aah"[:3]+"s"    = "aahs"
        2l         -> count 2,  "aa"[:2]+"l"     = "aal"
        ...
    The prefix is copied lazily on the first letter of a word (when the word buffer is
    still empty); a "count only" line with no letters produces no word (matches native,
    and never occurs in a sorted file anyway).

WHY MEMBERSHIP == THE DECODED SET (no hash needed for correctness)
    The native store is an open-addressing hash set whose buckets are {hash, key_ptr}
    pairs; Dict_Find scrambles the key hash with the MINSTD/Schrage LCG only to pick a
    bucket, then compares the actual KEY STRING on a candidate match.  So the hash is a
    lookup accelerator and produces no collision false-positives: a word is valid iff it
    is in the decoded, gated, uppercased set.  `minstd_hash()` below reproduces the scramble
    for fidelity / further RE, but is not used by is_valid_word().
"""

from __future__ import annotations
import sys


# front-coding decoder  (exact port of the FUN_00491f60 state machine)
def decode_compressed(path):
    """Yield every decoded word (lowercase, ungated) from a compressed.txt file, in order."""
    with open(path, "rb") as f:
        data = f.read()

    count = 0  # local_3c : chars to copy from prev word; PERSISTS across lines
    count_started = False  # local_35 : have we begun reading this line's count digits?
    prev = b""  # local_44 : previously finalized word
    cur = bytearray()  # _Memory  : word currently being built

    for b in data:
        c = b  # int 0..255
        if 0x30 <= c <= 0x39:  # ASCII digit -> (re)build the copy count
            d = c - 0x30
            if not count_started:
                count = d
                count_started = True
            else:
                count = count * 10 + d
        elif c == 0x0D or c == 0x0A:  # CR or LF -> finalize current word
            if len(cur) > 0:
                word = bytes(cur)
                prev = word  # save as previous (for the next line's prefix copy)
                yield word.decode("latin1")
                cur = bytearray()
            count_started = False  # NB: `count` itself is NOT reset (native behaviour)
        else:  # letter -> copy prefix lazily, then append
            if count > 0 and len(cur) == 0:
                cur.extend(prev[:count])
            cur.append(c)
    # A file that does not end in a newline would leave a final word in `cur`; the real
    # files are CRLF-terminated, but handle it for safety:
    if len(cur) > 0:
        yield bytes(cur).decode("latin1")


# MINSTD / Schrage hash  (fidelity reproduction of the Dict_Find scramble; see docstring)
_A, _Q, _R, _M = (
    16807,
    127773,
    2836,
    2147483647,
)  # Park-Miller minimal standard constants


def minstd_scramble(seed: int) -> int:
    """16807 * seed mod (2**31 - 1), via Schrage's overflow-safe form (matches FUN_00492aa0)."""
    seed &= 0xFFFFFFFF
    hi, lo = divmod(seed, _Q)
    val = _A * lo - _R * hi
    if val < 0:
        val += _M
    return val


# dictionary
MIN_LEN, MAX_LEN = 3, 16  # native gate: (len - 3) unsigned < 0xE  ==  3 <= len <= 16


class BookwormDictionary:
    """The game's word set. Validity is case-insensitive; stored words are uppercase."""

    def __init__(self, compressed_path):
        self.words = frozenset(
            w.upper()
            for w in decode_compressed(compressed_path)
            if MIN_LEN <= len(w) <= MAX_LEN
        )

    def is_valid_word(self, word: str) -> bool:
        """True iff `word` (any case) is a legal Bookworm word, exactly as the game decides."""
        if word is None:
            return False
        w = word.upper()
        return MIN_LEN <= len(w) <= MAX_LEN and w in self.words

    def __contains__(self, word):
        return self.is_valid_word(word)

    def __len__(self):
        return len(self.words)


# self-test / CLI
def _selftest(path):
    print(f"loading {path} ...")
    raw = list(decode_compressed(path))
    d = BookwormDictionary(path)
    print(f"decoded words (ungated): {len(raw):,}")
    print(f"valid words  (3..16):    {len(d):,}")

    # sorted-order sanity (front-coding requires sorted input)
    inorder = all(raw[i] <= raw[i + 1] for i in range(min(len(raw), 5000) - 1))
    print(f"first 5000 in non-decreasing order: {inorder}")
    print("first 8 decoded:", raw[:8])

    # spot checks
    should = ["cat", "QUARTZ", "aardvark", "Zymurgy", "bookworm", "the", "queue"]
    shouldnt = [
        "zzz",
        "qwxz",
        "a",
        "to",
        "xyzzyx",
        "thisisnotaword",
        "aaaaaaaaaaaaaaaaaa",
    ]
    print("\nexpected VALID:")
    for w in should:
        print(f"  {w!r:24} -> {d.is_valid_word(w)}")
    print("expected INVALID:")
    for w in shouldnt:
        print(f"  {w!r:24} -> {d.is_valid_word(w)}")

    # gate boundaries
    print(
        "\ngate: any 2-letter word valid?  ",
        any(len(w) == 2 for w in d.words),
        "(should be False)",
    )
    print(
        "gate: any 17+-letter word valid?",
        any(len(w) >= 17 for w in d.words),
        "(should be False)",
    )
    lens = [len(w) for w in d.words]
    print(f"length range of valid words: {min(lens)}..{max(lens)}")

    # hash demo
    print(
        "\nMINSTD scramble demo: minstd_scramble(1)=",
        minstd_scramble(1),
        " minstd_scramble(16807)=",
        minstd_scramble(16807),
    )


def main(argv):
    if len(argv) < 2:
        print("usage: bwa_dictionary.py <compressed.txt> [word ...]")
        print("       bwa_dictionary.py <compressed.txt> --dump <out.txt>")
        print("  no words  -> run self-test")
        return 2
    path = argv[1]
    if len(argv) >= 4 and argv[2] == "--dump":
        d = BookwormDictionary(path)
        with open(argv[3], "w") as out:
            out.write("\n".join(sorted(d.words)) + "\n")
        print(f"wrote {len(d):,} valid words to {argv[3]}")
        return 0
    if len(argv) == 2:
        _selftest(path)
        return 0
    d = BookwormDictionary(path)
    for w in argv[2:]:
        print(f"{w}: {'VALID' if d.is_valid_word(w) else 'invalid'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))


# front-coding ENCODER (inverse of decode_compressed; produces a game-loadable compressed.txt)
def encode_compressed(words, *, gate=True):
    """Front-code `words` into compressed.txt bytes. Uppercases, keeps A-Z only, dedups,
    sorts, and (by default) applies the game's 3..16 length gate. Returns (bytes, kept_words)."""
    seen, cleaned = set(), []
    for w in words:
        u = w.strip().upper()
        if not u or any(not ("A" <= c <= "Z") for c in u):
            continue
        if gate and not (MIN_LEN <= len(u) <= MAX_LEN):
            continue
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
    cleaned.sort()
    out, prev, carried = bytearray(), "", 0
    for u in cleaned:
        wl = u.lower()
        s = 0
        m = min(len(prev), len(wl))
        while s < m and prev[s] == wl[s]:
            s += 1
        if s != carried:
            out += str(s).encode()
            carried = s
        out += wl[s:].encode("latin-1") + b"\r\n"
        prev = wl
    return bytes(out), cleaned
