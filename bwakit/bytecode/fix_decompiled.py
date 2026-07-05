#!/usr/bin/env python3
"""Post-process patched-unluac output into faithful Lua source. Five passes:

1. SELF-REFERENCE: the patched TableLiteral prints `nil --[[ self-reference ]]` where a
   class table appears inside its own metatable (`X.mt = {__index = X}`). Replaced with the
   enclosing class name (resolved by bracket-tracking).
2. FLOOR markers: PopCap's unary floor opcode (no stock Lua 5.1 equivalent) is emitted by
   the transcoder as LEN ('#'), rendering as `#(EXPR)`. Real length always renders as
   `#identifier`, so `#(...)` is unambiguously floor -> `math.floor(EXPR)`.
3. GENERIC-FOR: PopCap's two-register `for k,v in pairs(t)` can't be reconstructed by stock
   unluac and emerges as a fixed garble; rewritten to a proper `for ... in ... do ... end`.
4. SETLIST array indices: PopCap flushes arrays every 32 elements but unluac strides by 50,
   mis-indexing everything past the first 32 (`[51]`, `[101]`, ...); renumbered to a
   contiguous positional array.
5. CONSTRUCTOR KEYS: PopCap's small NEWTABLE hash hint makes unluac render overflow fields
   as `["name"] = v`; rewritten to bare `name = v` for valid non-reserved identifiers.
6. LONG STRINGS: unluac renders any string constant containing a newline as a long-bracket
   literal `[[ ... ]]` instead of a quoted string (valid Lua but inconsistent, and the
   dropped leading newline is an editing footgun); rewritten back to standard double-quoted
   strings with escaped contents. Block comments `--[[ ... ]]` are left untouched.
7. SELF-ASSIGN NO-OPS: an in-place global update (GETGLOBAL/op/SETGLOBAL of the same name)
   makes unluac emit, after the real `x = x <op> ...` line, a spurious bare `x = x`;
   the redundant no-op statement is deleted. Constructor fields like `a = a` are preserved.

Usage:  python3 fix_decompiled.py <dir-or-file> [...]
Edits .lua files in place. Idempotent. Prints a summary.
"""

import sys, os, re

PLACEHOLDER = "nil --[[ self-reference ]]"
OPEN_NAMED = re.compile(r"(?:^|\b)(?:local\s+)?([A-Za-z_][\w.]*)\s*=\s*\{\s*$")


# pass 1: self-reference
def fix_selfref(text):
    lines = text.split("\n")
    name_stack = []
    out = []
    n = 0
    for ln in lines:
        if PLACEHOLDER in ln:
            enclosing = next((nm for nm in name_stack if nm), None)
            if enclosing:
                ln = ln.replace(PLACEHOLDER, enclosing)
                n += 1
        m = OPEN_NAMED.search(ln.strip())
        net = ln.count("{") - ln.count("}")
        if net == 1:
            name_stack.append(m.group(1) if m else None)
        elif net == -1 and name_stack:
            name_stack.pop()
        out.append(ln)
    return "\n".join(out), n


# pass 2: floor markers
def _match_paren(s, i):
    """s[i] == '('; return index just past the matching ')'. Respects strings."""
    depth = 0
    j = i
    instr = None
    while j < len(s):
        c = s[j]
        if instr:
            if c == "\\":
                j += 2
                continue
            if c == instr:
                instr = None
        else:
            if c in "\"'":
                instr = c
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return j + 1
        j += 1
    return -1  # unbalanced


# floor renders either as `#(EXPR)` (parenthesised) or, when applied straight to a call/field
# that always yields a NUMBER, as e.g. `#math.fn(...)`, `#math.pi`, `#tonumber(...)`,
# `#string.byte(...)`. Length is illegal on a number, so every such form is unambiguously
# floor and is rewritten. Calls that yield a string/table (`#string.sub(...)`), polymorphic
# getters (`#profile.Get(...)`), and bare `#identifier` are left alone (genuinely ambiguous
# with the real length operator without type information).
_FLOOR_MATH = re.compile(
    r"#(?:math\.[A-Za-z_]\w*|tonumber|string\.byte|string\.len|string\.find)(?![A-Za-z0-9_])"
)


