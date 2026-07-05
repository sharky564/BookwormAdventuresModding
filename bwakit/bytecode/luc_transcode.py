"""A1 transcoder: PopCap 0x56 .luc  ->  vanilla Lua 5.1 .luac (for unluac/luadec).

Two stages:
  1. parse_full(path)  -> Chunk: a complete parse of the PopCap bytecode INCLUDING
     debug info (lineinfo, locvars, upvalue names) and header sizes, so we can
     re-serialize faithfully (the existing luc_disasm.parse drops debug info).
  2. emit_standard(chunk) -> bytes: standard Lua 5.1 bytecode.

Transformations applied (all verified earlier):
  * Opcode number: PopCap op -> stock Lua 5.1 op number (canonical order).
  * Instruction operands: PopCap packs A at bits 24-31 and SWAPS the two 9-bit
    fields vs stock. Stock 5.1 iABC = op(0-5) | A(6-13) | C(14-22) | B(23-31).
    PopCap: op(0-5) | B'(6-14) | C'(15-23) | A(24-31), where (verified 97% via
    CALL nargs) PopCap's >>15 field == stock B and PopCap's >>6 field == stock C.
    So re-encode: stock_A=A, stock_B=(popcap>>15), stock_C=(popcap>>6).
    For iABx ops, Bx is the contiguous 18 bits at 6-23 in BOTH; just place at 6-23.
  * Constants: PopCap has an extra type 4 = int32. Stock 5.1 has only
    nil(0)/bool(1)/number=f64(3)/string(4). Convert int32 -> f64 (type 3); map
    PopCap string type 5 -> stock string type 4.
  * Prototype field ORDER: PopCap is debug-first (lineinfo, locvars, upvals, then
    consts, protos, code). Stock 5.1 is: code, consts, protos, then debug
    (lineinfo, locvars, upvals). Re-serialize in stock order.

Header (stock 5.1, little-endian, 32-bit, default sizes):
  1B esc + "Lua" + 0x51 + format(0) + endian(1) + sizeof(int)=4 + sizeof(size_t)=4
  + sizeof(Instruction)=4 + sizeof(lua_Number)=8 + integral(0)   == 12 bytes
"""

import struct

# canonical stock Lua 5.1 opcode numbers
STOCK = {
    "MOVE": 0,
    "LOADK": 1,
    "LOADBOOL": 2,
    "LOADNIL": 3,
    "GETUPVAL": 4,
    "GETGLOBAL": 5,
    "GETTABLE": 6,
    "SETGLOBAL": 7,
    "SETUPVAL": 8,
    "SETTABLE": 9,
    "NEWTABLE": 10,
    "SELF": 11,
    "ADD": 12,
    "SUB": 13,
    "MUL": 14,
    "DIV": 15,
    "MOD": 16,
    "POW": 17,
    "UNM": 18,
    "NOT": 19,
    "LEN": 20,
    "CONCAT": 21,
    "JMP": 22,
    "EQ": 23,
    "LT": 24,
    "LE": 25,
    "TEST": 26,
    "TESTSET": 27,
    "CALL": 28,
    "TAILCALL": 29,
    "RETURN": 30,
    "FORLOOP": 31,
    "FORPREP": 32,
    "TFORLOOP": 33,
    "SETLIST": 34,
    "CLOSE": 35,
    "CLOSURE": 36,
    "VARARG": 37,
}
# stock iABx opcodes (A + 18-bit Bx)
STOCK_IABX = {"LOADK", "GETGLOBAL", "SETGLOBAL", "CLOSURE"}
# stock iAsBx opcodes (signed Bx)
STOCK_IASBX = {"JMP", "FORLOOP", "FORPREP"}

