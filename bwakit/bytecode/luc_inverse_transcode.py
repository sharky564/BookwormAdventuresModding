"""A1 inverse transcoder: serialize a parsed chunk back to PopCap 0x56 .luc bytecode.

emit_popcap(FProto from luc_transcode.parse_full) reproduces the original .luc byte-for-byte --
the foundation of the modding round-trip (parse_full -> FProto -> emit_popcap must be identical).

Built against parse_full (lossless: keeps every PopCap code word verbatim and the int32 (type-4)
constant tag distinct from f64 (type-3)) -- NOT by reversing the forward transcoder, whose
PopCap->stock direction is deliberately lossy (SETLIST batch counts, overloaded opcodes, RK remaps)
and not invertible. Preserving type-4 matters: the VM has a real integer type.

Recompiling edits: edit .lua -> stock luac -> stock_to_popcap() -> PopCap .luc. stock_to_popcap
renumbers opcodes, repacks operand bit-fields, reorders proto fields debug-first, and (given the
original as --ref) re-promotes integer f64 consts back to type-4. The VM runs the stock opcode
subset natively, so PopCap's extra opcodes are never synthesised; stock-only constructs are lowered
here (numeric-for -> lower_numeric_for; generic-for -> _convert_genfor_entries; large array literals
need luac built with LFIELDS_PER_FLUSH=32). VARARG is unsupported and rejected with guidance.

Usage: python3 luc_inverse_transcode.py --roundtrip DIR|FILE.luc   # identity check
"""

import struct
import difflib
from bwakit.bytecode import luc_transcode as T  # reuse parse_full
from bwakit.bytecode.luc_transcode import FProto, POPCAP_OPS, STOCK, POPCAP_IABX

# The 23-byte PopCap header is invariant across the whole corpus:
#   \x1bLua | 0x56 | flags(1,1,4,4,4,6,8,9,9,8) | <d> 31415926.535897933  (pi*1e7 LUAC_NUM)
# parse_full() begins the root proto at offset 23, so we reproduce these 23 bytes verbatim.
POPCAP_HEADER = bytes(
    [0x1B, 0x4C, 0x75, 0x61, 0x56, 1, 1, 4, 4, 4, 6, 8, 9, 9, 8]
) + struct.pack("<d", 31415926.535897933)
assert len(POPCAP_HEADER) == 23


# serialize PopCap (exact inverse of luc_transcode._rd_*)


def _wr_str(raw):
    """raw is bytes-including-trailing-NUL, or None for 'no string' (size 0)."""
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
        elif t == "int":  # PopCap int32 constant -- PRESERVED, not collapsed
            out += b"\x04" + struct.pack("<i", int(v))
        elif t == "str":
            out += b"\x05" + _wr_str(v)
        else:
            raise ValueError(f"bad const tag {t!r}")
    return out


def _wr_proto(p):
    out = _wr_str(p.source)
    out += struct.pack("<ii", p.linedefined, p.lastlined)
    out += bytes((p.nups, p.nparams, p.vararg, p.maxstack))
    # PopCap order is DEBUG-FIRST: lineinfo, locvars, upvals, consts, protos, code.
    out += struct.pack("<I", len(p.lineinfo)) + b"".join(
        struct.pack("<i", x) for x in p.lineinfo
    )
    out += struct.pack("<I", len(p.locvars))
    for nm, s, e in p.locvars:
        out += _wr_str(nm) + struct.pack("<ii", s, e)
    out += struct.pack("<I", len(p.upvals))
    for nm in p.upvals:
        out += _wr_str(nm)
    out += _wr_constants(p.consts)
    out += struct.pack("<I", len(p.protos))
    for c in p.protos:
        out += _wr_proto(c)
    out += struct.pack("<I", len(p.code)) + b"".join(
        struct.pack("<I", c & 0xFFFFFFFF) for c in p.code
    )
    return out


def emit_popcap(chunk):
    """Serialize an FProto (root proto) to a complete PopCap .luc byte string. Exact inverse
    of luc_transcode.parse_full: code words and constant tags pass through verbatim."""
    return POPCAP_HEADER + _wr_proto(chunk)


# stock Lua 5.1 -> PopCap (for recompiled edits)
#
# Inverts the three MECHANICAL transforms of the forward transcoder (opcode renumber,
# operand bit-field repack, prototype field reorder), PLUS the numeric for-loop lowering
# (FORPREP/FORLOOP -> SUB+JMP+FORLOOP with a body-top MOVE; see lower_numeric_for above).
# It does NOT recreate PopCap's overloaded/extra opcodes -- stock luac never emits those, and
# the game VM (opcode space 0..48, verified) runs the stock subset natively. The opcode map
# below was read directly from the VM execute loop. VARARG remains unmapped (its PopCap number
# could not be confirmed from the VM and it is used only 7x in the whole corpus); we raise on
# it rather than emit a guessed opcode.

from bwakit.bytecode import lua51_stock as S

