"""Sonarr / Radarr / Prowlarr: seed settings through each app's REST API (never by writing its DB)."""
import json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import StackError, http, info, ok, warn, seed_json, wait_ready

READONLY = ("supportsOnGrab", "supportsOnDownload", "supportsOnUpgrade", "supportsOnRename",
            "supportsOnHealthIssue", "supportsOnHealthRestored", "supportsOnApplicationUpdate",
            "supportsOnManualInteractionRequired", "supportsOnSeriesAdd", "supportsOnSeriesDelete",
            "supportsOnEpisodeFileDelete", "supportsOnEpisodeFileDeleteForUpgrade",
            "supportsOnImportComplete", "supportsOnMovieAdded", "supportsOnMovieDelete",
            "supportsOnMovieFileDelete", "supportsOnMovieFileDeleteForUpgrade", "implementationName",
            "infoLink", "message", "presets")


class Arr:
    def __init__(self, name, base, key, ver):
        self.name, self.base, self.key, self.ver = name, base.rstrip("/"), key, ver

    # -- plumbing
    def url(self, path):
        return f"{self.base}/api/{self.ver}{path}"

    def req(self, method, path, body=None, retries=0, timeout=60):
        return http(method, self.url(path), headers={"X-Api-Key": self.key}, json_body=body,
                    retries=retries, timeout=timeout)

    def get(self, path):
        r = self.req("GET", path)
        if not r.ok:
            raise StackError(f"{self.name} GET {path} -> {r.status}: {r.body[:300]}")
        return r.json()

    # When an app saves a link to another of OUR apps (Prowlarr -> Radarr, Sonarr -> decypharr, ...) it tests the connection first.
    # If that other app is restarting right then, the save is refused with "Connection refused (radarr:7878)": wait and try again.
    OWN_HOSTS = r"\((?:sonarr|radarr|prowlarr|bazarr|decypharr|jellyfin|jellyseerr|flaresolverr|babysitarr|arabarr):\d+\)"
    OWN_WAIT_TRIES, OWN_WAIT_GAP = 12, 10

    def send(self, method, path, body):
        for attempt in range(self.OWN_WAIT_TRIES + 1):
            r = self.req(method, path, body)
            peer_down = (not r.ok and r.status in (400, 500, 502, 503) and re.search(self.OWN_HOSTS, r.body or "") is not None
                         and re.search(r"refused|cannot connect|unable to connect|timed? ?out|reset|no route|unreachable",
                                       r.body, re.I) is not None)
            if not peer_down or attempt == self.OWN_WAIT_TRIES:
                break
            info(f"{self.name}: waiting for the app it needs to connect to ({attempt + 1}/{self.OWN_WAIT_TRIES})...")
            time.sleep(self.OWN_WAIT_GAP)
        if not r.ok:
            raise StackError(f"{self.name} {method} {path} -> {r.status}: {r.body[:500]}")
        return r.json() if r.body else None

    def wait_ready(self, timeout=300):
        wait_ready(self.name, lambda: self.req("GET", "/system/status").status == 200, timeout)

    # -- tags
    def ensure_tags(self, labels):
        have = {t["label"]: t["id"] for t in self.get("/tag")}
        for lab in labels:
            if lab not in have:
                have[lab] = self.send("POST", "/tag", {"label": lab})["id"]
        return have

    def tag_ids(self, labels, tagmap):
        return [tagmap[l] for l in labels if l in tagmap]

    # -- generic "items with fields" upsert (indexer, downloadclient, notification, ...)
    def upsert(self, endpoint, item, force_save=True):
        item = {k: v for k, v in item.items() if k not in READONLY}
        if isinstance(item.get("fields"), dict):
            item["fields"] = [{"name": k, "value": v} for k, v in item["fields"].items()]
        existing = {x["name"]: x for x in self.get(endpoint)}
        q = "?forceSave=true" if force_save else ""
        if item["name"] in existing:
            item["id"] = existing[item["name"]]["id"]
            return self.send("PUT", f"{endpoint}/{item['id']}{q}", item)
        return self.send("POST", f"{endpoint}{q}", item)


def upsert_soft(app: Arr, endpoint, item, label):
    """Save an item whose connection is tested on save (indexers). If the test fails - wrong password, site down -
    save it switched OFF and warn, so one bad account never aborts the whole install."""
    try:
        app.upsert(endpoint, item)
        return True
    except StackError as e:
        # Sonarr/Radarr skip the save-time test only when every search flag is off; Prowlarr just uses `enable`
        item = dict(item, enable=False, enableRss=False, enableAutomaticSearch=False, enableInteractiveSearch=False)
        try:
            app.upsert(endpoint, item)
            warn(f"{app.name}: {label} failed its connection test; saved switched OFF ({str(e)[-110:].strip()})")
        except StackError as e2:
            warn(f"{app.name}: couldn't add {label}: {str(e2)[:140]}")
        return False


