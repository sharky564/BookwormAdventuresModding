"""modkit.transform - composable bytecode-transform primitives.

Rules that let N mods stack safely on this 2000s-era PopCap VM (learned the hard way):
  1. Never recompile an existing method body - the VM rejects it. Only ever append a
     proto, append constants, splice a block into a body, or add a binding in main.
     Every op here keeps untouched bodies byte-for-byte identical.
  2. Target protos by CONTENT (`find_proto` matches marker constants), never by index,
     so appends and other mods' edits can't shift the target out from under you.
  3. Compute indices dynamically (a new proto's index is len(protos) at append time),
     so two mods can each append without colliding.

PopCap instruction layout (confirmed against the shipped game):
  iABC: op[0:6] | C[6:15] | B[15:24] | A[24:32]
  iABx: op[0:6] | Bx[6:24] | A[24:32]      constant in a B/C field = 250 + index (RK)
"""

import os

from bwakit.bytecode import luc_transcode as T
from bwakit.bytecode import luc_inverse_transcode as INV
from bwakit.bytecode.edit import append_const
from bwakit.game.encounters import repoint_path as _repoint_path


def _iABC(op, A, B, C):
    return (op & 0x3F) | ((C & 0x1FF) << 6) | ((B & 0x1FF) << 15) | ((A & 0xFF) << 24)


def _iABx(op, A, Bx):
    return (op & 0x3F) | ((Bx & 0x3FFFF) << 6) | ((A & 0xFF) << 24)


# PopCap opcodes actually used by these transforms (verified in-engine)
OP_MOVE, OP_LOADK, OP_LOADBOOL = 0, 1, 2
OP_GETGLOBAL_MAIN = 5  # GETGLOBAL inside the main chunk (is_main)
OP_GETTABLE, OP_SETTABLE, OP_SELF = 6, 9, 11
OP_JMP, OP_CALL, OP_RETURN = 22, 27, 29
OP_FORLOOP, OP_GETGLOBAL_NESTED, OP_CLOSURE = 30, 42, 48
_PC_REL_JUMPS = (OP_JMP, OP_FORLOOP)  # pc-relative jumps needing fixup on splice
_BIAS = 131071


def _const_strs(proto):
    return [v.rstrip(b"\x00") for t, v in proto.consts if t == "str"]


def _const_index(proto, value):
    """Append (dedup) a string or number constant, return its index."""
    if isinstance(value, bool):
        raise ValueError("bool is not a constant; use LOADBOOL")
    if isinstance(value, str):
        return append_const(proto, ("str", value.encode() + b"\x00"))
    if isinstance(value, (int, float)):
        return append_const(proto, ("num", float(value)))
    raise TypeError("unsupported constant %r" % (value,))


def find_proto(chunk, markers):
    """Index of the UNIQUE proto whose string constants contain every marker.

    Raises if zero or multiple match, forcing the author to pick a specific-enough
    marker set. This is what replaces fragile hardcoded proto indices.
    """
    want = [m.encode() if isinstance(m, str) else m for m in markers]
    hits = [
        i for i, p in enumerate(chunk.protos) if all(m in _const_strs(p) for m in want)
    ]
    if len(hits) != 1:
        raise ValueError(
            "markers %r matched %d protos (need exactly 1)" % (markers, len(hits))
        )
    return hits[0]


def _splice(code, lineinfo, at_pc, block):
    """Insert `block` (raw PopCap words) before `at_pc`.

    Fixes pc-relative jumps that straddle the insertion point and extends lineinfo
    to keep it the same length as code. Inserting at pc 0 never straddles, so a
    prepended call leaves the original body byte-for-byte identical.
    """
    n = len(block)
    fixed = list(code)
    for pc, ins in enumerate(fixed):
        if (ins & 0x3F) in _PC_REL_JUMPS:
            sbx = ((ins >> 6) & 0x3FFFF) - _BIAS
            tgt = pc + 1 + sbx
            delta = 0
            if pc < at_pc <= tgt:
                delta = n
            elif tgt < at_pc <= pc:
                delta = -n
            if delta:
                sbx += delta
                fixed[pc] = (ins & ~(0x3FFFF << 6)) | (((sbx + _BIAS) & 0x3FFFF) << 6)
    src_line = (
        lineinfo[at_pc] if at_pc < len(lineinfo) else (lineinfo[-1] if lineinfo else 0)
    )
    new_code = fixed[:at_pc] + list(block) + fixed[at_pc:]
    new_line = lineinfo[:at_pc] + [src_line] * n + lineinfo[at_pc:]
    return new_code, new_line


