"""Jellyseerr: sign in through Jellyfin (that login becomes the owner), wire Sonarr/Radarr, apply settings."""
import base64, json, os, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import StackError, http, info, ok, warn, seed_json, wait_ready


class Jellyseerr:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.cookie = None

    def req(self, method, path, body=None, timeout=60):
        h = {"Cookie": self.cookie} if self.cookie else {}
        r = http(method, f"{self.base}/api/v1{path}", headers=h, json_body=body, timeout=timeout)
        sc = r.headers.get("Set-Cookie")
        if sc and "connect.sid" in sc:
            self.cookie = sc.split(";", 1)[0]
        return r

    def must(self, method, path, body=None):
        r = self.req(method, path, body)
        if not r.ok:
            raise StackError(f"jellyseerr {method} {path} -> {r.status}: {r.body[:400]}")
        return r.json() if r.body and r.body.strip().startswith(("{", "[")) else r.body

    def wait_ready(self, timeout=240):
        wait_ready("jellyseerr", lambda: self.req("GET", "/status").status == 200, timeout)

    def initialized(self):
        return bool(self.req("GET", "/settings/public").json().get("initialized"))

    def login(self, user, password):
        """Sign in with the Jellyfin account. On a fresh instance this also configures the media server."""
        body = {"username": user, "password": password, "hostname": "jellyfin", "port": 8096,
                "useSsl": False, "urlBase": "", "email": f"{user}@localhost",
                "serverType": 2}    # 2 = Jellyfin (a fresh instance is 'not configured' until this first login)
        r = self.req("POST", "/auth/jellyfin", body)
        if r.status == 500 and "already configured" in r.body:      # re-run: host is set, plain sign-in
            r = self.req("POST", "/auth/jellyfin", {"username": user, "password": password})
        if r.status == 401 or r.status == 403:
            raise StackError(f"Jellyseerr rejected the Jellyfin login '{user}' (HTTP {r.status})")
        if not r.ok:
            raise StackError(f"jellyseerr login -> {r.status}: {r.body[:300]}")

    def configure(self, a, S, sonarr, radarr, jellyfin_ext_url):
        info("Configuring jellyseerr")
        self.login(a["admin_user"], a["admin_pass"])

        # libraries: enable the two the stack uses
        libs = self.must("GET", "/settings/jellyfin/library?sync=true")
        ids = [l["id"] for l in libs if l["name"] in ("Movies", "Shows")]
        # NB: a bare GET of this endpoint re-saves every library as disabled, so only ever call it with enable=
        self.must("GET", "/settings/jellyfin/library?enable=" + ",".join(ids))
        cur = self.must("GET", "/settings/jellyfin")
        writable = {k: v for k, v in cur.items() if k not in ("name", "libraries", "serverId")}
        writable["externalHostname"] = jellyfin_ext_url
        self.must("POST", "/settings/jellyfin", writable)

        main = seed_json("jellyseerr/main.json")
        main.pop("mediaServerType", None)
        self.must("POST", "/settings/main", main)

        # Sonarr / Radarr (profile names are looked up by name: ids differ per install)
        def prof(app, name):
            for p in app.get("/qualityprofile"):
                if p["name"] == name:
                    return p["id"], p["name"]
            raise StackError(f"{app.name} has no quality profile '{name}'")

        rid, rname = prof(radarr, "Best available ")
        for existing in self.must("GET", "/settings/radarr"):
            self.must("DELETE", f"/settings/radarr/{existing['id']}")
        self.must("POST", "/settings/radarr", {
            "name": "Radarr", "hostname": "radarr", "port": 7878, "apiKey": S["radarr"], "useSsl": False,
            "baseUrl": "", "activeProfileId": rid, "activeProfileName": rname,
            "activeDirectory": "/mnt/symlinks/movies", "is4k": False, "minimumAvailability": "released",
            "isDefault": True, "syncEnabled": False, "preventSearch": False, "tagRequests": False})
        sid, sname = prof(sonarr, "Best Available")
        for existing in self.must("GET", "/settings/sonarr"):
            self.must("DELETE", f"/settings/sonarr/{existing['id']}")
        self.must("POST", "/settings/sonarr", {
            "name": "Sonarr", "hostname": "sonarr", "port": 8989, "apiKey": S["sonarr"], "useSsl": False,
            "baseUrl": "", "activeProfileId": sid, "activeProfileName": sname,
            "activeDirectory": "/mnt/symlinks/shows", "animeSeriesType": "anime", "tags": [], "animeTags": [],
            "is4k": False, "isDefault": True, "enableSeasonFolders": False, "syncEnabled": True,
            "preventSearch": False, "tagRequests": False})

        # webhook -> Moonfin plugin inside Jellyfin (secret is generated per install)
        wh = seed_json("jellyseerr/webhook.json")
        self.must("POST", "/settings/notifications/webhook", {
            "enabled": wh["enabled"], "types": wh["types"],
            "options": {"webhookUrl": f"http://jellyfin:8096/Moonfin/Seerr/Webhook?secret={S['moonfin_webhook']}",
                        "jsonPayload": base64.b64decode(wh["jsonPayload"]).decode()}})   # stored b64, API takes raw JSON
        self.must("POST", "/settings/initialize")
        ok("jellyseerr configured")

    def verify_login(self, user, password):
        c = Jellyseerr(self.base)
        r = c.req("POST", "/auth/jellyfin", {"username": user, "password": password})
        return r.ok and c.req("GET", "/auth/me").ok
