"""Post-install verification: the two failures that make an installer worthless are
  (1) an app that comes up showing its first-run wizard "as if nothing was configured", and
  (2) the login the user typed in the questionnaire not working.
So we check both for every app, by really signing in, and a wrong password must be rejected too."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http, ok, fail, warn, step
from arr import Arr, verify_login as arr_login


def run(a, S, ports, jf, js, bz, dec, sonarr, radarr, prowlarr, running):
    user, pw = a["admin_user"], a["admin_pass"]
    bad = "definitely-not-the-password-1"
    results = []

    def check(name, fn):
        try:
            good, detail = fn()
        except Exception as e:  # noqa: BLE001
            good, detail = False, repr(e)
        results.append((name, good, detail))

    # ---- logins: right password works, wrong password is refused
    for app in (sonarr, radarr, prowlarr):
        check(f"{app.name}: your login works",
              lambda app=app: (arr_login(app, user, pw) and not arr_login(app, user, bad), "signed in; wrong password refused"))
        check(f"{app.name}: sign-in is enforced",
              lambda app=app: (http("GET", f"{app.base}/api/{app.ver}/system/status").status == 401, "no key -> 401"))
    check("jellyfin: your login works",
          lambda: (jf.req("POST", "/Users/AuthenticateByName", {"Username": user, "Pw": pw}, auth=False, device_id="media-stack-verify").ok and
                   not jf.req("POST", "/Users/AuthenticateByName", {"Username": user, "Pw": bad}, auth=False, device_id="media-stack-verify").ok,
                   "signed in; wrong password refused"))
    check("jellyseerr: your login works", lambda: (js.verify_login(user, pw) and not js.verify_login(user, bad), "signed in via Jellyfin"))
    check("bazarr: your login works", lambda: (bz.verify_login(user, pw) and not bz.verify_login(user, bad), "signed in; wrong password refused"))
    check("decypharr: your login works (web)", lambda: (dec.verify_web_login(user, pw) and not dec.verify_web_login(user, bad), "signed in"))
    check("decypharr: login used by Sonarr/Radarr works",
          lambda: (dec.verify_qbit_login(user, pw) and not dec.verify_qbit_login(user, bad), "qBittorrent API"))

    # ---- first-run wizards must be finished
    check("jellyfin: setup wizard finished", lambda: (bool(jf.public_info().get("StartupWizardCompleted")), ""))
    check("jellyseerr: setup finished", lambda: (js.initialized(), ""))
    check("bazarr: sign-in configured", lambda: (bz.settings()["auth"]["type"] == "form", "form login on"))

    # ---- the seeded settings are really there
    check("jellyfin: libraries + plugins",
          lambda: ((lambda libs, pl: (len(libs) >= 2 and all(p["Status"] == "Active" for p in pl), f"{len(libs)} libraries, {len(pl)} plugins active"))(
              jf.must("GET", "/Library/VirtualFolders"), jf.must("GET", "/Plugins"))))
    for app, kind in ((sonarr, "sonarr"), (radarr, "radarr")):
        check(f"{kind}: profiles, root folder, decypharr, Jellyfin link",
              lambda app=app: ((lambda p, r, d, n: (len(p) >= 7 and len(r) == 1 and len(d) == 1 and len(n) == 1,
                                                  f"{len(p)} quality profiles"))(
                  app.get("/qualityprofile"), app.get("/rootfolder"), app.get("/downloadclient"), app.get("/notification"))))
        check(f"{kind}: indexers synced from Prowlarr",
              lambda app=app: ((lambda n: (n >= 1, f"{n} indexers"))(len([i for i in app.get('/indexer') if i['name'].endswith('(Prowlarr)')]))))
        check(f"{kind}: custom formats (Recyclarr)",
              lambda app=app: ((lambda c: (len(c) >= 1, ", ".join(x["name"] for x in c)))(app.get("/customformat"))))
    check("decypharr: debrid mount visible", lambda: (dec.mounted(a["media_root"]), a["media_root"] + "/decypharr"))
    check("bazarr: connected to Sonarr and Radarr", lambda: bz.linked())
    check("bazarr: English + Arabic profile",
          lambda: ((lambda p: (not a.get("subtitles") or any("ar" in [i["language"] for i in x["items"]] for x in p), f"{len(p)} profile(s)"))(bz.profiles())))

    # ---- Homepage's green/red status tags need Docker access: check what the page itself would show
    if "homepage" in running:
        def tags():
            import json as _j
            names = [n for sv, n in running.items() if sv != "homepage"]
            bad, sample = [], ""
            for n in names:
                r = http("GET", f"http://localhost:{ports['homepage']}/api/docker/status/{n}/my-docker")     # container first, then server
                try:
                    st = _j.loads(r.body).get("status")
                except Exception:  # noqa: BLE001
                    st = None
                if st not in ("running", "healthy") and not str(st).startswith("running"):
                    bad.append(f"{n}={st}")
                    sample = sample or f"HTTP {r.status}: {r.body[:100]!r}"
            return (not bad, f"{len(names)} tiles show running" if not bad else "tiles would show ERROR: " + ", ".join(bad[:6]) + " | " + sample)
        check("homepage: every tile shows 'running' (not ERROR)", tags)
    for app, kind in ((sonarr, "sonarr"), (radarr, "radarr")):
        check(f"{kind}: sees the download folder (no health warning)",
              lambda app=app: ((lambda h: (not h, "; ".join(h)[:140] if h else "clean"))(
                  [x["message"] for x in app.get("/health") if "does not appear to exist" in x.get("message", "")])))

    # ---- everything else just has to answer
    for svc, port, path in (("homepage", 3000, "/"), ("flaresolverr", 8191, "/"), ("babysitarr", 8284, "/")):
        if svc in running:
            p = ports[svc]
            check(f"{svc}: responding", lambda p=p, path=path: (http("GET", f"http://localhost:{p}{path}").status < 500, f":{p}"))

    step("Verification")
    failed = 0
    for name, good, detail in results:
        (ok if good else fail)(f"{name}" + (f"  ({detail})" if detail else ""))
        failed += 0 if good else 1
    return failed, results
