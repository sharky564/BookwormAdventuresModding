"""Structured reader for Bookworm Adventures .bwa saves.

The .bwa is a Torque SimObject serialization: a flat stream of named, typed
fields. Encoding validated across four saves (fresh 1.1.1, 1.2.1, 1.6.2, and a
completed game):

  field := <u16 namelen><name><u32 type><value>
    type 0 (int)    : <u32 value>            (immediately after the type tag)
    type 1 (string) : <u16 len><ascii>       (floats are stored as ascii too)
    type 2 (bool)   : <u8 value>
    type 4 (group)  : a Torque array; elements follow as further name->value
                      records, positionally paired (e.g. each Treasures element
                      is an `mScript` name followed by its `mEnabled` bool).

This module reads:
  - position (book/chapter/stage) and farthest reached
  - health / max health
  - lifetime stats (kills, words, letters, xp level)
  - inventory item counts (HealthItem / PurifyItem / PowerUpItem)
  - the Treasures array as (internal_name, equipped) pairs
  - the 16-tile rack + gems
  - the NumLetter* histogram (tile SPAWN counts, not usage)
  - a few combat stat floats

Notes / validated semantics:
  - Save indices are 1-BASED (CurrentBook=1 is Book 1); progress.py is 0-based,
    so use `position_0based` when crossing over.
  - NumLetter* counts tiles that have SPAWNED on the rack over the save's
    lifetime, NOT letters used in words (their sum is ~1.9x TotalLettersSpelled).
    It is therefore an empirical sample of the tile-spawn distribution.
  - NumTreasuresFound is a cumulative "earned" counter (~30 by game end); the
    Treasures array holds the ~18 DISTINCT current treasures, fewer because some
    earned treasures are upgrades that replace an earlier one. Both are correct.
  - Treasure internal names (mScript) can differ between game versions
    (e.g. "ArtemisBow" vs "ArchOfApollo"); they are read from the data, not
    guessed, so the reader is robust to this.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field as dc_field


GEM_CLASS_TO_NAME = {
    "AmethystTile": "amethyst",
    "EmeraldTile": "emerald",
    "GarnetTile": "garnet",
    "SapphireTile": "sapphire",
    "RubyTile": "ruby",
    "CrystalTile": "crystal",
    "DiamondTile": "diamond",
}

# Item block class names -> friendly inventory key.
ITEM_CLASSES = {
    "HealthItem": "health_potions",
    "PurifyItem": "purify_potions",
    "PowerUpItem": "powerup_potions",
}


@dataclass
class SaveState:
    current_book: int = -1  # 1-based as stored
    current_chapter: int = -1
    current_stage: int = -1
    farthest_book: int = -1
    farthest_chapter: int = -1
    farthest_stage: int = -1
    health: int = -1
    max_health: int = -1
    num_treasures_found: int = -1  # cumulative treasures EARNED over the run
    # (~30 by game end). Differs from the
    # Treasures array length (18 distinct) because
    # some earned treasures are UPGRADES that
    # replace an earlier one rather than adding a
    # new slot. Both numbers are correct.
    total_kills: int = -1
    total_words: int = -1
    total_letters: int = -1
    xp_level: int = -1
    rack: str = ""
    gems: list[str] = dc_field(default_factory=list)
    inventory: dict[str, int] = dc_field(default_factory=dict)
    treasures: list[str] = dc_field(default_factory=list)
    equipped_treasures: list[str] = dc_field(default_factory=list)
    letter_counts: dict[str, int] = dc_field(default_factory=dict)
    floats: dict[str, float] = dc_field(default_factory=dict)

    @property
    def position_0based(self) -> tuple[int, int, int]:
        """(book, chapter, enemy) in progress.py's 0-based convention."""
        return (self.current_book - 1, self.current_chapter - 1, self.current_stage - 1)


def _find_all(data: bytes, name: str) -> list[int]:
    needle = struct.pack("<H", len(name)) + name.encode()
    out = []
    i = data.find(needle)
    while i != -1:
        out.append(i)
        i = data.find(needle, i + 1)
    return out


