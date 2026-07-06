"""modkit.core - the programmatic API behind both the CLI and the web UI.

Pure logic: functions return data or raise, with an optional `log` callback; nothing
prints or exits. client.py and webui.py are thin layers over this and share one state
format, so a CLI-built install opens in the GUI and vice versa.
"""

import os
import io
import sys
import json
import shutil
import contextlib
import subprocess
import tempfile
import hashlib

from . import build as MB
from . import debuglog

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # bwa-mod-client/ (dev tree)
USER_CFG = os.path.join(os.path.expanduser("~"), ".bwa-mod.json")
BWAMOD = ".bwamod"  # per-install data dir inside the modded copy


def base_dir():
    """Root for bundled resources: PyInstaller's extraction dir when frozen, else
    the dev tree. `mods/` and `modkit/static/` sit directly under it in both."""
    return getattr(sys, "_MEIPASS", None) or REPO


def catalog_dir():
    return os.path.join(base_dir(), "mods")


def luac():
    if os.environ.get("BWA_LUAC"):
        return os.environ["BWA_LUAC"]
    for name in ("luac.exe", "luac"):  # bundled alongside resources
        cand = os.path.join(base_dir(), name)
        if os.path.exists(cand):
            return cand
    return shutil.which("luac")


def _list_exes(d):
    return [f for f in os.listdir(d) if f.lower().endswith(".exe")]


def _is_game_exe(f):
    return "bookworm" in f.lower() or "ba1" in f.lower()


def find_exe(d):
    exes = _list_exes(d)
    for f in exes:
        if _is_game_exe(f):
            return f
    return exes[0] if exes else None


def mod_kind(m):
    if m.get("builder"):
        return "builder"
    if any(t.get("op") == "replace_file" for t in m.get("transforms", [])):
        return "file-replace"
    return "code-inject"


def catalog(game=None):
    out = []
    cd = catalog_dir()
    for d in sorted(os.listdir(cd)):
        mj = os.path.join(cd, d, "mod.json")
        if os.path.exists(mj):
            m = json.load(open(mj))
            if game is not None and game not in MB.mod_games(m):
                continue
            m["_dir"] = os.path.join(cd, d)
            m["_kind"] = mod_kind(m)
            out.append(m)
    return out


def plan(ids):
    """Preview a selection without building: resolved apply order, hard issues
    (requires/conflicts), and semantic compat notes. The authoritative file-level
    conflict check happens in build()."""
    cat = {m["id"]: m for m in catalog()}
    sel = [cat[i] for i in ids if i in cat]
    sset = set(ids)
    issues = []
    for m in sel:
        for r in m.get("requires", []):
            if r not in sset:
                issues.append("%s requires %s (not selected)" % (m["id"], r))
        for c in m.get("conflicts", []):
            if c in sset:
                issues.append("%s conflicts with %s" % (m["id"], c))
    order = [
        m["id"] for m in sorted(sel, key=lambda m: (m.get("apply_order", 100), m["id"]))
    ]
    compat = [
        {"id": m["id"], "note": m["compat_note"]} for m in sel if m.get("compat_note")
    ]
    return {"order": order, "issues": issues, "compat": compat}


def is_configured():
    if not os.path.exists(USER_CFG):
        return False
    try:
        modded = json.load(open(USER_CFG))["modded_dir"]
        return os.path.exists(os.path.join(modded, BWAMOD, "state.json"))
    except Exception:
        return False


def load_state():
    if not is_configured():
        raise RuntimeError("No install configured. Run init / set up first.")
    modded = json.load(open(USER_CFG))["modded_dir"]
    return json.load(open(os.path.join(modded, BWAMOD, "state.json")))


def current_title():
    """The detected game id ('ba1'/'ba2') of the active install, or None if not configured."""
    try:
        return load_state().get("title", "ba1")
    except Exception:
        return None


def save_state(st):
    open(os.path.join(st["modded_dir"], BWAMOD, "state.json"), "w").write(
        json.dumps(st, indent=2)
    )
    json.dump({"modded_dir": st["modded_dir"]}, open(USER_CFG, "w"))


@contextlib.contextmanager
def _install_lock(key_path, what="build"):
    """Exclusive, cross-process lock so two Mod Builder processes can't work on the same
    install at once -- the footgun where two windows both extract/repack into one folder
    and deadlock over the files. It's an OS advisory lock in the temp dir, keyed by the
    install path. It lives OUTSIDE the modded folder on purpose: init wipes that folder,
    and Windows refuses to delete a file whose lock is held. The OS drops the lock
    automatically if a process crashes, so it can never get stuck stale. Holder PID is
    written for the message."""
    digest = hashlib.sha256(os.path.abspath(key_path).encode()).hexdigest()[:16]
    key = os.path.join(tempfile.gettempdir(), f"bwamod-{digest}.lock")
    f = open(key, "a+")
    try:
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            holder = f.read().strip() or "another process"
            raise RuntimeError(
                f"This install is already being worked on by {holder} (a {what} is in "
                "progress). Close the extra Mod Builder window and try again."
            )
        f.seek(0)
        f.truncate()
        f.write(f"PID {os.getpid()}")
        f.flush()
        yield
    finally:
        try:
            if os.name == "nt":
                f.seek(0)
                import msvcrt

                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            f.close()
        except Exception:  # noqa: BLE001 - releasing the lock must never raise
            pass


