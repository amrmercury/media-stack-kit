"""Homepage dashboard: generated LAST, from the containers that are actually running.

Anything that is up gets a tile (with a live widget where Homepage has one); anything that isn't deployed
(e.g. Arabarr when the friend skipped it) simply isn't listed, so the dashboard can never drift from reality.
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import compose, docker, info, ok, write_file

# service -> (group, title, description, icon, internal URL for widgets, widget type, container port)
CATALOG = [
    ("jellyfin", "Media", "Jellyfin", "Media server", "jellyfin.png", "http://jellyfin:8096", "jellyfin", 8096),
    ("jellyseerr", "Media", "Jellyseerr", "Requests", "jellyseerr.png", None, None, 5055),
    ("arabarr", "Automation", "Arabarr", "Arabic series proxy", "/icons/arabarr.png", None, None, 5011),
    ("sonarr", "Automation", "Sonarr", "TV", "sonarr.png", "http://sonarr:8989", "sonarr", 8989),
    ("radarr", "Automation", "Radarr", "Movies", "radarr.png", "http://radarr:7878", "radarr", 7878),
    ("prowlarr", "Automation", "Prowlarr", "Indexers", "prowlarr.png", "http://prowlarr:9696", "prowlarr", 9696),
    ("bazarr", "Automation", "Bazarr", "Subtitles", "bazarr.png", "http://bazarr:6767", "bazarr", 6767),
    ("recyclarr", "Automation", "Recyclarr", "Syncs TRaSH Guides custom formats (AI-upscale blocking) into Sonarr/Radarr",
     "recyclarr.png", None, None, None),
    ("babysitarr", "Automation", "Babysitarr", "Pipeline health monitor & stuck-download recovery",
     "mdi-baby-carriage", None, None, 8284),
    ("pearlarr", "Automation", "Pearlarr", "Grabs SeaDex-recommended releases for Sonarr anime",
     "mdi-diamond-stone", None, None, None),
    ("decypharr", "Downloads", "Decypharr", "Debrid client + symlink repair", "qbittorrent.png", None, None, 8282),
    ("flaresolverr", "Downloads", "FlareSolverr", "Cloudflare bypass", "flaresolverr.png", None, None, 8191),
]


def running_services(stack_dir):
    """{service: container_name} for everything in the compose project that is currently up."""
    out = compose(stack_dir, "ps", "--format", "json", "--status", "running").stdout
    res = {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            d = json.loads(line)
            res[d["Service"]] = d["Name"]
    return res


def generate(a, S, jellyfin_key):
    stack = a["stack_dir"]
    ports = a.get("ports", {})
    running = running_services(stack)
    keys = {"jellyfin": jellyfin_key, "sonarr": S["sonarr"], "radarr": S["radarr"],
            "prowlarr": S["prowlarr"], "bazarr": S["bazarr"]}
    groups = {}
    for svc, group, title, desc, icon, wurl, wtype, cport in CATALOG:
        if svc not in running:
            continue
        host_port = ports.get(svc, cport)
        entry = {"description": desc, "icon": icon, "server": "my-docker", "container": running[svc]}
        if host_port:
            entry["href"] = f"http://{a['host_ip']}:{host_port}"
        if wtype:
            entry["widget"] = {"type": wtype, "url": wurl, "key": keys[wtype]}
            if wtype == "jellyfin":
                entry["widget"].update(enableBlocks=True, enableNowPlaying=True)
        groups.setdefault(group, []).append({title: entry})
    write_file(os.path.join(stack, "homepage/services.yaml"), to_yaml(groups) + "\n", mode=0o600)
    ok(f"Homepage lists {sum(len(v) for v in groups.values())} services: "
       + ", ".join(next(iter(e)) for g in groups.values() for e in g))
    return running


def to_yaml(groups):
    from render import to_yaml as ty
    # Homepage wants:  - Group:\n    - Title:\n        key: value
    doc = [{g: items} for g, items in groups.items()]
    return ty(doc)
