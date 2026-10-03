"""Bazarr: providers, English+Arabic language profile, Sonarr/Radarr links, login — via its settings API."""
import json, os, sys, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import StackError, http, info, ok, warn, seed_json, wait_ready

PROVIDER_FIELDS = {          # answers key -> {bazarr setting: answers field}
    "opensubtitlescom": {"username": "username", "password": "password"},
    "subsource": {"apikey": "apikey"},
    "subdl": {"api_key": "api_key"},
}


class Bazarr:
    def __init__(self, base, key):
        self.base, self.key = base.rstrip("/"), key

    def req(self, method, path, form=None, timeout=60):
        data = urllib.parse.urlencode(form, doseq=True) if form is not None else None
        h = {"X-API-KEY": self.key}
        if data is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return http(method, f"{self.base}/api{path}", headers=h, data=data, timeout=timeout)

    def must(self, method, path, form=None):
        r = self.req(method, path, form)
        if not r.ok:
            raise StackError(f"bazarr {method} {path} -> {r.status}: {r.body[:300]}")
        return r.json() if r.body.strip().startswith(("{", "[")) else None

    def wait_ready(self, timeout=240):
        wait_ready("bazarr", lambda: self.req("GET", "/system/status").status == 200, timeout)

    def settings(self):
        return self.must("GET", "/system/settings")

    @staticmethod
    def _flatten(section, values):
        out = []
        for k, v in values.items():
            if v is None or v == "":        # empty == default; Bazarr's validator reads "" as an (invalid) list
                continue
            key = f"settings-{section}-{k}"
            if isinstance(v, bool):
                out.append((key, "true" if v else "false"))
            elif isinstance(v, list):
                for item in v:
                    out.append((key, str(item)))
            elif isinstance(v, dict):
                continue
            else:
                out.append((key, str(v)))
        return out

    def configure(self, a, S):
        info("Configuring bazarr")
        subs = a.get("subtitles", {})
        seed = seed_json("bazarr/settings.json")
        form = []
        # Always wired to Sonarr + Radarr (connection plumbing, not a provider setting)
        for sec in ("sonarr", "radarr"):
            port = 8989 if sec == "sonarr" else 7878
            v = dict(seed[sec])
            v.update(ip=sec, port=port, apikey=S[sec], ssl=False, base_url="")
            form += self._flatten(sec, v)
        form += [("settings-general-use_sonarr", "true"), ("settings-general-use_radarr", "true")]
        if subs:
            general = dict(seed["general"])
            for k in ("use_sonarr", "use_radarr"):      # already sent above; sending a key twice makes Bazarr read it as a list
                general.pop(k, None)
            general["enabled_providers"] = [p for p in ("subsource", "subdl", "opensubtitlescom") if p in subs]
            form += self._flatten("general", general)
            form += self._flatten("embeddedsubtitles", seed.get("embeddedsubtitles", {}))
            for prov, fields in PROVIDER_FIELDS.items():
                if prov not in subs:
                    continue
                base = dict(seed.get(prov, {}))
                for setting, ans in fields.items():
                    base[setting] = subs[prov][ans]
                form += self._flatten(prov, base)
            # language profile: English + Arabic (the author's profile, stored in Bazarr's DB, not config.yaml)
            profiles = seed_json("bazarr/language_profiles.json")
            langs = seed_json("bazarr/enabled_languages.json")
            form += [("languages-enabled", l) for l in langs]
            form.append(("languages-profiles", json.dumps(profiles)))
        else:
            warn("bazarr: linked to Sonarr and Radarr, but no subtitle providers or language profile (you skipped them)")
        form += [("settings-auth-type", "form"), ("settings-auth-username", a["admin_user"]),
                 ("settings-auth-password", a["admin_pass"])]
        self.must("POST", "/system/settings", form)
        ok("bazarr configured")

    def verify_login(self, user, password):
        r = http("POST", f"{self.base}/api/system/account?action=login",
                 form={"username": user, "password": password})
        return r.status in (200, 204)

    def linked(self, retries=6):
        """Bazarr reports the Sonarr/Radarr versions it can actually reach: proof the link works, not just that it's saved."""
        import time
        for _ in range(retries):
            d = (self.must("GET", "/system/status") or {}).get("data", {})
            if d.get("sonarr_version") and d.get("radarr_version"):
                return True, f"Sonarr {d['sonarr_version']}, Radarr {d['radarr_version']}"
            time.sleep(3)
        return False, "Bazarr can't reach Sonarr/Radarr"

    def profiles(self):
        return self.must("GET", "/system/languages/profiles")
