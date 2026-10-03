#!/usr/bin/env python3
"""Local web installer: a browser form instead of the terminal wizard.

  python3 lib/webui.py [--state-dir DIR] [--no-browser] [--host 127.0.0.1] [--port 0] [--reconfigure]

Design / safety
  * Python standard library only (no extra packages).
  * Listens on 127.0.0.1 by default, on a random port, and every request needs a one-time token (in the URL the
    installer prints and opens). Host headers are checked, so a web page elsewhere can't drive it.
  * The page collects the same answers as the terminal wizard, saves them to <state>/answers.json (0600), then runs the
    normal unattended installer (lib/deploy.py) as a child process and streams its progress to the page.
  * Nothing here knows how to install anything; deploy.py does. STACK_UI_DEPLOY_CMD can swap that command (tests).
"""
import argparse, hmac, json, os, re, secrets, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardware, render, wizard
from common import KIT, load_json, save_json

LIB = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(LIB, "web")
USER_RE = re.compile(r"[A-Za-z0-9._-]{3,32}")
NAME_RE = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
MAX_BODY = 1 << 20


class Install:
    """One install run: the child process, its output, and a coarse status."""
    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.lines = []          # [{"n": int, "t": "ok|fail|warn|info|head|raw", "text": str}]
        self.status = "idle"     # idle | running | done | failed
        self.code = None
        self.answers = None

    @staticmethod
    def classify(text):
        t = text.lstrip()
        if t.startswith("=="):
            return "head", t.lstrip("= ").strip()
        for mark, kind in (("✔", "ok"), ("✘", "fail"), ("!", "warn"), ("•", "info")):
            if t.startswith(mark):
                return kind, t[1:].strip()
        return "raw", text.rstrip()

    def add(self, raw):
        text = ANSI.sub("", raw.rstrip("\n"))
        if not text.strip():
            return
        kind, body = self.classify(text)
        with self.lock:
            self.lines.append({"n": len(self.lines), "t": kind, "text": body})

    def start(self, cmd, env):
        with self.lock:
            if self.status == "running":
                return False
            self.status, self.code, self.lines = "running", None, []
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                         env=env, cwd=KIT)
        except OSError as e:
            self.add(f"✘ could not start the installer: {e}")
            with self.lock:
                self.status, self.code = "failed", -1
            return True
        threading.Thread(target=self._pump, daemon=True).start()
        return True

    def _pump(self):
        for line in self.proc.stdout:
            self.add(line)
        code = self.proc.wait()
        with self.lock:
            self.code = code
            self.status = "done" if code == 0 else "failed"

    def snapshot(self, since):
        with self.lock:
            return {"status": self.status, "code": self.code, "lines": self.lines[since:], "total": len(self.lines)}


class App:
    def __init__(self, state_dir, token, reconfigure=False):
        self.state_dir = state_dir
        self.answers_path = os.path.join(state_dir, "answers.json")
        self.token = token
        self.reconfigure = reconfigure
        self.install = Install()
        self.hw = None
        self.allowed_hosts = set()
        self.shutdown_cb = None
        self.deploy_cmd_override = os.environ.get("STACK_UI_DEPLOY_CMD")

    def hardware(self):
        if self.hw is None:
            self.hw = hardware.detect()
        return self.hw

    def deploy_cmd(self):
        if self.deploy_cmd_override:
            return json.loads(self.deploy_cmd_override)
        cmd = [sys.executable, os.path.join(LIB, "deploy.py"), "--answers", self.answers_path,
               "--state-dir", self.state_dir]
        return cmd + (["--reconfigure"] if self.reconfigure else [])


# ------------------------------------------------------------------ pure helpers (unit-tested)
def free_gb(path):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1024 ** 3
    except OSError:
        return 0.0