# numeric for-loop lowering: stock FORPREP/FORLOOP -> PopCap form
#
# PopCap's numeric for-loop differs from stock Lua 5.1 in TWO ways, both verified from the VM
# (FORLOOP handler at case 0x1e, FUN_004e0c80):
#   1. There is NO FORPREP opcode. PopCap lowers the pre-loop step-subtraction into an explicit
#      `SUB R(A), R(A), R(A+2)` followed by a plain `JMP` to the FORLOOP. (Seen in BattleEngine:
#      SUB A=2 B=2 C=4 ; JMP -> FORLOOP.)
#   2. PopCap's FORLOOP uses a 3-REGISTER layout: it updates only R(A) (the index) and does NOT
#      copy it to R(A+3). The loop body reads the index from R(A) directly. Stock's FORLOOP, by
#      contrast, copies R(A)->R(A+3) each iteration and the stock-compiled body reads R(A+3).
#
# So a stock numeric for-loop cannot be transcoded by opcode-renumbering alone. We rewrite each
# stock FORPREP/FORLOOP pair into PopCap form AND inject `MOVE R(A+3) := R(A)` as the first body
# instruction, so the stock body (which reads R(A+3)) gets the right value every iteration under
# PopCap's 3-register FORLOOP. This is done on the STOCK instruction stream before opcode
# conversion, so SUB/JMP/MOVE/FORLOOP all go through the normal per-instruction mapper. All
# inserted instructions shift positions, so every pc-relative offset (JMP/FORLOOP) is recomputed.

_ST_JMP, _ST_FORLOOP, _ST_FORPREP, _ST_SUB, _ST_MOVE = 22, 31, 32, 13, 0


def _st_sbx(ins):
    return ((ins >> 14) & 0x3FFFF) - 131071


def _enc_iAsBx(op, A, sbx):
    return op | ((A & 0xFF) << 6) | (((sbx + 131071) & 0x3FFFF) << 14)


def _enc_sub(A, B, C):
    return _ST_SUB | ((A & 0xFF) << 6) | ((C & 0x1FF) << 14) | ((B & 0x1FF) << 23)


def _enc_move(A, B):
    return _ST_MOVE | ((A & 0xFF) << 6) | ((B & 0x1FF) << 23)


def lower_numeric_for(code, lineinfo):
    """Rewrite stock FORPREP/FORLOOP loops into PopCap-compatible stock instructions
    (FORPREP -> SUB+JMP; inject MOVE R(A+3):=R(A) at each loop-body start) and fix all jump
    offsets. Returns (new_code, new_lineinfo). Raises if a FORPREP's matching FORLOOP cannot
    be located, so we never emit a malformed loop."""
    n = len(code)
    forprep_target = {}  # old index of FORPREP -> old index of its FORLOOP
    body_start = {}  # old body-first index -> A (where to inject MOVE R(A+3):=R(A))
    for i, ins in enumerate(code):
        op = ins & 0x3F
        if op == _ST_FORPREP:
            tgt = i + 1 + _st_sbx(ins)
            if not (0 <= tgt < n and (code[tgt] & 0x3F) == _ST_FORLOOP):
                raise ValueError(
                    f"FORPREP@{i} target {tgt} is not a FORLOOP; cannot lower"
                )
            forprep_target[i] = tgt
        elif op == _ST_FORLOOP:
            bstart = i + 1 + _st_sbx(ins)
            if 0 <= bstart < n:
                body_start[bstart] = (ins >> 6) & 0xFF
    if not forprep_target:
        return list(code), list(lineinfo)

    # old index -> new start index (+1 per FORPREP for SUB+JMP, +1 per injected body MOVE)
    new_start = [0] * (n + 1)
    pos = 0
    for k in range(n):
        if k in body_start:
            pos += 1  # MOVE injected before this instruction
        new_start[k] = pos
        pos += 2 if (code[k] & 0x3F) == _ST_FORPREP else 1
    new_start[n] = pos

    out, out_li = [], []
    li_of = lambda k: lineinfo[k] if k < len(lineinfo) else 0
    for k in range(n):
        if k in body_start:
            A = body_start[k]
            out.append(_enc_move(A + 3, A))
            out_li.append(li_of(k))
        ins = code[k]
        op = ins & 0x3F
        A = (ins >> 6) & 0xFF
        if op == _ST_FORPREP:
            tgt = forprep_target[k]
            out.append(_enc_sub(A, A, A + 2))
            out_li.append(li_of(k))
            jmp_pos = new_start[k] + 1
            out.append(_enc_iAsBx(_ST_JMP, 0, new_start[tgt] - (jmp_pos + 1)))
            out_li.append(li_of(k))
        elif op in (_ST_JMP, _ST_FORLOOP):
            tgt = k + 1 + _st_sbx(ins)
            # A FORLOOP jumps back to its body start. We injected a MOVE R(A+3):=R(A) just
            # BEFORE that body-start instruction, and the loop variable copy must run every
            # iteration, so the FORLOOP must land ON the injected MOVE (one slot earlier than
            # new_start[body]). Any other jump (incl. a JMP into a loop body) lands after the
            # MOVE, on the body instruction itself, which is also correct for loop ENTRY.
            if op == _ST_FORLOOP and tgt in body_start:
                dest = new_start[tgt] - 1
            else:
                dest = new_start[tgt]
            out.append(_enc_iAsBx(op, A, dest - (new_start[k] + 1)))
            out_li.append(li_of(k))
        else:
            out.append(ins)
            out_li.append(li_of(k))
    return out, out_li


