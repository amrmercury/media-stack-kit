#!/usr/bin/env python3
"""Fair, isolated benchmark of decypharr's two mount engines (rclone VFS vs DFS) on the same debrid account.

Safety design (this runs next to a LIVE media stack):
  * two throwaway containers from the SAME image as the live decypharr, no /mnt bind, no mount propagation:
    their FUSE mounts exist only inside their own containers and can never touch the live /mnt/decypharr;
  * idle gate: nothing runs while anyone is playing in Jellyfin, and a unit is discarded if playback starts;
  * live-mount watchdog before every unit; on any loss it stops the benchmark and restarts the live decypharr;
  * resource caps on the benchmark containers; full cleanup at the end.

Fairness: both engines use the live settings; both caches on the same drive; every cold read hits a byte range
that was never read before (ledger); engine order alternates (ABBA); N repetitions; paired by file where possible.

Stages:  setup | run | report | cleanup | all
"""
import argparse, json, math, os, random, re, shlex, shutil, statistics, subprocess, sys, threading, time, urllib.request

HOME = os.path.expanduser("~")
BENCH = os.path.join(HOME, "decypharr-bench")
RESULTS = os.path.join(HOME, "decypharr-bench-results")
LIVE_CFG = os.path.join(HOME, "docker/arr/decypharr/config.json")
LIVE_MOUNT = "/mnt/decypharr"
MiB, GiB = 1 << 20, 1 << 30
SLOT = 512 * MiB
ENGINES = ("rclone", "dfs")
NEED_MBPS = 15.0            # a 120 Mbps 4K-remux peak
CONTAINER = {e: f"bench-{e}" for e in ENGINES}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ------------------------------------------------------------------ safety
def live_ok():
    try:
        with open("/proc/self/mountinfo") as f:
            if not any(" /mnt/decypharr " in l for l in f):
                return False
        return len(os.listdir(LIVE_MOUNT + "/__all__")) > 50
    except OSError:
        return False


def jellyfin_key():
    t = open(os.path.join(HOME, "docker/homepage/config/services.yaml")).read()
    return re.search(r"type: jellyfin\s+url: \S+\s+key: (\S+)", t).group(1)


_JK = None


def playing():
    global _JK
    _JK = _JK or jellyfin_key()
    req = urllib.request.Request("http://localhost:8096/Sessions", headers={"X-Emby-Token": _JK})
    with urllib.request.urlopen(req, timeout=10) as r:
        # a PAUSED stream moves no data, so it doesn't disturb the test; resuming mid-measurement is caught by unit()
        return [s["NowPlayingItem"]["Name"] for s in json.load(r)
                if s.get("NowPlayingItem") and not s.get("PlayState", {}).get("IsPaused")]


def panic(why):
    log("ABORT:", why)
    for c in CONTAINER.values():
        run(["docker", "rm", "-f", c])
    if not live_ok():
        log("live mount missing -> restarting live decypharr")
        run(["docker", "restart", "decypharr"])
    sys.exit(2)


def idle_gate(max_wait=8 * 3600):
    t0, announced = time.time(), False
    while True:
        if os.path.exists(os.path.join(BENCH, "STOP")):
            panic("STOP file present")
        try:
            p = playing()
        except Exception as e:  # noqa: BLE001
            log("jellyfin check failed, waiting:", e); p = ["?"]
        if not p:
            return
        if not announced:
            log(f"someone is playing ({len(p)} stream(s)); waiting until everyone stops"); announced = True
        if time.time() - t0 > max_wait:
            panic("gave up waiting for an idle server")
        time.sleep(20)


def guard():
    if not live_ok():
        panic("live /mnt/decypharr is not healthy")
    idle_gate()


# ------------------------------------------------------------------ containers
def image_id():
    return run(["docker", "inspect", "-f", "{{.Image}}", "decypharr"]).stdout.strip()


