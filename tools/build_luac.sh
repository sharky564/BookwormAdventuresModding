#!/bin/sh
# build_luac.sh -- build the luac that `bwa compile` needs.
#
# PopCap's Lua flushes table constructors every 32 array elements; stock Lua uses 50. To make
# luac's output line up with PopCap (so large array literals batch identically), we build stock
# Lua 5.1.5 with LFIELDS_PER_FLUSH patched from 50 to 32. Everything else is stock.
#
# Usage:
#   tools/build_luac.sh /path/to/lua-5.1.5            (an unpacked Lua 5.1.5 source tree)
#   tools/build_luac.sh /path/to/lua-5_1_5_Sources.zip (a zip; unpacked to a temp dir)
#
# Produces ./luac in the kit root. Point `bwa compile` at it with --luac ./luac or $BWA_LUAC.
#
# Requires a C compiler (gcc/clang) and make/unzip. No network access needed.
set -e

SRC="${1:?usage: build_luac.sh <lua-5.1.5 source dir or zip>}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"     # kit root (parent of tools/)
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

case "$SRC" in
  *.zip)
    echo "unpacking $SRC ..."
    unzip -oq "$SRC" -d "$WORK"
    TREE="$(dirname "$(find "$WORK" -name lopcodes.h -path '*/src/*' | head -1)")/.."
    ;;
  *)
    TREE="$SRC"
    ;;
esac

LO="$(find "$TREE" -name lopcodes.h -path '*/src/*' | head -1)"
[ -n "$LO" ] || { echo "error: lopcodes.h not found under $TREE" >&2; exit 1; }
SD="$(dirname "$LO")"
echo "lua source src/ = $SD"

# Patch the flush width 50 -> 32 (idempotent).
sed -i.bak 's/#define LFIELDS_PER_FLUSH[[:space:]].*/#define LFIELDS_PER_FLUSH\t32/' "$LO"
echo "patched: $(grep LFIELDS_PER_FLUSH "$LO")"

# Build just luac (skip the lua interpreter's lua.c / any wmain.c). ANSI build is the most
# portable and is what the kit was validated against.
CC="${CC:-gcc}"
echo "compiling luac with $CC ..."
( cd "$SD" && "$CC" -O2 -DLUA_ANSI -o luac \
    $(ls *.c | grep -vxE 'lua\.c|wmain\.c') -lm )

cp "$SD/luac" "$HERE/luac"
echo "done: $HERE/luac"
"$HERE/luac" -v 2>&1 | head -1
echo "use it with:  bwa compile your.lua -o your.luc --luac \"$HERE/luac\""
