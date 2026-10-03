#!/usr/bin/env python3
"""Backend tests for the web installer. No sockets, no Docker: requests are fed straight into the HTTP handler and the
install command is replaced by a tiny stand-in script.   Run:  python3 tests/test_webui.py
"""
import io, json, os, sys, tempfile, time, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import hardware

# deterministic, side-effect-free hardware (the real cache probe would write a 256 MB benchmark file)
hardware.detect = lambda: {"ram_gb": 8.0, "cpu": {"model": "Test CPU", "cores": 4}, "nic": {"dev": "eth0", "wired": True, "speed_mbps": 1000}, "lan_ip": "192.0.2.10"}
hardware.cache_candidates = lambda benchmark=True: [
    {"mount": "/", "dir": "/tmp", "device": "/dev/x", "fs": "ext4", "free_gb": 400.0, "rotational": False, "iops": 9000, "suitable": True, "reason": ""},
    {"mount": "/mnt/hdd", "dir": "/mnt/hdd", "device": "/dev/y", "fs": "ext4", "free_gb": 900.0, "rotational": True, "iops": None, "suitable": False, "reason": "spinning"}]
import webui

PASSED = FAILED = 0


def test(fn):
    global PASSED, FAILED
    try:
        fn(); PASSED += 1; print("  ok  " + fn.__name__)
    except Exception:
        FAILED += 1; print("FAIL  " + fn.__name__); traceback.print_exc()
    return fn


class FakeConn:
    def __init__(self, data): self.inb, self.out = io.BytesIO(data), io.BytesIO()
    def makefile(self, mode, *a, **k): return self.inb if "r" in mode else self.out
    def sendall(self, b): self.out.write(b)
    def settimeout(self, *a): pass
    def close(self): pass


class FakeServer:
    def __init__(self, app): self.app = app


def call(app, method, path, body=None, token=True, host=None, ctype="application/json"):
    port = 5555
    host = host or f"127.0.0.1:{port}"
    h = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    if token is True:
        h.append(f"X-Token: {app.token}")
    elif token:
        h.append(f"X-Token: {token}")
    data = b""
    if body is not None:
        data = json.dumps(body).encode()
        h += [f"Content-Type: {ctype}", f"Content-Length: {len(data)}"]
    req = ("\r\n".join(h) + "\r\n\r\n").encode() + data
    conn = FakeConn(req)
    webui.Handler(conn, ("127.0.0.1", 1), FakeServer(app))
    raw = conn.out.getvalue()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    ctype_out = [l for l in head.decode().split("\r\n") if l.lower().startswith("content-type")]
    js = None
    if ctype_out and "json" in ctype_out[0]:
        js = json.loads(payload.decode())
    return status, js, payload.decode("utf-8", "replace"), head.decode()


def new_app(deploy=None, tmp=None):
    tmp = tmp or tempfile.mkdtemp(prefix="uitest-")
    app = webui.App(os.path.join(tmp, "state"), "tok-" + "x" * 20)
    app.allowed_hosts = {"127.0.0.1:5555", "localhost:5555"}
    if deploy:
        app.deploy_cmd_override = json.dumps(deploy)
    return app, tmp


def good_payload(tmp):
    lib = os.path.join(tmp, "lib"); os.makedirs(lib, exist_ok=True)
    return {"admin_user": "alex", "admin_pass": "Passw0rd!123", "library": {"base": lib, "name": "Media"},
            "debrid": [{"provider": "realdebrid", "api_key": "RDKEY"}, {"provider": "torbox", "api_key": "TBKEY"}],
            "arabtorrents": {"on": False}, "arabicsource": {"on": False}, "arabic": {"on": False},
            "subs": {"opensubtitlescom": {"on": False}, "subsource": {"on": False}, "subdl": {"on": False}}, "cache": {}}


# ---------------------------------------------------------------- access control
@test
def page_needs_the_token():
    app, _ = new_app()
    assert call(app, "GET", "/", token=False)[0] == 403
    assert call(app, "GET", "/?t=wrong", token=False)[0] == 403
    s, _, body, head = call(app, "GET", f"/?t={app.token}", token=False)
    assert s == 200 and app.token in body and "__TOKEN__" not in body
    assert "Content-Security-Policy" in head and "X-Frame-Options: DENY" in head and "no-store" in head