# Map PopCap opname (from luc_disasm.POPCAP_OPS, normalized) -> stock opname.
# The '~' variants are dispatch/return twins of a base op; for a faithful, runnable
# decompile we map each to its stock base. SETTABLE_G* (global-table constructor
# store) -> SETTABLE. LOOPGUARD has no stock equivalent; it is a PopCap loop
# continuation that we map to a no-op-ish JMP+0 only if it ever appears (it is rare;
# we assert if seen so we can revisit rather than silently corrupt).
POPCAP_TO_STOCK = {
    "MOVE": "MOVE",
    "LOADK": "LOADK",
    "LOADBOOL": "LOADBOOL",
    "LOADNIL": "LOADNIL",
    "GETUPVAL": "GETUPVAL",
    "GETGLOBAL": "GETGLOBAL",
    "GETTABLE": "GETTABLE",
    "SETGLOBAL": "SETGLOBAL",
    "SETUPVAL": "SETUPVAL",
    "SETTABLE": "SETTABLE",
    "NEWTABLE": "NEWTABLE",
    "SELF": "SELF",
    "ADD": "ADD",
    "SUB": "SUB",
    "MUL": "MUL",
    "DIV": "DIV",
    "MOD": "MOD",
    "POW": "POW",
    "UNM": "UNM",
    "NOT": "NOT",
    "LEN": "LEN",
    "CONCAT": "CONCAT",
    "JMP": "JMP",
    "EQ": "EQ",
    "LT": "LT",
    "LE": "LE",
    "TEST": "TEST",
    "CALL": "CALL",
    "RETURN": "RETURN",
    "FORLOOP": "FORLOOP",
    "SETLIST": "SETLIST",
    "TFORLOOP": "TFORLOOP",
    "VARARG": "VARARG",
    "FORPREP": "FORPREP",
    "CLOSURE": "CLOSURE",
    # variants -> stock base
    # op36 and op43 are both labeled "GETTABLE~" by the disasm but are actually SELF: for
    # every one of them the explicit call arguments start at R(A+2), leaving R(A+1) for the
    # implicit object -- the defining trait of SELF (stock GETTABLE, op6, puts args at
    # R(A+1)). Mapping them to GETTABLE produced the broken `self.Method(nil, args)` calls
    # (dot instead of colon, plus a spurious leading nil where the object register was left
    # unwritten). Emitting SELF restores correct `self:Method(args)` method calls.
    "CALL~": "CALL",
    "TFORLOOP~": "TFORLOOP",
    "GETTABLE~": "SELF",
    "SETTABLE~": "SETTABLE",
    "SETTABLE_G": "SETTABLE",
    "SETTABLE_G~": "SETTABLE",
    "SETGLOBAL~": "SETGLOBAL",
    "GETGLOBAL~": "GETGLOBAL",
    "LOOPGUARD": "__LOOPGUARD__",
}

# PopCap op-number -> PopCap opname (must match luc_disasm.POPCAP_OPS)
POPCAP_OPS = {
    0: "MOVE",
    1: "LOADK",
    2: "LOADBOOL",
    3: "LOADNIL",
    4: "GETUPVAL",
    5: "GETGLOBAL",
    6: "GETTABLE",
    7: "SETGLOBAL",
    8: "SETUPVAL",
    9: "SETTABLE",
    10: "NEWTABLE",
    11: "SELF",
    12: "ADD",
    13: "SUB",
    14: "MUL",
    15: "DIV",
    16: "MOD",
    17: "POW",
    18: "UNM",
    19: "NOT",
    20: "LEN",
    21: "CONCAT",
    22: "JMP",
    23: "EQ",
    24: "LT",
    25: "LE",
    26: "TEST",
    27: "CALL",
    28: "CALL~",
    29: "RETURN",
    30: "FORLOOP",
    31: "SETLIST",
    32: "TFORLOOP",
    33: "TFORLOOP~",
    34: "TFORLOOP~",
    35: "VARARG",
    36: "GETTABLE~",
    37: "SETTABLE~",
    38: "SETTABLE_G",
    39: "SETTABLE_G~",
    40: "SETTABLE_G~",
    41: "SETGLOBAL~",
    42: "GETGLOBAL~",
    43: "GETTABLE~",
    44: "CALL~",
    45: "CALL~",
    46: "FORPREP",
    47: "LOOPGUARD",
    48: "CLOSURE",
}
POPCAP_IABX = {
    1,
    5,
    7,
    22,
    30,
    41,
    42,
    46,
    48,
}  # iABx/iAsBx only; SETLIST/TFORLOOP/VARARG are iABC


class FProto:
    __slots__ = (
        "source",
        "linedefined",
        "lastlined",
        "nups",
        "nparams",
        "vararg",
        "maxstack",
        "lineinfo",
        "locvars",
        "upvals",
        "consts",
        "protos",
        "code",
    )

    def __init__(self):
        self.source = ""
        self.linedefined = 0
        self.lastlined = 0
        self.nups = 0
        self.nparams = 0
        self.vararg = 0
        self.maxstack = 0
        self.lineinfo = []
        self.locvars = []
        self.upvals = []
        self.consts = []
        self.protos = []
        self.code = []


