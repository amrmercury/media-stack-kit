"""Shared helpers: logging, HTTP with retries, readiness gating, docker wrapper, secrets."""
import json, os, secrets, shlex, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(KIT, "seed")

_C = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _C else s
def info(m): print(_c("36", "•"), m, flush=True)
def ok(m): print(_c("32", "✔"), m, flush=True)
def warn(m): print(_c("33", "!"), m, flush=True)
def fail(m): print(_c("31", "✘"), m, flush=True)
def step(m): print("\n" + _c("1", f"== {m}"), flush=True)


class StackError(Exception):
    pass


class Fatal(StackError):
    """A problem that waiting and retrying cannot fix (wrong login for an existing app, ...): stop at once with the message."""


# ---------------------------------------------------------------- docker
def docker_cmd():
    """['docker'] or ['sudo','docker'] (install.sh exports STACK_DOCKER when the user isn't in the docker group)."""
    return shlex.split(os.environ.get("STACK_DOCKER", "docker"))


def docker(*args, check=True, capture=True, input=None, cwd=None, timeout=None):
    r = subprocess.run(docker_cmd() + list(args), text=True, capture_output=capture,
                       input=input, cwd=cwd, timeout=timeout)
    if check and r.returncode != 0:
        raise StackError(f"docker {' '.join(args)} failed:\n{r.stdout}{r.stderr}")
    return r


def compose(stack_dir, *args, check=True, capture=True, timeout=None):
    return docker("compose", "--project-directory", stack_dir, *args,
                  check=check, capture=capture, timeout=timeout)


# ---------------------------------------------------------------- http
class Resp:
    def __init__(self, status, body, headers):
        self.status, self.body, self.headers = status, body, headers

    def json(self):
        return json.loads(self.body) if self.body else None

    @property
    def ok(self):
        return 200 <= self.status < 300


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener_plain = urllib.request.build_opener(_NoRedirect)


TRANSIENT_STATUS = (502, 503, 504)      # an app that is (re)starting or still loading answers like this


def http(method, url, *, headers=None, json_body=None, form=None, data=None, timeout=30, retries=0,
         follow_redirects=True, patience=60):
    """Tiny HTTP client. Never raises on HTTP status (returns Resp); raises StackError on no connection.
    Apps restart themselves after plugin installs and config changes, and a slow machine takes a long time to come back, so
    'connection refused / reset' and 502/503/504 are treated as 'not ready yet' and retried for `patience` seconds."""
    hdrs = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = data if isinstance(data, bytes) else data.encode()
    last, attempt, t0 = None, 0, time.time()
    while True:
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            opener = urllib.request.urlopen if follow_redirects else _opener_plain.open
            with opener(req, timeout=timeout) as r:
                return Resp(r.status, r.read().decode("utf-8", "replace"), dict(r.headers))
        except urllib.error.HTTPError as e:
            resp = Resp(e.code, e.read().decode("utf-8", "replace"), dict(e.headers or {}))
            if resp.status not in TRANSIENT_STATUS or time.time() - t0 >= patience:
                return resp
            last = f"HTTP {resp.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
            if attempt >= retries and time.time() - t0 >= patience:
                raise StackError(f"cannot reach {url}: {last}")
        attempt += 1
        time.sleep(min(2 * attempt, 8))


def wait_ready(name, check, timeout=300, interval=3):
    """Block until check() returns truthy. check() must hit the app's *real API*, not just a TCP port."""
    t0 = time.time()
    last_err = ""
    while time.time() - t0 < timeout:
        try:
            if check():
                ok(f"{name} is ready ({int(time.time() - t0)}s)")
                return
        except StackError as e:
            last_err = str(e)
        except Exception as e:  # noqa: BLE001 - readiness probes must never crash the installer
            last_err = repr(e)
        time.sleep(interval)
    raise StackError(f"{name} did not become ready within {timeout}s. Last error: {last_err}")


# ---------------------------------------------------------------- secrets / state
def new_key():
    return secrets.token_hex(16)  # 32 hex chars: the format Sonarr/Radarr/Prowlarr use


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def seed_json(rel):
    with open(os.path.join(SEED, rel)) as f:
        return json.load(f)


def seed_text(rel):
    with open(os.path.join(SEED, rel), encoding="utf-8") as f:
        return f.read()


def write_file(path, text, mode=0o644, uid=None, gid=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, mode)
    if uid is not None and os.geteuid() == 0:
        os.chown(path, uid, gid)


def ensure_dir(path, mode=0o755):
    os.makedirs(path, mode=mode, exist_ok=True)
    return path
