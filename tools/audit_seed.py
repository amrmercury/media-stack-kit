#!/usr/bin/env python3
"""Fail if any real secret / personal value from the author's live stack appears in the kit.

Author-side gate: run before sharing the kit. Collects every secret-looking value from the
live config files, then greps the whole kit (excluding this tool's own output) for them.
Exit code 1 on any hit.
"""
import glob, json, os, re, sys

D = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/docker")
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SENS = re.compile(r"(key|token|pass|secret|cookie|user|uid|pwd)", re.I)
IGNORE = {"forms", "True", "False", "enabled", "Sonarr", "Radarr", "main", "plex", "jellyfin", "form",
          "radarr", "sonarr", "None", "none", "disabled", "external"}
secrets = set()


def add(v):
    if isinstance(v, str) and len(v) >= 5 and v not in IGNORE:
        secrets.add(v)


def walk(o, k=""):
    if isinstance(o, dict):
        for a, b in o.items():
            walk(b, a)
    elif isinstance(o, list):
        for x in o:
            walk(x, k)
    elif isinstance(o, str) and SENS.search(k):
        add(o)


def safe(fn):
    try:
        fn()
    except Exception as e:  # a missing live file just means fewer values to check
        print("  (skipped a source:", e, ")")


for app in ("sonarr", "radarr", "prowlarr"):
    safe(lambda a=app: add(re.search(r"<ApiKey>([^<]+)", open(f"{D}/arr/{a}/config.xml").read()).group(1)))
safe(lambda: walk(json.load(open(f"{D}/arr/decypharr/config.json"))))
safe(lambda: walk(json.load(open(f"{D}/arr/decypharr/auth.json"))))
safe(lambda: [add(l.strip().split("=", 1)[1]) for l in open(f"{D}/arabarr/.env") if "=" in l])
safe(lambda: [add(m.group(1)) for l in open(f"{D}/babysitarr/docker-compose.yml")
              for m in [re.search(r"=([A-Za-z0-9]{24,})\s*$", l)] if m])
safe(lambda: walk(json.load(open(f"{D}/arr/jellyseerr/settings.json"))))


def _bazarr():
    import yaml
    c = yaml.safe_load(open(f"{D}/arr/bazarr/config/config.yaml"))
    for sec in ("opensubtitlescom", "subdl", "subsource", "sonarr", "radarr", "auth", "general"):
        walk(c.get(sec, {}))


safe(_bazarr)


def _plugin_xml():
    d = f"{D}/jellyfin/config/plugins/configurations"
    for fn in os.listdir(d):
        if fn.endswith(".xml"):
            for m in re.finditer(r"<[A-Za-z]*(?:Secret|Token|Password|ApiKey)[A-Za-z]*>([^<]{5,})</", open(f"{d}/{fn}").read()):
                add(m.group(1))


safe(_plugin_xml)
import socket
_s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    _s.connect(("10.255.255.255", 1))
    secrets.add(_s.getsockname()[0])        # this machine's LAN address
finally:
    _s.close()
secrets.add(socket.gethostname())
secrets |= {x for x in os.environ.get("AUDIT_EXTRA", "").split(",") if x}   # e.g. your email address

hits = set()
for f in glob.glob(f"{KIT}/**/*", recursive=True):
    if not os.path.isfile(f) or "/.git/" in f or f.endswith("audit_seed.py") or "/state/" in f:
        continue
    try:
        t = open(f, errors="ignore").read()
    except OSError:
        continue
    for s in secrets:
        if s in t:
            hits.add((os.path.relpath(f, KIT), s[:3] + "…" + str(len(s))))
print(f"audited {len(secrets)} secret/personal values against the kit")
if hits:
    print("LEAKS FOUND:")
    for h in sorted(hits):
        print("  ", h)
    sys.exit(1)
print("clean: no secret or personal value from the live stack is in the kit")