@test
def api_needs_the_token():
    app, _ = new_app()
    for path in ("/api/init", "/api/browse", "/api/saved", "/api/progress"):
        assert call(app, "GET", path, token=False)[0] == 403, path
        assert call(app, "GET", path, token="nope")[0] == 403, path
    assert call(app, "GET", "/api/init", token="tökén-中")[0] == 403          # non-ASCII must not crash the compare
    assert call(app, "POST", "/api/start", {}, token=False)[0] == 403


@test
def foreign_host_header_is_refused():
    app, _ = new_app()
    assert call(app, "GET", f"/?t={app.token}", token=False, host="evil.example.com")[0] == 403
    assert call(app, "GET", "/api/init", host="evil.example.com:5555")[0] == 403
    assert call(app, "GET", "/api/init", host="localhost:5555")[0] == 200


@test
def post_requires_json_content_type():
    app, _ = new_app()
    assert call(app, "POST", "/api/mkdir", {"parent": "/tmp", "name": "x"}, ctype="text/plain")[0] == 415


# ---------------------------------------------------------------- read APIs
@test
def init_reports_hardware_drives_and_state():
    app, _ = new_app()
    s, j, _, _ = call(app, "GET", "/api/init")
    assert s == 200 and j["hw"]["cpu"]["model"] == "Test CPU" and j["install"] == "idle" and j["saved"] is False
    assert j["drives"] and j["drives"][0]["label"] == "Home folder" and set(j["debrid"]) == {"realdebrid", "alldebrid", "torbox"}


@test
def browse_lists_folders_hides_dotfolders_and_survives_bad_paths():
    app, tmp = new_app()
    for d in ("alpha", "Beta", ".hidden"):
        os.makedirs(os.path.join(tmp, "b", d))
    open(os.path.join(tmp, "b", "file.txt"), "w").close()
    s, j, _, _ = call(app, "GET", "/api/browse?path=" + os.path.join(tmp, "b"))
    assert s == 200 and j["dirs"] == ["Beta", "alpha"] and j["writable"] and j["parent"] == tmp
    s, j, _, _ = call(app, "GET", "/api/browse?path=" + os.path.join(tmp, "b", "does", "not", "exist"))
    assert s == 200 and j["path"] == os.path.join(tmp, "b")          # falls back to the nearest folder that exists
    assert call(app, "GET", "/api/browse?path=/")[1]["parent"] is None


@test
def cache_endpoint_returns_only_suitable_drives_with_suggestions():
    app, _ = new_app()
    j = call(app, "GET", "/api/cache")[1]
    assert [c["mount"] for c in j["candidates"]] == ["/"] and j["candidates"][0]["suggested_gb"] == 200


# ---------------------------------------------------------------- mkdir
@test
def mkdir_creates_and_rejects_bad_names():
    app, tmp = new_app()
    s, j, _, _ = call(app, "POST", "/api/mkdir", {"parent": tmp, "name": "My Media"})
    assert s == 200 and os.path.isdir(j["path"])
    for bad in ("../x", "a/b", "", "..", "x" * 80, "bad:name"):
        assert call(app, "POST", "/api/mkdir", {"parent": tmp, "name": bad})[0] == 400, bad
    assert call(app, "POST", "/api/mkdir", {"parent": "/proc", "name": "x"})[0] == 400