# stock opcode number -> PopCap opcode number. VERIFIED against the VM execute loop
# (FUN_004e0c80, jump table switchD_004e0d11) -- every entry below was read from its handler:
#   0..25 identical (MOVE..LE).
#   26 TEST  -> 26  and  27 TESTSET -> 26 : PopCap has ONE unified test op (handler 0x1a
#        always copies R(B)->R(A); stock TEST is that op with B==A, a no-op self-copy).
#   28 CALL     -> 27   (handler 0x1b)
#   29 TAILCALL -> 44   (handler 0x2c/0x2d: the tail-frame-collapse branch of the CALL handler)
#   30 RETURN   -> 29   (handler 0x1d)
#   31 FORLOOP  -> 30   (handler 0x1e: index+=step, compare to limit, jump back)
#   32 FORPREP  -> *** NO PopCap opcode ***  PopCap lowers FORPREP to a plain JMP (op 22);
#        all 444 shipped for-loops enter via JMP. Handled specially in stock_ins_to_popcap.
#   33 TFORLOOP -> 32   (handler 0x20: generic-for iterator call)
#   34 SETLIST  -> 31   (handler 0x1f)
#   35 CLOSE    -> 35   (handler 0x23: calls luaF_close == FUN_004e2de0). CONFIRMED CLOSE,
#        not VARARG: all 7 corpus uses of PopCap op35 sit in NON-vararg protos with the
#        A/B=0/C=0 shape of CLOSE (close upvalues >= R(A)); the disasm's "VARARG@35" is a
#        mislabel. So this 35->35 mapping is correct and closures compile fine.
#   36 CLOSURE  -> 48   (handler 0x30: luaF_newLclosure + upvalue capture loop)
#   37 VARARG   -> ???  The shipped game NEVER emits a real VARARG instruction: constructors
#        like `function X:new(...)` only DECLARE varargs (the proto flag) and never reference
#        `...` in the body, so nothing is produced. With no corpus example its PopCap number
#        is unknown; genuine `...` *use* is rejected in _UNMAPPABLE rather than guessed.
# Inline-cache opcode equivalence groups
# PopCap's VM keeps a per-instruction inline cache (Proto+0x50, 8 bytes/instruction, keyed
# by PC) for global access, table access, method dispatch (SELF) and calls. For each of
# these it has a "base" opcode and one or more "cache-variant" opcodes; the loader wires the
# cache up per-PC according to the *exact* opcode present, so the choice is load-bearing -
# emitting the base opcode where PopCap used a variant (or vice versa) reads a mis-initialised
# cache slot and crashes at runtime. We can't reconstruct PopCap's compiler heuristic for the
# choice, but for any instruction we did NOT change the operands are identical to the
# original, so we adopt the original's opcode verbatim (see _sproto_to_fproto). These groups
# list the interchangeable (base, variant...) opcode numbers per operation.
_EQUIV_GROUPS = [
    {5, 42},  # GETGLOBAL  : base 5,  variant 42
    {
        6,
        36,
    },  # GETTABLE   : base 6,  variant 36   (note: 36 also appears as SELF~ below)
    {11, 36, 43},  # SELF       : base 11, variants 36/43
    {27, 28, 44, 45},  # CALL       : base 27, variants 28/44/45
    {7, 39, 41},  # SETGLOBAL  : base 7,  variants 39/41
]


# For genuinely new/changed instructions there is no original opcode to inherit, so we must
# emit what PopCap's compiler would. Derived from a corpus-wide survey of base-vs-variant use:
#   * GETGLOBAL : main chunk is ALWAYS base; nested protos use the cache variant (op42).
#   * SETGLOBAL : almost always the variant (op39); plain base op7 is vanishingly rare (38x).
#   * SELF      : op43 is used ONLY for self-method calls (object register B==0); op36 only for
#                 B!=0. Base op11 covers the rest. We promote to op43 only when B==0, else keep
#                 base (emitting op43 with B!=0 is something PopCap never does and it crashes).
#   * CALL/GETTABLE: predominantly base; we leave them base.
def _promote_new_op(ins, is_main):
    op = ins & 0x3F
    if op == 5 and not is_main:  # GETGLOBAL in a nested proto -> cache variant
        return (ins & ~0x3F) | 42
    if op == 7:  # SETGLOBAL -> cache variant
        return (ins & ~0x3F) | 39
    if op == 11 and ((ins >> 15) & 0x1FF) == 0:  # SELF on self (B==0) -> cache variant
        return (ins & ~0x3F) | 43
    return ins


def _canon_op(op):
    """Collapse base/variant opcode numbers to a single representative so an unchanged
    instruction matches its original regardless of which encoding each side used."""
    for g in _EQUIV_GROUPS:
        if op in g:
            return min(g)
    return op


