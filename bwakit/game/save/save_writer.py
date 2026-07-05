"""Pure-Python port of save_state_editor/savestates.pyw's binary save logic.

The original editor mixes the byte-writing routine with a Tkinter GUI. This
module extracts the byte-writing as a standalone callable so the bank-building
automation can override save files without launching any UI.

Two public entry points, mirroring the editor's two actions:

    override_rack_in_place(save_path, rack, gems)
        Edits an existing .bwa save file IN PLACE. Reads the current file,
        swaps in the new rack/gems, writes back to the same path. ALL
        other game state (HP, position in chapter, items) is preserved.
        This is the right call for the bank builder.

    write_savestate(template_path, target_path, rack, gems)
        Reads from `template_path`, writes the result to `target_path`.
        The two-file variant - equivalent to the editor's "Reset Chapter"
        action (which copies the chapter template and overwrites the rack).
        Use this only when you want to start from a fresh chapter state.

The format constants below were copied verbatim from the editor; if the
editor is updated, those constants here may need to be updated in lockstep.
"""

from __future__ import annotations

from pathlib import Path


# Format constants
# All constants below are verbatim from save_state_editor/savestates.pyw.
# Keep in sync if that file is updated.

_DELIM_GRIDSTATE = b"GridState"
_DELIM_HOF = b"HOFNewTop"
_DELIM_ITEMS = b"Items"
_DELIM_LAST5HP = b"Last5HP"

_GRIDSTATE_HEAD_NO_GEM = (
    b"GridState\x04\x00\x00\x00\x01\x00\x00\x00\x00\x00\x04\x00\x00\x00"
    b"\x01\x00\x00\x00\x00\x00\x04\x00\x00\x00\x00\x01\x07\x00"
)
_GRIDSTATE_HEAD_WITH_GEM = (
    b"GridState\x04\x00\x00\x00\x01\x00\x00\x00\x00\x00\x04\x00\x00\x00"
    b"\x01\x00\x00\x00\x00\x00\x04\x00\x00\x00\x01\x01\x07\x00"
)
_M_LETTER = b"mLetter\x03\x00\x00\x00\x01\x00"
_AFTER_LETTER_1 = b"\x00\x00\x00\x04"
_EVERY_FOURTH_LETTER = b"\x00\x00\x00\x01\x00\x00\x00\x00\x00\x04"
_AFTER_LETTER_NO_GEM = b"\x00\x00\x00\x00\x01\x07\x00"
_AFTER_LETTER_WITH_GEM = b"\x00\x00\x00\x01\x01\x07\x00"
_GEM_SETUP = (
    b"\x00\x01\x0b\x00mAttributes\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x04\x00\x00\x00\x00\x01\x0a\x00mClassName\x03\x00\x00\x00"
)
_NO_POTIONS_BLOCK = (
    b"Items\x04\x00\x00\x00\x00\x00\x01\x00\x00\x00\x05\x00\x00\x00\x07\x00"
)

# 0 = none, 1-7 = the gem types. Same mapping as the editor README.
_BINARY_GEMS = {
    1: b"\x0c\x00AmethystTile",
    2: b"\x0b\x00EmeraldTile",
    3: b"\x0a\x00GarnetTile",
    4: b"\x0c\x00SapphireTile",
    5: b"\x08\x00RubyTile",
    6: b"\x0b\x00CrystalTile",
    7: b"\x0b\x00DiamondTile",
}

# Per-tile magic number trailers - undocumented but appear to encode the tile's
# slot index in the grid for the game's checksum/state tracking.
_MAGIC_NUMBERS = {
    0: b"\x01\x00\x01",
    1: b"\x01\x00\x02",
    2: b"\x00\x00\x03",
    3: b"\x01\x00\x01",
    4: b"\x01\x00\x01",
    5: b"\x01\x00\x02",
    6: b"\x00\x00\x03",
    7: b"\x01\x00\x02",
    8: b"\x01\x00\x01",
    9: b"\x01\x00\x02",
    10: b"\x00\x00\x03",
    11: b"\x00\x00\x03",
    12: b"\x01\x00\x01",
    13: b"\x01\x00\x02",
    14: b"\x00\x00\x03",
    15: b"\x0e\x00",
}