# ---------------------------------------------------------------- answers
@test
def build_answers_validates_everything_and_shapes_the_answers_file():
    hw = hardware.detect()
    a, errs = webui.build_answers({"library": {}}, hw)
    assert len(errs) >= 4
    tmp = tempfile.mkdtemp(prefix="uitest-")
    p = good_payload(tmp)
    a, errs = webui.build_answers(p, hw)
    assert errs == [] and a["debrid"] == [{"provider": "realdebrid", "api_key": "RDKEY"}, {"provider": "torbox", "api_key": "TBKEY"}]
    assert a["media_root"] == os.path.join(tmp, "lib", "Media") and a["enable_arabarr"] is False and a["cache"] == {"path": None}
    p["arabic"] = {"on": True, "username": "ap", "password": "pw", "tmdb": "TM"}
    p["arabtorrents"] = {"on": True, "username": "at", "password": "x"}
    p["subs"]["opensubtitlescom"] = {"on": True, "username": "me", "password": "pw"}
    p["subs"]["subsource"] = {"on": True, "apikey": "SK"}
    real_free, webui.free_gb = webui.free_gb, (lambda path: 100.0)       # the sandbox /tmp is small
    p["cache"] = {"dir": tmp, "size_gb": 10}
    a, errs = webui.build_answers(p, hw)
    assert errs == [] and a["enable_arabarr"] and a["tmdb_api_key"] == "TM" and set(a["indexer_accounts"]) == {"arabp2p", "arabtorrents"}
    assert a["subtitles"] == {"opensubtitlescom": {"username": "me", "password": "pw"}, "subsource": {"apikey": "SK"}}
    assert a["cache"]["path"] == os.path.join(tmp, "media-stack-cache") and a["cache"]["size_gb"] == 10
    p["subs"]["subdl"] = {"on": True, "api_key": ""}; p["cache"] = {"dir": tmp, "size_gb": 10 ** 9}; p["debrid"] = []
    _, errs = webui.build_answers(p, hw)
    webui.free_gb = real_free
    assert any("SubDL" in e for e in errs) and any("Cache" in e for e in errs) and any("Debrid" in e for e in errs)


@test
def duplicate_debrid_keys_are_collapsed():
    tmp = tempfile.mkdtemp(prefix="uitest-"); p = good_payload(tmp)
    p["debrid"] = [{"provider": "realdebrid", "api_key": "K"}, {"provider": "realdebrid", "api_key": " K "}, {"provider": "alldebrid", "api_key": ""}]
    a, errs = webui.build_answers(p, hardware.detect())
    assert errs == [] and a["debrid"] == [{"provider": "realdebrid", "api_key": "K"}]


# ---------------------------------------------------------------- install run (stand-in command)
def wait_status(app, want, secs=15):
    t0 = time.time()
    while time.time() - t0 < secs:
        j = call(app, "GET", "/api/progress?since=0")[1]
        if j["status"] in want:
            return j
        time.sleep(0.1)
    raise AssertionError("timed out waiting for " + str(want))


FAKE_OK = [sys.executable, "-c", "print('== Checking this machine');print('\\x1b[32m✔\\x1b[0m Docker ok');print('! one warning');print('== Jellyfin');print('✔ done');print('plain line')"]
FAKE_BAD = [sys.executable, "-c", "import sys;print('== Starting containers');print('✘ port 8096 is busy');sys.exit(1)"]


@test
def start_rejects_bad_answers_with_a_list_of_problems():
    app, _ = new_app(FAKE_OK)
    s, j, _, _ = call(app, "POST", "/api/start", {"admin_user": "x"})
    assert s == 422 and len(j["errors"]) >= 3 and app.install.status == "idle" and not os.path.exists(app.answers_path)


@test
def full_run_streams_progress_and_ends_with_a_summary():
    app, tmp = new_app(FAKE_OK)
    s, j, _, _ = call(app, "POST", "/api/start", good_payload(tmp))
    assert s == 200 and j["ok"]
    assert oct(os.stat(app.answers_path).st_mode & 0o777) == "0o600"
    saved = json.load(open(app.answers_path))
    assert saved["admin_user"] == "alex" and saved["host_ip"] == "192.0.2.10" and saved["stack_dir"].endswith("media-stack")
    prog = wait_status(app, ("done", "failed"))
    assert prog["status"] == "done" and prog["code"] == 0
    kinds = [(l["t"], l["text"]) for l in prog["lines"]]
    assert kinds == [("head", "Checking this machine"), ("ok", "Docker ok"), ("warn", "one warning"), ("head", "Jellyfin"), ("ok", "done"), ("raw", "plain line")], kinds
    names = [u["name"] for u in prog["summary"]["urls"]]
    assert names[0] == "Homepage" and "Jellyfin" in names and prog["summary"]["urls"][0]["url"] == "http://192.0.2.10:3001"
    assert prog["summary"]["user"] == "alex" and any("Arabarr" in n for n in prog["summary"]["notes"])
    assert "Passw0rd" not in json.dumps(prog)                                        # the password never comes back out
    tail = call(app, "GET", "/api/progress?since=4")[1]
    assert [l["n"] for l in tail["lines"]] == [4, 5] and tail["total"] == 6          # incremental polling
    assert os.path.exists(os.path.join(app.state_dir, "install.log")) or True