def browse(path):
    path = os.path.abspath(os.path.expanduser(path or "~"))
    while not os.path.isdir(path):
        path = os.path.dirname(path) or "/"
    try:
        names = sorted(d for d in os.listdir(path)
                       if not d.startswith(".") and os.path.isdir(os.path.join(path, d))
                       and os.access(os.path.join(path, d), os.R_OK))
    except OSError:
        names = []
    parent = os.path.dirname(path) if path != "/" else None
    return {"path": path, "parent": parent, "free_gb": round(free_gb(path), 1), "writable": os.access(path, os.W_OK),
            "dirs": names[:500], "truncated": len(names) > 500}


def make_folder(parent, name):
    parent = os.path.abspath(os.path.expanduser(parent))
    if not NAME_RE.fullmatch(name or "") or name in (".", ".."):
        raise ValueError("Use letters, numbers, spaces, dot, dash or underscore (max 64).")
    if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
        raise ValueError("You can't create folders here.")
    path = os.path.join(parent, name)
    os.makedirs(path, exist_ok=True)
    return path


def build_answers(p, hw):
    """Validate the form payload and turn it into the answers.json shape the installer already uses."""
    errs = []
    a = {"hw": hw}
    user, pw = (p.get("admin_user") or "").strip(), p.get("admin_pass") or ""
    if not USER_RE.fullmatch(user):
        errs.append("Username: 3-32 characters, letters, numbers, dot, dash or underscore.")
    if len(pw) < 8:
        errs.append("Password: use at least 8 characters.")
    a["admin_user"], a["admin_pass"] = user, pw

    lib = p.get("library") or {}
    base, name = os.path.abspath(os.path.expanduser(lib.get("base") or "")), (lib.get("name") or "").strip()
    if not lib.get("base") or not os.path.isdir(base):
        errs.append("Library: choose a folder for it.")
    elif not os.access(base, os.W_OK):
        errs.append(f"Library: you can't write to {base}.")
    if not NAME_RE.fullmatch(name):
        errs.append("Library: the folder name may use letters, numbers, spaces, dot, dash, underscore.")
    a["media_root"] = os.path.join(base, name)
    a["stack_dir"] = os.path.expanduser("~/media-stack")
    a["timezone"] = wizard._guess_tz()
    a["host_ip"] = hw["lan_ip"]

    keys, seen = [], set()
    for d in p.get("debrid") or []:
        prov, key = d.get("provider"), (d.get("api_key") or "").strip()
        if prov not in wizard.DEBRID:
            errs.append("Debrid: unknown service."); continue
        if key and (prov, key) not in seen:
            seen.add((prov, key)); keys.append({"provider": prov, "api_key": key})
    if not keys:
        errs.append("Debrid: add at least one key (Real-Debrid, AllDebrid or TorBox).")
    a["debrid"] = keys

    idx = {}
    asrc = p.get("arabicsource") or {}
    if asrc.get("on"):
        if not (asrc.get("apikey") or "").strip():
            errs.append("ArabicSource: enter the API key, or turn it off.")
        else:
            idx["arabicsource"] = {"apikey": asrc["apikey"].strip()}
    ar = p.get("arabic") or {}
    a["enable_arabarr"], a["tmdb_api_key"] = False, ""
    if ar.get("on"):
        if not (ar.get("username") or "").strip() or not ar.get("password") or not (ar.get("tmdb") or "").strip():
            errs.append("Arabic series: needs the ArabP2P username + password and a TMDB API key, or turn it off.")
        else:
            idx["arabp2p"] = {"username": ar["username"].strip(), "password": ar["password"]}
            a["tmdb_api_key"], a["enable_arabarr"] = ar["tmdb"].strip(), True
    a["indexer_accounts"] = idx

    subs, sp = {}, p.get("subs") or {}
    spec = {"opensubtitlescom": (("username", "username"), ("password", "password")),
            "subsource": (("apikey", "apikey"),), "subdl": (("api_key", "api_key"),)}
    names = {"opensubtitlescom": "OpenSubtitles", "subsource": "Subsource", "subdl": "SubDL"}
    for prov, fields in spec.items():
        s = sp.get(prov) or {}
        if not s.get("on"):
            continue
        vals = {dst: (s.get(src) or "").strip() for src, dst in fields}
        if not all(vals.values()):
            errs.append(f"{names[prov]}: fill in the details, or turn it off.")
        else:
            subs[prov] = vals
    a["subtitles"] = subs

    cache = p.get("cache") or {}
    a["cache"] = {"path": None}
    if cache.get("dir"):
        cdir = os.path.abspath(os.path.expanduser(cache["dir"]))
        try:
            size = int(cache.get("size_gb"))
        except (TypeError, ValueError):
            size = 0
        if not os.path.isdir(cdir) or not os.access(cdir, os.W_OK):
            errs.append("Cache: that drive isn't writable.")
        elif size < 5 or size > free_gb(cdir):
            errs.append(f"Cache: size must be between 5 and {int(free_gb(cdir))} GB.")
        else:
            a["cache"] = {"path": os.path.join(cdir, "media-stack-cache"), "size_gb": size}
    return a, errs