def build_config(engine):
    c = json.load(open(LIVE_CFG))
    rd = next(d for d in c["debrids"] if d["provider"] == "realdebrid")
    c["debrids"], c["arrs"], c["categories"] = [rd], [], []
    c["repair"].update(enabled=False, auto_repair=False)
    c["queue_cleanup"] = {"rules": []}
    c["use_auth"] = False
    c["download_folder"] = "/tmp/bench-dl"
    m = c["mount"]
    m["type"], m["mount_path"] = engine, LIVE_MOUNT         # the path is inside the container only
    m["rclone"].update(cache_dir="/app/cache/rclone", vfs_cache_max_size="40G")
    m["dfs"].update(cache_dir="/app/cache/dfs", disk_cache_size="40GB", cache_expiry="72h")
    return c


def setup():
    if not live_ok():
        panic("live mount unhealthy before setup")
    os.makedirs(BENCH, exist_ok=True)
    img, ready = image_id(), {}
    for e in ENGINES:
        d = os.path.join(BENCH, e)
        os.makedirs(os.path.join(d, "app"), exist_ok=True)
        cfg = os.path.join(d, "app/config.json")
        fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(build_config(e), f, indent=1)
        run(["docker", "rm", "-f", CONTAINER[e]])
    for e in ENGINES:
        t0 = time.time()
        r = run(["docker", "run", "-d", "--name", CONTAINER[e], "--memory", "3g", "--cpus", "4",
                 "--cpu-shares", "256", "--device", "/dev/fuse", "--cap-add", "SYS_ADMIN",
                 "--security-opt", "apparmor:unconfined",
                 "--tmpfs", f"{LIVE_MOUNT}:uid=1000,gid=1000,mode=755",     # private mountpoint, host never sees it
                 "-e", "PUID=1000", "-e", "PGID=1000",
                 "-v", f"{BENCH}/{e}/app:/app", "--label", "bench=1", img])
        if r.returncode:
            panic("could not start " + e + ": " + r.stderr)
        while time.time() - t0 < 240:
            o = run(["docker", "exec", CONTAINER[e], "sh", "-c", f"ls {LIVE_MOUNT}/__all__ 2>/dev/null | head -1"])
            if o.stdout.strip():
                ready[e] = time.time() - t0
                break
            time.sleep(1)
        else:
            lg = run(["docker", "logs", "--tail", "25", CONTAINER[e]])
            print((lg.stdout + lg.stderr).replace("\x1b", "")[-3000:])
            panic(f"{e} mount never became ready")
        log(f"{e}: mount ready in {ready[e]:.1f}s")
    if not live_ok():
        panic("live mount changed during setup")
    json.dump({"mount_ready_s": ready, "image": img}, open(os.path.join(BENCH, "setup.json"), "w"))
    log("setup ok; live mount still healthy")


def dexec(engine, script, timeout=900):
    t0 = time.perf_counter()
    r = run(["docker", "exec", CONTAINER[engine], "sh", "-c", script], timeout=timeout)
    return r.stdout, r.stderr, time.perf_counter() - t0


def dd_secs(text):
    return [float(x) for x in re.findall(r"copied, ([0-9.]+) seconds", text)]


# ------------------------------------------------------------------ files + cold-slot ledger
def pick_files(seed=42):
    out, _, _ = dexec("rclone", f"find {LIVE_MOUNT}/__all__ -type f \\( -name '*.mkv' -o -name '*.mp4' \\) "
                                f"-exec stat -c '%s %n' {{}} +")
    files = []
    for line in out.splitlines():
        m = re.match(r"(\d+) (.+)", line)
        if m and "sample" not in m.group(2).lower():
            files.append((int(m.group(1)), m.group(2)))
    rng = random.Random(seed)
    by_dir = {}
    for sz, p in files:
        by_dir.setdefault(os.path.dirname(p), []).append((sz, p))
    biggest = [max(v) for v in by_dir.values()]
    big = [x for x in biggest if x[0] >= 30 * GiB]
    mid = [x for x in biggest if 2 * GiB <= x[0] <= 10 * GiB]
    if len(big) < 4 or len(mid) < 3:
        panic(f"not enough suitable files (4K>=30GiB: {len(big)}, 2-10GiB: {len(mid)})")
    return rng.sample(big, 4), rng.sample(mid, 3)


