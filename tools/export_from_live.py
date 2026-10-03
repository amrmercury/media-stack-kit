#!/usr/bin/env python3
"""Snapshot the *settings* of the author's live stack into seed/ (no secrets, no content).

Run on the author's machine only. Re-run any time the live settings change, then
commit the diff. Everything that identifies an account (API keys, passwords,
usernames, cookies, debrid tokens) is stripped; the installer wizard asks each
friend for their own.

    python3 tools/export_from_live.py [--docker-dir ~/docker]
"""
import argparse, json, os, re, shutil, sys, urllib.request, urllib.error

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED = os.path.join(KIT, "seed")
SECRET_FIELD = re.compile(r"(key|pass|secret|token|cookie|user|uid|pwd|auth|login|email)", re.I)
import socket


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


LIVE_HOST = os.environ.get("LIVE_HOST") or _lan_ip()      # this machine's LAN address -> placeholder
LIVE_NAMES = {socket.gethostname(): "__SERVERNAME__"}     # and its hostname


def scrub(o):
    """Recursively replace the author's LAN IP / hostname with installer placeholders."""
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    if isinstance(o, list):
        return [scrub(v) for v in o]
    if isinstance(o, str):
        o = o.replace(LIVE_HOST, "__HOST__")
        for a, b in LIVE_NAMES.items():
            o = o.replace(a, b)
    return o


_XML_SECRET = re.compile(r"(<[A-Za-z]*(?:Secret|Token|Password|ApiKey)[A-Za-z]*>)[^<]+(</)")


def copy_text(src, dst):
    t = open(src, encoding="utf-8").read().replace(LIVE_HOST, "__HOST__")
    t = _XML_SECRET.sub(r"\1__SECRET__\2", t)
    for a, b in LIVE_NAMES.items():
        t = t.replace(a, b)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "w", encoding="utf-8").write(t)


def api_key(cfg_dir):
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(os.path.join(cfg_dir, "config.xml")).read())
    return m.group(1)


