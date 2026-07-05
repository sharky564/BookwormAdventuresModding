"""Per-chapter enemy roster: read and edit the creature lists in packs/Book{N}.luc.

Each chapter is an ordered list of creature script paths (e.g. "creatures/Book1/Medusa");
the last entry is the chapter boss. Replacing an enemy means pointing a slot at a different
creature path, which swaps in that creature's attacks, HP, animations, and behavior. Built
on the same constant-repoint primitive as the stat mods (bwakit.bytecode.edit)."""

from bwakit.bytecode import luc_transcode as T
from bwakit.bytecode.edit import repoint_loadk

_NEWTABLE, _LOADK = 10, 1
_PREFIX = "creatures/"
_TREASURES = "treasures/"


def _s(v):
    if isinstance(v, bytes):
        return v.rstrip(b"\x00").decode("latin-1", "replace")
    return v.rstrip("\x00") if isinstance(v, str) else v


def _list_proto(chunk, prefix):
    """The proto whose constants hold the most `prefix` path strings."""

    def walk(p):
        yield p
        for s in p.protos:
            yield from walk(s)

    best = None
    for p in walk(chunk):
        n = sum(
            1 for c in p.consts if isinstance(c[1], (str, bytes)) and prefix in _s(c[1])
        )
        if n and (best is None or n > best[1]):
            best = (p, n)
    return best[0] if best else None


def chapter_scripts_proto(chunk):
    """The proto whose constants hold the per-chapter creature table (nested)."""
    return _list_proto(chunk, _PREFIX)


def treasure_proto(chunk):
    """The proto holding the per-chapter treasure-award list (a flat list)."""
    return _list_proto(chunk, _TREASURES)


def _slots(proto):
    """Per-chapter list of (pc, kidx, path) for each creature-loading LOADK, in order."""
    chapters, cur, seen_outer = [], None, False
    for pc, w in enumerate(proto.code):
        op = w & 0x3F
        if op == _NEWTABLE:
            if not seen_outer:
                seen_outer = True  # the outer container table
                continue
            cur = []
            chapters.append(cur)
        elif op == _LOADK and cur is not None:
            kidx = (w >> 6) & 0x3FFFF
            path = _s(proto.consts[kidx][1])
            if isinstance(path, str) and path.startswith(_PREFIX):
                cur.append((pc, kidx, path))
    return [ch for ch in chapters if ch]  # drop the trailing `local temp = {}` table


def read_roster(pack_luc):
    """Return chapters as lists of creature paths (chapter 1 == index 0)."""
    return [
        [p for _, _, p in ch]
        for ch in _slots(chapter_scripts_proto(T.parse_full(pack_luc)))
    ]


def repoint_path(proto, pc, kidx, new_path):
    """Repoint the LOADK at `pc` (loading const `kidx`) to `new_path`, preserving the
    original constant's str/bytes form and trailing NUL. Appends a fresh constant and
    repoints only this LOADK, so shared constants are never disturbed. Returns old path."""
    ctype, oldval = proto.consts[kidx]
    old = _s(oldval)
    if isinstance(oldval, bytes):
        newval = new_path.encode("latin-1") + b"\x00"
    else:
        newval = new_path + ("\x00" if str(oldval).endswith("\x00") else "")
    repoint_loadk(proto, pc, (ctype, newval))
    return old


def chapter_slots(proto):
    """Public view of the per-chapter creature slots: list of [(pc, kidx, path), ...]."""
    return _slots(proto)


def treasure_slots(proto):
    """Flat ordered list of (pc, kidx, path) for each treasure-loading LOADK."""
    out = []
    for pc, w in enumerate(proto.code):
        if (w & 0x3F) == _LOADK:
            kidx = (w >> 6) & 0x3FFFF
            path = _s(proto.consts[kidx][1])
            if isinstance(path, str) and path.startswith(_TREASURES):
                out.append((pc, kidx, path))
    return out


def set_slot(proto, chapter0, slot0, new_path):
    """Repoint the creature at 0-based (chapter, slot) to `new_path`. Returns the old path."""
    pc, kidx, old = _slots(proto)[chapter0][slot0]
    return repoint_path(proto, pc, kidx, new_path)