def _is_protected(path):
    """True if `path` sits inside a Windows system / Program Files tree (writing there
    needs admin rights, or gets silently redirected to VirtualStore)."""
    p = os.path.abspath(path).lower()
    roots = []
    for var in (
        "ProgramFiles",
        "ProgramFiles(x86)",
        "ProgramW6432",
        "SystemRoot",
        "windir",
    ):
        v = os.environ.get(var)
        if v:
            roots.append(os.path.abspath(v).lower())
    roots += [r"c:\program files", r"c:\program files (x86)", r"c:\windows"]
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _writable_dir(path):
    """Can we create a file under `path`? Walks up to the nearest folder that already
    exists, since `path` itself may not have been created yet."""
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return False
        probe = parent
    test = os.path.join(probe, ".bwamod_write_test_%d" % os.getpid())
    try:
        with open(test, "w"):
            pass
        os.remove(test)
        return True
    except OSError:
        return False


def _clean_path(s):
    """Tidy a user-pasted path: trim whitespace, strip surrounding quotes (Windows
    Explorer's / PowerShell's 'Copy as path' wraps it in double quotes), and expand ~
    and environment variables (%USERPROFILE%, $HOME, ...)."""
    if not s:
        return s
    s = str(s).strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return os.path.expandvars(os.path.expanduser(s))


def init_install(
    game,
    modded=None,
    originals=None,
    no_rename=False,
    force=False,
    exe=None,
    keep_extra_exes=False,
    log=None,
):
    """Duplicate the install, back up + hash the template, cache unpacked originals,
    settle on a single game executable, and persist state. Returns the state dict.

    The duplicate is a verbatim copy, so any extra .exe in the source install (e.g. a
    DRM-carved trial alongside the retail exe) is copied too. We rename the game exe to
    "<name> Modded.exe" and, when it's unambiguous which exe is the game, drop the other
    .exe copies from the (disposable) duplicate so the folder has exactly one. Pass `exe`
    to pin a specific one, or keep_extra_exes=True to leave them all."""
    log = log or (lambda m: None)
    game = os.path.abspath(_clean_path(game))
    if not os.path.exists(os.path.join(game, "main.pak")):
        raise RuntimeError("No main.pak in %s -- point at the game folder." % game)
    base = os.path.basename(game.rstrip("/\\"))
    if modded:
        modded = os.path.abspath(_clean_path(modded))
    else:
        modded = os.path.join(os.path.dirname(game.rstrip("/\\")), base + " Modded")
        # A game under Program Files (x86) would put the modded copy there too, where
        # writing needs admin rights (or gets silently redirected to VirtualStore). Fall
        # back to the user's home folder, which is always writable. state.json still gets
        # found afterwards because its location is recorded in USER_CFG (~/.bwa-mod.json).
        if _is_protected(modded) or not _writable_dir(os.path.dirname(modded)):
            modded = os.path.join(os.path.expanduser("~"), base + " Modded")
            log(
                "Game is in a protected location; putting the modded copy in your "
                "home folder instead: %s" % modded
            )
    if not _writable_dir(os.path.dirname(modded) or "."):
        raise RuntimeError(
            "Can't write the modded copy to %s (no permission). Choose a different "
            "location with --modded (e.g. your Desktop or Documents)." % modded
        )

    with _install_lock(modded, "setup"):
        if os.path.exists(modded):
            if not force:
                raise RuntimeError(
                    "%s already exists (enable overwrite to recreate)." % modded
                )
            shutil.rmtree(modded)
        log("Duplicating install -> %s" % modded)
        shutil.copytree(game, modded)

        data = os.path.join(modded, BWAMOD)
        os.makedirs(data, exist_ok=True)
        template = os.path.join(data, "template.pak")
        shutil.copy2(os.path.join(modded, "main.pak"), template)
        digest = MB.sha256(template)

        originals = (
            os.path.abspath(_clean_path(originals))
            if originals
            else os.path.join(data, "originals")
        )
        if not os.path.exists(os.path.join(originals, "data", "compressed.txt")):
            log("Unpacking template (one-time, this can take a minute)...")
            from bwakit import popcap_pak as P

            P.extract(template, originals)

    exes = _list_exes(modded)
    games = [f for f in exes if _is_game_exe(f)]
    chosen = (
        exe
        if (exe and exe in exes)
        else (games[0] if games else (exes[0] if exes else None))
    )
    if len(exes) > 1:
        log(
            "Found %d executables (%s); using %s."
            % (len(exes), ", ".join(exes), chosen)
        )
    if chosen and not no_rename:
        new_exe = base + " Modded.exe"
        if chosen != new_exe:
            os.replace(os.path.join(modded, chosen), os.path.join(modded, new_exe))
        chosen = new_exe
    others = [f for f in _list_exes(modded) if f != chosen]
    if others and not keep_extra_exes and (exe or len(games) <= 1):
        for f in others:
            os.remove(os.path.join(modded, f))
        log("Removed redundant executable(s) from the copy: %s" % ", ".join(others))
    elif others and not keep_extra_exes:
        log(
            "Left %d other executable(s) in place (%s) -- can't tell which one runs; "
            "delete the DRM-locked one (keep the carved/trial one), or re-init with "
            "exe=<name> to pin it." % (len(others), ", ".join(others))
        )
    exe = chosen

    st = {
        "game_dir": game,
        "modded_dir": modded,
        "exe": exe,
        "template": template,
        "template_sha256": digest,
        "originals": originals,
        "title": MB.detect_game(originals),
        "installed": [],
    }
    save_state(st)
    log("Ready (template %s...)." % digest[:16])
    return st