def _adopt_ref_opcodes(my_code, ref_code, is_main):
    """Reconcile freshly-generated PopCap code against the original file's code.

    Unchanged instructions (same opcode-class and identical operands) take the original's
    exact instruction word, preserving PopCap's per-PC base-vs-variant choice. New or changed
    instructions keep our operands but have their opcode set by _promote_new_op (the
    corpus-derived rule for what PopCap's compiler would emit). Alignment is by operand
    sequence (difflib), so it survives insertions/deletions that shift later instructions.
    """
    result = list(my_code)
    if ref_code:
        key = lambda i: (_canon_op(i & 0x3F), i >> 6)  # (opcode-class, operands)
        sm = difflib.SequenceMatcher(
            a=[key(i) for i in ref_code], b=[key(i) for i in my_code], autojunk=False
        )
        adopted = [False] * len(my_code)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for off in range(i2 - i1):
                    result[j1 + off] = ref_code[i1 + off]  # original word verbatim
                    adopted[j1 + off] = True
    else:
        adopted = [False] * len(my_code)
    for k in range(len(result)):
        if not adopted[k]:
            result[k] = _promote_new_op(result[k], is_main)
    # Pairing pass: a method call's CALL must share its SELF's base/variant-ness (corpus:
    # op11->op27 and op43->op44, 100% with zero exceptions). SELF~ (op43) puts the method in
    # R(A); the CALL that invokes R(A) must therefore be CALL~ (op44). We promote only CALLs we
    # generated (adopted ones already carry the original's correct opcode). This covers a
    # self:method() call that an edit shifted off alignment, where the SELF~ is right but its
    # CALL would otherwise default to base.
    self_variant_regs = set()
    for k in range(len(result)):
        ins = result[k]
        op = ins & 0x3F
        A = (ins >> 24) & 0xFF
        if op == 43:  # SELF~ -> method now lives in R(A)
            self_variant_regs.add(A)
        elif op in (27, 44):  # a CALL of R(A) consumes the pairing
            if A in self_variant_regs:
                self_variant_regs.discard(A)
                if op == 27 and not adopted[k]:
                    result[k] = (
                        ins & ~0x3F
                    ) | 44  # base CALL -> CALL~ to match the SELF~
    return result


# PopCap's generic-for ('for k,v in expr do ... end') uses the SAME register layout and the
# same body / TFORLOOP / back-edge shape as stock Lua 5.1, with two encoding differences:
#   - the iterator call at the loop bottom is op31 (stock TFORLOOP is op33; remapped in
#     _STOCK_TO_POPCAP_OP), and
#   - the loop is ENTERED through a dedicated op32 instruction that carries the control-register
#     base A in its A field and jumps to the TFORLOOP, where stock uses a plain JMP (op22, A=0).
# stock_ins_to_popcap already converts the entry JMP into a PopCap JMP; this pass finds each such
# entry JMP (the one sitting just before a generic-for body and targeting its TFORLOOP) and
# rewrites it to op32 with A copied from the TFORLOOP. No instruction is inserted or moved, so
# every jump offset stays valid -- only the entry word's opcode and A field change. Verified
# against the corpus frame (e.g. common.luc m.12: MOVE R2; LOADNIL; op32 A=2 ->TFORLOOP;
# body; op31 TFORLOOP A=2 C=1; JMP back-to-body).
_POP_JMP, _POP_TFORLOOP, _POP_GENFOR_ENTRY = 22, 31, 32


def _pop_sbx(w):
    return ((w >> 6) & 0x3FFFF) - 131071


def _convert_genfor_entries(code):
    n = len(code)
    out = list(code)
    for t, w in enumerate(code):
        if (w & 0x3F) != _POP_TFORLOOP:
            continue
        # back-edge: the JMP right after the TFORLOOP jumps back to the loop body start
        if t + 1 >= n or (code[t + 1] & 0x3F) != _POP_JMP:
            continue
        body = (t + 1) + 1 + _pop_sbx(code[t + 1])
        entry = body - 1  # the instruction just before the body
        if not (0 <= entry < n) or (code[entry] & 0x3F) != _POP_JMP:
            continue
        if entry + 1 + _pop_sbx(code[entry]) != t:  # entry JMP must target the TFORLOOP
            continue
        A = (w >> 24) & 0xFF  # TFORLOOP control-register base
        # keep bits 6..23 (the signed-Bx jump field); replace opcode (0..5) and A (24..31)
        out[entry] = (code[entry] & 0x00FFFFC0) | _POP_GENFOR_ENTRY | (A << 24)
    return out


# generic-for register layout: Lua 5.0 (PopCap) vs Lua 5.1 (stock luac)
# In a generic-for, PopCap uses the classic Lua 5.0 frame where the control variable IS the first
# loop variable: the iterator's results land at R(A+2), R(A+3), ...  Stock Lua 5.1 keeps a separate
# control slot at R(A+2), so its loop variables sit one register higher: R(A+3), R(A+4), ...
# (Confirmed in the shipped corpus, e.g. Gravestone `for k,p in pairs(mPotions) do p.x=.. end`
# accesses p -- the SECOND variable -- at R(A+3), not R(A+4).)  After the opcodes are mapped the
# loop body still reads the variables at the 5.1 positions, so every body reference to a loop
# variable must be shifted down by one register to read where PopCap's TFORLOOP actually wrote it.

# Which operand fields hold register indices (so a loop-variable reference could appear there).
# Keyed by PopCap opcode. A is a register for every iABC opcode except the comparisons EQ/LT/LE.
_POP_B_IS_REG = {
    0,
    3,
    6,
    11,
    18,
    19,
    20,
    21,
    26,
    43,
}  # MOVE LOADNIL GETTABLE(~) SELF UNM NOT LEN CONCAT TEST(SET)
_POP_B_IS_RK = {9, 12, 13, 14, 15, 16, 17, 23, 24, 25}  # SETTABLE arithmetic EQ LT LE
_POP_C_IS_REG = {21}  # CONCAT (C is the end register)
_POP_C_IS_RK = {6, 9, 11, 12, 13, 14, 15, 16, 17, 23, 24, 25, 43}
_POP_A_NOT_REG = {22, 23, 24, 25}  # JMP (unused) + comparisons (A is a test flag)