def compile_method(lua_path, luac=None):
    """Compile a standalone `CLASS={}; function CLASS:M(...) ... end` file; return the
    method's FProto (protos[0]). Freshly authored logic must be SELF-free - the VM
    tolerates appended methods that use GETGLOBAL/GETTABLE/for-in/arithmetic/returns, but
    not a recompiled `obj:method()`. (Calling INTO the method via inject_self_call is fine.)
    """
    import tempfile
    from bwakit.bytecode import compile as C

    fd, out = tempfile.mkstemp(suffix=".luc")
    os.close(fd)
    try:
        argv = [lua_path, "-o", out]
        if luac:
            argv += ["--luac", luac]
        C.main(argv)  # in-process: works in a frozen app too
        return T.parse_full(out).protos[0]
    finally:
        if os.path.exists(out):
            os.remove(out)


def append_bound_method(chunk, class_name, method_name, method_proto):
    """Append `method_proto` and bind `CLASS.METHOD = closure(N)` in main, just
    before main's final RETURN. Returns the new proto index N.

    Binding (verified): GETGLOBAL R0,K[class] ; CLOSURE R1,N ; SETTABLE R0,K[method],R1
    """
    if chunk.protos:
        method_proto.source = chunk.protos[0].source  # inherit the file's source tag
    N = len(chunk.protos)
    chunk.protos = list(chunk.protos) + [method_proto]
    idx_class = _const_index(chunk, class_name)
    idx_method = _const_index(chunk, method_name)
    binding = [
        _iABx(OP_GETGLOBAL_MAIN, 0, idx_class),
        _iABx(OP_CLOSURE, 1, N),
        _iABC(OP_SETTABLE, 0, 250 + idx_method, 1),
    ]
    ret_pc = max(i for i, w in enumerate(chunk.code) if (w & 0x3F) == OP_RETURN)
    chunk.code, chunk.lineinfo = _splice(chunk.code, chunk.lineinfo, ret_pc, binding)
    chunk.maxstack = max(chunk.maxstack, 2)
    return N


def inject_self_call(proto, method_name, arg_regs, ret_reg, at_pc=0):
    """Splice `R[ret_reg] = self:method(R[arg_regs...])` at `at_pc`.

    Registers are by index; at pc 0 they are the method's parameters
    (R0 = self, R1 = first param, ...). Uses scratch registers above maxstack.
    SELF op 11, CALL op 27 (verified). The author must ensure the referenced
    registers hold the intended values at `at_pc`.
    """
    K = _const_index(proto, method_name)
    M = proto.maxstack
    block = [_iABC(OP_SELF, M, 0, 250 + K)]  # R[M]=self.method ; R[M+1]=self
    for j, a in enumerate(arg_regs):
        block.append(_iABC(OP_MOVE, M + 2 + j, a, 0))  # arg -> R[M+2+j]
    block.append(
        _iABC(OP_CALL, M, len(arg_regs) + 2, 2)
    )  # B=1(self)+nargs+1 ; C=1 result+1
    block.append(_iABC(OP_MOVE, ret_reg, M, 0))  # result -> R[ret_reg]
    proto.code, proto.lineinfo = _splice(proto.code, proto.lineinfo, at_pc, block)
    proto.maxstack = max(proto.maxstack, M + 2 + len(arg_regs))


def inject_global_call(proto, global_name, method_name, args, at_pc=0):
    """Splice `GLOBAL.method(args...)` at `at_pc` (return value discarded).

    Each arg is {"str": s}, {"bool": b}, or {"num": n}. GETGLOBAL(nested) op 42,
    GETTABLE op 6, LOADK op 1, LOADBOOL op 2, CALL op 27 (verified).
    """
    M = proto.maxstack
    idx_g = _const_index(proto, global_name)
    idx_m = _const_index(proto, method_name)
    block = [
        _iABx(OP_GETGLOBAL_NESTED, M, idx_g),
        _iABC(OP_GETTABLE, M, M, 250 + idx_m),
    ]
    for j, a in enumerate(args):
        reg = M + 1 + j
        if "str" in a:
            block.append(_iABx(OP_LOADK, reg, _const_index(proto, a["str"])))
        elif "bool" in a:
            block.append(_iABC(OP_LOADBOOL, reg, 1 if a["bool"] else 0, 0))
        elif "num" in a:
            block.append(_iABx(OP_LOADK, reg, _const_index(proto, a["num"])))
        else:
            raise ValueError("bad arg %r" % (a,))
    block.append(_iABC(OP_CALL, M, len(args) + 1, 1))  # C=1 -> 0 results
    proto.code, proto.lineinfo = _splice(proto.code, proto.lineinfo, at_pc, block)
    proto.maxstack = max(proto.maxstack, M + 1 + len(args))


def load_chunk(path):
    return T.parse_full(path)


def save_chunk(chunk, path):
    data = INV.emit_popcap(chunk)
    with open(path, "wb") as f:
        f.write(data)
    return data