def _coerce_overrides(ids, overrides):
    """Keep only override keys declared in each mod's param_spec, coerced to the
    declared type. Protects builder.build() from unexpected kwargs / bad types."""
    cat = {m["id"]: m for m in catalog()}
    clean = {}
    for mid, vals in (overrides or {}).items():
        if mid not in ids:
            continue
        spec = {p["key"]: p for p in cat.get(mid, {}).get("param_spec", [])}
        out = {}
        for k, v in (vals or {}).items():
            if k not in spec:
                continue
            t = spec[k].get("type", "text")
            try:
                if t == "bool":
                    v = (
                        v.strip().lower() in ("1", "true", "yes", "on")
                        if isinstance(v, str)
                        else bool(v)
                    )
                elif t == "int":
                    v = int(v)
                elif t == "float":
                    v = float(v)
                else:
                    v = str(v)
            except (ValueError, TypeError):
                continue
            out[k] = v
        if out:
            clean[mid] = out
    return clean


def build(ids, overrides=None, log=None):
    """Compose the selected mods into the modded main.pak. `overrides` maps mod id ->
    {param: value} (validated against each mod's param_spec). Returns {out, order,
    files}. Raises RuntimeError on unknown mods / conflicts."""
    log = log or (lambda m: None)
    st = load_state()
    game = st.get("title", "ba1")
    known = {m["id"] for m in catalog(game)}
    bad = [i for i in ids if i not in known]
    if bad:
        raise RuntimeError(
            "Unknown mod(s) for this game (%s): %s" % (game, ", ".join(bad))
        )
    if not ids:
        raise RuntimeError("Select at least one mod.")
    overrides = _coerce_overrides(ids, overrides)
    mod_dirs = [os.path.join(catalog_dir(), i) for i in ids]
    out = os.path.join(st["modded_dir"], "main.pak")
    log("Verifying template and composing: %s" % ", ".join(ids))
    sink = io.StringIO()  # swallow builders' chatty stdout
    try:
        with _install_lock(st["modded_dir"], "build"), contextlib.redirect_stdout(sink):
            r = MB.build(
                st["template"],
                st["originals"],
                mod_dirs,
                out,
                luac=luac(),
                known_hashes=[st["template_sha256"]],
                work_dir=os.path.join(st["modded_dir"], BWAMOD, "stage"),
                overrides=overrides,
                game=game,
            )
    except Exception as e:
        debuglog.record(f"build failed: {','.join(ids)} | set={overrides}")
        tail = "\n".join(sink.getvalue().splitlines()[-4:])
        raise RuntimeError(str(e) + (("\n" + tail) if tail.strip() else ""))
    st["installed"] = r["mods"]
    save_state(st)
    log("Built %s" % out)
    log("Files changed: %d   order: %s" % (len(r["files"]), " -> ".join(r["mods"])))
    debuglog.note(
        f"build ok: {','.join(ids)} | set={overrides} -> {out} ({len(r['files'])} files)"
    )
    return {"out": out, "order": r["mods"], "files": len(r["files"])}


def launch():
    st = load_state()
    exe = st.get("exe")
    if not exe:
        raise RuntimeError("No executable recorded for this install.")
    path = os.path.join(st["modded_dir"], exe)
    if sys.platform.startswith("win"):
        # Run from the game folder: BASS-based games load bass.dll (and other resources)
        # relative to the current directory, so launching with the builder's cwd fails with
        # "Can't find bass.dll" even though it sits next to the exe. os.startfile can't set
        # cwd on older Pythons, so use Popen; fall back to os.startfile if that's refused
        # (e.g. an exe that demands elevation).
        try:
            subprocess.Popen([path], cwd=st["modded_dir"])
        except OSError:
            os.startfile(path)  # noqa
        return {"path": path, "launched": True}
    if shutil.which("wine"):
        subprocess.Popen(["wine", path], cwd=st["modded_dir"])
        return {"path": path, "launched": True}
    return {"path": path, "launched": False}


def restore():
    st = load_state()
    shutil.copy2(st["template"], os.path.join(st["modded_dir"], "main.pak"))
    st["installed"] = []
    save_state(st)
    return {"restored": True}