def _int_after(data: bytes, name: str) -> int | None:
    """Read the u32 value of an integer field: name + 4-byte tag + u32."""
    pos = data.find(struct.pack("<H", len(name)) + name.encode())
    if pos < 0:
        return None
    after = pos + 2 + len(name)
    if after + 8 > len(data):
        return None
    # 4-byte tag (00 00 00 00) then the value.
    return struct.unpack_from("<I", data, after + 4)[0]


def _float_after(data: bytes, name: str) -> float | None:
    """Float fields store an ASCII string after a 1-byte-ish tag."""
    pos = data.find(struct.pack("<H", len(name)) + name.encode())
    if pos < 0:
        return None
    after = pos + 2 + len(name)
    # Scan a short window for a length-prefixed ascii float.
    for d in range(0, 8):
        o = after + d
        if o + 2 > len(data):
            break
        ln = struct.unpack_from("<H", data, o)[0]
        if 3 <= ln <= 12 and o + 2 + ln <= len(data):
            s = data[o + 2 : o + 2 + ln]
            try:
                txt = s.decode("ascii")
                return float(txt)
            except (ValueError, UnicodeDecodeError):
                continue
    return None


def _read_rack(data: bytes) -> tuple[str, list[str]]:
    """Extract the 16 tiles between GridState and the HOFNewTop region.

    Each tile is `mLetter` + length-prefixed single letter; gem tiles carry a
    `*Tile` class name shortly after. We pair each mLetter with the nearest
    following gem class (if any) before the next mLetter.
    """
    g = data.find(b"GridState")
    h = data.find(b"HOFNewTop")
    if g < 0 or h < 0:
        return "", []
    region = data[g:h]
    letters = []
    gems = []
    # Offsets of each mLetter within region.
    ml = b"mLetter"
    positions = []
    i = region.find(ml)
    while i != -1:
        positions.append(i)
        i = region.find(ml, i + 1)
    for idx, p in enumerate(positions):
        # The letter is a length-prefixed string shortly after 'mLetter'.
        seg_end = positions[idx + 1] if idx + 1 < len(positions) else len(region)
        seg = region[p:seg_end]
        # find first 1-char ascii letter token
        letter = "?"
        for j in range(len(ml), min(len(ml) + 12, len(seg) - 2)):
            ln = struct.unpack_from("<H", seg, j)[0]
            if ln == 1 and j + 3 <= len(seg):
                ch = seg[j + 2]
                if 65 <= ch <= 90 or 97 <= ch <= 122:
                    letter = chr(ch).upper()
                    break
        letters.append(letter)
        # gem: any *Tile class in this segment
        gem = "none"
        for cls, gname in GEM_CLASS_TO_NAME.items():
            if cls.encode() in seg:
                gem = gname
                break
        gems.append(gem)
    return "".join(letters[:16]), gems[:16]


def _parse_treasures(data: bytes) -> list[tuple[str, bool]]:
    """Parse the Treasures array into (internal_name, equipped) pairs.

    Element layout (validated): each array element contains an `mScript`
    string field (the treasure's internal name, e.g. "ArchOfApollo") followed
    by an `mEnabled` bool (the equipped flag). Pairing is positional.
    """
    off = data.find(struct.pack("<H", 9) + b"Treasures")
    if off < 0:
        return []
    i = off + 2 + 9 + 4 + 4  # skip name, type tag, count
    pairs: list[tuple[str, bool]] = []
    cur: str | None = None
    misses = 0
    while i < len(data) - 2 and len(pairs) < 128:
        ln = struct.unpack_from("<H", data, i)[0]
        if ln in (7, 8) and i + 2 + ln <= len(data):
            tok = data[i + 2 : i + 2 + ln]
            if tok == b"mScript":
                j = i + 2 + ln
                slen = struct.unpack_from("<H", data, j + 4)[0]
                cur = data[j + 6 : j + 6 + slen].decode("ascii", "replace")
                i = j + 6 + slen
                misses = 0
                continue
            if tok == b"mEnabled":
                j = i + 2 + ln
                if cur is not None:
                    pairs.append((cur, bool(data[j + 4])))
                    cur = None
                i = j + 5
                misses = 0
                continue
        i += 1
        misses += 1
        if misses > 4000:  # left the array region
            break
    return pairs


