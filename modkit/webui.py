"""bwa-mod-gui -- a local web UI over modkit.core.

Runs a stdlib HTTP server bound to localhost and opens the page in the browser.
Long actions (init, build) run as background jobs the page polls. No third-party
deps, so it bundles with PyInstaller like the CLI.

  python -m modkit.webui [--port 8765] [--no-browser]
"""

import os
import sys
import json
import uuid
import threading
import webbrowser
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _cand in (
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bwakit"),
):
    if _cand not in sys.path and os.path.isdir(_cand):
        sys.path.insert(0, _cand)
from modkit import core  # noqa: E402
from modkit import debuglog  # noqa: E402

JOBS = {}  # job id -> {state, log, result, error}


def static_dir():
    return os.path.join(core.base_dir(), "modkit", "static")


def _start_job(fn):
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"state": "running", "log": [], "result": None, "error": None}

    def run():
        try:
            res = fn(lambda m: JOBS[jid]["log"].append(m))
            JOBS[jid].update(state="done", result=res)
        except BaseException as e:  # noqa: BLE001 - report ANY failure (incl. SystemExit) to the UI
            debuglog.record(f"web job {jid} failed")
            JOBS[jid].update(
                state="error",
                error=f"{e}\n(full traceback saved to {debuglog.PATH} -- send that file if you need help)",
            )

    threading.Thread(target=run, daemon=True).start()
    return jid


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            try:
                return self._html(
                    200, open(os.path.join(static_dir(), "index.html"), "rb").read()
                )
            except FileNotFoundError:
                return self._html(500, b"<h1>index.html missing</h1>")
        if p == "/api/status":
            if core.is_configured():
                st = core.load_state()
                return self._json(
                    200,
                    {
                        "configured": True,
                        "modded": st["modded_dir"],
                        "exe": st.get("exe"),
                        "template_sha256": st["template_sha256"],
                        "installed": st["installed"],
                        "luac": bool(core.luac()),
                    },
                )
            return self._json(200, {"configured": False, "luac": bool(core.luac())})
        if p == "/api/mods":
            game = core.current_title()  # None until an install is configured
            mods = [
                {
                    "id": m["id"],
                    "name": m["name"],
                    "description": m.get("description", "").strip(),
                    "kind": m["_kind"],
                    "apply_order": m.get("apply_order", 100),
                    "requires": m.get("requires", []),
                    "conflicts": m.get("conflicts", []),
                    "games": m.get("games", ["ba1"]),
                    "param_spec": m.get("param_spec", []),
                    "compat_note": m.get("compat_note", ""),
                }
                for m in core.catalog(game)
            ]
            label = {
                "ba1": "Bookworm Adventures Deluxe",
                "ba2": "Bookworm Adventures Volume 2",
            }.get(game)
            return self._json(200, {"mods": mods, "game": game, "game_label": label})
        if p == "/api/plan":
            ids = [x for x in parse_qs(u.query).get("ids", [""])[0].split(",") if x]
            return self._json(200, core.plan(ids))
        if p.startswith("/api/job/"):
            j = JOBS.get(p.rsplit("/", 1)[-1])
            return (
                self._json(200, j) if j else self._json(404, {"error": "no such job"})
            )
        return self._json(404, {"error": "not found"})

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        p = urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}
        if p == "/api/init":
            jid = _start_job(
                lambda log: core.init_install(
                    body.get("game", ""),
                    body.get("modded") or None,
                    body.get("originals") or None,
                    bool(body.get("no_rename")),
                    bool(body.get("force")),
                    log=log,
                )
            )
            return self._json(200, {"job": jid})
        if p == "/api/build":
            ids = body.get("ids", [])
            ov = body.get("overrides", {})
            return self._json(
                200,
                {"job": _start_job(lambda log: core.build(ids, overrides=ov, log=log))},
            )
        if p == "/api/launch":
            try:
                return self._json(200, core.launch())
            except Exception as e:
                return self._json(400, {"error": str(e)})
        if p == "/api/restore":
            try:
                return self._json(200, core.restore())
            except Exception as e:
                return self._json(400, {"error": str(e)})
        return self._json(404, {"error": "not found"})


def serve(host="127.0.0.1", port=8765, open_browser=True):
    debuglog.install("web UI")
    url = "http://%s:%d/" % (host, port)
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError:
        # Port busy: almost certainly another Mod Builder is already running here. Point the
        # browser at it and exit, instead of quietly starting a second copy on a random port --
        # two windows fighting over one install is the footgun we're avoiding. For a deliberate
        # second instance (e.g. a different game copy), pass --port.
        print(
            "Mod Builder already appears to be running at %s -- opening that.\n"
            "(If something else is using port %d, close it or pass --port.)"
            % (url, port)
        )
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return
    print("bwa-mod web UI -> %s   (Ctrl-C to stop)" % url)
    print("debug log: %s" % debuglog.PATH)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="bwa-mod-gui")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    serve(port=a.port, open_browser=not a.no_browser)


if __name__ == "__main__":
    main()