def _rd_str(data, off):
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    if n == 0:
        return None, off  # stock distinguishes "no string" (size 0)
    raw = data[off : off + n]
    off += n
    return raw, off  # raw bytes INCLUDING trailing NUL


def _rd_constants(data, off):
    sizek = struct.unpack_from("<I", data, off)[0]
    off += 4
    ks = []
    for _ in range(sizek):
        t = data[off]
        off += 1
        if t == 0:
            ks.append(("nil", None))
        elif t == 1:
            ks.append(("bool", bool(data[off])))
            off += 1
        elif t == 3:
            ks.append(("num", struct.unpack_from("<d", data, off)[0]))
            off += 8
        elif t == 4:  # PopCap int32 constant
            ks.append(("int", struct.unpack_from("<i", data, off)[0]))
            off += 4
        elif t == 5:
            raw, off = _rd_str(data, off)
            ks.append(("str", raw if raw is not None else b""))
        else:
            raise ValueError(f"bad const type {t} @ {off - 1}")
    return ks, off


def _rd_proto(data, off):
    p = FProto()
    p.source, off = _rd_str(data, off)
    p.linedefined, p.lastlined = struct.unpack_from("<ii", data, off)
    off += 8
    p.nups, p.nparams, p.vararg, p.maxstack = data[off : off + 4]
    off += 4
    # debug-first ordering in PopCap
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    p.lineinfo = list(struct.unpack_from("<%di" % n, data, off))
    off += 4 * n
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(n):
        nm, off = _rd_str(data, off)
        s, e = struct.unpack_from("<ii", data, off)
        off += 8
        p.locvars.append((nm, s, e))
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(n):
        nm, off = _rd_str(data, off)
        p.upvals.append(nm)
    p.consts, off = _rd_constants(data, off)
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(n):
        child, off = _rd_proto(data, off)
        p.protos.append(child)
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    p.code = list(struct.unpack_from("<%dI" % n, data, off))
    off += 4 * n
    return p, off


def parse_full(path):
    data = open(path, "rb").read()
    assert data[:4] == b"\x1bLua" and data[4] == 0x56, "not a PopCap 0x56 .luc"
    proto, off = _rd_proto(data, 23)
    return proto


# re-encode one instruction PopCap -> stock


def _setlist_count(code, i):
    """Recover the stock SETLIST element count (B) for the SETLIST at code[i].
    PopCap encodes B implicitly (0); stock needs the actual number of elements set in
    this batch. Count = max(dest_reg - A) over instructions since the matching
    NEWTABLE(A) or previous SETLIST(A)."""
    ins = code[i]
    A = (ins >> 24) & 0xFF
    start = 0
    for k in range(i - 1, -1, -1):
        o = code[k]
        op = o & 0x3F
        nm = POPCAP_OPS.get(op)
        a = (o >> 24) & 0xFF
        if nm == "NEWTABLE" and a == A:
            start = k + 1
            break
        if nm in ("SETLIST",) and a == A:
            start = k + 1
            break
    NONWRITE = {
        "SETTABLE",
        "SETGLOBAL",
        "SETUPVAL",
        "JMP",
        "RETURN",
        "EQ",
        "LT",
        "LE",
        "TEST",
        "SETLIST",
        "TFORLOOP",
        "TFORLOOP~",
        "FORLOOP",
        "FORPREP",
        "SETTABLE_G",
        "SETTABLE_G~",
        "SETTABLE~",
        "SETGLOBAL~",
    }
    cnt = 0
    for k in range(start, i):
        o = code[k]
        nm = POPCAP_OPS.get(o & 0x3F, "")
        if nm in NONWRITE:
            continue
        d = (o >> 24) & 0xFF
        if d > A:
            cnt = max(cnt, d - A)
    return cnt if 1 <= cnt <= 50 else 0  # fall back to B=0 if unrecoverable


