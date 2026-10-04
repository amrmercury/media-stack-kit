#!/usr/bin/env python3
"""Front-loaded questionnaire. Collects EVERYTHING interactive up front, then the install runs unattended.

Writes <state>/answers.json (0600). Also usable non-interactively:  wizard.py --answers file.json
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hardware
from common import info, ok, warn, fail, save_json, load_json

B = "\033[1m" if sys.stdout.isatty() else ""
D = "\033[2m" if sys.stdout.isatty() else ""
Y = "\033[33m" if sys.stdout.isatty() else ""
R = "\033[0m" if sys.stdout.isatty() else ""

DEBRID = {
    "realdebrid": {"label": "Real-Debrid", "url": "https://real-debrid.com/apitoken",
                   "hint": "API token: real-debrid.com/apitoken"},
    "alldebrid": {"label": "AllDebrid", "url": "https://alldebrid.com/apikeys",
                  "hint": "API key: alldebrid.com/apikeys"},
    "torbox": {"label": "TorBox", "url": "https://torbox.app/settings",
               "hint": "API key: torbox.app/settings"},
}


def ask(prompt, default=None, secret=False, validate=None, allow_empty=False):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = input(f"{prompt}{suffix}: ").strip()      # always visible: you can see (and check) what you type or paste
        if not raw and default is not None:
            raw = default
        if not raw and not allow_empty:
            warn("This can't be empty.")
            continue
        if validate:
            err = validate(raw)
            if err:
                warn(err)
                continue
        return raw


def yesno(prompt, default=False):
    d = "Y/n" if default else "y/N"
    while True:
        r = input(f"{prompt} ({d}): ").strip().lower()
        if not r:
            return default
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False


def header(t):
    print(f"\n{B}━━ {t} ━━{R}")


# ---------------------------------------------------------------- sections
def sec_account(a):
    header("1/7  Your login")
    print("One username + password for the whole stack (Jellyfin, Jellyseerr, Sonarr, Radarr,\n"
          "Prowlarr, Bazarr, decypharr). The installer sets it in every app for you.")

    def vu(v):
        return None if re.fullmatch(r"[A-Za-z0-9._-]{3,32}", v) else \
            "3-32 characters: letters, numbers, dot, dash, underscore (no spaces or @)."

    a["admin_user"] = ask("Username", validate=vu)

    def vp(v):
        return None if len(v) >= 8 else "Use at least 8 characters."

    a["admin_pass"] = ask("Password", validate=vp)


def _fmt_free(gb):
    return f"{gb / 1024:.1f} TB free" if gb >= 1024 else f"{gb:.0f} GB free"


def _writable_hint(path):
    return "" if os.access(path, os.W_OK) else "  (not writable by you)"


def browse_folder(start):
    """Numbered folder browser. Returns the chosen folder, or None to go back to the drive menu."""
    cur, page = os.path.abspath(start), 0
    while True:
        try:
            dirs = sorted(d for d in os.listdir(cur)
                          if not d.startswith(".") and os.path.isdir(os.path.join(cur, d)) and os.access(os.path.join(cur, d), os.R_OK))
        except OSError:
            warn(f"Can't open {cur}"); cur = os.path.dirname(cur) or "/"; continue
        per = 15
        chunk = dirs[page * per:(page + 1) * per]
        try:
            free = _fmt_free(hardware.shutil.disk_usage(cur).free / 1024 ** 3)
        except OSError:
            free = "?"
        print(f"\n  {B}{cur}{R}   {D}({free}){_writable_hint(cur)}{R}")
        for i, d in enumerate(chunk, 1):
            print(f"   {i:>2}) {d}/")
        if not dirs:
            print(f"   {D}(no folders here){R}")
        more = (page + 1) * per < len(dirs)
        print(f"   {D}number = open · u = up · s = use THIS folder · n = new folder here · h = home · d = back to drives"
              + (" · m = more" if more else "") + R)
        c = input("  > ").strip().lower()
        if c.isdigit() and 1 <= int(c) <= len(chunk):
            cur, page = os.path.join(cur, chunk[int(c) - 1]), 0
        elif c == "u":
            cur, page = os.path.dirname(cur) or "/", 0
        elif c == "h":
            cur, page = os.path.expanduser("~"), 0
        elif c == "d":
            return None
        elif c == "m" and more:
            page += 1
        elif c == "n":
            name = input("  New folder name: ").strip()
            if not re.fullmatch(r"[^/\\\0]{1,64}", name or "") or name in (".", ".."):
                warn("Use a simple name (no slashes)."); continue
            try:
                os.makedirs(os.path.join(cur, name), exist_ok=True)
                cur, page = os.path.join(cur, name), 0
            except OSError as e:
                warn(f"Couldn't create it: {e.strerror}")
        elif c == "s":
            if os.access(cur, os.W_OK):
                return cur
            warn("You can't write to this folder; pick another.")
        else:
            warn("Type a number from the list, or one of the letters shown.")


def pick_library_folder():
    opts = hardware.drive_options()
    while True:
        print("\n  Where should your library go?")
        for i, o in enumerate(opts, 1):
            where = o["path"]
            print(f"   {i}) {o['label']:<14} {D}{where} · {o['kind']} · {_fmt_free(o['free_gb'])}{R}")
        print(f"   B) Browse for another folder")
        c = ask("Choose", default="1", validate=lambda v: None if v.lower() == "b" or (v.isdigit() and 1 <= int(v) <= len(opts)) else "Pick a number from the list, or B.").lower()
        base = browse_folder(os.path.expanduser("~")) if c == "b" else opts[int(c) - 1]["path"]
        if base is None:
            continue
        name = ask("Name for the library folder", default="Media",
                   validate=lambda v: None if re.fullmatch(r"[A-Za-z0-9 ._-]{1,64}", v) else "Use letters, numbers, spaces, dot, dash or underscore.")
        path = os.path.join(base, name)
        print(f"  {D}Your library will be at: {path}{R}")
        return path


def sec_paths(a, hw):
    header("2/7  Where should your library live?")
    print("Series and movies appear here as links to your debrid account, so this folder stays small.\n"
          "Pick the drive you want; the fast-cache choice comes later.")
    a["media_root"] = pick_library_folder()
    a["stack_dir"] = os.path.expanduser("~/media-stack")       # settings live here; no need to ask
    a["timezone"] = _guess_tz()                                 # taken from this computer
    a["host_ip"] = hw["lan_ip"]


def _guess_tz():
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return open("/etc/timezone").read().strip() or "UTC"
    except OSError:
        return "UTC"


def sec_debrid(a):
    header("3/7  Debrid accounts")
    print("Real-Debrid, AllDebrid and TorBox are all supported. Add as many keys as you like, in any\n"
          "mix; the first one is used first. You need at least one. (Keys are shown as you paste them.)")
    keys = []
    while True:
        print(f"\n  1) {DEBRID['realdebrid']['label']}   2) {DEBRID['alldebrid']['label']}   "
              f"3) {DEBRID['torbox']['label']}")
        c = ask("Which service", validate=lambda v: None if v in ("1", "2", "3") else "Type 1, 2 or 3.")
        prov = ["realdebrid", "alldebrid", "torbox"][int(c) - 1]
        print(f"  {D}Get it here: {DEBRID[prov]['hint']}{R}")
        k = ask(f"{DEBRID[prov]['label']} API key", secret=True)
        if (prov, k) in [(x["provider"], x["api_key"]) for x in keys]:
            warn("You already added that exact key.")
        else:
            keys.append({"provider": prov, "api_key": k})
            ok(f"Added {DEBRID[prov]['label']} key #{len([x for x in keys if x['provider'] == prov])}")
        if not yesno("Add another key?", default=False):
            break
    a["debrid"] = keys


def sec_indexers(a):
    print("Most indexers need nothing (ArabTorrents works without an account, so it's simply on). ArabicSource needs an\n"
          "API key from your own account. Press Enter to skip; a skipped indexer is simply left switched off.")
    idx = {}
    print(f"\n{B}ArabicSource{R}  {D}an extra source of Arabic series and movies for a bigger coverage. Sign up for free at https://arabicsource.net  (key: My Settings -> API Key tab){R}")
    k = ask("  API key (Enter to skip)", allow_empty=True, secret=True)
    if k:
        idx["arabicsource"] = {"apikey": k}
    a["indexer_accounts"] = idx


def sec_arabic(a):
    print("If you want Arabic series and movies in your library, set up Arabarr. It needs YOUR OWN free account on ArabP2P\n"
          "and a free API key from TMDB. (Please don't share one person's tracker account: trackers can ban\n"
          "accounts that log in from several places.)")
    a["enable_arabarr"] = False
    if not yesno("\nSet up Arabarr?", default=False):
        return
    print(f"\n{B}ArabP2P{R}\n"
          f"      Sign up for free at: https://www.arabp2p.net/index.php?page=signup")
    u = ask("      Username (or type S to skip Arabarr)")
    if u.lower() == "s":
        warn("Skipped: no Arabarr setup. Re-run the installer any time to add it.")
        return
    pw = ask("      Password", secret=True)
    print(f"\n{B}TMDB API key{R}  (Arabarr uses it to match Arabic titles)\n"
          f"      Sign up for free at: https://www.themoviedb.org/signup  (your key is under Settings -> API)")
    k = ask("      API key (or type S to skip Arabarr)", secret=True)
    if k.lower() == "s":
        warn("Skipped: no Arabarr setup. Re-run the installer any time to add it.")
        return
    a["indexer_accounts"]["arabp2p"] = {"username": u, "password": pw}   # same account feeds the ArabP2P indexer
    a["tmdb_api_key"] = k
    a["enable_arabarr"] = True


def sec_subtitles(a):
    header("5/7  Subtitles (English + Arabic)")
    print("Bazarr downloads subtitles from the providers you set up here. All are free to sign up.\n"
          f"{Y}OpenSubtitles free accounts are capped at 20 subtitles/day. Subsource and SubDL have no daily\n"
          f"limit and are the best Arabic sources, so adding all three is recommended.{R}")
    subs = {}
    print(f"\n[1/3] {B}OpenSubtitles{R}  - 20 files/day (free) · Arabic ✅\n"
          f"      Sign up for free at: https://www.opensubtitles.com\n"
          f"      {Y}⚠ Use your account USERNAME - not your email address.{R}")
    u = ask("      Username (or type S to skip)")
    if u.lower() != "s":
        subs["opensubtitlescom"] = {"username": u, "password": ask("      Password", secret=True)}
    print(f"\n[2/3] {B}Subsource{R}  - no daily limit · best Arabic source ✅\n"
          f"      Sign up for free at: https://subsource.net\n      Your API key is at the bottom of: https://subsource.net/dashboard/profile")
    k = ask("      API key (or type S to skip)", secret=True)
    if k.lower() != "s":
        subs["subsource"] = {"apikey": k}
    print(f"\n[3/3] {B}SubDL{R}  - no daily limit · Arabic ✅\n"
          f"      Sign up for free at: https://subdl.com\n      Create your API key at: https://subdl.com/panel/api")
    k = ask("      API key (or type S to skip)", secret=True)
    if k.lower() != "s":
        subs["subdl"] = {"api_key": k}
    a["subtitles"] = subs
    if not subs:
        warn("All skipped: Bazarr will start completely fresh with no providers or language profile.")


def sec_cache(a, hw):
    header("6/7  Cache (optional speed-up)")
    print("A cache keeps recently watched files on a local drive so replays, seeking and several people watching\n"
          "at once stay smooth. Turning it on also switches your debrid mount to the faster DFS engine (tested:\n"
          "quicker cold reads, re-reads and seeks, better with several streams, far less RAM). Playback works fine\n"
          "without it; it only helps on a fast SSD, so it is only offered when one is found.")
    cands = [c for c in hardware.cache_candidates() if c["suitable"]]
    a["cache"] = {"path": None}
    if not cands:
        print(f"\n  {D}No fast SSD with {hardware.MIN_CACHE_FREE_GB}+ GB free was found (a cache on a slow hard drive would "
              f"compete with playback), so the cache stays off and the standard mount is used. Everything works.{R}")
        return
    cands.sort(key=lambda c: -(c["iops"] or 0))
    print("\n  Suitable drives found:")
    for i, c in enumerate(cands, 1):
        gb = hardware.suggested_cache_gb(c["free_gb"])
        sp = f"{c['iops']} random reads/s" if c["iops"] else "SSD"
        print(f"   {i}) {c['mount']:<22} {c['free_gb']:.0f} GB free · {sp}  → would use up to {B}{gb} GB{R}")
    print(f"   {D}The cache only fills as you watch things, up to that limit, and cleans itself after 72h.{R}")
    if not yesno("Enable the cache? (recommended)", default=True):
        return
    pick = cands[0]
    if len(cands) > 1:
        n = ask("Which drive", default="1",
                validate=lambda v: None if v.isdigit() and 1 <= int(v) <= len(cands) else "Pick a number from the list.")
        pick = cands[int(n) - 1]
    gb = hardware.suggested_cache_gb(pick["free_gb"])
    sz = ask(f"Maximum cache size in GB (it will use up to this much of {pick['mount']})", default=str(gb),
             validate=lambda v: None if v.isdigit() and 5 <= int(v) <= pick["free_gb"] else
             f"Enter a whole number between 5 and {int(pick['free_gb'])}.")
    a["cache"] = {"path": os.path.join(pick["dir"], "media-stack-cache"), "size_gb": int(sz)}


def sec_confirm(a, hw):
    header("7/7  Review")
    nic = hw["nic"]
    link = ("wired " + (f"{nic['speed_mbps']} Mbps" if nic["speed_mbps"] else "")) if nic["wired"] else "Wi-Fi"
    print(f"  This machine: {hw['cpu']['model']} · {hw['ram_gb']} GB RAM · network: {link}")
    print(f"  {D}4K remux files are large (50-100+ Mbps): a wired connection is best, fast Wi-Fi also works.\n"
          f"  Playback is set to direct play by default, so even an older CPU handles it.{R}\n")
    print(f"  Login:        {a['admin_user']}")
    print(f"  Library:      {a['media_root']}")
    print(f"  Settings in:  {a['stack_dir']}     Timezone: {a['timezone']}")
    print(f"  Debrid keys:  {', '.join(DEBRID[k['provider']]['label'] for k in a['debrid'])}")
    print(f"  Subtitles:    {', '.join(a['subtitles']) or 'skipped'}")
    print(f"  Arabic series and movies (Arabarr): {'yes' if a['enable_arabarr'] else 'no'}")
    c = a["cache"]
    print(f"  Cache:        {('up to %d GB at %s (fast DFS mount)' % (c['size_gb'], c['path'])) if c.get('path') else 'off (standard mount)'}")
    return yesno("\nLooks right? Start the install", default=True)


# ---------------------------------------------------------------- main
def run_interactive():
    hw = hardware.detect()
    print(f"{B}Media stack installer{R}: answer a few questions now, then it runs by itself.")
    a = {"hw": hw}
    while True:
        sec_account(a)
        sec_paths(a, hw)
        sec_debrid(a)
        header("4/7  Arabic series and movies (optional, skip if you only watch English)")
        sec_indexers(a)
        sec_arabic(a)
        sec_subtitles(a)
        sec_cache(a, hw)
        if sec_confirm(a, hw):
            return a
        print("\nOkay, let's go through it again.")


def validate_answers(a):
    need = ["admin_user", "admin_pass", "media_root", "stack_dir", "timezone", "debrid"]
    miss = [k for k in need if not a.get(k)]
    if miss:
        raise SystemExit(f"answers file is missing: {', '.join(miss)}")
    a.setdefault("hw", hardware.detect())
    a.setdefault("host_ip", a["hw"]["lan_ip"])
    a.setdefault("indexer_accounts", {})
    a.setdefault("subtitles", {})
    a.setdefault("cache", {})
    if not a["cache"].get("path"):            # no cache drive (also reads older answer files)
        a["cache"] = {"path": None}
    a.setdefault("tmdb_api_key", "")
    a.setdefault("enable_arabarr", bool(a["indexer_accounts"].get("arabp2p") and a["tmdb_api_key"]))
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--answers", help="skip the questions and use this JSON file")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    kit = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = args.out or os.path.join(kit, "state", "answers.json")
    if args.answers:
        a = validate_answers(json.load(open(args.answers)))
    else:
        if not sys.stdin.isatty():
            raise SystemExit("No terminal attached. Run interactively, or pass --answers file.json")
        a = run_interactive()
    a["media_root"] = os.path.abspath(os.path.expanduser(a["media_root"]))
    a["stack_dir"] = os.path.abspath(os.path.expanduser(a["stack_dir"]))
    save_json(out, a)
    ok(f"Answers saved to {out} (private, only you can read it)")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was installed.")
        sys.exit(130)