def _remap_loopvar_regs(iw, lo, hi):
    """Shift any register operand in [lo, hi] down by one, in a single PopCap instruction."""
    op = iw & 0x3F
    if op in (_POP_TFORLOOP, _POP_GENFOR_ENTRY):
        return iw  # loop-control ops carry a jump/count, never touch

    def sh(v):  # remap a plain register index
        return v - 1 if lo <= v <= hi else v

    def shrk(v):  # remap an RK field only when it is a register
        return (v - 1) if (v < 256 and lo <= v <= hi) else v

    if op in POPCAP_IABX:  # iABx / iAsBx: only A is a register
        if op in _POP_A_NOT_REG:
            return iw
        A = (iw >> 24) & 0xFF
        return (iw & 0x00FFFFFF) | ((sh(A) & 0xFF) << 24)
    A = (iw >> 24) & 0xFF
    B = (iw >> 15) & 0x1FF
    C = (iw >> 6) & 0x1FF
    if op not in _POP_A_NOT_REG:
        A = sh(A)
    if op in _POP_B_IS_REG:
        B = sh(B)
    elif op in _POP_B_IS_RK:
        B = shrk(B)
    if op in _POP_C_IS_REG:
        C = sh(C)
    elif op in _POP_C_IS_RK:
        C = shrk(C)
    return (op & 0x3F) | ((C & 0x1FF) << 6) | ((B & 0x1FF) << 15) | ((A & 0xFF) << 24)


def _shift_genfor_body_vars(code):
    """Down-shift loop-variable references in every generic-for body (5.1 -> 5.0 frame). Run AFTER
    _convert_genfor_entries so the entry op32 markers are in place."""
    n = len(code)
    out = list(code)
    for t, w in enumerate(code):
        if (w & 0x3F) != _POP_TFORLOOP:
            continue
        if t + 1 >= n or (out[t + 1] & 0x3F) != _POP_JMP:
            continue
        body_start = (t + 1) + 1 + _pop_sbx(out[t + 1])
        entry = body_start - 1
        if not (0 <= entry < n) or (out[entry] & 0x3F) != _POP_GENFOR_ENTRY:
            continue
        A = (w >> 24) & 0xFF
        nvars = ((w >> 6) & 0x1FF) + 1  # PopCap TFORLOOP C == nvars - 1
        lo, hi = A + 3, A + 2 + nvars  # the stock-5.1 loop-variable registers
        for pc in range(body_start, t):  # body = [body_start, t-1]
            out[pc] = _remap_loopvar_regs(out[pc], lo, hi)
    return out


_STOCK_TO_POPCAP_OP = {
    0: 0,
    1: 1,
    2: 2,
    3: 3,
    4: 4,
    5: 5,
    6: 6,
    7: 7,
    8: 8,
    9: 9,
    10: 10,
    11: 11,
    12: 12,
    13: 13,
    14: 14,
    15: 15,
    16: 17,  # MOD: PopCap's op17 is MOD (its op16 is unused); stock MOD(16) -> PopCap 17
    # 17 POW: PopCap op17 is MOD, so there is no plain POW opcode -> raised in _UNMAPPABLE
    18: 18,
    19: 19,
    20: 20,
    21: 21,
    22: 22,
    23: 23,
    24: 24,
    25: 25,
    26: 26,  # TEST     (encode with B := A)
    27: 26,  # TESTSET  (B carries the source register)
    28: 27,  # CALL
    29: 44,  # TAILCALL
    30: 29,  # RETURN
    31: 30,  # FORLOOP
    # 32 FORPREP handled specially (-> SUB+JMP via lower_numeric_for); not in this table
    33: 31,  # TFORLOOP (generic-for iterator call) -> PopCap op31. The loop's ENTRY JMP is
    # additionally rewritten to op32 by _convert_genfor_entries (a post-pass). Registers
    # are unchanged: both layouts use 3 control regs R(A)..R(A+2) and vars at R(A+3)+.
    # 34 SETLIST: handled specially in stock_ins_to_popcap (PopCap op 0x21/0x22 re-encoding)
    35: 35,  # CLOSE
    36: 48,  # CLOSURE
    # 37 VARARG deliberately omitted until its PopCap number is VM-confirmed
}
# Opcodes that need bespoke handling rather than a 1:1 number swap:
_FORPREP_STOCK = 32
_VARARG_STOCK = 37
_STOCK_OP_NAME = {v: k for k, v in enumerate(S.STOCK_NAMES)}  # name->num (unused dir)
# FORPREP (32) is no longer unmappable: lower_numeric_for() rewrites the whole FORPREP/FORLOOP
# loop into PopCap form (SUB+JMP + body-top MOVE) before per-instruction conversion. The only
# remaining unmapped opcode is VARARG (37): its PopCap number could not be confirmed from the
# VM and it appears just 7x in the whole corpus, so we raise rather than emit a guessed opcode.
_UNMAPPABLE = {
    17: "POW ('^'): PopCap's op17 is MOD, so there is no plain power opcode; rewrite the "
    "source to avoid '^' (e.g. call a helper) before recompiling",
    _VARARG_STOCK: "genuine '...' use inside a function body. PopCap op35 is CLOSE (all 7 "
    "corpus uses are in non-vararg protos), and the shipped game never emits a real VARARG, "
    "so its PopCap opcode number is unknown. Declaring varargs is fine (e.g. "
    "'function X:new(...)'); only *referencing* them -- f(...), {...}, select('#', ...), "
    "'local a = ...' -- hits this. Rewrite to explicit parameters before recompiling.",
}