# Same names as recognize.GEM_STATES, in editor order.
GEM_NAME_TO_INT = {
    "none": 0,
    "amethyst": 1,
    "emerald": 2,
    "garnet": 3,
    "sapphire": 4,
    "ruby": 5,
    "crystal": 6,
    "diamond": 7,
}


def _normalize_rack(rack: str) -> bytes:
    if len(rack) != 16 or not rack.isalpha():
        raise ValueError(f"rack must be exactly 16 alphabetic characters, got {rack!r}")
    return rack.upper().encode("ascii")


def _normalize_gems(gems) -> list[int]:
    """Accepts either a list of ints (0-7), a list of gem name strings, or a
    16-character numeric string. Returns a list of 16 ints in [0, 7]."""
    if isinstance(gems, str):
        if len(gems) != 16 or not gems.isdecimal():
            raise ValueError(f"gems string must be 16 digits 0-7, got {gems!r}")
        out = [int(c) for c in gems]
    else:
        out = []
        for g in gems:
            if isinstance(g, str):
                g_lower = g.lower()
                if g_lower not in GEM_NAME_TO_INT:
                    raise ValueError(f"unknown gem name {g!r}")
                out.append(GEM_NAME_TO_INT[g_lower])
            else:
                out.append(int(g))
    if len(out) != 16:
        raise ValueError(f"need exactly 16 gem entries, got {len(out)}")
    for g in out:
        if g < 0 or g > 7:
            raise ValueError(f"gem value {g} out of range 0..7")
    return out


def _write_save_data(
    out,
    file_content: bytearray,
    rack_bytes: bytes,
    gems: list[int],
    enable_gems: bool,
) -> None:
    """Write the modified save bytes to `out` (a writable binary file).

    `file_content` is the unmodified template/source save (read from disk).
    `rack_bytes` is a 16-byte ASCII letter sequence.
    `gems` is a list of 16 ints in [0, 7] (0 = no gem).
    `enable_gems` toggles the per-save 'gems can spawn' flag at byte 15.
    """
    # Header (first 15 bytes) is preserved verbatim, then we set the gems flag.
    out.write(file_content[0:15])
    out.write(b"\x01" if enable_gems else b"\x00")

    # Locate the GridState region in the template.
    rack_beginning = 0
    rack_ending = 0
    for i in range(len(file_content)):
        if file_content[i : i + 9] == _DELIM_GRIDSTATE:
            rack_beginning = i  # the G of GridState
        if file_content[i : i + 9] == _DELIM_HOF:
            rack_ending = i  # the H of HOFNewTop
            break
    if rack_beginning == 0 or rack_ending == 0:
        raise ValueError("template missing GridState/HOFNewTop delimiters")

    # Body up to the rack.
    out.write(file_content[16:rack_beginning])

    # The 16 tile records.
    for i in range(16):
        if i == 0:
            if gems[0]:
                out.write(_GRIDSTATE_HEAD_WITH_GEM)
            else:
                out.write(_GRIDSTATE_HEAD_NO_GEM)
        out.write(_M_LETTER)
        out.write(bytes([rack_bytes[i]]))
        if gems[i]:
            out.write(_GEM_SETUP)
            out.write(_BINARY_GEMS[gems[i]])
        out.write(_MAGIC_NUMBERS[i])
        if i != 15:
            out.write(_AFTER_LETTER_1)
            if i % 4 == 3:
                out.write(_EVERY_FOURTH_LETTER)
            if gems[i + 1]:
                out.write(_AFTER_LETTER_WITH_GEM)
            else:
                out.write(_AFTER_LETTER_NO_GEM)

    # Locate the Items/Last5HP region.
    j = 0
    potions_beginning = 0
    potions_ending = 0
    for i in range(len(file_content)):
        if file_content[i : i + 5] == _DELIM_ITEMS:
            if j == 0:
                j += 1
            else:
                potions_beginning = i
        if file_content[i : i + 7] == _DELIM_LAST5HP:
            potions_ending = i
            break
    if potions_beginning == 0 or potions_ending == 0:
        raise ValueError("template missing Items/Last5HP delimiters")

    # Bytes between rack end and potions block.
    out.write(file_content[rack_ending:potions_beginning])

    # For the automation we always run with zero potions. This is the same
    # path the editor takes when all three potion counts are 0.
    out.write(_NO_POTIONS_BLOCK)

    # Trailer: everything after the potions region.
    out.write(file_content[potions_ending:])