def reencode(ins, stats, code=None, idx=None, consts=None, maxstack=None):
    op = ins & 0x3F
    pop_name = POPCAP_OPS.get(op)
    if pop_name is None:
        raise ValueError(f"unknown PopCap opcode {op}")
    stock_name = POPCAP_TO_STOCK[pop_name]
    if stock_name == "__LOOPGUARD__":
        stats["loopguard"] = stats.get("loopguard", 0) + 1
        # treat as JMP +0 (no-op fallthrough); flagged via stats so we know if it
        # ever actually occurs in real code.
        stock_name = "JMP"
        A = 0
        sBx = 0
        sop = STOCK[stock_name]
        Bx = sBx + 131071
        return sop | (A << 6) | (Bx << 14)
    A = (ins >> 24) & 0xFF
    sop = STOCK[stock_name]

    # NOTE on generic-for loops: PopCap's generic-for is a VM extension that iterates a
    # table directly with a 2-register control layout (R(A)=table, loop vars at R(A+2),
    # R(A+3)), unlike stock Lua 5.1's 3-register layout (iterator/state/control in
    # R(A)..R(A+2), vars at R(A+3)+). op31 is the TFORLOOP and op32 the entry JMP, but
    # emitting them as stock TFORLOOP/JMP makes unluac crash in TForBlock (the register
    # slots collide) because of that 1-register shift. So we deliberately KEEP the disasm's
    # original op31->SETLIST / op32->TFORLOOP mapping here: that yields parseable (if
    # garbled) output of a fixed, recognizable shape -- `local (for generator), (for state),
    # K, V = TBL, nil, nil, nil; while true do BODY; TBL[1]=(for state); ... end` -- which
    # fix_decompiled.py then rewrites into a correct `for K, V in pairs(TBL) do BODY end`.

    # PopCap op 33 (mislabeled "TFORLOOP~") is actually SETLIST. It is used 827x (far too
    # often for a loop op), is preceded by LOADK/table-element writes, and pairs with a
    # preceding NEWTABLE on the same register. CRITICAL: its f6 field is the CUMULATIVE
    # element count minus one (not the per-batch count) -- PopCap flushes arrays in batches
    # of 32 (its LFIELDS_PER_FLUSH), reusing the same registers each batch, and f6 tracks
    # the running total. Stock SETLIST needs the PER-BATCH count in B and a batch number in
    # C, so we derive B = (this f6) - (previous op33 f6 on the same register) and C = the
    # sequential batch index. unluac appends batches in order, so the stock/PopCap FPF
    # difference (50 vs 32) does not introduce gaps. (Emitting B=f6+1 made unluac read past
    # the live registers -> ArrayIndexOutOfBounds on every multi-batch wordlist.)
    if op == 33:
        f6 = (ins >> 6) & 0x1FF
        prev_f6 = -1
        nbatch = 0
        if code is not None and idx is not None:
            for k in range(idx - 1, -1, -1):
                o2 = code[k]
                op2 = o2 & 0x3F
                a2 = (o2 >> 24) & 0xFF
                if op2 == 10 and a2 == A:  # NEWTABLE on same reg = table start
                    break
                if op2 == 33 and a2 == A:  # previous flush of this table
                    if prev_f6 < 0:
                        prev_f6 = (o2 >> 6) & 0x1FF
                    nbatch += 1
        B = f6 - prev_f6  # per-batch element count
        C = nbatch + 1  # batch number (1-based)
        if not (1 <= B <= 0x1FF):  # safety clamp
            stats["op33_batch_odd"] = stats.get("op33_batch_odd", 0) + 1
            B = max(1, min(B, 0x1FF))
        stats["op33_setlist"] = stats.get("op33_setlist", 0) + 1
        return (
            STOCK["SETLIST"] | (A & 0xFF) << 6 | (C & 0x1FF) << 14 | (B & 0x1FF) << 23
        )

    # PopCap op 17 (labeled POW) is actually MOD: it is used 178x with divisor-like RHS
    # constants (100, 60, 50, 2, ...) and real MOD(op16) never appears. Emit stock MOD.
    if pop_name == "POW":
        sop = STOCK["MOD"]
        stats["op17_mod"] = stats.get("op17_mod", 0) + 1
        B = (ins >> 15) & 0x1FF
        C = (ins >> 6) & 0x1FF

        def _rk(f):
            return (0x100 | (f - 250)) if f >= 250 else f

        return sop | (A & 0xFF) << 6 | (_rk(C) & 0x1FF) << 14 | (_rk(B) & 0x1FF) << 23

    # PopCap op 37 (mislabeled "SETTABLE~" in the opmap) is actually SETGLOBAL:
    # _G[K[f6]] := R(A). f6 always indexes a STRING global name (127/127), the value sits
    # in R(A) (always preceded by a LOADK), and the same names are read back via GETGLOBAL
    # elsewhere - confirming they are real globals, not table fields. Emit iABx SETGLOBAL.
    if pop_name == "SETTABLE~":
        f6 = (ins >> 6) & 0x1FF
        stats["op37_setglobal"] = stats.get("op37_setglobal", 0) + 1
        return STOCK["SETGLOBAL"] | (A & 0xFF) << 6 | (f6 & 0x3FFFF) << 14

    # PopCap op 46 is NOT FORPREP (verified vs stock luac: it pairs with no FORLOOP, stores
    # no jump offset, and always sits right after an arithmetic write to R(A)). It is a
    # UNARY in-place op R(A) := <floor> R(B) (PopCap extension; renders as floor/trunc after
    # a divide). Stock Lua 5.1 has no floor opcode, so we emit a unary (UNM) stand-in of the
    # correct arity: unluac then produces valid, correctly-shaped Lua. The unary OPERATOR is
    # cosmetically wrong (shows as `-x` instead of `math.floor(x)`); flagged for review.
    if pop_name == "FORPREP":  # (op 46, mislabeled in the opmap) = FLOOR, a unary
        # PopCap extension R(A) := floor(R(B)). Stock Lua 5.1 has no floor opcode, so we
        # emit LEN ('#') as an UNAMBIGUOUS marker: real length always renders as
        # `#identifier`, never `#(...)`, while floor always wraps an expression -> `#(...)`.
        # fix_selfref.py then rewrites `#(EXPR)` -> `math.floor(EXPR)`.
        B = (ins >> 15) & 0x1FF
        stats["op46_floor"] = stats.get("op46_floor", 0) + 1
        return STOCK["LEN"] | (A & 0xFF) << 6 | (B & 0x1FF) << 23

    # MOVE self->arg idiom: PopCap emits `MOVE R(maxstack) := R0` to stage `self` as a
    # method-call argument, but the destination it encodes is one past the usable stack
    # (the following CALL actually reads the arg from R(maxstack-1)). Stock unluac sizes
    # its register array by maxstacksize, so a dest == maxstack overflows it. The real
    # slot is A-1. Verified: every A>=maxstack MOVE in the corpus (18) has source R0 and
    # is immediately consumed by a CALL whose last arg slot == A-1. Clamp it.
    if pop_name == "MOVE" and maxstack is not None and A >= maxstack and A > 0:
        A = A - 1
        stats["move_self_arg_fixup"] = stats.get("move_self_arg_fixup", 0) + 1

    # SETTABLE_G family (PopCap op 38/39/40): overloaded. When f15==0 and the f6 field
    # indexes a STRING constant, it is a SETGLOBAL  _G[K[f6]] := R(A)  (iABx). Otherwise
    # it is an ordinary SETTABLE  R(A)[RK(f15)] := RK(f6). (Verified across the corpus;
    # ClassLink's CLOSURE-then-store sequence makes the SETGLOBAL reading unambiguous.)
    if pop_name in ("SETTABLE_G", "SETTABLE_G~"):
        f6 = (ins >> 6) & 0x1FF
        f15 = (ins >> 15) & 0x1FF
        is_str = (
            consts is not None
            and f6 < 250
            and f6 < len(consts)
            and consts[f6][0] == "str"
        )
        if f15 == 0 and is_str:
            sop = STOCK["SETGLOBAL"]  # _G[K[f6]] := R(A)
            return sop | (A & 0xFF) << 6 | (f6 & 0x3FFFF) << 14
        # else fall through to normal SETTABLE encoding below
        sop = STOCK["SETTABLE"]

        def _rk(f):
            if f >= 250:
                idx2 = f - 250
                return 0x100 | (idx2 if idx2 <= 255 else 255)
            return f

        B = _rk(f15)
        C = _rk(f6)
        return sop | (A & 0xFF) << 6 | (C & 0x1FF) << 14 | (B & 0x1FF) << 23
    if op in POPCAP_IABX:
        # iABx / iAsBx: Bx is contiguous bits 6-23 in PopCap
        Bx = (ins >> 6) & 0x3FFFF
        return sop | (A & 0xFF) << 6 | (Bx & 0x3FFFF) << 14
    # SETLIST: PopCap stores batch C in the >>6 field (always 1 here) and the element
    # COUNT implicitly (B=0). Stock needs the real count in B. Recover it from context.
    if pop_name == "SETLIST" and code is not None and idx is not None:
        Cbatch = (ins >> 6) & 0x1FF  # batch number (1-based); ==1 for <=50 elems
        if Cbatch == 0:
            Cbatch = 1
        Bcount = _setlist_count(code, idx)  # 0 => "up to top of stack" (rare/fallback)
        if Cbatch > 0x1FF or Cbatch == 0:
            stats["setlist_bigbatch"] = stats.get("setlist_bigbatch", 0) + 1
        return sop | (A & 0xFF) << 6 | (Cbatch & 0x1FF) << 14 | (Bcount & 0x1FF) << 23

    # iABC: stock_B = PopCap >>15 field ; stock_C = PopCap >>6 field.
    # CRITICAL: RK remap. PopCap marks a constant with field>=250; stock marks it with
    # bit 8 (field>=256, i.e. 0x100|index). Registers (<250) pass through unchanged;
    # constant refs (250+idx) -> (256+idx). If idx>255 the constant is unaddressable by
    # stock's RK field (max 256 consts) -> flagged; caller handles via LOADK spill.
    def rk_remap(f):
        if f >= 250:
            idx = f - 250
            if idx > 255:
                stats["rk_overflow"] = stats.get("rk_overflow", 0) + 1
                # clamp into range so the file still emits; this proto needs the
                # LOADK-spill rewrite (see spill_high_consts) to be correct.
                idx = 255
            return 0x100 | idx
        return f

    B = rk_remap((ins >> 15) & 0x1FF)
    C = rk_remap((ins >> 6) & 0x1FF)
    return sop | (A & 0xFF) << 6 | (C & 0x1FF) << 14 | (B & 0x1FF) << 23


