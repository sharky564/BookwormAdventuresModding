"""Shared low-level bytecode edits used by the mods: append a constant and repoint the
LOADK that references it. Every edit appends a fresh constant rather than mutating a shared
one, so unrelated uses are never disturbed. Both the stat mods (hp_scaling) and the roster
editor (game.encounters) build on these."""


def append_const(proto, const):
    """Append a (type, value) constant, deduplicating exact matches. Returns its index."""
    for i, c in enumerate(proto.consts):
        if c == const:
            return i
    proto.consts = list(proto.consts) + [const]
    return len(proto.consts) - 1


def repoint_loadk(proto, pc, const):
    """Repoint the LOADK at `pc` to `const` (appended/deduped). Returns the old constant index."""
    w = proto.code[pc]
    assert (w & 0x3F) == 1, f"pc{pc} is not a LOADK (op={w & 0x3F})"
    old = (w >> 6) & 0x3FFFF
    ni = append_const(proto, const)
    a = (w >> 24) & 0xFF
    proto.code[pc] = 1 | (a << 24) | (ni << 6)
    return old


def repoint_getglobal(proto, pc, const):
    """Repoint a global-load instruction at `pc` (which loads the global named by its Bx
    constant) to load `const` instead. PopCap's VM has two global-load opcodes (5 and 42,
    GETGLOBAL / GETGLOBAL_MEM); both carry the name in Bx, so the opcode is preserved.
    Returns the old constant index."""
    w = proto.code[pc]
    op = w & 0x3F
    assert op in (5, 42), f"pc{pc} op={op} is not a global load"
    old = (w >> 6) & 0x3FFFF
    ni = append_const(proto, const)
    a = (w >> 24) & 0xFF
    proto.code[pc] = op | (a << 24) | (ni << 6)
    return old


def patch_compare_rk(proto, opcode, operand, from_num, to_num):
    """Repoint the RK `operand` ('B' or 'C') of the single instruction with `opcode` whose
    that operand is an RK referencing a numeric constant equal to `from_num`, pointing it at
    a fresh constant equal to `to_num`. Asserts exactly one match so it can never silently
    patch the wrong comparison. Used to neutralize a chapter gate like `chapter >= 4` (op 25
    LE, operand B = constant 4) without disturbing the shared constant. Returns its pc."""
    assert operand in ("B", "C")
    shift = 15 if operand == "B" else 6
    fk = {
        i: t
        for i, (t, v) in enumerate(proto.consts)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v == from_num
    }
    hits = []
    for pc, w in enumerate(proto.code):
        if (w & 0x3F) != opcode:
            continue
        field = (w >> shift) & 0x1FF
        if field >= 250 and (field - 250) in fk:
            hits.append(pc)
    assert len(hits) == 1, (
        "patch_compare_rk: expected 1 match for op%d %s==%r, got %d"
        % (opcode, operand, from_num, len(hits))
    )
    pc = hits[0]
    w = proto.code[pc]
    ctype = fk[((w >> shift) & 0x1FF) - 250]
    ni = append_const(proto, (ctype, int(to_num) if ctype == "int" else float(to_num)))
    proto.code[pc] = (w & ~(0x1FF << shift)) | ((250 + ni) << shift)
    return pc


def patch_compare_rk_all(chunk, opcode, operand, from_num, to_num):
    """Across every proto in `chunk`, repoint the RK `operand` ('B'|'C') of EVERY
    instruction with `opcode` whose that operand references a numeric constant == from_num,
    to a fresh constant == to_num (appended per proto). Returns the count patched. Use when a
    single logical gate (e.g. the `chapter >= 4` scramble-enable) is duplicated across
    several methods AND you've confirmed no unrelated comparison shares the same shape."""
    assert operand in ("B", "C")
    shift = 15 if operand == "B" else 6

    def _walk(p):
        yield p
        for s in p.protos:
            yield from _walk(s)

    n = 0
    for p in _walk(chunk):
        fk = {
            i: t
            for i, (t, v) in enumerate(p.consts)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == from_num
        }
        if not fk:
            continue
        targets = [
            pc
            for pc, w in enumerate(p.code)
            if (w & 0x3F) == opcode
            and ((w >> shift) & 0x1FF) >= 250
            and (((w >> shift) & 0x1FF) - 250) in fk
        ]
        for pc in targets:
            w = p.code[pc]
            ctype = fk[((w >> shift) & 0x1FF) - 250]
            ni = append_const(
                p, (ctype, int(to_num) if ctype == "int" else float(to_num))
            )
            p.code[pc] = (w & ~(0x1FF << shift)) | ((250 + ni) << shift)
            n += 1
    return n


def scale_loadks(proto, pcs, factor):
    """For each LOADK at the given PCs, scale its numeric constant by `factor`, appending a
    new constant per distinct scaled value and repointing. Returns [(pc, base, scaled)]."""
    cache, out = {}, []
    for pc in pcs:
        w = proto.code[pc]
        assert (w & 0x3F) == 1, f"pc{pc} is not a LOADK (op={w & 0x3F})"
        bx = (w >> 6) & 0x3FFFF
        ctype, base = proto.consts[bx]
        assert ctype in ("int", "num"), f"pc{pc} constant {ctype} is not numeric"
        scaled = max(1, round(float(base) * factor))
        out.append((pc, base, scaled))
        key = (ctype, scaled)
        if key not in cache:
            proto.consts = list(proto.consts) + [
                (ctype, int(scaled) if ctype == "int" else float(scaled))
            ]
            cache[key] = len(proto.consts) - 1
        ni = cache[key]
        a = (w >> 24) & 0xFF
        proto.code[pc] = 1 | (a << 24) | (ni << 6)
    return out


def set_xp(proto, value):
    """Set self.mXP = value in a creature Init proto by repointing the mXP SETTABLE's value
    constant (or the LOADK feeding it) to a fresh constant -- never mutating a shared const.
    The engine runs assignments in order, so the LAST mXP write is the one that sticks (a few
    creatures assign it twice); this edits that one. Returns True if found and repointed."""

    def _s(v):
        return (
            v.split(b"\x00")[0].decode("latin1", "replace")
            if isinstance(v, bytes)
            else v
        )

    ki = {i for i, (t, v) in enumerate(proto.consts) if _s(v) == "mXP"}
    if not ki:
        return False
    hits = [
        pc
        for pc, w in enumerate(proto.code)
        if (w & 0x3F) == 9
        and ((w >> 15) & 0x1FF) >= 250
        and (((w >> 15) & 0x1FF) - 250) in ki
    ]
    if not hits:
        return False
    pc = hits[-1]  # last write wins at runtime
    w = proto.code[pc]
    C = (w >> 6) & 0x1FF
    if C >= 250:  # value is a constant -> repoint it
        ctype = proto.consts[C - 250][0]
        ni = append_const(
            proto, (ctype, int(value) if ctype == "int" else float(value))
        )
        proto.code[pc] = (w & ~(0x1FF << 6)) | ((250 + ni) << 6)
        return True
    for j in range(pc - 1, -1, -1):  # value in a register -> repoint its LOADK
        wj = proto.code[j]
        if (wj & 0x3F) == 1 and ((wj >> 24) & 0xFF) == C:
            ctype = proto.consts[(wj >> 6) & 0x3FFFF][0]
            ni = append_const(
                proto, (ctype, int(value) if ctype == "int" else float(value))
            )
            proto.code[j] = 1 | (C << 24) | (ni << 6)
            return True
    return False