class Slots:
    """Each cold read uses a 512MiB slot that has never been read, on any engine."""
    def __init__(self, files, seed=7):
        rng = random.Random(seed)
        self.free = {p: rng.sample(range(2, max(3, sz // SLOT - 2)), max(1, sz // SLOT - 4)) for sz, p in files}

    def take(self, path):
        return self.free[path].pop() * SLOT

    def richest(self, files, k=1, skip=()):
        """The k files with the most never-read slots left (keeps every test on fresh data)."""
        c = sorted((f for f in files if f[1] not in skip), key=lambda f: -len(self.free[f[1]]))
        return c[:k]


# ------------------------------------------------------------------ resource sampler
class Sampler(threading.Thread):
    def __init__(self, engine):
        super().__init__(daemon=True)
        self.e, self.stop, self.cpu, self.mem = engine, False, [], []

    def run(self):
        while not self.stop:
            r = run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", CONTAINER[self.e]])
            m = re.match(r"([\d.]+)% ([\d.]+)(MiB|GiB)", r.stdout.strip())
            if m:
                self.cpu.append(float(m.group(1)))
                self.mem.append(float(m.group(2)) * (1024 if m.group(3) == "GiB" else 1))


def with_sampler(engine, fn):
    s = Sampler(engine); s.start()
    try:
        return fn(), s
    finally:
        s.stop = True; s.join(timeout=5)


# ------------------------------------------------------------------ test units
def baseline_overhead(engine, n=15):
    xs = []
    for _ in range(n):
        xs.append(dexec(engine, "true")[2])
    return statistics.median(xs)


def t1_first_touch(engine, path, size, ovh):
    """What pressing Play on a never-opened file costs: first byte, then the head+tail a player reads at startup."""
    q = shlex.quote(path)
    _, _, w1 = dexec(engine, f"dd if={q} bs=64k count=1 >/dev/null 2>&1")
    _, _, w2 = dexec(engine, f"dd if={q} bs=1M count=16 >/dev/null 2>&1")
    _, _, w3 = dexec(engine, f"dd if={q} bs=1M skip={size // MiB - 16} count=16 >/dev/null 2>&1")
    return {"ttfb_s": w1 - ovh, "head16_s": w2 - ovh, "tail16_s": w3 - ovh, "startup_total_s": w1 + w2 + w3 - 3 * ovh}


def t2_cold_sequential(engine, path, slot_off, chunks=12, chunk_mib=32):
    q = shlex.quote(path)
    sk = slot_off // MiB
    script = (f'exec 3<{q}; dd bs=1M skip={sk} count={chunk_mib} <&3 2>&1 >/dev/null | tail -1; '
              f'i=1; while [ $i -lt {chunks} ]; do dd bs=1M count={chunk_mib} <&3 2>&1 >/dev/null | tail -1; '
              f'i=$((i+1)); done')
    (out, _, _), samp = with_sampler(engine, lambda: dexec(engine, script))
    secs = dd_secs(out)
    sp = [chunk_mib / s for s in secs if s > 0]
    total = chunk_mib * len(secs) / sum(secs)
    return {"mbps": total, "first_chunk_mbps": sp[0], "min_chunk_mbps": min(sp),
            "stalls": sum(1 for x in sp if x < NEED_MBPS), "chunks": len(sp),
            "cpu_max": max(samp.cpu or [0]), "mem_max_mib": max(samp.mem or [0]),
            "region": (path, slot_off, chunks * chunk_mib)}


def t2b_warm_reread(engine, region):
    path, off, mib = region
    out, _, _ = dexec(engine, f"dd if={shlex.quote(path)} bs=1M skip={off // MiB} count={mib} 2>&1 >/dev/null | tail -1")
    s = dd_secs(out)
    return {"mbps": mib / s[0]} if s else None


def t3_seeks(engine, path, size, slots, n=15, read_mib=4, seed=0):
    rng = random.Random(seed)
    offs = []
    for _ in range(n):
        o = slots.take(path) + rng.randrange(0, 480) * MiB
        offs.append(o // MiB)
    q = shlex.quote(path)
    loop = lambda lst: "; ".join(f"dd if={q} bs=1M skip={o} count={read_mib} 2>&1 >/dev/null | tail -1" for o in lst)
    cold, _, _ = dexec(engine, loop(offs))
    warm, _, _ = dexec(engine, loop(list(reversed(offs))))
    return {"cold_ms": [s * 1000 for s in dd_secs(cold)], "warm_ms": [s * 1000 for s in dd_secs(warm)]}


def t5_concurrent(engine, trio, slots, mib=256):
    parts = []
    for i, (sz, p) in enumerate(trio):
        off = slots.take(p) // MiB
        parts.append(f"(dd if={shlex.quote(p)} bs=1M skip={off} count={mib} 2>&1 >/dev/null | tail -1 > /tmp/c{i}) &")
    script = " ".join(parts) + " wait; cat /tmp/c0 /tmp/c1 /tmp/c2"
    t0 = time.perf_counter()
    (out, _, _), samp = with_sampler(engine, lambda: dexec(engine, script))
    wall = time.perf_counter() - t0
    secs = dd_secs(out)
    per = [mib / s for s in secs]
    return {"per_stream_mbps": per, "min_stream_mbps": min(per) if per else 0, "aggregate_mbps": 3 * mib / wall,
            "cpu_max": max(samp.cpu or [0]), "mem_max_mib": max(samp.mem or [0])}


def unit(label, fn, tries=3):
    """Run one measurement only while the server is idle; discard it if playback began meanwhile."""
    for k in range(tries):
        guard()
        res = fn()
        if not playing() and live_ok():
            return res
        log(f"  {label}: playback started mid-measurement; discarding and retrying")
    panic(f"{label}: could not get an undisturbed measurement")


def run_bench(reps=6, seed=1):
    setup_info = json.load(open(os.path.join(BENCH, "setup.json")))
    big, mid = pick_files()
    log("4K files:", [f"{p.split('/')[-1][:48]} ({sz / GiB:.0f}GiB)" for sz, p in big])
    log("1080p files:", [f"{p.split('/')[-1][:48]} ({sz / GiB:.1f}GiB)" for sz, p in mid])
    slots = Slots(big)
    ovh = {e: baseline_overhead(e) for e in ENGINES}
    R = {"setup": setup_info, "overhead_s": ovh, "files": {"4k": [s for s, _ in big], "mid": [s for s, _ in mid]},
         "t1": [], "t2": [], "t2b": [], "t3": [], "t5": []}
    order = lambda r: ENGINES if r % 2 == 0 else ENGINES[::-1]

    log("== T1 first touch of never-opened files (paired by file)")
    allf = big + mid
    for i, (sz, p) in enumerate(allf):
        for e in (ENGINES if i % 2 == 0 else ENGINES[::-1]):
            res = unit(f"T1 {e}", lambda e=e: t1_first_touch(e, p, sz, ovh[e]))
            R["t1"].append({"engine": e, "file": i, **res}); log(f"  file{i} {e}: ttfb {res['ttfb_s']:.2f}s startup {res['startup_total_s']:.2f}s")

    log("== T2 cold sequential throughput (single continuous stream) + T2b warm re-read")
    for r in range(reps):
        f = slots.richest(big)[0]
        for e in order(r):
            res = unit(f"T2 {e}", lambda e=e, f=f: t2_cold_sequential(e, f[1], slots.take(f[1])))
            R["t2"].append({"engine": e, "rep": r, **{k: v for k, v in res.items() if k != "region"}})
            w = unit(f"T2b {e}", lambda e=e, res=res: t2b_warm_reread(e, res["region"]))
            R["t2b"].append({"engine": e, "rep": r, **(w or {})})
            log(f"  rep{r} {e}: cold {res['mbps']:.0f} MB/s (min chunk {res['min_chunk_mbps']:.0f}, stalls {res['stalls']}) warm {w['mbps']:.0f} MB/s")

    log("== T3 random seeks (cold, then warm)")
    for r in range(max(4, reps - 1)):
        f = slots.richest(big)[0]
        for e in order(r):
            res = unit(f"T3 {e}", lambda e=e, f=f, r=r: t3_seeks(e, f[1], f[0], slots, seed=100 + r))
            R["t3"].append({"engine": e, "rep": r, **res})
            c = sorted(res["cold_ms"]); log(f"  rep{r} {e}: cold p50 {statistics.median(c):.0f}ms max {c[-1]:.0f}ms")

    log("== T5 three concurrent streams (cold)")
    for r in range(max(4, reps - 1)):
        trio = slots.richest(big, 3)
        for e in order(r):
            res = unit(f"T5 {e}", lambda e=e: t5_concurrent(e, trio, slots))
            R["t5"].append({"engine": e, "rep": r, **res})
            log(f"  rep{r} {e}: per-stream {[round(x) for x in res['per_stream_mbps']]} MB/s")

    os.makedirs(RESULTS, exist_ok=True)
    out = os.path.join(RESULTS, time.strftime("bench-%Y%m%d-%H%M%S.json"))
    json.dump(R, open(out, "w"), indent=1)
    log("raw results saved:", out)
    return out


# ------------------------------------------------------------------ statistics + report
def summ(xs):
    xs = sorted(xs)
    if not xs:
        return {}
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return {"n": len(xs), "median": statistics.median(xs), "mean": statistics.mean(xs),
            "sd": statistics.pstdev(xs) if len(xs) > 1 else 0.0, "min": xs[0], "p95": q(0.95), "max": xs[-1]}


def boot_ratio(a, b, n=5000, seed=3):
    """median(b)/median(a) with a 95% bootstrap CI."""
    rng = random.Random(seed)
    rs = []
    for _ in range(n):
        ma = statistics.median(rng.choices(a, k=len(a))); mb = statistics.median(rng.choices(b, k=len(b)))
        if ma > 0:
            rs.append(mb / ma)
    rs.sort()
    return statistics.median(b) / statistics.median(a), rs[int(.025 * len(rs))], rs[int(.975 * len(rs))]


def perm_p(a, b, n=5000, seed=5):
    rng = random.Random(seed)
    obs = abs(statistics.median(a) - statistics.median(b)); pool = a + b; c = 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(statistics.median(pool[:len(a)]) - statistics.median(pool[len(a):])) >= obs:
            c += 1
    return (c + 1) / (n + 1)


def sign_p(wins, n):
    p = sum(math.comb(n, k) for k in range(min(wins, n - wins) + 1)) / 2 ** n
    return min(1.0, 2 * p)


def report(path):
    R = json.load(open(path))
    get = lambda sec, key, e: [x[key] for x in R[sec] if x["engine"] == e and x.get(key) is not None]
    lines = []
    P = lines.append
    P("# decypharr mount benchmark: rclone vs DFS (same account, same drive, live settings)\n")
    P(f"mount ready: rclone {R['setup']['mount_ready_s']['rclone']:.1f}s, DFS {R['setup']['mount_ready_s']['dfs']:.1f}s\n")

    def row(name, sec, key, unit, higher_better, scale=1.0):
        a, b = [v * scale for v in get(sec, key, "rclone")], [v * scale for v in get(sec, key, "dfs")]
        if len(a) < 2 or len(b) < 2:
            return
        sa, sb = summ(a), summ(b)
        ratio, lo, hi = boot_ratio(a, b); p = perm_p(a, b)
        better = "DFS" if (ratio > 1) == higher_better else "rclone"
        sig = "significant" if (lo > 1 or hi < 1) else "not significant"
        P(f"| {name} | {sa['median']:.1f} ±{sa['sd']:.1f} | {sb['median']:.1f} ±{sb['sd']:.1f} | {unit} | "
          f"{ratio:.2f}x [{lo:.2f}-{hi:.2f}] | p={p:.3f} | {better} ({sig}) |")

    hdr = "| metric | rclone median ±sd | DFS median ±sd | unit | DFS/rclone (95% CI) | perm. p | edge |\n|---|---|---|---|---|---|---|"
    P("## Cold reads, first touch (never-opened files; includes link generation)\n"); P(hdr)
    for k, n in (("ttfb_s", "time to first byte"), ("startup_total_s", "startup (first byte + head 16MiB + tail 16MiB)")):
        row(n, "t1", k, "s", False)
    P("\n## Cold sustained sequential (one continuous 4K stream, 384MiB of fresh data)\n"); P(hdr)
    row("throughput", "t2", "mbps", "MB/s", True); row("slowest 32MiB chunk", "t2", "min_chunk_mbps", "MB/s", True)
    row("first 32MiB chunk", "t2", "first_chunk_mbps", "MB/s", True)
    for e in ENGINES:
        st = get("t2", "stalls", e)
        P(f"\n* {e}: chunks slower than {NEED_MBPS:.0f} MB/s (a 4K-remux peak): {sum(st)} of {sum(x['chunks'] for x in R['t2'] if x['engine'] == e)}")
    P("\n## Warm re-read (cache hit, same drive for both)\n"); P(hdr); row("re-read throughput", "t2b", "mbps", "MB/s", True)
    P("\n## Random seeks, 4MiB reads (latency, lower is better)\n"); P(hdr)
    for kind in ("cold_ms", "warm_ms"):
        a = [v for x in R["t3"] if x["engine"] == "rclone" for v in x[kind]]; b = [v for x in R["t3"] if x["engine"] == "dfs" for v in x[kind]]
        if a and b:
            sa, sb = summ(a), summ(b); ratio, lo, hi = boot_ratio(a, b); p = perm_p(a, b)
            P(f"| {kind[:4]} seek p50 | {sa['median']:.0f} | {sb['median']:.0f} | ms | {ratio:.2f}x [{lo:.2f}-{hi:.2f}] | p={p:.3f} | "
              f"{'DFS' if ratio < 1 else 'rclone'} ({'significant' if (lo > 1 or hi < 1) else 'not significant'}) |")
            P(f"| {kind[:4]} seek p95 / max | {sa['p95']:.0f} / {sa['max']:.0f} | {sb['p95']:.0f} / {sb['max']:.0f} | ms | | | |")
    P("\n## Three concurrent streams (a busy household)\n"); P(hdr)
    for k, n in (("aggregate_mbps", "aggregate"), ("min_stream_mbps", "slowest stream")):
        row(n, "t5", k, "MB/s", True)
    P("\n## Resource use (container)\n\n| | CPU max % | RAM max MiB |\n|---|---|---|")
    for e in ENGINES:
        cpu = [x["cpu_max"] for sec in ("t2", "t5") for x in R[sec] if x["engine"] == e]
        mem = [x["mem_max_mib"] for sec in ("t2", "t5") for x in R[sec] if x["engine"] == e]
        P(f"| {e} | {max(cpu):.0f} | {max(mem):.0f} |")
    P("\n## Paired first-touch comparison per file (DFS vs rclone startup time)\n")
    for i in sorted({x["file"] for x in R["t1"]}):
        a = next(x for x in R["t1"] if x["file"] == i and x["engine"] == "rclone"); b = next(x for x in R["t1"] if x["file"] == i and x["engine"] == "dfs")
        P(f"* file {i}: rclone {a['startup_total_s']:.2f}s, DFS {b['startup_total_s']:.2f}s")
    wins = sum(1 for i in {x['file'] for x in R['t1']}
               if next(x for x in R['t1'] if x['file'] == i and x['engine'] == 'dfs')['startup_total_s'] <
               next(x for x in R['t1'] if x['file'] == i and x['engine'] == 'rclone')['startup_total_s'])
    n = len({x['file'] for x in R['t1']}); P(f"\nDFS started faster in {wins} of {n} files (sign test p={sign_p(wins, n):.3f})")
    txt = "\n".join(lines)
    outp = path.replace(".json", ".md"); open(outp, "w").write(txt)
    print(txt); log("report saved:", outp)


# ------------------------------------------------------------------ cleanup
def cleanup():
    for c in CONTAINER.values():
        run(["docker", "rm", "-f", c])
    time.sleep(1)
    with open("/proc/self/mountinfo") as f:
        stuck = [l for l in f if "decypharr-bench" in l]
    if stuck:
        log("WARNING: benchmark mounts still present; not deleting the scratch dir:", len(stuck))
        return
    real = os.path.realpath(BENCH)
    if real == os.path.join(os.path.realpath(HOME), "decypharr-bench") and not os.path.ismount(real):
        shutil.rmtree(real, ignore_errors=True)
        log("scratch dir removed")
    log("live mount healthy:", live_ok())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["setup", "run", "report", "cleanup", "all"])
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--results")
    a = ap.parse_args()
    try:
        if a.stage in ("setup", "all"):
            setup()
        if a.stage in ("run", "all"):
            path = run_bench(a.reps); report(path)
        if a.stage == "report":
            report(a.results)
    finally:
        if a.stage in ("cleanup", "all"):
            cleanup()


if __name__ == "__main__":
    main()