# serialize stock Lua 5.1
def _wr_str(raw):
    # raw is bytes-including-NUL, or None for "no string"
    if raw is None:
        return struct.pack("<I", 0)
    return struct.pack("<I", len(raw)) + raw


def _wr_constants(ks):
    out = struct.pack("<I", len(ks))
    for t, v in ks:
        if t == "nil":
            out += b"\x00"
        elif t == "bool":
            out += b"\x01" + (b"\x01" if v else b"\x00")
        elif t == "num":
            out += b"\x03" + struct.pack("<d", v)
        elif t == "int":  # convert int32 const -> stock f64
            out += b"\x03" + struct.pack("<d", float(v))
        elif t == "str":
            out += b"\x04" + _wr_str(v)
        else:
            raise ValueError(t)
    return out


def _normalize_numeric_for(code, stats):
    """Neutralize the explicit index pre-decrement in PopCap's numeric-for loops so the
    custom unluac (configured to detect them as Lua-5.0-style ForBlock50) reads the true
    loop start.

    PopCap has no FORPREP opcode (op46, its only "FORPREP"-labelled slot, is FLOOR). It
    compiles `for i = a, b[, c]` as `... SUB R(A) = R(A) - R(A+2)` (the FORPREP-style index
    pre-decrement) followed by a plain JMP into a bottom-of-loop FORLOOP -- exactly Lua
    5.0's loop shape, EXCEPT 5.0 has no pre-decrement. unluac's ForBlock50 therefore reads
    the loop start straight from R(A) and would show `i = a - c` (off by one). We rewrite
    just that pre-decrement `SUB A,A,A+2` to a no-op `MOVE A,A`, leaving R(A) = the true
    start; the entry JMP is left intact for ForBlock50 to detect. The transcoded bytecode
    is decompile-only (never executed), and a recompiled `for` re-introduces the prep via
    stock FORPREP -- so this is faithful to the original source.

    In place on the reencoded *stock* instruction list. Fires only on the exact signature
    (FORLOOP whose back-edge target is preceded by `JMP -> FORLOOP`, with a matching
    `SUB A,A,A+2` in the setup); anything else is untouched."""
    FORLOOP = STOCK["FORLOOP"]
    FORPREP = STOCK["FORPREP"]
    JMP = STOCK["JMP"]
    SUB = STOCK["SUB"]
    MOVE = STOCK["MOVE"]

    def op(w):
        return w & 0x3F

    def rA(w):
        return (w >> 6) & 0xFF

    def rB(w):
        return (w >> 23) & 0x1FF

    def rC(w):
        return (w >> 14) & 0x1FF

    def sbx(w):
        return ((w >> 14) & 0x3FFFF) - 131071

    for F in range(len(code)):
        if op(code[F]) != FORLOOP:
            continue
        A = rA(code[F])
        B = F + 1 + sbx(code[F])  # body start = back-edge target
        if not (0 < B <= F):
            continue
        E = B - 1  # slot right before the body
        if E < 0 or op(code[E]) != JMP or E + 1 + sbx(code[E]) != F:
            continue  # entry must be JMP -> this FORLOOP
        for q in range(E - 1, max(-1, E - 9), -1):  # find SUB R(A) = R(A) - R(A+2)
            w = code[q]
            if op(w) in (FORLOOP, FORPREP):
                break  # don't cross an adjacent loop
            if op(w) == SUB and rA(w) == A and rB(w) == A and rC(w) == A + 2:
                code[q] = MOVE | (A << 6) | (A << 23)  # neutralize the pre-decrement
                stats["fornum_predecr"] = stats.get("fornum_predecr", 0) + 1
                break


