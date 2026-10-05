#!/usr/bin/env python3
"""Unattended install: render -> start -> wait for REAL readiness -> seed via APIs -> Homepage last -> verify.

Everything interactive was collected by wizard.py beforehand; this never asks anything.
Safe to re-run: every step is idempotent, and app-owned files are never overwritten (use --reconfigure to force).
"""
import argparse, json, os, shutil, socket, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardware, homepage, render, verify
from arr import Arr, seed_prowlarr, seed_sonarr_radarr, wait_indexers_synced
from bazarr import Bazarr
from common import KIT, StackError, compose, docker, fail, info, load_json, ok, save_json, step, warn, http, wait_ready
from decypharr import Decypharr
from jellyfin import Jellyfin
from jellyseerr import Jellyseerr


class Tee:
    """Everything the installer prints is also appended to <state>/install.log, so a crash or shutdown can't lose it."""
    def __init__(self, a, b): self.a, self.b = a, b
    def write(self, t):
        self.a.write(t); self.b.write(t); self.b.flush(); return len(t)
    def flush(self): self.a.flush(); self.b.flush()
    def isatty(self): return False
    def __getattr__(self, k): return getattr(self.a, k)


# ------------------------------------------------------------------ preflight
def sudo_run(cmd):
    """Try without a password prompt first; the installer must never hang waiting on sudo."""
    for c in (cmd, ["sudo", "-n"] + cmd):
        if subprocess.run(c, capture_output=True).returncode == 0:
            return True
    return False


def clear_stale_mount(path):
    """After the container is removed, the propagated FUSE mount can linger as 'Transport endpoint is not
    connected' and blocks the next start. A user-level lazy unmount clears it."""
    try:
        os.listdir(path)
        return
    except OSError as e:
        if e.errno != 107:          # ENOTCONN
            return
    warn(f"{path} is a leftover dead mount from a previous run; clearing it")
    for tool in ("fusermount3", "fusermount"):
        if shutil.which(tool) and subprocess.run([tool, "-uz", path], capture_output=True).returncode == 0:
            return
    if not sudo_run(["umount", "-l", path]):
        raise StackError(f"Couldn't clear the dead mount at {path}. Run:  sudo umount -l {path}  and re-run.")


def preflight(a, ports, reconfigure):
    step("Checking this machine")
    try:
        docker("info")
    except StackError as e:
        raise StackError("Docker isn't usable by this user. Run install.sh (it sets Docker up) or add yourself to "
                         f"the 'docker' group.\n{e}")
    docker("compose", "version")
    ok("Docker + Compose are available")

    if not os.path.exists("/dev/fuse"):
        warn("/dev/fuse is missing; trying to load the FUSE kernel module")
        sudo_run(["modprobe", "fuse"])
    if not os.path.exists("/dev/fuse"):
        raise StackError("FUSE isn't available on this kernel, and the debrid mount needs it. "
                         "Install your distro's 'fuse3' package (or run: sudo modprobe fuse) and re-run.")
    ok("FUSE is available")

    root = a["media_root"]
    os.makedirs(root, exist_ok=True)
    clear_stale_mount(os.path.join(root, "decypharr"))
    prop = subprocess.run(["findmnt", "-no", "PROPAGATION", "-T", root], capture_output=True, text=True).stdout.strip()
    if "shared" not in prop:
        warn(f"mount propagation for {root} is '{prop or 'unknown'}', but the debrid mount needs 'shared'; fixing")
        if not sudo_run(["mount", "--make-rshared", "/"]):
            raise StackError("Couldn't make the filesystem mount-shared. Run once:  sudo mount --make-rshared /   "
                             "then re-run the installer.")
    ok("Mount propagation is shared")

    existing = {}
    try:
        from homepage import running_services
        existing = running_services(a["stack_dir"]) if os.path.exists(os.path.join(a["stack_dir"], "docker-compose.yml")) else {}
    except Exception:  # noqa: BLE001
        pass
    busy = []
    for svc, port in ports.items():
        if svc == "arabarr" and not a.get("enable_arabarr"):
            continue
        if svc in existing:
            continue            # our own container from a previous run
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(f"{svc} (:{port})")
    if busy:
        raise StackError("These ports are already in use by something else: " + ", ".join(busy) +
                         ". Stop that program, or set different ports in the answers file ('ports').")
    ok("All needed ports are free")
    free = hardware.media_fs_free_gb(root)
    if free < 5:
        raise StackError(f"Only {free:.1f} GB free at {root}; need at least 5 GB.")


STACK_NAMES = ["decypharr", "prowlarr", "sonarr", "radarr", "bazarr", "flaresolverr", "jellyseerr", "jellyfin", "recyclarr",
               "pearlarr", "homepage", "babysitarr", "arabarr"]