def set_login(app: Arr, user, password):
    """Enable forms auth with the wizard's credentials. The app hashes the password itself."""
    cfg = app.get("/config/host")
    cfg.update(authenticationMethod="forms", authenticationRequired="enabled",
               username=user, password=password, passwordConfirmation=password)
    app.send("PUT", f"/config/host/{cfg['id']}", cfg)


def verify_login(app: Arr, user, password):
    """Log in the way a browser does, then use the session. Returns True only if it really works."""
    path = "/login" if app.name in ("sonarr", "radarr", "prowlarr") else None
    r = http("POST", f"{app.base}{path}", form={"username": user, "password": password, "rememberMe": "on"},
             follow_redirects=False)       # success = 302 + an auth cookie; failure = 302 to ?loginFailed=true
    return r.status in (302, 303) and "Auth=" in r.headers.get("Set-Cookie", "") and "loginFailed" not in r.headers.get("Location", "")


# ------------------------------------------------------------------ Sonarr / Radarr
def seed_sonarr_radarr(app: Arr, kind, a, S, jellyfin_key, arabarr=None):
    """kind: 'sonarr' | 'radarr'."""
    info(f"Configuring {app.name}")
    tagmap = app.ensure_tags(seed_json(f"{kind}/tags.json"))

    # quality definitions (min/max/preferred size per quality)
    try:
        app.send("PUT", "/qualitydefinition/update", seed_json(f"{kind}/qualitydefinitions.json"))
    except StackError as e:
        warn(f"{app.name}: quality definitions not applied ({e})")

    for cfg_name, fn in (("naming", "naming"), ("mediamanagement", "mediamanagement"), ("ui", "ui")):
        body = seed_json(f"{kind}/{fn}.json")
        try:
            cur = app.get(f"/config/{cfg_name}")
            body["id"] = cur["id"]
            app.send("PUT", f"/config/{cfg_name}/{cur['id']}", body)
        except StackError as e:
            warn(f"{app.name}: {cfg_name} not applied ({e})")

    # quality profiles: the author's profiles by name. Custom-format scores are added by Recyclarr afterwards.
    cfs = {c["name"]: c["id"] for c in app.get("/customformat")}
    have = {p["name"]: p for p in app.get("/qualityprofile")}
    for p in seed_json(f"{kind}/qualityprofiles.json"):
        p = dict(p)
        p["formatItems"] = [{"format": cfs[f["name"]], "name": f["name"], "score": f["score"]}
                            for f in p.get("formatItems", []) if f["name"] in cfs]
        if p["name"] in have:
            p["id"] = have[p["name"]]["id"]
            app.send("PUT", f"/qualityprofile/{p['id']}", p)
        else:
            app.send("POST", "/qualityprofile", p)

    # delay profile: the single default row
    dps = seed_json(f"{kind}/delayprofiles.json")
    cur = app.get("/delayprofile")
    if dps and cur:
        d = dict(dps[0]); d["id"] = cur[0]["id"]; d["tags"] = []
        app.send("PUT", f"/delayprofile/{d['id']}", d)

    for ep, fn in (("metadata", "metadata"), ("releaseprofile", "releaseprofiles")):
        for item in seed_json(f"{kind}/{fn}.json"):
            item = dict(item)
            item["tags"] = app.tag_ids(item.get("tags", []), tagmap)
            try:
                app.upsert(f"/{ep}", item, force_save=False)
            except StackError as e:
                warn(f"{app.name}: {ep} '{item.get('name')}' skipped ({e})")

    # root folder (a fixed container path, identical on every machine)
    root = f"/mnt/symlinks/{'shows' if kind == 'sonarr' else 'movies'}"
    if root not in [r["path"] for r in app.get("/rootfolder")]:
        app.send("POST", "/rootfolder", {"path": root})

    # download client = decypharr, signed in with the wizard's credentials
    for dc in seed_json(f"{kind}/downloadclients.json"):
        dc = dict(dc)
        f = dict(dc["fields"])
        f.update(host="decypharr", port=8282, username=a["admin_user"], password=a["admin_pass"])
        dc["fields"] = f
        dc["tags"] = app.tag_ids(dc.get("tags", []), tagmap)
        app.upsert("/downloadclient", dc)

    # Jellyfin: refresh the library when something is imported
    for n in seed_json(f"{kind}/notifications.json"):
        n = dict(n)
        f = dict(n["fields"])
        f.update(host="jellyfin", port=8096, apiKey=jellyfin_key)
        n["fields"] = f
        n["tags"] = app.tag_ids(n.get("tags", []), tagmap)
        app.upsert("/notification", n)

    # manually-added indexers (Prowlarr syncs the rest). Only Arabarr, and only if it's deployed.
    for ix in seed_json(f"{kind}/indexers_manual.json"):
        if ix["name"] == "Arabarr" and not arabarr:
            continue
        ix = dict(ix)
        f = dict(ix["fields"])
        f.update(baseUrl="http://arabarr:5011", apiKey=arabarr["proxy_key"])
        ix["fields"] = f
        ix["tags"] = app.tag_ids(ix.get("tags", []), tagmap)
        upsert_soft(app, "/indexer", ix, ix["name"])

    set_login(app, a["admin_user"], a["admin_pass"])
    ok(f"{app.name} configured")