def _build_new_save_bytes(
    source_bytes: bytes,
    rack: str,
    gems,
    enable_gems: bool,
) -> bytes:
    """Return the full new save file contents.

    `source_bytes` is the bytes of an existing valid .bwa save (used for
    headers/footers and any state we don't override).
    """
    import io

    rack_bytes = _normalize_rack(rack)
    gem_ints = _normalize_gems(gems)
    buf = io.BytesIO()
    _write_save_data(buf, bytearray(source_bytes), rack_bytes, gem_ints, enable_gems)
    return buf.getvalue()


def override_rack_in_place(
    save_path: Path,
    rack: str,
    gems,
    *,
    enable_gems: bool = False,
) -> None:
    """Override the rack/gems of an existing .bwa save file IN PLACE.

    Mirrors the editor's "Override Current Rack" action. The file at
    `save_path` is read, the rack and gems are swapped to the given values,
    and the new bytes are written back to the same path. All other game
    state (HP, position in chapter, items, etc.) is preserved.

    This is what the bank builder uses between iterations - we want to
    keep the player in the same chapter, just change the rack each round.

    `rack` is 16 alphabetic characters (case-insensitive); `gems` is either
    16 ints 0..7, 16 gem-name strings, or a 16-char digit string.

    Raises FileNotFoundError if the save doesn't exist (no template fallback -
    use write_savestate if you want to create a fresh save from a template).
    """
    save_path = Path(save_path)
    if not save_path.exists():
        raise FileNotFoundError(
            f"save not found at {save_path}; can't override in place. "
            "Create a save in-game first, or use write_savestate to create "
            "one from a template."
        )
    source_bytes = save_path.read_bytes()
    new_bytes = _build_new_save_bytes(source_bytes, rack, gems, enable_gems)
    save_path.write_bytes(new_bytes)


def write_savestate(
    template_path: Path,
    target_path: Path,
    rack: str,
    gems,
    *,
    enable_gems: bool = False,
) -> None:
    """Write a new BAD save from a template, with rack/gems substituted.

    Equivalent to the editor's "Reset Chapter" action (minus the side
    effect of deleting other SAVESTATE files in the directory). Reads
    `template_path`, swaps in the rack and gems, writes to `target_path`.

    `target_path` is replaced if it already exists. If you want to
    preserve player progress (HP, position, items, etc.), use
    `override_rack_in_place` instead - that variant edits an existing
    save in place.

    `rack` is 16 alphabetic characters (case-insensitive); `gems` is either
    16 ints 0..7, 16 gem-name strings, or a 16-char digit string.

    Raises ValueError on malformed input or a template without the expected
    delimiters.
    """
    template_path = Path(template_path)
    target_path = Path(target_path)
    if not template_path.exists():
        raise FileNotFoundError(f"template not found: {template_path}")

    source_bytes = template_path.read_bytes()
    new_bytes = _build_new_save_bytes(source_bytes, rack, gems, enable_gems)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(new_bytes)