def _repo(image):
    """'lscr.io/linuxserver/jellyfin@sha256:..' or '...:latest' -> 'lscr.io/linuxserver/jellyfin'"""
    return image.split("@")[0].rsplit(":", 1)[0] if (":" in image.split("/")[-1] or "@" in image) else image


def clear_leftover_containers(a):
    """A container named like ours (e.g. 'jellyfin') left by an earlier attempt from another folder blocks 'compose up'.
    If it runs one of OUR images, it is a leftover: remove it. If it is something else, stop and say exactly what to do."""
    pre = a.get("container_prefix", "")
    ours = {_repo(v["image"]) for v in load_json(os.path.join(KIT, "seed/images.json")).values()} | {_repo(v["tag"]) for v in load_json(os.path.join(KIT, "seed/images.json")).values()}
    fmt = "{{.ID}}|{{.Names}}|{{.Image}}|{{.Label \"com.docker.compose.project.working_dir\"}}"
    out = docker("ps", "-a", "--format", fmt).stdout.splitlines()
    foreign = []
    for line in out:
        cid, name, image, workdir = (line.split("|") + ["", "", "", ""])[:4]
        if name not in {pre + n for n in STACK_NAMES} or (workdir and os.path.realpath(workdir) == os.path.realpath(a["stack_dir"])):
            continue
        if _repo(image) in ours or image.startswith(("media-stack", "stack-")) or "babysitarr" in image or "arabarr" in image:
            docker("rm", "-f", cid, check=False)
            info(f"Removed a leftover '{name}' container from an earlier attempt")
        else:
            foreign.append(f"{name} (image {image})")
    if foreign:
        raise StackError("These containers already use names this stack needs, and they are not from this installer: " +
                         ", ".join(foreign) + ". If you don't need them, remove them (docker rm -f <name>) and re-run.")


TRANSIENT_DOCKER = ("tls handshake", "timeout", "timed out", "eof", "connection reset", "toomanyrequests", "429",
                    "temporary failure", "no such host", "try again", "unexpected status", "503", "502", "network is unreachable")


def compose_up_with_retries(stack, services, tries=4):
    """Image downloads fail on flaky home networks (and Docker Hub rate-limits). Retry those; fail fast on real errors."""
    for n in range(1, tries + 1):
        try:
            compose(stack, "up", "-d", "--build", *services, timeout=1800)
            return
        except StackError as e:
            if n == tries or not any(t in str(e).lower() for t in TRANSIENT_DOCKER):
                raise
            warn(f"Starting containers hit a network hiccup; retrying ({n}/{tries - 1})...")
            time.sleep(15 * n)


