"""proto_splice.py - Tier-3 surgical editing for PopCap .luc files that DON'T cleanly
decompile (files containing numeric/generic for-loops that unluac renders as invalid Lua).

Instead of recompiling the whole file, edit ONE function and leave every other function as
its original bytecode. Two techniques:

  1. splice_function(...)  - recompile a CLEAN function (no for-loops) from source, transcode
     it against the original proto as opcode reference, and swap just that proto into the
     original chunk's tree. The parent's CLOSURE reference is by index, so an in-place swap is
     transparent. Verified byte-identical when the function is recompiled unchanged.

  2. insert_block(...)     - for a function that CONTAINS for-loops (can't be recompiled),
     splice raw instruction words directly into the proto's code, fixing any pc-relative jump
     (JMP op22 / FORLOOP op30) that straddles the insertion point. The inline cache is runtime-
     only (not in the file), so as long as the inserted opcodes are valid the loader wires it.

Requires: luc_transcode (parse_full, FProto), luc_inverse_transcode (_sproto_to_fproto,
emit_popcap), lua51_stock (parse_stock), and a native Lua 5.1.5 luac.
"""

import subprocess, sys

sys.path.insert(0, "/mnt/user-data/outputs")
from bwakit.bytecode import (
    lua51_stock as S,
    luc_transcode as T,
    luc_inverse_transcode as INV,
)

LUAC = "/tmp/luasrc/lua5.1/src/luac"
BIAS = 131071
_JUMP_OPS = (22, 30)  # PopCap JMP, FORLOOP (iAsBx, pc-relative)


def _navigate(chunk, proto_path):
    """'main.1.0' -> (parent_proto, index, target_proto)."""
    idx = [int(x) for x in proto_path.split(".")[1:]]
    parent = node = chunk
    for i in idx:
        parent, node = node, node.protos[i]
    return parent, idx[-1], node


def splice_function(orig_luc, proto_path, func_src, out_path, luac=LUAC):
    """Recompile func_src (a standalone `function ... end`), transcode against the original
    proto at proto_path, swap it in, and write out_path. Returns (ref_proto, new_proto)."""
    open("/tmp/_splice_fn.lua", "w").write(func_src)
    r = subprocess.run(
        [luac, "-o", "/tmp/_splice_fn.luac", "/tmp/_splice_fn.lua"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError("luac failed:\n" + r.stderr)
    stock_main, _, _ = S.parse_stock("/tmp/_splice_fn.luac")
    stock_fn = stock_main.protos[0]  # the function = first closure in the chunk
    ch = T.parse_full(orig_luc)
    parent, i, ref = _navigate(ch, proto_path)
    new_fp = INV._sproto_to_fproto(stock_fn, ref, is_main=False)
    parent.protos[i] = new_fp
    open(out_path, "wb").write(INV.emit_popcap(ch))
    return ref, new_fp


def insert_block(orig_luc, proto_path, at_pc, block, line_src_pc, out_path):
    """Insert `block` (list of raw instruction words) into the proto at proto_path, BEFORE
    index at_pc. lineinfo entries are copied from line_src_pc (a slice of equal length).
    Fixes JMP/FORLOOP offsets that straddle at_pc. maxstack is the caller's responsibility
    (ensure it already covers the block's registers). Returns the modified FProto."""
    ch = T.parse_full(orig_luc)
    _, _, p = _navigate(ch, proto_path)
    n = len(block)
    # fix straddling pc-relative jumps in the ORIGINAL code (offsets are relative; +n gap)
    fixed = list(p.code)
    for pc, ins in enumerate(fixed):
        if (ins & 0x3F) in _JUMP_OPS:
            sbx = ((ins >> 6) & 0x3FFFF) - BIAS
            tgt = pc + 1 + sbx
            d = 0
            if pc < at_pc <= tgt:
                d = +n  # forward jump over the gap
            elif tgt < at_pc <= pc:
                d = -n  # backward jump over the gap
            if d:
                sbx += d
                fixed[pc] = (ins & ~(0x3FFFF << 6)) | (((sbx + BIAS) & 0x3FFFF) << 6)
    p.code = fixed[:at_pc] + list(block) + fixed[at_pc:]
    p.lineinfo = p.lineinfo[:at_pc] + list(line_src_pc) + p.lineinfo[at_pc:]
    assert len(p.lineinfo) == len(p.code)
    open(out_path, "wb").write(INV.emit_popcap(ch))
    return p


if __name__ == "__main__":
    # self-test: unchanged splice must be byte-identical
    lines = open("/tmp/packs_Book1.lua").read().splitlines()
    src = "\n".join(lines[35:121])
    splice_function(
        "/tmp/pak_out/scripts/packs/Book1.luc", "main.2", src, "/tmp/_selftest.luc"
    )
    a = open("/tmp/pak_out/scripts/packs/Book1.luc", "rb").read()
    b = open("/tmp/_selftest.luc", "rb").read()
    print("self-test (unchanged splice byte-identical):", a == b)
