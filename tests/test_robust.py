#!/usr/bin/env python3
"""Robustness checks, all in-process (a tiny local HTTP server and fake `docker`; nothing touches real containers).
  python3 tests/test_robust.py"""
import json, os, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import common, deploy
from common import StackError

PASS = 0
def check(name, cond):
    global PASS
    assert cond, "FAIL: " + name
    PASS += 1; print("  ok  " + name)


def server(script):
    """script: list of behaviours used one per request: 503 | 'reset' | 200"""
    seq = list(script); hits = []
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(1)
            b = seq.pop(0) if len(seq) > 1 else seq[0]
            if b == "reset":
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"); self.connection.close(); return
            self.send_response(b); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        do_POST = do_GET
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, hits

_real = time
class FastTime:                                     # keeps the test quick; only the modules under test see it
    def __getattr__(self, n): return getattr(_real, n)
    def sleep(self, s): _real.sleep(0.01)
common.time = FastTime()

# --- http waits through restarts
srv, hits = server([503, 503, "reset", 200])
r = common.http("GET", f"http://127.0.0.1:{srv.server_port}/x", patience=30)
check("503s and a reset connection are waited out, then the real answer is returned", r.status == 200 and len(hits) == 4)

srv, hits = server([401])
r = common.http("GET", f"http://127.0.0.1:{srv.server_port}/x", patience=30)
check("a real 401 comes back immediately (no pointless waiting)", r.status == 401 and len(hits) == 1)

srv, hits = server([404])
check("a 404 comes back immediately", common.http("GET", f"http://127.0.0.1:{srv.server_port}/x", patience=30).status == 404 and len(hits) == 1)

srv, hits = server([503])
t = time.time(); r = common.http("GET", f"http://127.0.0.1:{srv.server_port}/x", patience=0.3)
check("an app stuck on 503 is eventually reported (503), not waited on forever", r.status == 503 and time.time() - t < 5)

s = socket.socket(); s.bind(("127.0.0.1", 0)); dead = s.getsockname()[1]; s.close()
try: common.http("GET", f"http://127.0.0.1:{dead}/x", patience=0.3); ok = False
except StackError as e: ok = "cannot reach" in str(e)
check("nothing listening -> a clear 'cannot reach' error after the wait", ok)