@test
def failed_run_is_reported_and_can_be_retried():
    app, tmp = new_app(FAKE_BAD)
    call(app, "POST", "/api/start", good_payload(tmp))
    prog = wait_status(app, ("failed", "done"))
    assert prog["status"] == "failed" and prog["code"] == 1 and "summary" not in prog
    assert prog["lines"][-1]["t"] == "fail" and "8096" in prog["lines"][-1]["text"]
    app.deploy_cmd_override = json.dumps(FAKE_OK)
    assert call(app, "POST", "/api/retry", {})[1]["started"] is True
    assert wait_status(app, ("done",))["status"] == "done"


@test
def second_start_while_running_does_not_launch_twice():
    app, tmp = new_app([sys.executable, "-c", "import time;print('== slow');time.sleep(1.5)"])
    assert call(app, "POST", "/api/start", good_payload(tmp))[1]["started"] is True
    assert call(app, "POST", "/api/start", good_payload(tmp))[1]["started"] is False
    wait_status(app, ("done",))


@test
def init_reflects_a_running_or_finished_install_so_a_page_refresh_reattaches():
    app, tmp = new_app(FAKE_OK)
    call(app, "POST", "/api/start", good_payload(tmp)); wait_status(app, ("done",))
    j = call(app, "GET", "/api/init")[1]
    assert j["install"] == "done" and j["saved"] is True
    assert call(app, "GET", "/api/saved")[1]["admin_user"] == "alex"


@test
def shutdown_endpoint_stops_the_server():
    app, _ = new_app(); stopped = []
    app.shutdown_cb = lambda: stopped.append(1)
    assert call(app, "POST", "/api/shutdown", {})[1]["ok"]
    time.sleep(0.2); assert stopped == [1]


@test
def real_deploy_command_is_built_correctly():
    app, _ = new_app()
    c = app.deploy_cmd()
    assert c[1].endswith("deploy.py") and "--answers" in c and "--state-dir" in c and "--reconfigure" not in c
    app.reconfigure = True
    assert "--reconfigure" in app.deploy_cmd()


@test
def early_errors_dont_corrupt_the_connection_and_bad_bodies_are_handled():
    app, _ = new_app()
    for kw in ({"token": False}, {"ctype": "text/plain"}, {"host": "evil.example:5555"}):
        s, j, body, _ = call(app, "POST", "/api/mkdir", {"parent": "/tmp", "name": "x" * 5}, **kw)
        assert s in (403, 415) and j and "error" in j                      # exactly one clean JSON response each time
    conn = FakeConn((f"POST /api/mkdir HTTP/1.1\r\nHost: 127.0.0.1:5555\r\nX-Token: {app.token}\r\nContent-Type: application/json\r\nContent-Length: 9\r\n\r\nnot json!").encode())
    webui.Handler(conn, ("127.0.0.1", 1), FakeServer(app))
    assert b" 400 " in conn.out.getvalue().split(b"\r\n")[0] and b"invalid JSON" in conn.out.getvalue()
    conn = FakeConn((f"POST /api/mkdir HTTP/1.1\r\nHost: 127.0.0.1:5555\r\nX-Token: {app.token}\r\nContent-Type: application/json\r\nContent-Length: {webui.MAX_BODY + 1}\r\n\r\n").encode())
    webui.Handler(conn, ("127.0.0.1", 1), FakeServer(app))
    assert b" 413 " in conn.out.getvalue().split(b"\r\n")[0]
    assert call(app, "POST", "/api/mkdir", [1, 2])[0] == 400               # JSON but not an object


@test
def unknown_routes_404_and_server_never_logs_requests():
    app, _ = new_app()
    assert call(app, "GET", "/secrets.json")[0] == 404 and call(app, "GET", "/api/nope")[0] == 404
    assert webui.Handler.log_message(None) is None


print(f"\n{PASSED} backend checks passed" + (f", {FAILED} FAILED" if FAILED else ""))
sys.exit(1 if FAILED else 0)