# stock iABx opcodes (by stock number): LOADK, GETGLOBAL, SETGLOBAL, CLOSURE
_STOCK_IABX_NUM = {1, 5, 7, 36}
# stock iAsBx (signed Bx): JMP, FORLOOP, FORPREP
_STOCK_IASBX_NUM = {22, 31, 32}


def _rk_stock_to_popcap(field):
    """Stock marks a constant operand with bit 8 (0x100 | idx). PopCap marks it as 250+idx.
    Registers (no bit 8 set) pass through unchanged."""
    if field & 0x100:
        return 250 + (field & 0xFF)
    return field


def stock_ins_to_popcap(ins):
    """Re-encode one stock Lua 5.1 instruction word into a PopCap instruction word."""
    sop = ins & 0x3F
    if sop in _UNMAPPABLE:
        raise ValueError(
            f"stock opcode {S.STOCK_NAMES[sop]} ({sop}): {_UNMAPPABLE[sop]}"
        )
    # SETLIST (stock op 34): PopCap split it into two opcodes with a different operand
    # layout, so it cannot go through the generic iABC path. PopCap op 0x21 carries the
    # element count inline (count-1 in the low 5 bits of the C field, array page in the
    # upper bits); op 0x22 takes the count from the stack top (stock's B==0 form). NOTE:
    # the naive map used to send stock SETLIST to PopCap op 0x1f, which is actually
    # TFORLOOP -- the engine then tried to call the table as a generic-for iterator and
    # read the next instruction as a jump, desyncing the instruction stream (a hang). We
    # support a single batch of up to 32 array elements (page 0), covering all normal
    # table literals; larger / multi-batch literals need register re-staging (PopCap
    # flushes every 32 elements vs stock's 50) and are rejected rather than mis-encoded.
    if sop == 34:
        A = (ins >> 6) & 0xFF
        B = (ins >> 23) & 0x1FF  # element count this batch (0 => up to stack top)
        C = (ins >> 14) & 0x1FF  # batch number, 1-based (0 => count in next ins)
        if C == 0:
            raise ValueError(
                "SETLIST with its batch count in the following instruction "
                "(array literal with tens of thousands of elements) unsupported"
            )
        page = C - 1  # PopCap array pages are 0-based
        if B == 0:  # `{ f() }` / `{ ... }`: count taken from the stack top
            return 0x22 | ((A & 0xFF) << 24) | ((page & 0xF) << 11)
        if B > 32:
            raise ValueError(
                f"SETLIST batch of {B} elements exceeds PopCap's 32-per-flush window. This "
                "means luac was built with the stock LFIELDS_PER_FLUSH=50; rebuild it with "
                "LFIELDS_PER_FLUSH=32 (see tools/build_luac.sh) so table-constructor batching "
                "matches PopCap, then recompile."
            )
        # PopCap op 0x21: the 9-bit C field (bits 6..14) packs (page << 5) | (count-1).
        # Verified against LetterRipData.luc: page0 -> C=31 (cnt32), page1 -> C=63 (cnt32).
        cfield = (((page & 0xF) << 5) | ((B - 1) & 0x1F)) & 0x1FF
        return 0x21 | ((A & 0xFF) << 24) | (cfield << 6)
    if sop == 33:
        # TFORLOOP. PopCap's VM uses Lua 5.0 generic-for semantics: the iterator is called
        # for C+1 results, so PopCap's C operand is (number-of-loop-vars - 1). Stock Lua 5.1
        # puts the var count itself in C. Decrement it. (Verified against the corpus: 367/370
        # shipped loops have C=1 == two vars k,v, and 3 have C=0 == one var.) B is unused.
        A = (ins >> 6) & 0xFF
        nvars = (ins >> 14) & 0x1FF
        cpop = (nvars - 1) & 0x1FF
        return 31 | (cpop << 6) | ((A & 0xFF) << 24)
    if sop not in _STOCK_TO_POPCAP_OP:
        raise ValueError(f"unknown/unhandled stock opcode {sop}")
    pop = _STOCK_TO_POPCAP_OP[sop]
    A = (ins >> 6) & 0xFF
    if sop in _STOCK_IABX_NUM or sop in _STOCK_IASBX_NUM:
        # iABx/iAsBx: stock Bx at bits 14-31; PopCap places Bx at bits 6-23.
        Bx = (ins >> 14) & 0x3FFFF
        return pop | ((Bx & 0x3FFFF) << 6) | ((A & 0xFF) << 24)
    # iABC fields
    C = _rk_stock_to_popcap((ins >> 14) & 0x1FF)
    B = _rk_stock_to_popcap((ins >> 23) & 0x1FF)
    if sop == 26:
        # stock TEST has no B (operands A, C); PopCap's unified test op always reads R(B)
        # and copies it to R(A). Encode B := A so a plain TEST becomes a no-op self-copy.
        B = A & 0x1FF
    # iABC: stock op|A<<6|C<<14|B<<23  ->  PopCap op|C<<6|B<<15|A<<24
    return pop | ((C & 0x1FF) << 6) | ((B & 0x1FF) << 15) | ((A & 0xFF) << 24)