# an app that is down for a moment and then comes up
port_holder = socket.socket(); port_holder.bind(("127.0.0.1", 0)); port = port_holder.getsockname()[1]; port_holder.close()
def late():
    time.sleep(0.5)
    class H(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    HTTPServer(("127.0.0.1", port), H).handle_request()
threading.Thread(target=late, daemon=True).start()
check("an app that comes back after a restart is reached", common.http("GET", f"http://127.0.0.1:{port}/x", patience=20).status == 200)

# --- leftover containers
IMG = json.load(open(os.path.join(common.KIT, "seed/images.json")))
jf_image = IMG["jellyfin"]["image"]
calls = []
def fake_docker(rows, removed):
    def d(*args, **kw):
        if args[0] == "ps":
            class R: stdout = "\n".join(rows)
            return R
        if args[0] == "rm": removed.append(args[2]); return None
    return d
A = {"stack_dir": "/home/u/media-stack"}
rem = []; deploy.docker = fake_docker([f"abc123|jellyfin|{IMG['jellyfin']['tag']}|/home/u/old-kit-copy/stack"], rem)
deploy.clear_leftover_containers(A)
check("a leftover jellyfin from an earlier attempt (our image, other folder) is removed", rem == ["abc123"])

rem = []; deploy.docker = fake_docker([f"abc123|jellyfin|{jf_image}|{A['stack_dir']}"], rem)
deploy.clear_leftover_containers(A)
check("this stack's own containers are left alone (compose manages them)", rem == [])

rem = []; deploy.docker = fake_docker(["zzz|jellyfin|someone/their-own-media-server:1|/srv/theirs", "q|unrelated|nginx|"], rem)
try: deploy.clear_leftover_containers(A); ok = False
except StackError as e: ok = "docker rm -f" in str(e) and "jellyfin" in str(e) and "nginx" not in str(e)
check("someone else's container with our name is NOT deleted; the message says what to do", ok and rem == [])

# --- image download retries
tries = []
def flaky(*a, **k):
    tries.append(1)
    if len(tries) < 3: raise StackError("docker compose up failed: net/http: TLS handshake timeout")
deploy.compose = flaky; deploy.time = FastTime()
deploy.compose_up_with_retries("/s", ["x"]); check("a flaky image download is retried until it works", len(tries) == 3)

tries.clear()
def broken(*a, **k): tries.append(1); raise StackError("docker compose up failed: invalid compose file: services.x.image must be a string")
deploy.compose = broken
try: deploy.compose_up_with_retries("/s", ["x"]); ok = False
except StackError: ok = True
check("a real error (bad compose file) fails at once, no retries", ok and len(tries) == 1)

# --- SELinux (Fedora/RHEL): containers must not be blocked from their config folders
import collections, render
ans = json.load(open("/dev/stdin")) if False else {"admin_user": "u", "admin_pass": "p", "media_root": "/m", "stack_dir": "/s", "timezone": "UTC",
       "debrid": [{"provider": "realdebrid", "api_key": "k"}], "indexer_accounts": {}, "subtitles": {}, "tmdb_api_key": "", "enable_arabarr": False, "cache": {"path": None}, "host_ip": "10.0.0.5"}
S = collections.defaultdict(lambda: "x")
pins = json.load(open(os.path.join(common.KIT, "seed/images.json")))
on = render.build_compose({**ans, "selinux_enforcing": True}, S, pins, 1000, 1000)["services"]
off = render.build_compose({**ans, "selinux_enforcing": False}, S, pins, 1000, 1000)["services"]
check("SELinux enforcing: every container opts out of confinement", all("label=disable" in d.get("security_opt", []) for d in on.values()))
check("SELinux enforcing: options a service already had (e.g. the FUSE one) are kept",
      all(set(off[n].get("security_opt", [])) <= set(on[n]["security_opt"]) for n in off) and any(len(d.get("security_opt", [])) > 0 for d in off.values()))
check("SELinux not enforcing: nothing is added", not any("label=disable" in d.get("security_opt", []) for d in off.values()))

# --- the firewall note appears only when a firewall is active
import subprocess as _sp
class _R:
    def __init__(self, out): self.stdout = out
_real_run = _sp.run
deploy.subprocess.run = lambda cmd, **k: _R("Status: active" if cmd[0] == "ufw" else "")
check("an active ufw firewall produces a note about other devices", "firewall is on" in deploy.firewall_note())
deploy.subprocess.run = lambda cmd, **k: _R("Status: inactive")
check("no firewall, no note", deploy.firewall_note() == "")
deploy.subprocess.run = _real_run

# --- a stage that fails because an app is still settling is repeated; a fatal problem is not
from common import Fatal
settled = []; runs = []
def stage_fn():
    runs.append(1)
    if len(runs) < 3: raise StackError("prowlarr POST /applications -> 400: cannot connect to Radarr")
    return "done"
check("a failing stage waits for the apps and repeats until it works",
      deploy.resilient("t", stage_fn, lambda: settled.append(1)) == "done" and len(runs) == 3 and len(settled) == 2)
runs.clear(); settled.clear()
def always_fails(): runs.append(1); raise StackError("real bug")
try: deploy.resilient("t", always_fails, lambda: settled.append(1)); ok = False
except StackError: ok = len(runs) == 3
check("a stage that keeps failing is reported after 3 tries", ok)
runs.clear()
def fatal(): runs.append(1); raise Fatal("Jellyfin rejected the login")
try: deploy.resilient("t", fatal, lambda: None); ok = False
except Fatal: ok = len(runs) == 1
check("a fatal problem (wrong existing login) stops at once", ok)

# --- Prowlarr/Sonarr saving a link to another of our apps that is mid-restart
import arr
class FakeResp:
    def __init__(self, status, body): self.status, self.body = status, body; self.ok = 200 <= status < 300
    def json(self): return {"id": 1}
a_ = arr.Arr("prowlarr", "http://x", "k", "v1"); arr.time = FastTime()
seq = [FakeResp(400, '[{"errorMessage": "Unable to complete application test, cannot connect to Radarr. (Connection refused) (radarr:7878)"}]')] * 2 + [FakeResp(201, "{}")]
calls_ = []
a_.req = lambda *x, **k: (calls_.append(1), seq.pop(0))[1]
a_.send("POST", "/applications", {"n": 1})
check("saving a link to a restarting sibling app waits and retries", len(calls_) == 3)
seq = [FakeResp(400, '[{"errorMessage": "Unable to connect to indexer. (Connection refused) (some-tracker.example:443)"}]')]
calls_.clear()
try: a_.send("POST", "/indexer", {}); ok = False
except StackError: ok = len(calls_) == 1
check("a dead third-party indexer is NOT waited on (only our own apps are)", ok)

print(f"\n{PASS} robustness checks passed")