def fix_floor(text):
    n = 0
    i = 0
    out = []
    while True:
        k = text.find("#", i)
        if k == -1:
            out.append(text[i:])
            break
        out.append(text[i:k])
        if text.startswith("#(", k):  # case 1: #(EXPR)
            end = _match_paren(text, k + 1)  # k+1 is the '('
            if end == -1:
                out.append("#")
                i = k + 1
                continue
            out.append("math.floor(" + text[k + 2 : end - 1] + ")")
            n += 1
            i = end
            continue
        m = _FLOOR_MATH.match(text, k)  # case 2: #math.fn(...) or #math.field
        if m:
            j = m.end()
            if j < len(text) and text[j] == "(":  # a call: include its (balanced) args
                end = _match_paren(text, j)
                if end == -1:
                    out.append("#")
                    i = k + 1
                    continue
                out.append("math.floor(" + text[k + 1 : end] + ")")
                n += 1
                i = end
                continue
            out.append(
                "math.floor(" + text[k + 1 : j] + ")"
            )  # a bare math.field number
            n += 1
            i = j
            continue
        out.append("#")
        i = k + 1  # genuine length operator -> leave as-is
    return "".join(out), n


# pass 3: generic-for loops (PopCap VM extension unluac can't reconstruct)
# PopCap compiles `for k,v in pairs(t)` with a 2-register control layout that stock unluac
# (3-register) mis-decompiles into a fixed, recognizable garble:
#     <N>do                                              (sometimes; a scope wrapper)
#     <M>local (for generator), (for state), VARS = TBL, nil, nil[, nil]
#     <M>while true do
#     <M+2>BODY...
#     <M+2>TBL[1] = (for state)        <- garbage trailer (mis-emitted loop tail)
#     <M+2>TBL[2] = ...                   (values may span multiple lines, e.g. `= {`)
#     <M>end                            <- closes the while
#     <N>end                            (only if the `do` wrapper was present)
# We rewrite this to `for VARS in pairs(TBL) do BODY end`, dropping the trailer and
# collapsing the redundant `do` wrapper when it exclusively wraps the loop. `(for state)`
# is unluac's pseudo-name and never appears in real code, so the trailer start is
# unambiguous; the trailer ends at the `end` sitting at the loop's own indent (its
# multi-line table values contain `{`/`}` but never `end`).
import re as _re

# RHS is either `TBL, nil, nil[, nil]` (iterate a table -> wrap in pairs()) or a real
# iterator expression like `f:lines()` (use as-is). The `while` condition is whatever junk
# unluac put in the control slot (`true`, a leftover string, etc.) -- it is always replaced.
_LOCAL = _re.compile(r"^(\s*)local \(for generator\), \(for state\), (.+?) = (.+)$")
_WHILE = _re.compile(r"^(\s*)while .+ do\s*$")
_NILS = _re.compile(r"^(.*?)((?:, nil)+)$")


def fix_genfor(text):
    n = 0
    for _ in range(64):  # repeat for nested loops
        lines = text.split("\n")
        out = []
        i = 0
        changed = False
        while i < len(lines):
            ml = _LOCAL.match(lines[i])
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            is_repeat = nxt.strip() == "repeat"  # BOTTOM_CONDITION generic-for form
            if ml and (_WHILE.match(nxt) or is_repeat):
                M, vars_, rhs = ml.group(1), ml.group(2), ml.group(3).rstrip()
                mn = _NILS.match(rhs)
                if mn:  # `TBL, nil, nil` -> pairs(TBL)
                    tbl = mn.group(1)
                    iterator = "pairs(" + tbl + ")"
                else:  # real iterator expression
                    tbl = rhs
                    iterator = rhs
                esc = _re.escape(tbl)
                gstart = _re.compile(r"^\s*" + esc + r"\[1\] = \(for state\)\s*$")
                # loop terminator: `until <cond>` for the repeat form, else `end`
                while_end = (
                    _re.compile(r"^" + _re.escape(M) + r"until .+$")
                    if is_repeat
                    else _re.compile(r"^" + _re.escape(M) + r"end\s*$")
                )
                j = i + 2
                while j < len(lines) and not gstart.match(lines[j]):
                    j += 1
                if j < len(lines):
                    body = lines[i + 2 : j]
                    k = j
                    while k < len(lines) and not while_end.match(lines[k]):
                        k += 1
                    if k < len(lines):  # found the while's end
                        do_indent = M[:-2]
                        collapse = (
                            out
                            and out[-1] == do_indent + "do"
                            and k + 1 < len(lines)
                            and lines[k + 1] == do_indent + "end"
                        )
                        if collapse:
                            out.pop()
                            out.append(
                                do_indent + "for " + vars_ + " in " + iterator + " do"
                            )
                            for b in body:
                                out.append(b[2:] if b[:2] == "  " else b)
                            out.append(do_indent + "end")
                            i = k + 2
                        else:
                            out.append(M + "for " + vars_ + " in " + iterator + " do")
                            out.extend(body)
                            out.append(M + "end")
                            i = k + 1
                        n += 1
                        changed = True
                        continue
            out.append(lines[i])
            i += 1
        text = "\n".join(out)
        if not changed:
            break
    return text, n


