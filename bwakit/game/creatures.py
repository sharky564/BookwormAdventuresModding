"""Creature-level operations built on the compile pipeline (decompile -> edit Lua source ->
recompile). Cloning authors new code, so unlike the surgical mods it needs the Lua toolchain:
unluac (decompile, Java) and luac 5.1 (compile), bridged by our stock<->PopCap transcoders
(luc_transcode / luc_inverse_transcode.stock_to_popcap).

A clone is a self-contained copy of a creature under a new class name with custom stats: the
dialog hooks are dropped (they are defined on the *original* class via a shared dialog file),
the class identifier is renamed to match the clone's new script path, and the HP is changed.
The art is reused as-is (GetInitialChapterResources / PAM paths are left untouched), so the
clone looks like the original."""

import os
import re
import subprocess
import tempfile

from bwakit.bytecode import luc_transcode as T, luc_inverse_transcode as INV

# dialog hooks: `self:dofile(".../dialog/...")` and the matching `self:Init<Name>Dialog(...)`
_DIALOG = re.compile(
    r"^[ \t]*self:dofile\([\"'][^\"']*dialog[^\"']*[\"']\)[^\n]*\n"
    r"|^[ \t]*self:Init\w*Dialog\([^\n]*\n",
    re.M,
)


def class_name(source):
    """The creature's class identifier (the `Name = {` at the top of the script)."""
    m = re.search(r"^(\w+)\s*=\s*\{", source, re.M)
    return m.group(1) if m else None


def decompile(luc_path, *, unluac_jar):
    """PopCap .luc -> Lua source (stock transcode + unluac)."""
    fd, stock = tempfile.mkstemp(suffix=".luc")
    os.close(fd)
    try:
        T.transcode(luc_path, stock)
        r = subprocess.run(
            ["java", "-jar", unluac_jar, stock], capture_output=True, text=True
        )
    finally:
        os.unlink(stock)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"decompile failed for {luc_path}: {(r.stderr or '')[:200]}")
    return r.stdout


def compile_source(source, *, luac, ref_luc=None):
    """Lua source -> PopCap .luc bytes (stock luac 5.1 + stock_to_popcap)."""
    with tempfile.TemporaryDirectory() as d:
        src, stock = os.path.join(d, "m.lua"), os.path.join(d, "m.luc")
        with open(src, "w") as fh:
            fh.write(source)
        r = subprocess.run([luac, "-o", stock, src], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"luac failed: {(r.stderr or '')[:300]}")
        return INV.stock_to_popcap(stock, ref_luc=ref_luc)


def clone_source(source, new_name, *, hp=None):
    """Transform a creature's source into a self-contained clone named `new_name`."""
    old = class_name(source)
    if not old:
        raise RuntimeError("could not find the creature's class name in its source")
    s = _DIALOG.sub("", source)  # drop dialog hooks
    s = s.replace(f"{old}_mt", f"{new_name}_mt")  # rename the metatable global
    s = re.sub(rf"\b{re.escape(old)}\b", new_name, s)  # rename the class identifier
    if hp is not None:
        s, n = re.subn(
            r"(local\s+t\s*=\s*\w+:Init\()\s*\d+(\s*\))",
            rf"\g<1>{int(hp)}\g<2>",
            s,
            count=1,
        )  # creature sets HP via base Init(N)
        if not n:
            s, n = re.subn(
                r"(\n[ \t]*)(return t\b)",
                rf"\g<1>t:SetHealth({int(hp)})\g<1>\g<2>",
                s,
                count=1,
            )
        if not n:
            raise RuntimeError("could not find where to set HP in the creature's Init")
    return s


def clone(orig_luc, new_name, *, hp=None, unluac_jar, luac):
    """Return PopCap .luc bytes for a self-contained clone of `orig_luc` named `new_name`."""
    src = clone_source(decompile(orig_luc, unluac_jar=unluac_jar), new_name, hp=hp)
    return compile_source(src, luac=luac, ref_luc=orig_luc)
