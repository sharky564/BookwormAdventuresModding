"""Disassembler for PopCap 0x56 Lua bytecode (Bookworm Adventures Deluxe).

Instruction encoding (recovered from the engine's luaV_execute):
  opcode = ins & 0x3F                       (low 6 bits; 49 opcodes, 0..48)
  A      = (ins >> 24) & 0xFF               (iABC A field)
  B      = (ins >> 6)  & 0x1FF              (iABC B, RK-encoded)
  C      = (ins >> 15) & 0x1FF              (iABC C, RK-encoded)
  Bx     = (ins >> 6)  & 0x3FFFF            (iABx)
  sBx    = Bx - 131071
  RK(x): x >= 250 -> constant K[x-250];  else register R[x]   (threshold 250)

Opcodes 0..37 match stock Lua 5.1 ordering (verified for MOVE/LOADK/GETGLOBAL/
SETGLOBAL/NEWTABLE/SETTABLE); 38..48 are PopCap additions/variants. The store
family used by data tables is op 9 (SETTABLE) in stock and also appears as
op 38 here for the global-table-constructor variant.

Proto layout (PopCap-reordered):
  header(23) + source string
  proto: linedefined(i32) lastlined(i32) nups(u8) nparams(u8) vararg(u8)
         maxstack(u8)
         sizelineinfo(u32) + lineinfo(i32*)
         sizelocvars(u32) + locvars         (debug-first ordering)
         sizeupvalues(u32) + upvalue-names
         sizek(u32) + constants  (type 0 nil,1 bool,3 f64,4 i32,5 string)
         sizep(u32) + nested protos (recursive)
         sizecode(u32) + code (u32 instructions)
"""

import struct

RK_THRESHOLD = 250

# PopCap 0x56 opcode map, recovered from luaV_execute's 49-entry dispatch table
# (VA 0x4619a4) by disassembling EVERY handler and reading its actual work
# (FPU ops for arithmetic, helper-call fingerprints, string refs, jump/loop
# patterns), corroborated with opcode frequency across 621 scripts and the
# fully-decoded Consts.luc. The opcode numbering is REORDERED from stock 5.1.
#
# Confidence:
#   CONFIRMED  - identified from decisive handler evidence (FPU op, referenced
#                string like "next", or validated decode).
#   '~' suffix - a variant that shares/branches into another op's handler and
#                differs only in minor return/dispatch handling (e.g. several
#                CALL forms); semantics for disassembly reading are equivalent.
# opcode map deduplicated: single source of truth is luc_transcode.POPCAP_OPS
from bwakit.bytecode.luc_transcode import POPCAP_OPS

# iABx form (A + 18-bit Bx). Confirmed for the ops below from their handlers
# (shr 6 / and 0x3ffff with no C-field RK decode).
IABX_OPS = {
    1,
    5,
    7,
    22,
    30,
    41,
    42,
    46,
    48,
}  # corrected: SETLIST/TFORLOOP/VARARG are iABC, not iABx


def opname(op):
    return POPCAP_OPS.get(op, f"OP{op}")


def decode_insn(ins):
    # OPERAND-ROLE NOTE (calibrated vs GEM_BONUS_PCT where amethyst=0.15): for the
    # ABC table opcodes the correct semantics are SETTABLE R(A)[RK(c)]:=RK(b) and
    # GETTABLE R(A):=R(c)[RK(b)] (and SELF uses c as the object). Renderers that
    # assume standard "R(A)[b]:=c" will show tables REVERSED; use key=c,val=b.
    op = ins & 0x3F
    a = (ins >> 24) & 0xFF
    b = (ins >> 6) & 0x1FF
    c = (ins >> 15) & 0x1FF
    bx = (ins >> 6) & 0x3FFFF
    sbx = bx - 131071
    return op, a, b, c, bx, sbx


def rk(field, consts):
    if field >= RK_THRESHOLD:
        idx = field - RK_THRESHOLD
        v = consts[idx] if 0 <= idx < len(consts) else f"K?{idx}"
        return f"K[{idx}]={v!r}"
    return f"R{field}"