# pass 4: SETLIST array indices (PopCap flush-size != stock LFIELDS_PER_FLUSH)
# PopCap flushes table-constructor arrays every 32 elements (its FPF), but stock unluac
# places SETLIST batch C at stride 50 (stock FPF). So an array's first 32 elements render
# positionally, then later batches get WRONG explicit indices: batch 2 lands at [51..], not
# [33..], leaving phantom gaps. The displayed key of the m-th indexed element after a full
# 32-element first batch is 50*(1 + m//32) + m%32 + 1, so the artifact has an exact,
# self-validating signature (first index == 51, every index within a 32-wide window of its
# 50-stride, each step either +1 or a jump to the next 50-boundary). When that holds for a
# constructor whose first 32 entries are positional, the indexed entries are simply the
# array continued in order, so we strip the `[K] = ` prefix to make them positional again
# (works for single-line and multi-line `[K] = { ... }` values alike). Genuine keyed tables
# never match this signature, so they are left untouched.
import re as _re2

_FPF_IDX = _re2.compile(r"^(\s*)\[(\d+)\] = ")


def _fpf_strip_comments_strings(s):
    s = _re2.sub(r"--.*", "", s)
    s = _re2.sub(r'"(\\.|[^"\\])*"', '""', s)
    s = _re2.sub(r"'(\\.|[^'\\])*'", "''", s)
    return s


def _fpf_is_entry(line, want_depth, line_depth):
    return (
        line_depth == want_depth
        and line.strip() != ""
        and not _re2.match(r"^\s*[}\])]", line)
    )


def fix_setlist_fpf(text):
    lines = text.split("\n")
    n = len(lines)
    sh = [_fpf_strip_comments_strings(l) for l in lines]
    depth = [0] * n
    d = 0
    for i in range(n):
        depth[i] = d
        d += sh[i].count("{") - sh[i].count("}")
    fixed = 0
    handled = set()
    for i in range(n):
        m = _FPF_IDX.match(lines[i])
        if not m or int(m.group(2)) != 51 or i in handled:
            continue
        D = depth[i]
        ents = []
        j = i - 1
        while j >= 0 and depth[j] >= D:
            if _fpf_is_entry(lines[j], D, depth[j]):
                ents.append(j)
            j -= 1
        ents.reverse()
        j = i
        while j < n and depth[j] >= D:
            if _fpf_is_entry(lines[j], D, depth[j]):
                ents.append(j)
            j += 1
        pos = [e for e in ents if not _FPF_IDX.match(lines[e])]
        idx = [e for e in ents if _FPF_IDX.match(lines[e])]
        if not idx or (pos and max(pos) > min(idx)) or len(pos) != 32:
            continue
        keys = [int(_FPF_IDX.match(lines[e]).group(2)) for e in idx]
        ok = keys[0] == 51 and all((k - 1) % 50 < 32 for k in keys)
        for a, b in zip(keys, keys[1:]):
            if not (b == a + 1 or (b - 1) % 50 == 0) or b <= a:
                ok = False
                break
        if not ok:
            continue
        for e in idx:
            lines[e] = _FPF_IDX.sub(lambda mo: mo.group(1), lines[e])
            fixed += 1
            handled.add(e)
    return "\n".join(lines), fixed


# pass 5: quoted-identifier constructor keys (cosmetic)
# PopCap's NEWTABLE emits a hash-size hint smaller than the real number of hash fields, so
# stock unluac folds only the first <hint>-many fields into the constructor as bare
# `name = v` and renders every later field as `["name"] = v`. In Lua a constructor field
# `["name"] = v` is byte-for-byte equivalent to `name = v` whenever `name` is a valid
# identifier that is not a reserved word, and a source line that *begins* with `["name"] =`
# can only be a table-constructor field (such a statement is illegal at the top level), so
# the rewrite is always safe. This unifies the rendering of structs like PAMAnimator:Init's.
_LUA_KW = {
    "and",
    "break",
    "do",
    "else",
    "elseif",
    "end",
    "false",
    "for",
    "function",
    "goto",
    "if",
    "in",
    "local",
    "nil",
    "not",
    "or",
    "repeat",
    "return",
    "then",
    "true",
    "until",
    "while",
}
_CTOR_KEY = re.compile(r'^(\s*)\["([A-Za-z_][A-Za-z0-9_]*)"\] = ')


