"""Jellyfin: unattended first-run wizard, admin account, API key, settings, plugins, libraries."""
import json, os, socket, sys, time, urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import StackError, compose, http, info, ok, warn, seed_json, seed_text, wait_ready, write_file, \
    save_json, load_json

def _auth(device_id="media-stack-installer"):
    # Jellyfin keeps ONE token per DeviceId: signing in again with the same id revokes the earlier token.
    return f'MediaBrowser Client="media-stack", Device="installer", DeviceId="{device_id}", Version="1.0"'
EXTRA_REPOS = [("Intro Skipper", "https://intro-skipper.org/manifest.json")]   # official; not in the stock list
BUNDLED = {"AudioDB", "MusicBrainz", "OMDb", "Studio Images", "TMDb"}   # ship inside the server image


class Jellyfin:
    def __init__(self, base, stack_dir):
        self.base, self.stack_dir = base.rstrip("/"), stack_dir
        self.token = None

    def req(self, method, path, body=None, form=None, timeout=60, auth=True, device_id="media-stack-installer"):
        h = {"Authorization": _auth(device_id) + (f', Token="{self.token}"' if (self.token and auth) else "")}
        return http(method, f"{self.base}{path}", headers=h, json_body=body, form=form, timeout=timeout)

    def must(self, method, path, body=None, **kw):
        r = self.req(method, path, body, **kw)
        if not r.ok:
            raise StackError(f"jellyfin {method} {path} -> {r.status}: {r.body[:400]}")
        return r.json() if r.body and "json" in r.headers.get("Content-Type", "") else r.body

    # ---------------------------------------------------------------- readiness
    def public_info(self):
        r = self.req("GET", "/System/Info/Public", auth=False)
        return r.json() if r.ok else None

    def wait_ready(self, timeout=300):
        wait_ready("jellyfin", lambda: bool(self.public_info()), timeout)

    # ---------------------------------------------------------------- wizard + account
    def first_run(self, user, password, server_name):
        info_ = self.public_info()
        if info_ and info_.get("StartupWizardCompleted"):
            info("Jellyfin first-run wizard already completed; signing in with your login")
        else:
            self.must("POST", "/Startup/Configuration", {"ServerName": server_name, "UICulture": "en-US",
                                                          "MetadataCountryCode": "US",
                                                          "PreferredMetadataLanguage": "en"}, auth=False)
            self.req("GET", "/Startup/User", auth=False)           # initialises the default user record
            self.must("POST", "/Startup/User", {"Name": user, "Password": password}, auth=False)
            self.must("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True,
                                                         "EnableAutomaticPortMapping": False}, auth=False)
            self.must("POST", "/Startup/Complete", None, auth=False)
            ok("Jellyfin setup wizard completed")
        self.login(user, password)

    def login(self, user, password):
        r = self.req("POST", "/Users/AuthenticateByName", {"Username": user, "Pw": password}, auth=False)
        if not r.ok:
            raise StackError(
                f"Jellyfin rejected the login '{user}' (HTTP {r.status}). If Jellyfin was set up before with a "
                f"different password, remove {self.stack_dir}/jellyfin/config and re-run.")
        d = r.json()
        self.token, self.user_id = d["AccessToken"], d["User"]["Id"]

    def api_key(self, app="media-stack"):
        """A permanent API key for the arrs, Homepage, Jellyseerr. Created once, then reused."""
        keys = self.must("GET", "/Auth/Keys")["Items"]
        found = next((k for k in keys if k["AppName"] == app), None)
        if not found:
            self.must("POST", f"/Auth/Keys?app={urllib.parse.quote(app)}")
            keys = self.must("GET", "/Auth/Keys")["Items"]
            found = next(k for k in keys if k["AppName"] == app)
        return found["AccessToken"]

    # ---------------------------------------------------------------- server settings
    def apply_settings(self):
        cur = self.must("GET", "/System/Configuration")
        seed = seed_json("jellyfin/system_configuration.json")
        keep_local = {"ServerName", "IsStartupWizardCompleted", "IsPortAuthorized", "MetadataPath"}
        cur.update({k: v for k, v in seed.items() if k not in keep_local})
        urls = {r["Url"] for r in cur["PluginRepositories"]}
        for name, url in EXTRA_REPOS:
            if url not in urls:
                cur["PluginRepositories"].append({"Name": name, "Url": url, "Enabled": True})
        self.must("POST", "/System/Configuration", cur)
        try:
            self.must("POST", "/System/Configuration/branding", seed_json("jellyfin/branding.json"))
        except StackError as e:
            warn(f"jellyfin branding not applied: {e}")
        ok("Jellyfin settings applied")

    # ---------------------------------------------------------------- plugins
    def pin_plugin_versions(self):
        """Jellyfin's 'Update Plugins' task would upgrade plugins mid-install (and leave them pending a restart),
        drifting from the versions this setup was tested with. Plugins are pinned, like the container images."""
        for t in self.must("GET", "/ScheduledTasks"):
            if t.get("Key") in ("PluginUpdates", "PluginUpdateTask") or t.get("Name") == "Update Plugins":
                self.must("POST", f"/ScheduledTasks/{t['Id']}/Triggers", [])
                ok("Plugin versions pinned (automatic plugin updates off)")
                return
        warn("couldn't find Jellyfin's plugin-update task to switch off")

    def install_plugins(self):
        """Install every non-bundled plugin the author runs, at the same version when the repo still has it."""
        wanted = [p for p in seed_json("jellyfin/plugins.json") if p["Name"] not in BUNDLED]
        installed = {p["Name"] for p in self.must("GET", "/Plugins")}
        avail = self.must("GET", "/Packages")
        by_name = {p["name"]: p for p in avail}
        changed = False
        for p in wanted:
            if p["Name"] in installed:
                continue
            pkg = by_name.get(p["Name"])
            if not pkg:
                warn(f"plugin '{p['Name']}' not found in any configured repository; skipped")
                continue
            vers = pkg["versions"]
            v = next((x for x in vers if x["version"] == p["Version"]), vers[0])   # exact version if still hosted
            q = urllib.parse.urlencode({"assemblyGuid": pkg["guid"], "version": v["version"],
                                        "repositoryUrl": v.get("repositoryUrl", "")})
            r = self.req("POST", f"/Packages/Installed/{urllib.parse.quote(p['Name'])}?{q}", timeout=180)
            if r.ok:
                ok(f"plugin {p['Name']} {v['version']} installed")
                changed = True
            else:
                warn(f"plugin '{p['Name']}' failed to install (HTTP {r.status}): {r.body[:160]}")
        return changed

    def write_plugin_configs(self, a, S):
        """Plugin configuration XML. Written while Jellyfin is stopped so it can't be overwritten."""
        dst = os.path.join(self.stack_dir, "jellyfin/config/plugins/configurations")
        os.makedirs(dst, exist_ok=True)
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed/jellyfin/plugin-configs")
        ports = a.get("ports", {})
        for fn in sorted(os.listdir(src)):
            t = seed_text(f"jellyfin/plugin-configs/{fn}")
            t = t.replace(f"http://__HOST__:5055", f"http://{a['host_ip']}:{ports.get('jellyseerr', 5055)}")
            t = t.replace(f"http://__HOST__:8096", f"http://{a['host_ip']}:{ports.get('jellyfin', 8096)}")
            t = t.replace("__SECRET__", S["moonfin_webhook"])
            write_file(os.path.join(dst, fn), t)

    def restart_with_configs(self, a, S):
        compose(self.stack_dir, "stop", "jellyfin", timeout=120)
        self.write_plugin_configs(a, S)
        compose(self.stack_dir, "start", "jellyfin", timeout=120)
        self.token = None
        self.wait_ready()

    # ---------------------------------------------------------------- libraries, policy
    def libraries(self, a):
        have = {v["Name"] for v in self.must("GET", "/Library/VirtualFolders")}
        for lib in seed_json("jellyfin/libraries.json"):
            if lib["Name"] in have:
                continue
            opts = dict(lib["LibraryOptions"])
            paths = ["/mnt/symlinks/movies" if lib["CollectionType"] == "movies" else "/mnt/symlinks/shows"]
            opts["PathInfos"] = [{"Path": p} for p in paths]
            q = urllib.parse.urlencode({"name": lib["Name"], "collectionType": lib["CollectionType"],
                                        "refreshLibrary": "false"})
            self.must("POST", f"/Library/VirtualFolders?{q}", {"LibraryOptions": opts})
            ok(f"library '{lib['Name']}' created")

    def direct_play_policy(self):
        """Direct play by default: no heavy video transcodes. (Remux + audio conversion stay allowed: they're cheap.)"""
        for u in self.must("GET", "/Users"):
            pol = u["Policy"]
            pol["EnableVideoPlaybackTranscoding"] = False
            pol["EnablePlaybackRemuxing"] = True
            pol["EnableAudioPlaybackTranscoding"] = True
            self.must("POST", f"/Users/{u['Id']}/Policy", pol)
        ok("Direct play is the default (video transcoding off; the owner can turn it on in Jellyfin)")

    def scan(self):
        self.req("POST", "/Library/Refresh")