class Proto:
    def __init__(self):
        self.linedefined = self.lastlined = 0
        self.nups = self.nparams = self.vararg = self.maxstack = 0
        self.consts = []
        self.code = []
        self.protos = []
        self.source = ""


def _read_string(data, off):
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    if n == 0:
        return "", off
    s = data[off : off + n].rstrip(b"\x00").decode("latin-1")
    return s, off + n


def _read_constants(data, off):
    sizek = struct.unpack_from("<I", data, off)[0]
    off += 4
    ks = []
    for _ in range(sizek):
        t = data[off]
        off += 1
        if t == 0:
            ks.append(None)
        elif t == 1:
            ks.append(bool(data[off]))
            off += 1
        elif t == 3:
            ks.append(struct.unpack_from("<d", data, off)[0])
            off += 8
        elif t == 4:
            ks.append(struct.unpack_from("<i", data, off)[0])
            off += 4
        elif t == 5:
            s, off = _read_string(data, off)
            ks.append(s)
        else:
            raise ValueError(f"bad const type {t} at {off - 1}")
    return ks, off


def _read_proto(data, off):
    p = Proto()
    # Every proto (top-level and nested) begins with a source string
    # (empty for nested protos).
    p.source, off = _read_string(data, off)
    p.linedefined, p.lastlined = struct.unpack_from("<ii", data, off)
    off += 8
    p.nups, p.nparams, p.vararg, p.maxstack = data[off : off + 4]
    off += 4
    # debug-first: lineinfo, locvars, upvalues
    sizelineinfo = struct.unpack_from("<I", data, off)[0]
    off += 4
    off += 4 * sizelineinfo
    sizelocvars = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(sizelocvars):
        _, off = _read_string(data, off)  # varname
        off += 8  # startpc, endpc
    sizeupval = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(sizeupval):
        _, off = _read_string(data, off)
    # constants
    p.consts, off = _read_constants(data, off)
    # nested protos
    sizep = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(sizep):
        child, off = _read_proto(data, off)
        p.protos.append(child)
    # code
    sizecode = struct.unpack_from("<I", data, off)[0]
    off += 4
    p.code = [struct.unpack_from("<I", data, off + 4 * i)[0] for i in range(sizecode)]
    off += 4 * sizecode
    return p, off


def parse(path):
    data = open(path, "rb").read()
    assert data[:4] == b"\x1bLua" and data[4] == 0x56, "not a PopCap 0x56 .luc"
    off = 23
    proto, off = _read_proto(data, off)
    return proto