def fix_constructor_keys(text):
    out = []
    n = 0
    for line in text.split("\n"):
        m = _CTOR_KEY.match(line)
        if m and m.group(2) not in _LUA_KW:
            line = m.group(1) + m.group(2) + " = " + line[m.end() :]
            n += 1
        out.append(line)
    return "\n".join(out), n


# pass 6: long-bracket strings -> quoted strings
# When a string constant contains a newline, unluac renders it as a Lua long-bracket
# literal ([[ ... ]]) instead of a quoted string. That is valid Lua but inconsistent
# (single-line constants stay quoted, multi-line ones flip to brackets) and the leading
# newline that Lua silently drops after `[[` is a well-known editing footgun -- both bad
# for dialog that gets read and modded. This pass rewrites every long-bracket STRING
# literal back to a standard double-quoted string with escaped contents.
#
# Lua's rule: an opening long bracket is `[` + zero or more `=` + `[`, and it is closed
# by `]` + the same number of `=` + `]`; no escapes are processed in between, and a
# single newline immediately after the opener is dropped. The game's corpus uses only
# the level-0 form `[[ ... ]]` (no `[=[`), but this handles any level for safety.
#
# We only treat `[[`/`[=[` as an opener when it is NOT inside a quoted string or a
# comment, so brackets that appear as ordinary text inside "..." are left untouched.
# A `--[[ ... ]]` block comment opener is likewise skipped (the `--` is consumed by the
# comment branch first), so real long comments are preserved as comments.
_LONG_OPEN = re.compile(r"\[(=*)\[")


def _escape_dq(s):
    out = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


def fix_long_strings(text):
    out = []
    i = 0
    n = 0
    L = len(text)
    while i < L:
        c = text[i]
        # skip quoted strings verbatim
        if c == '"' or c == "'":
            q = c
            j = i + 1
            while j < L:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q:
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            i = j
            continue
        # skip comments: -- line, or --[[ ]] block (consume the -- so the [[ below
        # is never seen as a string opener)
        if c == "-" and text[i : i + 2] == "--":
            m = _LONG_OPEN.match(text, i + 2)
            if m:
                eqs = m.group(1)
                close = "]" + eqs + "]"
                k = text.find(close, m.end())
                k = (k + len(close)) if k != -1 else L
                out.append(text[i:k])
                i = k
                continue
            nl = text.find("\n", i)
            nl = nl if nl != -1 else L
            out.append(text[i:nl])
            i = nl
            continue
        # a genuine long-bracket string literal?
        m = _LONG_OPEN.match(text, i)
        if m:
            eqs = m.group(1)
            close = "]" + eqs + "]"
            k = text.find(close, m.end())
            if k == -1:  # unterminated; leave the rest as-is
                out.append(text[i:])
                i = L
                continue
            body = text[m.end() : k]
            if body.startswith("\n"):  # Lua drops one leading newline after [[
                body = body[1:]
            elif body.startswith("\r\n"):
                body = body[2:]
            out.append('"' + _escape_dq(body) + '"')
            i = k + len(close)
            n += 1
            continue
        out.append(c)
        i += 1
    return "".join(out), n