# ------------------------------------------------------------------ Prowlarr
PRIVATE_NEEDS = {          # indexer name -> (answers key, field map)
    "ArabP2P": ("arabp2p", {"username": "username", "password": "password"}),
    "ArabicSource (API)": ("arabicsource", {"apikey": "apikey"}),
}
DEBRID_LABEL = {"realdebrid": "real-debrid", "alldebrid": "alldebrid", "torbox": "torbox"}


def seed_prowlarr(p: Arr, a, S, sonarr_key, radarr_key):
    info("Configuring prowlarr")
    tagmap = p.ensure_tags(seed_json("prowlarr/tags.json"))

    # FlareSolverr proxy (tag-scoped, so only the indexers that need it use it)
    for px in seed_json("prowlarr/proxies.json"):
        px = dict(px)
        px["tags"] = p.tag_ids(px.get("tags", []), tagmap)
        p.upsert("/indexerProxy", px)

    # indexers
    schema = p.get("/indexer/schema")
    first_debrid = a["debrid"][0]
    accounts = a.get("indexer_accounts", {})
    skipped, failed_tests = [], []
    for ix in seed_json("prowlarr/indexers.json"):
        ix = dict(ix)
        f = dict(ix["fields"])
        ix["tags"] = p.tag_ids(ix.get("tags", []), tagmap)
        name = ix["name"]
        if name in PRIVATE_NEEDS:
            key, fmap = PRIVATE_NEEDS[name]
            if key in accounts:
                for field, ans in fmap.items():
                    f[field] = accounts[key][ans]
            else:
                ix["enable"] = False
                skipped.append(name)
        if f.get("definitionFile") == "torrentio":
            sch = next((s for s in schema if s.get("definitionName") == "torrentio"), None)
            if sch:
                opts = next((x for x in sch["fields"] if x["name"] == "debrid_provider"), {}).get("selectOptions", [])
                want = DEBRID_LABEL[first_debrid["provider"]]
                match = next((o for o in opts if o["name"].lower().replace(" ", "") == want.replace("-", "").replace(" ", "")
                              or want in o["name"].lower()), None)
                if match:
                    f["debrid_provider"] = match["value"]
            f["debrid_provider_key"] = first_debrid["api_key"]
        ix["fields"] = f
        try:
            p.upsert("/indexer", ix)
        except StackError as e:
            # Prowlarr tests an indexer when it is saved (wrong password, tracker down...). One bad indexer must
            # never abort the install: save it switched off, tell the user, move on.
            if not ix.get("enable", True):
                warn(f"prowlarr: couldn't add {name}: {str(e)[:160]}")
                continue
            ix["enable"] = False
            try:
                p.upsert("/indexer", ix)
                warn(f"prowlarr: {name} failed its connection test (check the account details); saved switched OFF")
                failed_tests.append(name)
            except StackError as e2:
                warn(f"prowlarr: couldn't add {name}: {str(e2)[:160]}")
    if skipped:
        warn(f"prowlarr: left off (no account given): {', '.join(skipped)}")

    # link Sonarr + Radarr so indexers sync to them automatically
    app_schema = {s["implementation"]: s for s in p.get("/applications/schema")}
    have = {x["name"] for x in p.get("/applications")}
    for nm, impl, key, port, cats in (("Sonarr", "Sonarr", sonarr_key, 8989, [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090]),
                                      ("Radarr", "Radarr", radarr_key, 7878, [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080, 2090])):
        if nm in have:
            continue
        s = dict(app_schema[impl])
        vals = {"prowlarrUrl": "http://prowlarr:9696", "baseUrl": f"http://{nm.lower()}:{port}",
                "apiKey": key, "syncCategories": cats}
        s["fields"] = [dict(f, value=vals.get(f["name"], f.get("value"))) for f in s["fields"]]
        s.update(name=nm, syncLevel="fullSync", tags=[])
        p.send("POST", "/applications?forceSave=true", s)
    set_login(p, a["admin_user"], a["admin_pass"])
    ok("prowlarr configured")


def wait_indexers_synced(arr: Arr, minimum, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = len([i for i in arr.get("/indexer") if i["name"].endswith("(Prowlarr)")])
        if n >= minimum:
            ok(f"{arr.name}: {n} indexers synced from Prowlarr")
            return n
        time.sleep(3)
    warn(f"{arr.name}: only {n} Prowlarr indexers synced after {timeout}s (they keep syncing in the background)")
    return n