# --- books/Book{N}.luc : the actual fight roster -------------------------------------
# packs/Book{N}.luc only *preloads* creature scripts per chapter; the roster the engine
# actually fights is built by the AddEnemy("Name", ...) calls in books/Book{N}.luc, where
# each creature is a bare class NAME (not a "creatures/..." path) held as a string constant.
# These helpers find and repoint those name constants so the roster can be reordered.


def book_roster_proto(chunk, names):
    """The proto whose constants hold the most creature-name strings from `names`
    (the AddEnemy arguments live together in one proto of books/Book{N}.luc)."""

    def walk(p):
        yield p
        for s in p.protos:
            yield from walk(s)

    best = None
    for p in walk(chunk):
        n = sum(
            1 for c in p.consts if isinstance(c[1], (str, bytes)) and _s(c[1]) in names
        )
        if n and (best is None or n > best[1]):
            best = (p, n)
    return best[0] if best else None


def book_roster_slots(proto, names):
    """Ordered [(pc, kidx, chapter, name), ...] for each AddEnemy(chapter, "Name", ...)
    creature argument, in code order (== chapter-major roster order).

    An AddEnemy name is compiled as two consecutive constant loads -- LOADK <chapter int>
    then LOADK "Name" -- so a creature-name LOADK is a roster entry iff the LOADK right
    before it loads a small integer (the chapter). This excludes other creature-name uses
    such as AddCutScene("MaladinWave", "MysteriousAssassin", ...), where the preceding
    constant is a string, not the chapter number."""
    out = []
    code = proto.code
    for pc in range(1, len(code)):
        w = code[pc]
        if (w & 0x3F) != _LOADK:
            continue
        kidx = (w >> 6) & 0x3FFFF
        name = _s(proto.consts[kidx][1])
        if not (isinstance(name, str) and name in names):
            continue
        pw = code[pc - 1]
        if (pw & 0x3F) != _LOADK:
            continue
        cval = proto.consts[(pw >> 6) & 0x3FFFF][1]
        if isinstance(cval, bool) or not isinstance(cval, (int, float)):
            continue
        if 1 <= cval <= 30 and float(cval).is_integer():
            out.append((pc, kidx, int(cval), name))
    return out


# --- treasure awards : creatures award a treasure via `mTreasure = <Class>:new()` -----
# The treasure class is loaded with a global-load op (PopCap has two: 5 and 42, GETGLOBAL
# and GETGLOBAL_MEM); both carry the global name in Bx. Swapping which class a boss loads
# changes which treasure it drops. Same append+repoint discipline, on the Bx constant.
_GLOBAL_OPS = (5, 42)


def _walk(p):
    yield p
    for s in p.protos:
        yield from _walk(s)


def boss_treasure(chunk, treasure_names):
    """The treasure class this script awards (first global-load whose name is in
    `treasure_names`), or None."""
    for p in _walk(chunk):
        for w in p.code:
            if (w & 0x3F) in _GLOBAL_OPS:
                nm = _s(p.consts[(w >> 6) & 0x3FFFF][1])
                if isinstance(nm, str) and nm in treasure_names:
                    return nm
    return None


def repoint_global(chunk, from_name, to_name):
    """Repoint every global-load of `from_name` to load `to_name`, preserving the
    constant's str/bytes form and trailing NUL. Returns the count repointed (handles the
    multi-phase bosses that load their treasure class in more than one method)."""
    from bwakit.bytecode.edit import repoint_getglobal

    n = 0
    for p in _walk(chunk):
        for pc, w in enumerate(p.code):
            if (w & 0x3F) not in _GLOBAL_OPS:
                continue
            kidx = (w >> 6) & 0x3FFFF
            ctype, oldval = p.consts[kidx]
            if _s(oldval) != from_name:
                continue
            if isinstance(oldval, bytes):
                newval = to_name.encode("latin-1") + (
                    b"\x00" if oldval.endswith(b"\x00") else b""
                )
            else:
                newval = to_name + ("\x00" if str(oldval).endswith("\x00") else "")
            repoint_getglobal(p, pc, (ctype, newval))
            n += 1
    return n
