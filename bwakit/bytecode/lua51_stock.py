"""Minimal STOCK Lua 5.1 bytecode reader - used only to VALIDATE the transcoder
output (luc_transcode.py). If this reads the emitted .luac cleanly and the decoded
instructions/constants match the source semantics, the file is well-formed stock
Lua 5.1 and unluac/luadec will accept it.

Stock 5.1 iABC layout:  op(0-5) A(6-13) C(14-22) B(23-31)
Stock iABx:            op(0-5) A(6-13) Bx(14-31)
"""

import struct

STOCK_NAMES = [
    "MOVE",
    "LOADK",
    "LOADBOOL",
    "LOADNIL",
    "GETUPVAL",
    "GETGLOBAL",
    "GETTABLE",
    "SETGLOBAL",
    "SETUPVAL",
    "SETTABLE",
    "NEWTABLE",
    "SELF",
    "ADD",
    "SUB",
    "MUL",
    "DIV",
    "MOD",
    "POW",
    "UNM",
    "NOT",
    "LEN",
    "CONCAT",
    "JMP",
    "EQ",
    "LT",
    "LE",
    "TEST",
    "TESTSET",
    "CALL",
    "TAILCALL",
    "RETURN",
    "FORLOOP",
    "FORPREP",
    "TFORLOOP",
    "SETLIST",
    "CLOSE",
    "CLOSURE",
    "VARARG",
]


def decode_stock(ins):
    op = ins & 0x3F
    A = (ins >> 6) & 0xFF
    C = (ins >> 14) & 0x1FF
    B = (ins >> 23) & 0x1FF
    Bx = (ins >> 14) & 0x3FFFF
    sBx = Bx - 131071
    return op, A, B, C, Bx, sBx


class SProto:
    def __init__(self):
        self.source = None
        self.code = []
        self.consts = []
        self.protos = []
        self.nparams = 0
        self.maxstack = 0
        self.vararg = 0
        self.nups = 0
        self.lineinfo = []
        self.locvars = []
        self.upvals = []


def _rs(data, off, st=4):
    # string length is a size_t: 4 bytes from a 32-bit luac, 8 from a 64-bit one
    n = struct.unpack_from("<Q" if st == 8 else "<I", data, off)[0]
    off += st
    if n == 0:
        return None, off
    return data[off : off + n], off + n


def _rk(data, off, st=4):
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
        elif t == 4:
            s, off = _rs(data, off, st)
            ks.append(("str", s if s is not None else b""))
        else:
            raise ValueError(f"STOCK bad const type {t} @ {off - 1}")
    return ks, off


def _rp(data, off, st=4):
    p = SProto()
    p.source, off = _rs(data, off, st)
    p.linedefined, p.lastlined = struct.unpack_from("<ii", data, off)
    off += 8
    p.nups, p.nparams, p.vararg, p.maxstack = data[off : off + 4]
    off += 4
    # stock order: code, consts, protos, debug
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    p.code = list(struct.unpack_from("<%dI" % n, data, off))
    off += 4 * n
    p.consts, off = _rk(data, off, st)
    n = struct.unpack_from("<I", data, off)[0]
    off += 4
    for _ in range(n):
        c, off = _rp(data, off, st)
        p.protos.append(c)
    n = struct.unpack_from("<I", data, off)[0]
    off += 4  # lineinfo
    p.lineinfo = list(struct.unpack_from("<%di" % n, data, off))
    off += 4 * n
    n = struct.unpack_from("<I", data, off)[0]
    off += 4  # locvars
    for _ in range(n):
        nm, off = _rs(data, off, st)
        s, e = struct.unpack_from("<ii", data, off)
        off += 8
        p.locvars.append((nm, s, e))
    n = struct.unpack_from("<I", data, off)[0]
    off += 4  # upvals
    for _ in range(n):
        nm, off = _rs(data, off, st)
        p.upvals.append(nm)
    return p, off


def parse_stock(path_or_bytes):
    data = (
        path_or_bytes
        if isinstance(path_or_bytes, (bytes, bytearray))
        else open(path_or_bytes, "rb").read()
    )
    assert data[:4] == b"\x1bLua", "not Lua bytecode"
    assert data[4] == 0x51, f"not 5.1 (ver={data[4]:#x})"
    # header bytes: [5]=format [6]=endian [7]=int [8]=size_t [9]=instr [10]=number [11]=integral
    fmt, endian, sz_int, sz_size_t, sz_instr, sz_num, integral = data[5:12]
    if endian != 1:
        raise ValueError("big-endian bytecode unsupported (need little-endian)")
    if sz_int != 4:
        raise ValueError(f"sizeof(int)={sz_int}, expected 4")
    if sz_instr != 4:
        raise ValueError(f"sizeof(Instruction)={sz_instr}, expected 4")
    if sz_num != 8 or integral != 0:
        raise ValueError(
            f"lua_Number must be 8-byte double (size={sz_num}, integral={integral})"
        )
    if sz_size_t not in (4, 8):
        raise ValueError(f"unsupported sizeof(size_t)={sz_size_t}")
    # 32-bit and 64-bit luac differ ONLY in size_t (string-length) width; everything the game
    # needs is re-emitted as 32-bit PopCap by emit_popcap regardless, so either host works.
    proto, off = _rp(data, 12, sz_size_t)
    return proto, off, len(data)