def _promote_consts(stock_ks, ref_ks=None):
    """Convert a stock constant list (SProto form) into PopCap form.

    Stock luac has no int32 constant type -- every numeric literal becomes an 8-byte f64
    (type 3) -- whereas PopCap's compiler emits an int32 (type 4) for integer-valued literals
    and an f64 only for fractional ones. We replicate that by VALUE: any f64 whose value is an
    exact 32-bit integer is re-promoted to int32, matching how PopCap would have compiled the
    same source. This is order-independent (no positional ref matching needed).

    The engine treats int32 and f64 of equal value identically (the value-equality operator
    does cross-type numeric comparison, and numeric table keys normalise the same way), so the
    choice is behaviourally safe; promoting simply reproduces the original representation for
    the common case. A reference list, when supplied, can still PIN exact types for the rare
    files where PopCap stored an integer value as f64 (240 such constants exist corpus-wide):
    if ref says f64 for a value we'd otherwise promote, we honour the ref and keep it f64.
    """
    INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
    out = []
    for i, (t, v) in enumerate(stock_ks):
        if t == "num":
            is_int_val = (
                (v == v) and float(v).is_integer() and INT32_MIN <= v <= INT32_MAX
            )
            # ref override: if the original explicitly stored this position as f64, keep f64
            ref_says_f64 = (
                ref_ks is not None
                and i < len(ref_ks)
                and ref_ks[i][0] == "num"
                and ref_ks[i][1] == v
            )
            if is_int_val and not ref_says_f64:
                out.append(("int", int(v)))
            else:
                out.append(("num", v))
        elif t == "str":
            out.append(("str", v))
        else:
            out.append((t, v))
    return out


def _sproto_to_fproto(sp, ref=None, is_main=True):
    """Convert a parsed stock SProto into an FProto, re-encoding code + reordering happens
    at serialize time (emit_popcap writes debug-first). `ref` is the matching original
    FProto (for int32 re-promotion), aligned positionally. `is_main` marks the top-level
    chunk so its is_vararg flag is normalised to PopCap's convention."""
    fp = T.FProto()
    # source / chunk name: stock luac embeds the local build path (e.g. an absolute
    # /mnt/... path), but the engine expects the in-pak script path the original used and
    # reads this string at runtime (error/verbose-log paths). Carry the original's source
    # verbatim when we have the reference file, so the recompiled chunk is indistinguishable
    # from PopCap's own. (Nested protos have source == None in both, so this is a no-op there.)
    fp.source = ref.source if ref is not None else sp.source
    fp.linedefined = ref.linedefined if ref is not None else sp.linedefined
    fp.lastlined = ref.lastlined if ref is not None else sp.lastlined
    fp.nups = sp.nups
    fp.nparams = sp.nparams
    fp.maxstack = sp.maxstack
    # is_vararg: PopCap's convention (verified across all 621 shipped files) is main chunk == 0,
    # a nested function declared with `...` == 1, everything else == 0. Stock luac built with
    # LUA_COMPAT_VARARG instead encodes VARARG_ISVARARG(2)|HASARG(1)|NEEDSARG(4) (== 7 for a
    # `...` function) and marks the main chunk vararg (2). The HASARG/NEEDSARG bits drive a
    # legacy `arg`-table path the PopCap VM doesn't implement, which makes the engine reject
    # the chunk on load. Collapse to PopCap's 0/1 form (VARARG_ISVARARG == 2 is the bit that
    # actually means "declared vararg"; main is forced to 0 like every shipped main chunk).
    fp.vararg = 0 if is_main else (1 if (sp.vararg & 2) else 0)
    fp.locvars = list(sp.locvars)
    fp.upvals = list(sp.upvals)
    fp.consts = _promote_consts(sp.consts, ref.consts if ref else None)
    # Lower numeric for-loops (FORPREP/FORLOOP) into PopCap form, then convert each
    # resulting stock instruction. lower_numeric_for keeps lineinfo length in sync.
    low_code, low_li = lower_numeric_for(sp.code, sp.lineinfo)
    fp.lineinfo = low_li
    fp.code = [stock_ins_to_popcap(i) for i in low_code]
    # Debug line table: stock luac's line numbers reflect the *decompiled* source, which
    # differs from the original .lua, so they won't match PopCap's. When the recompiled
    # code has the same instruction count as the reference (i.e. no logic change shifted
    # things), reuse the original's line table verbatim so the file is byte-identical in
    # its debug section too. (lineinfo length must always equal code length.)
    if ref is not None and len(ref.lineinfo) == len(fp.code):
        fp.lineinfo = list(ref.lineinfo)
    # Inline-cache opcode handling. PopCap's per-PC inline cache makes the base-vs-variant
    # opcode choice load-bearing. We align our recompiled code against the original by
    # operands (base/variant collapse to the same key), then:
    #   - unchanged instructions adopt the original's exact opcode word (incl. its variant),
    #   - newly inserted/changed instructions have their cache-eligible base opcodes promoted
    #     to the inline-cache variant (the variant self-initialises its per-PC slot, so it is
    #     valid at any position, whereas a base opcode dropped at a fresh position is not).
    # Sequence alignment (rather than index-by-index) means this still works when a logic
    # change shifts subsequent instructions to new PCs.
    fp.code = _adopt_ref_opcodes(
        fp.code, ref.code if ref is not None else None, is_main
    )
    # Generic-for: rewrite each loop's entry JMP to the dedicated op32 (carries the control
    # base A and jumps to the op31 TFORLOOP). Done after opcode adoption so it also fixes up
    # files reconstructed against a reference. No-op for code without a generic-for.
    fp.code = _convert_genfor_entries(fp.code)
    # Generic-for: PopCap's iterator results land at R(A+2),R(A+3),... (Lua 5.0 frame, control ==
    # first variable), one register lower than stock Lua 5.1 puts them. Shift each loop body's
    # variable references down by one so they read where PopCap actually wrote them.
    fp.code = _shift_genfor_body_vars(fp.code)
    ref_protos = ref.protos if ref else []
    fp.protos = [
        _sproto_to_fproto(
            c, ref_protos[k] if k < len(ref_protos) else None, is_main=False
        )
        for k, c in enumerate(sp.protos)
    ]
    return fp