# pass 7: spurious self-assignment no-ops
# A global updated in place compiles to GETGLOBAL r,'x' / <op> r / SETGLOBAL r,'x'.
# unluac reconstructs the real `x = x <op> ...` line from the op+SETGLOBAL but then
# ALSO emits a bare `x = x` for the same global round-trip -- a redundant no-op. The
# pattern is always a standalone `x = x` statement on the line immediately after a
# full assignment statement to the SAME name, at the SAME indentation, e.g.
#       maxx = maxx + rel_obj.mAnim.mImage:GetCelWidth()
#       maxx = maxx                      <- deleted
# Assigning a name to itself is a pure no-op in Lua (globals and locals alike), so
# dropping the line cannot change behaviour.
#
# The one thing that LOOKS the same but must be kept is a table-constructor field
# `a = a` (as in `return { r = r, g = g, b = b, a = a }`). That is distinguishable
# because the line before a constructor field ends in `,` or `{` (or the field line
# itself ends in `,`), whereas the artifact's predecessor is a complete statement.
# We require the predecessor to be an assignment to the same name that does NOT end
# in `,`, `{`, `(`, or `=`, which excludes every constructor / multi-line context.
_SELF_ASSIGN = re.compile(r"^(\s*)([A-Za-z_][\w.]*)\s*=\s*([A-Za-z_][\w.]*)\s*$")
_ASSIGN_HEAD = re.compile(r"^(\s*)([A-Za-z_][\w.]*)\s*=\s*\S")


def fix_self_assign(text):
    lines = text.split("\n")
    out = []
    n = 0
    for i, ln in enumerate(lines):
        m = _SELF_ASSIGN.match(ln)
        # only a genuine no-op when LHS and RHS are the identical name
        if m and m.group(2) == m.group(3):
            # find the previous non-blank emitted line
            prev = None
            for k in range(len(out) - 1, -1, -1):
                if out[k].strip() != "":
                    prev = out[k]
                    break
            if prev is not None and not prev.rstrip().endswith((",", "{", "(", "=")):
                pm = _ASSIGN_HEAD.match(prev)
                if (
                    pm
                    and pm.group(2) == m.group(2)  # same name updated
                    and pm.group(1) == m.group(1)  # same indentation/block
                    and prev.strip() != ln.strip()
                ):  # not a duplicate real line
                    n += 1
                    continue  # drop the no-op
        out.append(ln)
    return "\n".join(out), n


# pass 8: numeric for-loops (FORPREP/FORLOOP unluac couldn't reconstruct)
# A numeric `for i = a, b, s do ... end` whose body unluac failed to structure is emitted
# as the loop's three hidden control registers plus an immediately-breaking guard block:
#       do
#         do break end -- pseudo-goto
#         local i, _forlimit, _forstep = <a> - <s>, <b>, <s>
#         repeat
#           <body>
#         until true
#       end
# The index is initialised to `a - s` because Lua's FORLOOP adds the step BEFORE the first
# body iteration, so the real start is `a` (we strip the trailing `- <s>`). `_forlimit` is
# the genuine inclusive limit `b`, `_forstep` the step. The `do break end` / `repeat ...
# until true` scaffold is unluac's failed body reconstruction; we verified across the whole
# corpus that no such body contains a top-level `break`, so collapsing the scaffold into a
# real `for` cannot change control flow. Reconstructed as:
#       for i = <a>, <b>, <s> do
#         <body>
#       end
# Only the exact canonical shape (pseudo-goto guard + repeat/until true) is rewritten; any
# variant that doesn't match is left untouched for manual review.
_NUMFOR = re.compile(
    r"^(\s*)local\s+(\w+),\s*_forlimit,\s*_forstep\s*=\s*(.+?),\s*(.+?),\s*(\S+)\s*$"
)


def _strip_step(start_expr, step):
    # unluac prints the start as `<a> - <step>`; reverse it to recover `<a>`.
    suffix = " - " + step
    s = start_expr.rstrip()
    if s.endswith(suffix):
        s = s[: -len(suffix)].rstrip()
    if s == "0 - 1" or s == "0":  # tidy the common `0 - 1` -> 0
        s = "0"
    return s