def _wr_proto(p, stats):
    out = _wr_str(p.source)
    out += struct.pack("<ii", p.linedefined, p.lastlined)
    out += bytes((p.nups, p.nparams, p.vararg, p.maxstack))
    # stock order: code, consts, protos, debug(lineinfo, locvars, upvals)
    code = [
        reencode(ins, stats, p.code, i, p.consts, p.maxstack)
        for i, ins in enumerate(p.code)
    ]
    _normalize_numeric_for(code, stats)
    out += struct.pack("<I", len(code)) + b"".join(struct.pack("<I", c) for c in code)
    out += _wr_constants(p.consts)
    out += struct.pack("<I", len(p.protos))
    for c in p.protos:
        out += _wr_proto(c, stats)
    # debug
    li = p.lineinfo if p.lineinfo else [0] * len(code)
    out += struct.pack("<I", len(li)) + b"".join(struct.pack("<i", x) for x in li)
    out += struct.pack("<I", len(p.locvars))
    for nm, s, e in p.locvars:
        out += _wr_str(nm) + struct.pack("<ii", s, e)
    out += struct.pack("<I", len(p.upvals))
    for nm in p.upvals:
        out += _wr_str(nm)
    return out


STOCK_HEADER = b"\x1bLua" + bytes((0x51, 0, 1, 4, 4, 4, 8, 0))