def disasm_proto(p, consts=None, indent=0):
    consts = p.consts
    pad = "  " * indent
    out = [
        f"{pad}; proto params={p.nparams} stack={p.maxstack} "
        f"consts={len(p.consts)} code={len(p.code)}"
    ]
    for i, ins in enumerate(p.code):
        op, a, b, c, bx, sbx = decode_insn(ins)
        nm = opname(op)
        base = nm.rstrip("?")
        if base == "LOADK":
            s = (
                f"R{a} := K[{bx}]={consts[bx]!r}"
                if bx < len(consts)
                else f"R{a} := K?{bx}"
            )
        elif base in ("GETGLOBAL", "GETGLOBAL_v", "SETGLOBAL", "SETGLOBAL_v"):
            g = consts[bx] if bx < len(consts) else f"K?{bx}"
            if base.startswith("GET"):
                s = f"R{a} := _G[{g!r}]"
            else:
                s = f"_G[{g!r}] := R{a}"
        elif base in ("SETTABLE", "SETTABLE_G", "SETTABLE_Gv", "SETTABLE_Gw"):
            s = f"R{a}[{rk(b, consts)}] := {rk(c, consts)}"
        elif base in ("GETTABLE", "GETTABLE_v", "GETTABLE_K"):
            s = f"R{a} := R?[{rk(c, consts)}]  (tbl={rk(b, consts)})"
        elif base == "NEWTABLE":
            s = f"R{a} := {{}}"
        elif base == "SELF":
            s = f"R{a + 1} := self; R{a} := R?[{rk(c, consts)}]  (method)"
        elif base in ("CALL", "CALL_v", "CALL_w", "TAILCALL"):
            na = (b - 1) if b else "var"
            nr = (c - 1) if c else "var"
            s = f"call R{a}, args={na}, results={nr}"
        elif base == "RETURN":
            s = f"return R{a}..(+{(b - 1) if b else 'var'})"
        elif base == "JMP":
            s = f"-> pc{sbx:+d}"
        elif base in ("MOVE",):
            s = f"R{a} := R{b}"
        elif base in ("CLOSURE", "CLOSURE_v"):
            s = f"R{a} := closure(proto[{bx}])"
        elif base in ("ADD", "SUB", "MUL", "DIV", "MOD", "POW"):
            symn = {
                "ADD": "+",
                "SUB": "-",
                "MUL": "*",
                "DIV": "/",
                "MOD": "%",
                "POW": "^",
            }[base]
            s = f"R{a} := {rk(b, consts)} {symn} {rk(c, consts)}"
        elif base in ("UNM", "NOT", "LEN"):
            s = f"R{a} := {base.lower()} {rk(b, consts)}"
        elif base == "CONCAT":
            s = f"R{a} := concat(R{b}..R{c})"
        elif base in ("EQ", "LT", "LE"):
            s = f"if ({rk(b, consts)} {base} {rk(c, consts)}) != {a}: pc++"
        elif base == "TEST":
            s = f"if bool(R{a}) != {c}: pc++"
        elif base in ("FORLOOP", "FORPREP"):
            s = f"R{a} for-loop -> pc{sbx:+d}"
        elif base == "TFORLOOP":
            s = f"R{a} generic-for (iterator), {c} results"
        elif base == "SETLIST":
            s = f"R{a}[..] := R{a + 1}..  (count field={c})"
        elif base in ("LOOPGUARD",):
            s = "(dispatch/loop continuation)"
        else:
            s = f"A={a} B={b} C={c} Bx={bx} sBx={sbx}"
        out.append(f"{pad}[{i:3}] {nm:12} {s}")
    for child in p.protos:
        out.append(f"{pad}--- nested proto ---")
        out.append(disasm_proto(child, indent=indent + 1))
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    p = parse(sys.argv[1])
    print(disasm_proto(p))


# Verified operand-role helpers (calibrated + cross-checked over 150+ .luc)
# This PopCap variant's ABC layout: A=(ins>>24)&0xFF. The two 9-bit fields are
# F6=(ins>>6)&0x1FF and F15=(ins>>15)&0x1FF. Their KEY/VALUE roles are opcode-dependent:
#   SETTABLE R(A)[ RK(F15) ] := RK(F6)      key=F15, value=F6   (verified: 3670 vs 12; closures 896/0; value==closure-reg 820/0)
#   GETTABLE R(A) := R(F15)[ RK(F6) ]       table=F15, key=F6   (verified: key-string in F6 5984/0)
#   SELF     R(A+1):=R(F15); R(A):=R(F15)[ RK(F6) ]  method=F6  (verified: method-string in F6 4300/0)
#   CALL     A B C with nargs+1 = F6, nresults+1 = F15 (standard; F6 dominated by 1/2)
# For ADD/SUB/MUL/etc and LT/LE/EQ the two RK operands are F6 and F15; order only
# matters for non-commutative compares (LT/LE) and division/sub - treat F6 as the
# 'B' (left) and F15 as the 'C' (right) per standard, UNLESS a specific check shows
# otherwise for this build.
def tbl_settable(ins):
    """Return (table_reg, key_rk, val_rk) for a SETTABLE instruction."""
    a = (ins >> 24) & 0xFF
    f6 = (ins >> 6) & 0x1FF
    f15 = (ins >> 15) & 0x1FF
    return a, f15, f6


def tbl_gettable(ins):
    """Return (dst_reg, table_rk, key_rk) for a GETTABLE instruction."""
    a = (ins >> 24) & 0xFF
    f6 = (ins >> 6) & 0x1FF
    f15 = (ins >> 15) & 0x1FF
    return a, f15, f6


def self_method(ins):
    """Return (base_reg, obj_rk, method_rk) for a SELF instruction."""
    a = (ins >> 24) & 0xFF
    f6 = (ins >> 6) & 0x1FF
    f15 = (ins >> 15) & 0x1FF
    return a, f15, f6