# Lua's numeric `for` copies its limit and step into hidden registers and ignores any
# reassignment of the loop variable or limit inside the body. The bytecode confirms these
# loops really are numeric `for`s (a FORLOOP opcode closes them), so we reconstruct the
# `for` faithfully and leave the BODY VERBATIM. That matters: a few loops contain in-body
# `i = i - 1` / `_forlimit = _forlimit - 1` lines (the original author's "remove an element,
# step back" idiom) which under numeric-for semantics are actually inert -- they are part
# of the real program and must be preserved exactly, not "helpfully" turned into a while
# loop (which would make them live and change behaviour). We do NOT re-indent or rewrite
# the body; only the guard + control-register declaration + repeat/until-true scaffold is
# replaced by the `for ... do` / `end`.
def _block_balanced(body_lines):
    """True if the body opens and closes the same number of blocks. Rejects bodies that
    unluac left with scrambled control flow (e.g. an `until true` emitted before the
    matching `if ... end`), which must not be wrapped in a `for` -- doing so would yield
    syntactically broken Lua. Uses a keyword-token tally that ignores comments/strings."""
    OPENERS = re.compile(r"\b(function|if|for|while|do|repeat)\b")
    # `then`/`do`/`elseif`/`else` don't add depth beyond their statement's opener except
    # standalone `do`; count opener keywords that introduce an `end`-terminated block.
    depth = 0
    for ln in body_lines:
        s = ln
        # strip line comments and quoted strings crudely (enough for token counting)
        s = re.sub(r"--.*$", "", s)
        s = re.sub(r'"(?:\\.|[^"\\])*"', '""', s)
        s = re.sub(r"'(?:\\.|[^'\\])*'", "''", s)
        for tok in re.findall(r"\b(function|if|for|while|do|repeat|end|until)\b", s):
            if tok in ("function", "if", "for", "while", "do"):
                depth += 1
            elif tok == "repeat":
                depth += 1
            elif tok == "end":
                depth -= 1
            elif tok == "until":
                depth -= 1
            if depth < 0:
                return False
    return depth == 0


def fix_numeric_for(text):
    total = 0
    for _ in range(64):  # iterate so nested loops fully resolve
        lines = text.split("\n")
        out = []
        i = 0
        n = 0
        while i < len(lines):
            m = _NUMFOR.match(lines[i])
            prev = out[-1].strip() if out else ""
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if m and prev == "do break end -- pseudo-goto" and nxt == "repeat":
                var, a_raw, b, s = m.group(2), m.group(3), m.group(4), m.group(5)
                a = _strip_step(a_raw, s)
                depth = 0
                j = i + 2
                until_j = None
                while j < len(lines):
                    st = lines[j].strip()
                    if re.match(r"^(for|while|repeat)\b", st):
                        depth += 1
                    elif st == "until true" and depth == 0:
                        until_j = j
                        break
                    elif st.startswith("until") and depth > 0:
                        depth -= 1
                    j += 1
                if until_j is not None:
                    body = lines[i + 2 : until_j]
                    if _block_balanced(body):
                        indent = m.group(1)
                        out.pop()  # drop the `do break end` guard
                        out.append(
                            indent
                            + "for "
                            + var
                            + " = "
                            + a
                            + ", "
                            + b
                            + ", "
                            + s
                            + " do"
                        )
                        out.extend(body)  # body verbatim (semantics preserved)
                        out.append(indent + "end")
                        i = until_j + 1
                        n += 1
                        continue
            out.append(lines[i])
            i += 1
        text = "\n".join(out)
        total += n
        if n == 0:
            break
    return text, total


def fix_text(text):
    text, n1 = fix_selfref(text)
    text, n2 = fix_floor(text)
    text, n3 = fix_genfor(text)
    text, n4 = fix_setlist_fpf(text)
    text, n5 = fix_constructor_keys(text)
    text, n6 = fix_long_strings(text)
    text, n7 = fix_self_assign(text)
    text, n8 = fix_numeric_for(text)
    return text, n1, n2, n3, n4, n5, n6, n7, n8


def iter_lua(paths):
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    if f.endswith(".lua"):
                        yield os.path.join(root, f)
        elif p.endswith(".lua"):
            yield p


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    nf = nsr = nfl = nfor = nfpf = nkey = nls = nsa = nnf = touched = 0
    for path in iter_lua(argv[1:]):
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        new, n1, n2, n3, n4, n5, n6, n7, n8 = fix_text(text)
        nf += 1
        if n1 or n2 or n3 or n4 or n5 or n6 or n7 or n8:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
            touched += 1
            nsr += n1
            nfl += n2
            nfor += n3
            nfpf += n4
            nkey += n5
            nls += n6
            nsa += n7
            nnf += n8
    print(
        f"scanned {nf} files; fixed {nsr} self-references, {nfl} floor() calls, "
        f"{nfor} generic-for loops, {nnf} numeric-for loops, {nfpf} SETLIST array indices, "
        f"{nkey} constructor keys, {nls} long-bracket strings, and {nsa} self-assignment "
        f"no-ops in {touched} files"
    )
    leftover = sum(
        1
        for p in iter_lua(argv[1:])
        if PLACEHOLDER in open(p, encoding="utf-8", errors="replace").read()
    )
    if leftover:
        print(f"WARNING: {leftover} files still contain an unresolved placeholder")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