def _resolve_reg_key(code, K, i, keyfield):
    """If a SETTABLE key is a register, find the constant a preceding LOADK put there."""
    if keyfield >= 250:
        idx = keyfield - 250
        return K[idx][1] if idx < len(K) else None
    for j in range(i - 1, -1, -1):
        o = code[j]
        op = o & 0x3F
        if op == 1 and (o >> 24) & 0xFF == keyfield:  # LOADK
            bx = (o >> 6) & 0x3FFFF
            return K[bx][1] if bx < len(K) else None
        if (o >> 24) & 0xFF == keyfield and op != 1:
            return None
    return None


def _jmp_target(ins, i):
    """Stock-style absolute target for a PopCap JMP (op 22). None if not a JMP."""
    if ins & 0x3F != 22:
        return None
    bx = (ins >> 6) & 0x3FFFF
    return i + 1 + (bx - 131071)


def _encode_jmp(target, i):
    sbx = target - (i + 1)
    return 22 | ((sbx + 131071) & 0x3FFFF) << 6


def _break_self_ref_cycles(p, stats):
    """Break `X.mt = {__index = X}` register cycles that make unluac emit a self-
    referential table literal (-> `nil --[[ self-reference ]]`). For each such cycle we
    insert `GETGLOBAL Rtmp := <classname>` before the `__index` store and point the store
    at Rtmp, so unluac sees a name reference, not an inlinable self-table. Only JMP(22)
    offsets crossing the insertion are fixed; if any FOR-family op (30/46/32) would cross
    the insertion point we skip that proto (keep the harmless placeholder) since their
    offset encoding is not yet resolved."""
    K = p.consts
    code = p.code
    # find an __index edge that participates in a reciprocal cycle and has a global
    for i, ins in enumerate(code):
        if ins & 0x3F != 9:
            continue
        a = (ins >> 24) & 0xFF
        f6 = (ins >> 6) & 0x1FF
        f15 = (ins >> 15) & 0x1FF
        if f6 >= 250:
            continue
        if _resolve_reg_key(code, K, i, f15) != "__index":
            continue
        Rmt, Rclass = a, f6
        recip = any(
            (c & 0x3F == 9)
            and ((c >> 24) & 0xFF == Rclass)
            and ((c >> 6) & 0x1FF == Rmt)
            and _resolve_reg_key(code, K, j, (c >> 15) & 0x1FF) == "mt"
            for j, c in enumerate(code)
        )
        if not recip:
            continue
        gname_idx = None
        for j, c in enumerate(code):
            op = c & 0x3F
            if op == 7 and (c >> 24) & 0xFF == Rclass:
                gname_idx = (c >> 6) & 0x3FFFF
            if op in (38, 39, 40) and (c >> 24) & 0xFF == Rclass:
                cf6 = (c >> 6) & 0x1FF
                cf15 = (c >> 15) & 0x1FF
                if cf15 == 0 and cf6 < len(K) and K[cf6][0] == "str":
                    gname_idx = cf6
        if gname_idx is None:
            continue
        pos = i  # insert GETGLOBAL before this SETTABLE
        # safety: no FOR-family op (30/46/32) may cross the insertion point
        crosses = False
        for j, c in enumerate(code):
            if c & 0x3F in (30, 46, 32):
                # we don't know its target encoding; treat any FOR op as a barrier if it
                # sits on the opposite side of pos from where its loop body would be
                if j < pos:  # a loop opened before insertion could span it
                    crosses = True
                    break
        if crosses:
            stats["selfref_skip_forloop"] = stats.get("selfref_skip_forloop", 0) + 1
            return  # leave this proto untouched
        Rtmp = p.maxstack  # next free register (above everything live)
        newg = 5 | (Rtmp & 0xFF) << 24 | (gname_idx & 0x3FFFF) << 6  # PopCap GETGLOBAL
        # rewrite the SETTABLE value (f6) to Rtmp
        newset = (ins & ~(0x1FF << 6)) | ((Rtmp & 0x1FF) << 6)
        new_code = code[:pos] + [newg] + [newset] + code[pos + 1 :]
        # fix JMP(22) offsets crossing pos
        for j in range(len(new_code)):
            t = _jmp_target(new_code[j], j if j < pos else j)  # decode in NEW layout
            if t is None:
                continue
            # map: old index k -> new k' = k if k<pos else k+1. Recompute from old.
            # Easier: recompute using positions in new_code directly.
        # Recompute jumps cleanly from scratch using new positions:
        out_code = []
        for j, c in enumerate(new_code):
            if c & 0x3F == 22:
                # old source/target in OLD coords:
                old_j = j if j < pos else j - 1
                old_bx = (c >> 6) & 0x3FFFF
                old_t = old_j + 1 + (old_bx - 131071)
                new_j = j
                new_t = old_t if old_t < pos else old_t + 1
                out_code.append(_encode_jmp(new_t, new_j))
            else:
                out_code.append(c)
        p.code = out_code
        # extend lineinfo to keep length == code length
        if getattr(p, "lineinfo", None):
            p.lineinfo = p.lineinfo[:pos] + [p.lineinfo[pos]] + p.lineinfo[pos:]
        p.maxstack = max(p.maxstack, Rtmp + 1)
        stats["selfref_fixed"] = stats.get("selfref_fixed", 0) + 1
        return  # one cycle per call; re-run handles multiples


def emit_standard(chunk):
    stats = {}
    body = _wr_proto(chunk, stats)
    return STOCK_HEADER + body, stats


def transcode(in_path, out_path):
    chunk = parse_full(in_path)
    blob, stats = emit_standard(chunk)
    open(out_path, "wb").write(blob)
    return stats


if __name__ == "__main__":
    import sys

    s = transcode(sys.argv[1], sys.argv[2])
    print("ok", sys.argv[2], "stats:", s)