def apply_op(chunk, op, mod_dir, luac=None):
    """Apply one transform op (a dict from mod.json) to an in-memory chunk."""
    kind = op["op"]
    if kind == "append_bound_method":
        proto = compile_method(os.path.join(mod_dir, op["source"]), luac)
        append_bound_method(chunk, op["class"], op["method"], proto)
    elif kind == "inject_self_call":
        idx = find_proto(chunk, op["markers"])
        inject_self_call(
            chunk.protos[idx], op["method"], op["args"], op["ret"], op.get("at_pc", 0)
        )
    elif kind == "inject_global_call":
        idx = find_proto(chunk, op["markers"])
        inject_global_call(
            chunk.protos[idx],
            op["global"],
            op["method"],
            op["args"],
            op.get("at_pc", 0),
        )
    elif kind == "repoint_global":
        from bwakit.game import encounters as E

        E.repoint_global(chunk, op["from"], op["to"])
    elif kind == "patch_compare_const":
        from bwakit.bytecode.edit import patch_compare_rk

        idx = find_proto(chunk, op["markers"])
        patch_compare_rk(
            chunk.protos[idx], op["opcode"], op["operand"], op["from"], op["to"]
        )
    elif kind == "patch_compare_all":
        from bwakit.bytecode.edit import patch_compare_rk_all

        patch_compare_rk_all(chunk, op["opcode"], op["operand"], op["from"], op["to"])
    elif kind == "scale_health":
        from bwakit.bytecode.edit import scale_loadks

        p = chunk.protos[1]  # creature Init proto; SetHealth LOADK at pc 2
        if (p.code[2] & 0x3F) == 1:
            scale_loadks(p, [2], op["factor"])
    elif kind == "force_return_false":
        idx = find_proto(chunk, op["markers"])
        inject_return_false(chunk.protos[idx], op.get("at_pc", 0))
    elif kind == "inject_self_global_calls":
        idx = find_proto(chunk, op["markers"])
        inject_self_global_calls(
            chunk.protos[idx],
            op["calls"],
            at_pc=op.get("at_pc", 0),
            then_return=op.get("then_return", False),
        )
    elif kind == "repoint_roster":
        # randomizer roster repoints: each moves one AddEnemy (books) / preloader (packs) LOADK
        p = chunk.protos[op["proto_index"]]
        for pc, kidx, val in op["repoints"]:
            _repoint_path(p, pc, kidx, val)
    elif kind == "set_xp":
        from bwakit.bytecode.edit import set_xp

        set_xp(chunk.protos[1], op["value"])  # creature Init proto; sets self.mXP
    else:
        raise ValueError("unknown transform op %r" % kind)


def inject_self_global_calls(proto, calls, at_pc=0, then_return=False):
    """Splice one or more `GLOBAL:method(args...)` COLON calls (self = the global object) at
    `at_pc`, optionally followed by a bare `return`. Each call: GETGLOBAL -> SELF -> load
    args -> CALL with 0 results. Calls run sequentially so they reuse the same scratch
    registers. Used to make Tutorial.Chap4ScrambleUpdate immediately tear down its interrupt
    (RestoreState / RemoveOverlayCallback / IntroDialogComplete) instead of running the
    now-redundant scramble lesson, which conflicts with scramble being pre-enabled."""
    M = proto.maxstack
    block, need = [], M
    for c in calls:
        block.append(_iABx(OP_GETGLOBAL_NESTED, M, _const_index(proto, c["global"])))
        block.append(_iABC(OP_SELF, M, M, 250 + _const_index(proto, c["method"])))
        for j, a in enumerate(c.get("args", [])):
            reg = M + 2 + j
            if "str" in a:
                block.append(_iABx(OP_LOADK, reg, _const_index(proto, a["str"])))
            elif "bool" in a:
                block.append(_iABC(OP_LOADBOOL, reg, 1 if a["bool"] else 0, 0))
            elif "num" in a:
                block.append(_iABx(OP_LOADK, reg, _const_index(proto, a["num"])))
        nargs = len(c.get("args", []))
        block.append(_iABC(OP_CALL, M, nargs + 2, 1))  # self + nargs ; 0 results
        need = max(need, M + 2 + nargs)
    if then_return:
        block.append(_iABC(OP_RETURN, 0, 1, 0))  # return (no values)
    proto.code, proto.lineinfo = _splice(proto.code, proto.lineinfo, at_pc, block)
    proto.maxstack = max(proto.maxstack, need + 1)


def inject_return_false(proto, at_pc=0):
    """Prepend `return false` (LOADBOOL R0=false ; RETURN R0) at `at_pc`, short-circuiting a
    boolean predicate to false. Valid only for functions whose callers consume a single
    boolean result -- e.g. Book:HasPanCompleteDialog / HasVictoryCompleteDialog, where a
    false return makes the engine skip the conversation-panel cutscene and proceed."""
    block = [
        _iABC(OP_LOADBOOL, 0, 0, 0),  # R0 = false
        _iABC(OP_RETURN, 0, 2, 0),
    ]  # return R0 (one value)
    proto.code, proto.lineinfo = _splice(proto.code, proto.lineinfo, at_pc, block)