def stock_to_popcap(stock_path_or_bytes, ref_luc=None):
    """Compile-direction entry point: read stock .luac, return PopCap .luc bytes.
    ref_luc (path to the original PopCap .luc) enables exact int32 constant re-promotion."""
    sp, _, _ = S.parse_stock(stock_path_or_bytes)
    ref = T.parse_full(ref_luc) if ref_luc else None
    fp = _sproto_to_fproto(sp, ref)
    return emit_popcap(fp)


def transcode_roundtrip_file(path):
    """Full transcode round-trip: original .luc -> (forward) stock .luac -> (inverse)
    PopCap .luc', compared to original. Where the forward transcoder is lossless this
    reproduces the original; divergences localise exactly what the stock representation
    cannot carry (the overloaded/extra PopCap opcodes). Returns (ok, detail, opcodes_lost)."""
    original = open(path, "rb").read()
    chunk = T.parse_full(path)
    stock_bytes, _ = T.emit_standard(chunk)
    # which stock opcodes appear that we refuse to map? (collect, don't crash)
    sp, _, _ = S.parse_stock(stock_bytes)
    lost = set()

    def scan(p):
        for ins in p.code:
            o = ins & 0x3F
            if o in _UNMAPPABLE:
                lost.add(_UNMAPPABLE[o])
        for c in p.protos:
            scan(c)

    scan(sp)
    if lost:
        return False, "uses unmappable stock opcodes: " + ",".join(sorted(lost)), lost
    rebuilt = stock_to_popcap(stock_bytes, ref_luc=path)
    if rebuilt == original:
        return True, "identical (%d bytes)" % len(original), lost
    n = min(len(original), len(rebuilt))
    i = 0
    while i < n and original[i] == rebuilt[i]:
        i += 1
    return (
        False,
        (f"diverge @byte {i} (orig {len(original)}B, rebuilt {len(rebuilt)}B)"),
        lost,
    )


# identity round-trip check (the foundational correctness proof)


def roundtrip_file(path):
    """Return (ok, detail). ok == True iff emit_popcap(parse_full(path)) reproduces the
    file's bytes exactly. detail localises the first divergence when ok is False."""
    original = open(path, "rb").read()
    chunk = T.parse_full(path)
    rebuilt = emit_popcap(chunk)
    if rebuilt == original:
        return True, "identical (%d bytes)" % len(original)
    # localise the first difference
    n = min(len(original), len(rebuilt))
    i = 0
    while i < n and original[i] == rebuilt[i]:
        i += 1
    ctx_o = original[max(0, i - 4) : i + 8].hex()
    ctx_r = rebuilt[max(0, i - 4) : i + 8].hex()
    return False, (
        f"diverge @byte {i} (orig {len(original)}B, rebuilt {len(rebuilt)}B); "
        f"orig[..]={ctx_o} rebuilt[..]={ctx_r}"
    )


def _iter_luc(paths):
    import os

    for pth in paths:
        if os.path.isdir(pth):
            for r, _, fs in os.walk(pth):
                for f in fs:
                    if f.endswith(".luc"):
                        yield os.path.join(r, f)
        elif pth.endswith(".luc"):
            yield pth


def main(argv):
    if len(argv) >= 3 and argv[1] == "--roundtrip":
        ok = bad = 0
        failures = []
        for p in _iter_luc(argv[2:]):
            good, detail = roundtrip_file(p)
            if good:
                ok += 1
            else:
                bad += 1
                if len(failures) < 25:
                    failures.append((p, detail))
        print(f"identity round-trip: {ok} exact, {bad} mismatched")
        for p, d in failures:
            print("  MISMATCH", p, "--", d)
        return 0 if bad == 0 else 1
    if len(argv) >= 3 and argv[1] == "--transcode-roundtrip":
        ok = bad = 0
        reasons = {}
        lostall = {}
        for p in _iter_luc(argv[2:]):
            good, detail, lost = transcode_roundtrip_file(p)
            if good:
                ok += 1
            else:
                bad += 1
                key = detail.split(" @")[0].split(":")[0]
                reasons[key] = reasons.get(key, 0) + 1
                for L in lost:
                    lostall[L] = lostall.get(L, 0) + 1
        print(f"transcode round-trip: {ok} exact, {bad} differ")
        for k, c in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"   {c:4d}  {k}")
        if lostall:
            print(
                "   stock opcodes that block exact transcode (file counts):",
                ", ".join(f"{k}:{v}" for k, v in sorted(lostall.items())),
            )
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv))