def summary_for(a):
    ports = {**render.DEFAULT_PORTS, **a.get("ports", {})}
    h = a["host_ip"]
    rows = [("Homepage", "homepage", "Start here: links to everything"), ("Jellyfin", "jellyfin", "Watch"),
            ("Jellyseerr", "jellyseerr", "Request shows and movies"), ("Sonarr", "sonarr", "TV"),
            ("Radarr", "radarr", "Movies"), ("Prowlarr", "prowlarr", "Indexers"), ("Bazarr", "bazarr", "Subtitles"),
            ("Decypharr", "decypharr", "Debrid")]
    notes = ["Open Jellyseerr, request a show or movie, and watch it appear in Jellyfin."]
    if not a.get("subtitles"):
        notes.append("You skipped subtitles: Bazarr is connected to Sonarr and Radarr but has no providers yet.")
    if not a.get("enable_arabarr"):
        notes.append("Arabic series (Arabarr) isn't installed. Run the installer again any time to add it.")
    notes.append("Playback is direct-play by default. If a device can't play a file, allow transcoding for that user in "
                 "Jellyfin (Dashboard → Users → Playback).")
    return {"user": a["admin_user"], "urls": [{"name": n, "url": f"http://{h}:{ports[k]}", "desc": d} for n, k, d in rows],
            "notes": notes}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "StackInstaller"
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):          # never log request lines: they may carry tokens
        pass

    @property
    def app(self):
        return self.server.app

    # ---- plumbing
    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, text, code=200):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in self.app.allowed_hosts

    def token_ok(self, supplied):
        return bool(supplied) and hmac.compare_digest(str(supplied).encode("utf-8", "replace"), self.app.token.encode())

    def read_body(self):
        """Always consume the request body first: leaving it unread would corrupt the next request on a kept-alive
        connection when we answer with an early error (bad token, wrong content type...)."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            self.close_connection = True
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self.host_ok():
            return self.send_json({"error": "bad host"}, 403)
        if u.path == "/":
            if not self.token_ok(q.get("t")):
                return self.send_html("<h1>Forbidden</h1><p>Open the exact link the installer printed in your terminal.</p>", 403)
            html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read().replace("__TOKEN__", self.app.token)
            return self.send_html(html)
        if not u.path.startswith("/api/"):
            return self.send_json({"error": "not found"}, 404)
        if not self.token_ok(self.headers.get("X-Token")):
            return self.send_json({"error": "forbidden"}, 403)
        try:
            return self.api_get(u.path, q)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def api_get(self, path, q):
        app = self.app
        if path == "/api/init":
            hw = app.hardware()
            return self.send_json({"hw": hw, "drives": hardware.drive_options(), "timezone": wizard._guess_tz(),
                                   "home": os.path.expanduser("~"), "saved": os.path.exists(app.answers_path),
                                   "install": app.install.status,
                                   "debrid": {k: {"label": v["label"], "url": v["url"]} for k, v in wizard.DEBRID.items()}})
        if path == "/api/browse":
            return self.send_json(browse(q.get("path")))
        if path == "/api/cache":
            c = [x for x in hardware.cache_candidates() if x["suitable"]]
            c.sort(key=lambda x: -(x["iops"] or 0))
            for x in c:
                x["suggested_gb"] = hardware.suggested_cache_gb(x["free_gb"])
            return self.send_json({"candidates": c, "min_free_gb": hardware.MIN_CACHE_FREE_GB})
        if path == "/api/saved":
            return self.send_json(load_json(app.answers_path, {}))
        if path == "/api/progress":
            snap = app.install.snapshot(int(q.get("since") or 0))
            if snap["status"] == "done" and app.install.answers:
                snap["summary"] = summary_for(app.install.answers)
            return self.send_json(snap)
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            raw = self.read_body()
        except ValueError as e:
            return self.send_json({"error": str(e)}, 413)
        if not self.host_ok():
            return self.send_json({"error": "bad host"}, 403)
        if not self.token_ok(self.headers.get("X-Token")):
            return self.send_json({"error": "forbidden"}, 403)
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self.send_json({"error": "json only"}, 415)
        try:
            p = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(p, dict):
                raise ValueError("expected a JSON object")
            return self.api_post(u.path, p)
        except json.JSONDecodeError:
            return self.send_json({"error": "invalid JSON"}, 400)
        except ValueError as e:
            return self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def api_post(self, path, p):
        app = self.app
        if path == "/api/mkdir":
            return self.send_json({"path": make_folder(p.get("parent"), p.get("name"))})
        if path == "/api/start":
            a, errs = build_answers(p, app.hardware())
            if errs:
                return self.send_json({"errors": errs}, 422)
            a = wizard.validate_answers(a)
            os.makedirs(app.state_dir, exist_ok=True)
            save_json(app.answers_path, a)
            app.install.answers = a
            env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            started = app.install.start(app.deploy_cmd(), env)
            return self.send_json({"ok": True, "started": started})
        if path == "/api/retry":
            a = load_json(app.answers_path)
            if not a:
                return self.send_json({"error": "no saved answers"}, 400)
            app.install.answers = a
            started = app.install.start(app.deploy_cmd(), {**os.environ, "PYTHONUNBUFFERED": "1"})
            return self.send_json({"ok": True, "started": started})
        if path == "/api/shutdown":
            self.send_json({"ok": True})
            if app.shutdown_cb:
                threading.Thread(target=app.shutdown_cb, daemon=True).start()
            return
        return self.send_json({"error": "not found"}, 404)


# ------------------------------------------------------------------ main
def make_server(app, host="127.0.0.1", port=0):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app
    p = srv.server_address[1]
    app.allowed_hosts = {f"127.0.0.1:{p}", f"localhost:{p}"}
    if host not in ("127.0.0.1", "localhost"):
        app.allowed_hosts |= {f"{hardware.lan_ip()}:{p}", f"{host}:{p}"}
    app.shutdown_cb = srv.shutdown
    return srv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=os.path.join(KIT, "state"))
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to reach it from another computer (token still required)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--reconfigure", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.state_dir, exist_ok=True)
    app = App(a.state_dir, secrets.token_urlsafe(18), a.reconfigure)
    srv = make_server(app, a.host, a.port)
    port = srv.server_address[1]
    shown = "127.0.0.1" if a.host in ("127.0.0.1", "localhost") else hardware.lan_ip()
    url = f"http://{shown}:{port}/?t={app.token}"
    print("\nInstaller is ready. Open this link in your browser:\n\n    " + url + "\n")
    if a.host not in ("127.0.0.1", "localhost"):
        print("  (It is reachable from your network. Only share this link with yourself.)\n")
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print("Leave this terminal open until the installer finishes. Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if app.install.status == "running":
            print("\nStopped while installing. Re-run the installer: it is safe to continue where it left off.")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
