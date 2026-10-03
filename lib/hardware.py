"""Hardware detection: RAM/CPU/NIC (informational) and cache-drive suitability (decision input)."""
import mmap, os, random, re, shutil, subprocess, time

MIN_CACHE_FREE_GB = 40          # below this a cache can't hold even one big remux plus headroom
CACHE_MAX_GB = 200              # the author's own setting
CACHE_FREE_FRACTION = 0.5       # never offer more than half the free space
MIN_IOPS = 1000                 # 4K random reads, QD1. HDDs do ~80-150; even cheap SATA SSDs do >3000.
_PSEUDO_FS = {"tmpfs", "devtmpfs", "proc", "sysfs", "overlay", "squashfs", "fuse", "fuse.rclone",
              "fuse.mergerfs", "nfs", "nfs4", "cifs", "smb3", "9p", "ramfs", "efivarfs", "autofs"}


def ram_gb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / 1024 / 1024
    return 0.0


def cpu():
    model, cores = "unknown CPU", os.cpu_count() or 1
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"model": model, "cores": cores}


def nic():
    """Default-route interface: wired/wifi and negotiated link speed if known."""
    dev = None
    try:
        out = subprocess.run(["ip", "-o", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
        m = re.search(r"\bdev (\S+)", out)
        dev = m.group(1) if m else None
    except FileNotFoundError:
        pass
    if not dev:
        return {"dev": None, "wired": None, "speed_mbps": None}
    wifi = os.path.isdir(f"/sys/class/net/{dev}/wireless")
    speed = None
    try:
        s = int(open(f"/sys/class/net/{dev}/speed").read().strip())
        speed = s if s > 0 else None
    except (OSError, ValueError):
        pass
    return {"dev": dev, "wired": not wifi, "speed_mbps": speed}


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ------------------------------------------------------------------ storage
def _mounts():
    seen = {}
    with open("/proc/self/mounts") as f:
        for line in f:
            dev, mp, fs, *_ = line.split()
            mp = mp.replace("\\040", " ")
            seen[mp] = (dev, fs)
    return seen


def _parent_block(dev):
    """/dev/sda1 -> sda ; /dev/nvme0n1p2 -> nvme0n1 ; dm-* -> first slave, recursively. None if unknown."""
    name = os.path.basename(os.path.realpath(dev))
    sysp = f"/sys/class/block/{name}"
    if not os.path.exists(sysp):
        return None
    real = os.path.realpath(sysp)
    if os.path.exists(os.path.join(real, "partition")):
        real = os.path.dirname(real)
    base = os.path.basename(real)
    slaves = os.path.join(real, "slaves")
    if base.startswith("dm-") and os.path.isdir(slaves) and os.listdir(slaves):
        return _parent_block("/dev/" + sorted(os.listdir(slaves))[0])
    return base


def is_rotational(dev):
    """True/False, or None when it can't be determined (btrfs/zfs pools, odd virtual devices)."""
    base = _parent_block(dev) if dev.startswith("/dev/") else None
    if not base:
        return None
    try:
        return open(f"/sys/block/{base}/queue/rotational").read().strip() == "1"
    except OSError:
        return None


def _aligned_buf(size):
    return mmap.mmap(-1, size)  # page-aligned, which is what O_DIRECT needs


def random_read_iops(directory, seconds=2.0, size_mb=256):
    """4K random reads, queue-depth 1, bypassing the page cache. None if O_DIRECT isn't possible here."""
    path = os.path.join(directory, f".stack-iobench-{os.getpid()}")
    try:
        with open(path, "wb") as f:
            blk = os.urandom(1024 * 1024)
            for _ in range(size_mb):
                f.write(blk)
            f.flush()
            os.fsync(f.fileno())
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
        except OSError:
            return None
        try:
            buf = _aligned_buf(4096)
            blocks = size_mb * 1024 * 1024 // 4096
            n, t0 = 0, time.time()
            while time.time() - t0 < seconds:
                os.preadv(fd, [buf], random.randrange(blocks) * 4096)
                n += 1
            return n / (time.time() - t0)
        except OSError:
            return None
        finally:
            os.close(fd)
    except OSError:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _writable_dir_for(mp):
    home = os.path.expanduser("~")
    if mp in ("/", "/home") or home.startswith(mp.rstrip("/") + "/") and mp != "/":
        return home if os.access(home, os.W_OK) else None
    return mp if os.access(mp, os.W_OK) else None


def suggested_cache_gb(free_gb):
    return int(max(10, min(CACHE_MAX_GB, free_gb * CACHE_FREE_FRACTION)))


def cache_candidates(benchmark=True):
    """Every real, writable filesystem, annotated with whether it's a sensible cache location."""
    out = []
    for mp, (dev, fs) in sorted(_mounts().items()):
        if fs in _PSEUDO_FS or not dev.startswith("/dev/"):
            if not (fs in ("btrfs", "zfs", "xfs", "ext4", "f2fs") and dev.startswith("/dev/")):
                continue
        if any(mp.startswith(p) for p in ("/boot", "/snap", "/var/lib/docker", "/sys", "/proc", "/run")):
            continue
        wdir = _writable_dir_for(mp)
        if not wdir:
            continue
        try:
            st = os.statvfs(mp)
        except OSError:
            continue
        free_gb = st.f_bavail * st.f_frsize / 1024 ** 3
        rot = is_rotational(dev)
        c = {"mount": mp, "dir": wdir, "device": dev, "fs": fs, "free_gb": round(free_gb, 1),
             "rotational": rot, "iops": None, "suitable": False, "reason": ""}
        if free_gb < MIN_CACHE_FREE_GB:
            c["reason"] = f"only {free_gb:.0f} GB free (need {MIN_CACHE_FREE_GB}+)"
        elif rot is True:
            c["reason"] = "spinning hard drive: a cache on it competes with playback reads"
        else:
            iops = random_read_iops(wdir) if benchmark else None
            c["iops"] = None if iops is None else round(iops)
            if iops is not None and iops < MIN_IOPS:
                c["reason"] = f"too slow for a cache ({iops:.0f} random reads/s, need {MIN_IOPS}+)"
            elif iops is None and rot is None:
                c["reason"] = "could not verify drive speed"
            else:
                c["suitable"] = True
        out.append(c)
    return out


def drive_options():
    """Places a library could live: the home folder plus every real drive the user can write to. No benchmark."""
    home = os.path.expanduser("~")
    mounts = _mounts()

    def kind_of(path):
        best = max((m for m in mounts if path == m or path.startswith(m.rstrip("/") + "/")), key=len, default="/")
        dev = mounts.get(best, ("", ""))[0]
        r = is_rotational(dev) if dev.startswith("/dev/") else None
        return "hard drive" if r is True else "SSD" if r is False else "drive"

    def free_gb(path):
        try:
            st = os.statvfs(path)
            return st.f_bavail * st.f_frsize / 1024 ** 3
        except OSError:
            return 0.0

    out = [{"label": "Home folder", "path": home, "free_gb": free_gb(home), "kind": kind_of(home)}]
    for mp, (dev, fs) in sorted(mounts.items()):
        if mp in ("/", "/home") or fs in _PSEUDO_FS or not dev.startswith("/dev/"):
            continue
        if any(mp.startswith(p) for p in ("/boot", "/snap", "/var", "/sys", "/proc", "/run/user", "/run/snapd")):
            continue
        if not os.access(mp, os.W_OK):
            continue
        out.append({"label": os.path.basename(mp.rstrip("/")) or mp, "path": mp,
                    "free_gb": free_gb(mp), "kind": kind_of(mp)})
    return out


def detect():
    return {"ram_gb": round(ram_gb(), 1), "cpu": cpu(), "nic": nic(), "lan_ip": lan_ip()}


def media_fs_free_gb(path):
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return shutil.disk_usage(p or "/").free / 1024 ** 3