def get(base, key, path):
    req = urllib.request.Request(f"{base}{path}", headers={"X-Api-Key": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def dump(rel, obj):
    path = os.path.join(SEED, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(scrub(obj), f, indent=2, sort_keys=True)
        f.write("\n")
    print("  wrote", rel)


def strip_fields(fields):
    """Turn [{'name':..,'value':..}] into {name: value}, blanking anything secret."""
    out = {}
    for f in fields or []:
        n, v = f["name"], f.get("value")
        if f.get("privacy") in ("apiKey", "password") or SECRET_FIELD.search(n):
            v = None
        out[n] = v
    return out


def clean_item(item, tag_labels):
    item = dict(item)
    item.pop("id", None)
    for k in ("added", "lastModified", "infoLink", "message", "presets", "capabilities", "sortName"):
        item.pop(k, None)
    if "fields" in item:
        item["fields"] = strip_fields(item["fields"])
    if "tags" in item:
        item["tags"] = [tag_labels[t] for t in item["tags"] if t in tag_labels]
    return item


def export_arr(name, base, key, docker_dir):
    print(f"[{name}]")
    v = "v3"
    tags = {t["id"]: t["label"] for t in get(base, key, f"/api/{v}/tag")}
    dump(f"{name}/tags.json", sorted(tags.values()))

    # quality profiles: map custom-format ids -> names so they survive a fresh DB
    cfs = {c["id"]: c["name"] for c in get(base, key, f"/api/{v}/customformat")}
    profiles = []
    for p in get(base, key, f"/api/{v}/qualityprofile"):
        p = dict(p); p.pop("id", None)
        for fi in p.get("formatItems", []):
            fi["name"] = cfs.get(fi.get("format"), fi.get("name"))
            fi.pop("format", None)
        profiles.append(p)
    dump(f"{name}/qualityprofiles.json", profiles)

    dump(f"{name}/qualitydefinitions.json", get(base, key, f"/api/{v}/qualitydefinition"))
    dump(f"{name}/naming.json", get(base, key, f"/api/{v}/config/naming"))
    dump(f"{name}/mediamanagement.json", get(base, key, f"/api/{v}/config/mediamanagement"))
    dump(f"{name}/ui.json", get(base, key, f"/api/{v}/config/ui"))
    dump(f"{name}/delayprofiles.json", [dict((k, x) for k, x in d.items() if k != "id")
                                          for d in get(base, key, f"/api/{v}/delayprofile")])
    for ep, fn in (("metadata", "metadata"), ("releaseprofile", "releaseprofiles"),
                   ("autotagging", "autotagging")):
        try:
            dump(f"{name}/{fn}.json", [clean_item(i, tags) for i in get(base, key, f"/api/{v}/{ep}")])
        except urllib.error.HTTPError:
            pass

    # Only the manually-added indexer(s); Prowlarr syncs the rest.
    inds = [clean_item(i, tags) for i in get(base, key, f"/api/{v}/indexer")
            if not i["name"].endswith("(Prowlarr)")]
    dump(f"{name}/indexers_manual.json", inds)
    dump(f"{name}/downloadclients.json",
         [clean_item(i, tags) for i in get(base, key, f"/api/{v}/downloadclient")])
    # Jupiterr is a dead service of the author's; never ship it.
    notes = [clean_item(i, tags) for i in get(base, key, f"/api/{v}/notification")
             if i["implementation"] == "MediaBrowser"]
    dump(f"{name}/notifications.json", notes)


def export_prowlarr(base, key, docker_dir):
    print("[prowlarr]")
    tags = {t["id"]: t["label"] for t in get(base, key, "/api/v1/tag")}
    dump("prowlarr/tags.json", sorted(tags.values()))
    inds = [clean_item(i, tags) for i in get(base, key, "/api/v1/indexer")]
    dump("prowlarr/indexers.json", inds)
    dump("prowlarr/proxies.json", [clean_item(i, tags) for i in get(base, key, "/api/v1/indexerProxy")])
    dump("prowlarr/appprofiles.json", [dict((k, x) for k, x in a.items() if k != "id")
                                        for a in get(base, key, "/api/v1/appprofile")])
    custom = os.path.join(docker_dir, "arr/prowlarr/Definitions/Custom")
    dst = os.path.join(SEED, "prowlarr/Definitions/Custom")
    os.makedirs(dst, exist_ok=True)
    for fn in os.listdir(custom):
        shutil.copy2(os.path.join(custom, fn), dst)
        print("  copied custom definition", fn)


def export_bazarr(docker_dir):
    print("[bazarr]")
    import sqlite3
    db = sqlite3.connect(f"file:{docker_dir}/arr/bazarr/db/bazarr.db?mode=ro", uri=True)
    profs = []
    for pid, cutoff, orig, items, name, mc, mnc, tag in db.execute(
            "select profileId,cutoff,originalFormat,items,name,mustContain,mustNotContain,tag "
            "from table_languages_profiles"):
        profs.append({"profileId": pid, "cutoff": cutoff, "originalFormat": bool(orig),
                      "items": json.loads(items), "name": name,
                      "mustContain": json.loads(mc or "[]"), "mustNotContain": json.loads(mnc or "[]"),
                      "tag": tag})
    dump("bazarr/language_profiles.json", profs)
    langs = [r[0] for r in db.execute("select code2 from table_settings_languages where enabled=1")]
    dump("bazarr/enabled_languages.json", sorted(langs))


def export_bazarr_settings(docker_dir):
    """Non-secret general settings, re-applied through Bazarr's own settings API."""
    import yaml  # author machine only; the installer itself never needs PyYAML
    c = yaml.safe_load(open(f"{docker_dir}/arr/bazarr/config/config.yaml"))
    g = {k: v for k, v in c["general"].items()
         if k not in ("flask_secret_key", "hostname", "ip", "port", "enabled_providers",
                      "external_webhook_password", "external_webhook_username", "external_webhook_url")}
    keep = {"general": g,
            "embeddedsubtitles": c.get("embeddedsubtitles", {}),
            "sonarr": {k: v for k, v in c["sonarr"].items()
                       if k not in ("apikey", "ip", "port", "ssl", "base_url")},
            "radarr": {k: v for k, v in c["radarr"].items()
                       if k not in ("apikey", "ip", "port", "ssl", "base_url")},
            "opensubtitlescom": {k: v for k, v in c["opensubtitlescom"].items()
                                 if k not in ("username", "password")},
            "subdl": {k: v for k, v in c["subdl"].items() if k != "api_key"},
            "subsource": {}}
    dump("bazarr/settings.json", keep)


def export_jellyseerr(docker_dir):
    print("[jellyseerr]")
    s = json.load(open(f"{docker_dir}/arr/jellyseerr/settings.json"))
    main = {k: v for k, v in s["main"].items() if k != "apiKey"}
    dump("jellyseerr/main.json", main)
    # Jellyseerr -> Moonfin plugin webhook. URL carries a secret, so only the payload/event mask ships.
    w = s["notifications"]["agents"]["webhook"]
    dump("jellyseerr/webhook.json", {"enabled": w["enabled"], "types": w["types"],
                                      "jsonPayload": w["options"]["jsonPayload"]})
    # The webhook notification pointed at the author's (now removed) Jupiterr service: not shipped.


def export_jellyfin(docker_dir, hp_services):
    print("[jellyfin]")
    cfg = f"{docker_dir}/jellyfin/config"
    # Plugin configuration (credentials blanked)
    pdst = os.path.join(SEED, "jellyfin/plugin-configs")
    os.makedirs(pdst, exist_ok=True)
    skip = {"Jellyfin.Plugin.OpenSubtitles.xml"}  # per-user credentials
    for fn in sorted(os.listdir(f"{cfg}/plugins/configurations")):
        src = os.path.join(f"{cfg}/plugins/configurations", fn)
        if os.path.isfile(src) and fn not in skip:
            copy_text(src, os.path.join(pdst, fn))
            print("  copied plugin config", fn)


def export_templates(docker_dir):
    """Config files that are rendered (not API-seeded): placeholders instead of secrets."""
    print("[templates]")
    # decypharr: keep every tuning knob, drop accounts; wizard/renderer fills debrids, arrs, cache.
    c = json.load(open(f"{docker_dir}/arr/decypharr/config.json"))
    c["debrids"] = []
    for a in c.get("arrs", []):
        a["token"] = "__TOKEN__"
    dump("decypharr/config.template.json", c)

    # pearlarr: the author's file is mostly comments + defaults; swap the connection values only.
    t = open(f"{docker_dir}/pearlarr/config/config.yml").read()
    t = re.sub(r"(?m)^(  api_key:).*$", r"\1 __ARR_KEY__", t)
    t = re.sub(r"(?m)^(  password:).*$", r"\1 __QBIT_PASSWORD__", t)
    t = re.sub(r"(?m)^(  username:).*$", r"\1 __QBIT_USERNAME__", t)
    t = re.sub(r"(?m)^(  host:).*$", r"\1 http://decypharr:8282", t)
    # sonarr.url only (radarr.url is intentionally blank in the author's setup)
    t = re.sub(r"(?m)^(sonarr:\n(?:(?:  #.*|\s*)\n)*  url:).*$", r"\1 http://sonarr:8989", t)
    copy_text_str(t, os.path.join(SEED, "pearlarr/config.template.yml"))
    print("  wrote pearlarr/config.template.yml")
    # first __ARR_KEY__ is sonarr's, second is radarr's (radarr stays blank-URL = skipped)

    # recyclarr: author's file with instance names/urls generalized
    t = open(f"{docker_dir}/recyclarr/config/recyclarr.yml").read()
    t = re.sub(r"base_url: .*:8989", "base_url: http://sonarr:8989", t)
    t = re.sub(r"base_url: .*:7878", "base_url: http://radarr:7878", t)
    t = re.sub(r"(?m)^(\s+api_key:).*$", r"\1 __KEY__", t)
    # Recyclarr v8 requires instance names to be unique across services; the author's file names both
    # instances "main", which makes every scheduled sync a silent no-op. Give them distinct names.
    t = re.sub(r"(?m)^sonarr:\n  main:", "sonarr:\n  Shows:", t)
    t = re.sub(r"(?m)^radarr:\n  main:", "radarr:\n  Movies:", t)
    copy_text_str(t, os.path.join(SEED, "recyclarr/recyclarr.template.yml"))
    print("  wrote recyclarr/recyclarr.template.yml")

    # homepage: static look & feel only; services.yaml is generated from the live container list.
    hp = f"{docker_dir}/homepage/config"
    out = os.path.join(SEED, "homepage")
    for fn in ("widgets.yaml", "docker.yaml", "bookmarks.yaml"):
        copy_text(os.path.join(hp, fn), os.path.join(out, fn))
    # settings.yaml: drop the placeholder weather keys, keep a clean minimal file
    open(os.path.join(out, "settings.yaml"), "w").write("---\ntitle: Media Server\nheaderStyle: boxed\nlayout:\n  Media:\n    style: row\n    columns: 3\n  Automation:\n    style: row\n    columns: 4\n  Downloads:\n    style: row\n    columns: 2\n")
    os.makedirs(os.path.join(out, "icons"), exist_ok=True)
    shutil.copy2(os.path.join(hp, "icons/arabarr.png"), os.path.join(out, "icons/arabarr.png"))
    print("  wrote homepage static files")


def copy_text_str(text, dst):
    text = text.replace(LIVE_HOST, "__HOST__")
    for a, b in LIVE_NAMES.items():
        text = text.replace(a, b)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    open(dst, "w", encoding="utf-8").write(text)


def export_images_and_vendor(docker_dir):
    """Pin the exact image digests the author runs (seeded API settings are version-sensitive),
    and vendor the two services that aren't published images."""
    import subprocess
    print("[images + vendored services]")
    names = ["decypharr", "prowlarr", "sonarr", "radarr", "flaresolverr", "jellyseerr", "bazarr",
             "jellyfin", "recyclarr", "pearlarr", "homepage"]
    pins = {}
    for n in names:
        fmt = "{{.Config.Image}}|{{index .Config.Labels \"org.opencontainers.image.version\"}}"
        img, ver = subprocess.run(["docker", "inspect", "-f", fmt, n], capture_output=True,
                                  text=True, check=True).stdout.strip().split("|")
        dig = subprocess.run(["docker", "image", "inspect", "-f", "{{index .RepoDigests 0}}", img],
                             capture_output=True, text=True, check=True).stdout.strip()
        pins[n] = {"image": dig, "tag": img, "version": ver or None}
    dump("images.json", pins)

    # babysitarr (the author's modified fork) is built from source on each machine
    bs_src, bs_dst = f"{docker_dir}/babysitarr", os.path.join(KIT, "vendor/babysitarr")
    os.makedirs(bs_dst, exist_ok=True)
    for fn in ("Dockerfile", "babysitarr.py", "LICENSE", "README.md", ".dockerignore"):
        shutil.copy2(os.path.join(bs_src, fn), bs_dst)
    ar_dst = os.path.join(KIT, "vendor/arabarr")
    os.makedirs(ar_dst, exist_ok=True)
    # the author's LAN IP appears as a default URL in app.py; inside the stack it is the container name
    t = open(f"{docker_dir}/arabarr/app.py", encoding="utf-8").read().replace(LIVE_HOST, "arabarr")
    open(os.path.join(ar_dst, "app.py"), "w", encoding="utf-8").write(t)
    open(os.path.join(ar_dst, "name_overrides.json"), "w").write("{}\n")
    print("  vendored babysitarr + arabarr")


def jellyfin_key(docker_dir):
    """The author's Jellyfin API key lives in the Homepage widget config."""
    t = open(f"{docker_dir}/homepage/config/services.yaml").read()
    m = re.search(r"type: jellyfin\s+url: \S+\s+key: (\S+)", t)
    return m.group(1)


def export_jellyfin_api(docker_dir):
    print("[jellyfin api]")
    key = jellyfin_key(docker_dir)
    base = "http://localhost:8096"

    def g(path):
        req = urllib.request.Request(base + path, headers={"X-Emby-Token": key})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    cfg = g("/System/Configuration")
    for k in ("ServerName", "IsStartupWizardCompleted", "IsPortAuthorized", "MetadataPath"):
        cfg.pop(k, None)
    dump("jellyfin/system_configuration.json", cfg)
    libs = []
    for v in g("/Library/VirtualFolders"):
        if v.get("CollectionType") not in ("movies", "tvshows"):
            continue            # music is out of scope for this kit
        libs.append({"Name": v["Name"], "CollectionType": v["CollectionType"],
                     "LibraryOptions": {k: x for k, x in v["LibraryOptions"].items() if k != "PathInfos"},
                     "Paths": [p["Path"] for p in v["LibraryOptions"].get("PathInfos", [])]})
    dump("jellyfin/libraries.json", libs)
    try:
        dump("jellyfin/branding.json", g("/System/Configuration/branding"))
    except urllib.error.HTTPError:
        pass
    # which packages (plugins) are installed, so the installer can fetch exactly those
    plugs = [{"Name": p["Name"], "Id": p["Id"], "Version": p["Version"], "Status": p["Status"]}
             for p in g("/Plugins")]
    dump("jellyfin/plugins.json", plugs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docker-dir", default=os.path.expanduser("~/docker"))
    a = ap.parse_args()
    d = a.docker_dir
    export_arr("sonarr", "http://localhost:8989", api_key(f"{d}/arr/sonarr"), d)
    export_arr("radarr", "http://localhost:7878", api_key(f"{d}/arr/radarr"), d)
    export_prowlarr("http://localhost:9696", api_key(f"{d}/arr/prowlarr"), d)
    export_bazarr(d)
    export_bazarr_settings(d)
    export_jellyseerr(d)
    export_jellyfin(d, None)
    export_jellyfin_api(d)
    export_templates(d)
    export_images_and_vendor(d)
    print("done. Review seed/ for anything personal before sharing the kit.")


if __name__ == "__main__":
    main()