# ------------------------------------------------------------------ main flow
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", default=os.path.join(KIT, "state", "answers.json"))
    ap.add_argument("--state-dir", default=os.path.join(KIT, "state"), help="where generated secrets are kept")
    ap.add_argument("--reconfigure", action="store_true", help="re-render app config files from the answers")
    ap.add_argument("--localhost", action="store_true",
                    help="talk to apps via localhost:<published port> (default; required on a normal host)")
    args = ap.parse_args()

    a = json.load(open(args.answers))
    state_dir = args.state_dir
    os.makedirs(state_dir, exist_ok=True)
    logf = open(os.path.join(state_dir, "install.log"), "a", encoding="utf-8", buffering=1)
    logf.write("\n===== install run started " + time.strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
    sys.stdout, sys.stderr = Tee(sys.stdout, logf), Tee(sys.stderr, logf)
    ports = {**render.DEFAULT_PORTS, **a.get("ports", {})}
    t0 = time.time()

    preflight(a, ports, args.reconfigure)

    step("Preparing the stack")
    if args.reconfigure and os.path.exists(os.path.join(a["stack_dir"], "docker-compose.yml")):
        compose(a["stack_dir"], "down", timeout=300)
    S = render.render_all(a, state_dir, force=args.reconfigure)
    stack = a["stack_dir"]

    step("Starting containers (the first run downloads images; this can take a few minutes)")
    services = [s for s in render.build_compose(a, S, load_json(os.path.join(KIT, "seed/images.json")), *render.ids())["services"]
                if s != "arabarr"]
    clear_leftover_containers(a)
    compose_up_with_retries(stack, services)
    ok(f"{len(services)} containers started")

    L = lambda p: f"http://localhost:{ports[p]}"
    sonarr = Arr("sonarr", L("sonarr"), S["sonarr"], "v3")
    radarr = Arr("radarr", L("radarr"), S["radarr"], "v3")
    prowlarr = Arr("prowlarr", L("prowlarr"), S["prowlarr"], "v1")
    jf = Jellyfin(L("jellyfin"), stack)
    js = Jellyseerr(L("jellyseerr"))
    bz = Bazarr(L("bazarr"), S["bazarr"])
    dec = Decypharr(L("decypharr"), stack, S["decypharr_token"])

    step("Waiting for every app to really be up (checking their APIs, not just their ports)")
    for app in (sonarr, radarr, prowlarr):
        app.wait_ready()
    jf.wait_ready(); js.wait_ready(); bz.wait_ready(); dec.wait_ready()
    wait_ready("flaresolverr", lambda: http("GET", L("flaresolverr") + "/").status == 200, 120)

    step("Decypharr: your login")
    dec.register(a["admin_user"], a["admin_pass"])
    if not dec.verify_qbit_login(a["admin_user"], a["admin_pass"]):
        raise StackError("decypharr was already registered with a different username/password than the one you "
                         f"entered. Remove {stack}/decypharr/auth.json (keep nothing else) and re-run, or use the "
                         "existing login.")

    step("Jellyfin: setup, your account, plugins, libraries")
    jf.first_run(a["admin_user"], a["admin_pass"], socket.gethostname() or "Jellyfin")
    jfkey = jf.api_key()
    jf.apply_settings()
    jf.pin_plugin_versions()
    time.sleep(5)       # let the server refresh its plugin repositories
    jf.install_plugins()
    jf.restart_with_configs(a, S)        # plugin settings are written while it's stopped, so they stick
    jf.login(a["admin_user"], a["admin_pass"])
    jf.libraries(a)
    jf.direct_play_policy()

    step("Prowlarr: indexers")
    seed_prowlarr(prowlarr, a, S, S["sonarr"], S["radarr"])

    arabarr = None
    if a.get("enable_arabarr"):
        step("Arabarr")
        idx = next((i for i in prowlarr.get("/indexer") if i["name"] == "ArabP2P"), None)
        if not idx:
            raise StackError("ArabP2P isn't in Prowlarr, so Arabarr can't be set up.")
        envp = os.path.join(stack, "arabarr.env")
        t = open(envp).read().replace("__SET_BY_INSTALLER__", str(idx["id"]))
        open(envp, "w").write(t)
        compose(stack, "up", "-d", "arabarr", timeout=300)
        arabarr = {"proxy_key": S["arabarr_proxy"]}
        ok("Arabarr started")

    step("Sonarr + Radarr")
    seed_sonarr_radarr(sonarr, "sonarr", a, S, jfkey, arabarr)
    seed_sonarr_radarr(radarr, "radarr", a, S, jfkey, arabarr)
    wait_indexers_synced(sonarr, 5)
    wait_indexers_synced(radarr, 5)

    step("Bazarr: subtitles")
    bz.configure(a, S)

    step("Jellyseerr")
    js.configure(a, S, sonarr, radarr, f"http://{a['host_ip']}:{ports['jellyfin']}")

    step("Recyclarr: quality scores")
    r = compose(stack, "exec", "-T", "recyclarr", "recyclarr", "sync", check=False, timeout=900)
    (ok if r.returncode == 0 else warn)("Recyclarr synced custom formats and scores" if r.returncode == 0
                                        else f"Recyclarr sync didn't finish ({r.stderr[-200:]}); it retries daily")

    step("Homepage (last, so it lists everything that's running)")
    running = homepage.generate(a, S, jfkey)

    jf.scan()
    failed, _ = verify.run(a, S, ports, jf, js, bz, dec, sonarr, radarr, prowlarr, running)
    print()
    if failed:
        fail(f"{failed} check(s) failed. See above; re-running the installer is safe.")
        sys.exit(1)
    summary(a, ports, int(time.time() - t0))


def summary(a, ports, secs):
    h = a["host_ip"]
    step(f"Done in {secs // 60}m {secs % 60}s: everything is installed, configured and verified")
    rows = [("Homepage (start here)", "homepage"), ("Jellyfin (watch)", "jellyfin"), ("Jellyseerr (request)", "jellyseerr"),
            ("Sonarr", "sonarr"), ("Radarr", "radarr"), ("Prowlarr", "prowlarr"), ("Bazarr", "bazarr"),
            ("Decypharr", "decypharr")]
    for label, svc in rows:
        print(f"   {label:<24} http://{h}:{ports[svc]}")
    print(f"\n   Sign in everywhere with:  {a['admin_user']}  /  (the password you chose)")
    print("\n   What's left for you:")
    print("   1. Open Jellyseerr, request a show or movie, and watch it appear in Jellyfin.")
    if not a.get("subtitles"):
        print("   2. Subtitles: you skipped them, so Bazarr is a blank slate. Add providers in Bazarr when ready.")
    if not a.get("enable_arabarr"):
        print("   - Arabarr isn't installed (needs an ArabP2P account + a TMDB key). Re-run the installer to add it.")
    print("   - Playback is direct-play by default. If a device can't play a file, turn transcoding on for\n"
          "     that user in Jellyfin (Dashboard -> Users -> Playback).")


if __name__ == "__main__":
    try:
        main()
    except StackError as e:
        fail(str(e))
        print("\nIf you need help: run  ./install.sh --diag  and send the file it writes (~/media-stack-diag.txt).")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted. Re-running the installer is safe.")
        sys.exit(130)