def read_save(path: str) -> SaveState:
    with open(path, "rb") as f:
        data = f.read()

    s = SaveState()

    def _i(name: str) -> int:
        # Note: must distinguish a real 0 from absent. `x or -1` would turn a
        # legitimate 0 (e.g. XPLevel=0 in a fresh save) into -1, so test None.
        v = _int_after(data, name)
        return v if v is not None else -1

    s.current_book = _i("CurrentBook")
    s.current_chapter = _i("CurrentChapter")
    s.current_stage = _i("CurrentStage")
    s.farthest_book = _i("FarthestBook")
    s.farthest_chapter = _i("FarthestChapter")
    s.farthest_stage = _i("FarthestStage")
    s.health = _i("Health")
    s.max_health = _i("MaxHealth")
    s.num_treasures_found = _i("NumTreasuresFound")
    s.total_kills = _i("TotalKills")
    s.total_words = _i("TotalWordsSpelled")
    s.total_letters = _i("TotalLettersSpelled")
    s.xp_level = _i("XPLevel")

    # Inventory: count item-class blocks.
    for cls, key in ITEM_CLASSES.items():
        s.inventory[key] = len(_find_all(data, cls))

    # Treasures: parse the Treasures array as (mScript, mEnabled) element
    # pairs. Each array element stores the treasure's internal name in an
    # `mScript` string field followed by its `mEnabled` bool (the equipped
    # flag). This reads the TRUE names from the data rather than guessing.
    _treasure_pairs = _parse_treasures(data)
    s.treasures = [name for name, _eq in _treasure_pairs]
    s.equipped_treasures = [name for name, eq in _treasure_pairs if eq]

    # Lifetime letter SPAWN counts (NumLetter*). NOTE: this is a histogram of
    # tiles that have SPAWNED on the rack over the save's lifetime, NOT letters
    # used in words (confirmed: sum(NumLetter*) >> TotalLettersSpelled, ~1.9x).
    # As such it is an empirical sample of the game's tile-spawn distribution.
    for c in range(ord("A"), ord("Z") + 1):
        v = _int_after(data, f"NumLetter{chr(c)}")
        if v is not None:
            s.letter_counts[chr(c)] = v

    # A few useful floats.
    for fn in ("BeatdownMultiplier", "OffenseBonusPct", "OffenseLevel", "DefenseLevel"):
        fv = _float_after(data, fn)
        if fv is not None:
            s.floats[fn] = fv

    s.rack, s.gems = _read_rack(data)
    return s


def _pretty(s: SaveState) -> str:
    lines = []
    lines.append(
        f"Position (save, 1-based): {s.current_book}.{s.current_chapter}.{s.current_stage}"
    )
    b, c, e = s.position_0based
    lines.append(f"Position (0-based for progress.py): book={b} chapter={c} enemy={e}")
    lines.append(f"Farthest: {s.farthest_book}.{s.farthest_chapter}.{s.farthest_stage}")
    lines.append(f"Health: {s.health}/{s.max_health}")
    lines.append(
        f"Lifetime: kills={s.total_kills}, words={s.total_words}, "
        f"letters={s.total_letters}, xp_level={s.xp_level}"
    )
    lines.append(f"Treasures earned (cumulative counter): {s.num_treasures_found}")
    lines.append(
        f"Treasures in array ({len(s.treasures)}): {', '.join(s.treasures) or '(none)'}"
    )
    lines.append(
        f"Equipped ({len(s.equipped_treasures)}): {', '.join(s.equipped_treasures) or '(none)'}"
    )
    lines.append(f"Inventory: {s.inventory}")
    lines.append(f"Rack: {s.rack}")
    gem_str = (
        ", ".join(f"{s.rack[i]}={g}" for i, g in enumerate(s.gems) if g != "none")
        or "(no gems)"
    )
    lines.append(f"Gems: {gem_str}")
    if s.floats:
        lines.append(f"Stats: " + ", ".join(f"{k}={v}" for k, v in s.floats.items()))
    if s.letter_counts:
        top = sorted(s.letter_counts.items(), key=lambda kv: -kv[1])[:6]
        lines.append(
            "Most-spawned letters (lifetime tile spawns, not usage): "
            + ", ".join(f"{k}={v}" for k, v in top)
        )
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python3 bwa_reader.py save.bwa")
        sys.exit(1)
    print(_pretty(read_save(sys.argv[1])))
