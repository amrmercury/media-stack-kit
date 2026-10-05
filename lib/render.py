"""Render the stack directory: docker-compose.yml, .env, and every file-based app config.

Only things that are safe to write as files go here (pinned API keys, connection URLs, custom
indexer definitions, decypharr/recyclarr/pearlarr configs). Anything with hashed state (logins)
or wizard state is done through each app's API in deploy.py, after the app reports ready.
"""
import grp, json, os, re, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import KIT, SEED, ensure_dir, load_json, new_key, save_json, seed_json, seed_text, write_file, info

DEFAULT_PORTS = {"jellyfin": 8096, "jellyseerr": 5055, "sonarr": 8989, "radarr": 7878, "prowlarr": 9696,
                 "bazarr": 6767, "decypharr": 8282, "flaresolverr": 8191, "homepage": 3001,
                 "babysitarr": 8284, "arabarr": 5011}

# Hard ceilings, not reservations: observed idle use of the whole stack is ~2 GB.
MEM = {"jellyfin": "1280m", "sonarr": "640m", "radarr": "640m", "prowlarr": "512m", "bazarr": "512m",
       "jellyseerr": "512m", "flaresolverr": "640m", "decypharr": "768m", "homepage": "256m",
       "recyclarr": "320m", "pearlarr": "192m", "babysitarr": "192m", "arabarr": "96m"}


# ------------------------------------------------------------------ tiny YAML emitter
def _scalar(v):
    if v is True: return "true"
    if v is False: return "false"
    if v is None: return "null"
    if isinstance(v, (int, float)): return str(v)
    return json.dumps(str(v), ensure_ascii=False)   # a JSON string is a valid YAML double-quoted scalar


def to_yaml(o, ind=0):
    pad = "  " * ind
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out.append(to_yaml(v, ind + 1))
            else:
                out.append(f"{pad}{k}: {'{}' if v == {} else '[]' if v == [] else _scalar(v)}")
    elif isinstance(o, list):
        for v in o:
            if isinstance(v, (dict, list)):
                sub = to_yaml(v, ind + 1).split("\n")
                out.append(f"{pad}- {sub[0].lstrip()}")
                out.extend(sub[1:])
            else:
                out.append(f"{pad}- {_scalar(v)}")
    return "\n".join(out)


# ------------------------------------------------------------------ identity / secrets
def ids():
    puid = int(os.environ.get("SUDO_UID") or os.getuid())
    pgid = int(os.environ.get("SUDO_GID") or os.getgid())
    return puid, pgid


def docker_gid():
    """The group that owns the Docker socket (what a container needs to be able to use it)."""
    try:
        return os.stat("/var/run/docker.sock").st_gid
    except OSError:
        pass
    try:
        return grp.getgrnam("docker").gr_gid
    except KeyError:
        return None


def load_secrets(state_dir):
    """Stable keys across re-runs, so a second install never invalidates the first."""
    path = os.path.join(state_dir, "secrets.json")
    s = load_json(path, {})
    changed = False
    for k in ("sonarr", "radarr", "prowlarr", "bazarr", "decypharr_token", "arabarr_proxy", "moonfin_webhook"):
        if k not in s:
            s[k] = new_key()
            changed = True
    if changed:
        save_json(path, s)
    return s


# ------------------------------------------------------------------ compose
def build_compose(a, S, pins, puid, pgid):
    pre = a.get("container_prefix", "")
    ports = {**DEFAULT_PORTS, **a.get("ports", {})}
    stack = a["stack_dir"]
    tz = a["timezone"]
    cache = a.get("cache", {})
    envbase = [f"PUID={puid}", f"PGID={pgid}", f"TZ={tz}"]

    def svc(name, image_key=None, **kw):
        d = {"image": pins[image_key or name]["image"], "container_name": f"{pre}{name}",
             "restart": "unless-stopped", "mem_limit": MEM[name]}
        d.update(kw)
        return d

    media_mount = f"{a['media_root']}:/mnt:rshared"
    dns = ["1.1.1.1", "9.9.9.9"]
    nolabel = {"com.centurylinklabs.watchtower.enable": "false"}   # pinned digests: never auto-update

    services = {}
    dec_vol = [f"{stack}/decypharr:/app", media_mount]
    if cache.get("path"):
        dec_vol.append(f"{cache['path']}:/app/cache")
    services["decypharr"] = svc(
        "decypharr", labels=nolabel, ports=[f"{ports['decypharr']}:8282"], volumes=dec_vol,
        environment=envbase, devices=["/dev/fuse:/dev/fuse:rwm"], cap_add=["SYS_ADMIN"],
        security_opt=["apparmor:unconfined"])
    for n, p in (("prowlarr", ports["prowlarr"]), ("sonarr", ports["sonarr"]), ("radarr", ports["radarr"])):
        vol = [f"{stack}/{n}:/config"] + ([media_mount] if n != "prowlarr" else [])
        services[n] = svc(n, ports=[f"{p}:{DEFAULT_PORTS[n]}"], volumes=vol, environment=envbase, dns=dns)
    services["bazarr"] = svc("bazarr", ports=[f"{ports['bazarr']}:6767"],
                             volumes=[f"{stack}/bazarr:/config", media_mount], environment=envbase, dns=dns)
    services["flaresolverr"] = svc("flaresolverr", ports=[f"{ports['flaresolverr']}:8191"],
                                   environment=[f"TZ={tz}", "LOG_LEVEL=info"])
    services["jellyseerr"] = svc("jellyseerr", user=f"{puid}:{pgid}", ports=[f"{ports['jellyseerr']}:5055"],
                                 volumes=[f"{stack}/jellyseerr:/app/config"], environment=[f"TZ={tz}"])
    services["jellyfin"] = svc(
        "jellyfin", user=f"{puid}:{pgid}", ports=[f"{ports['jellyfin']}:8096"],
        volumes=[f"{stack}/jellyfin/config:/config", f"{stack}/jellyfin/cache:/cache", media_mount],
        environment=[f"TZ={tz}"])
    services["recyclarr"] = svc("recyclarr", user=f"{puid}:{pgid}", volumes=[f"{stack}/recyclarr:/config"],
                                environment=[f"TZ={tz}", "CRON_SCHEDULE=@daily"])
    services["pearlarr"] = svc("pearlarr", labels=nolabel, user=f"{puid}:{pgid}",
                               volumes=[f"{stack}/pearlarr:/config"], environment=[f"TZ={tz}"])
    # Homepage shows each tile's green "running"/red "error" tag by asking Docker through the socket. The app drops to PUID:PGID at
    # start-up and loses supplemental groups, so make the socket's own group its primary group (the file owner stays you).
    sock_gid = docker_gid()
    hp_env = {"HOMEPAGE_ALLOWED_HOSTS":
              f"{a['host_ip']}:{ports['homepage']},localhost:{ports['homepage']},127.0.0.1:{ports['homepage']}",
              "PUID": puid, "PGID": sock_gid if sock_gid is not None else pgid}
    hp = svc("homepage", ports=[f"{ports['homepage']}:3000"],
             volumes=[f"{stack}/homepage:/app/config", f"{stack}/homepage/icons:/app/public/icons",
                      "/var/run/docker.sock:/var/run/docker.sock:ro"], environment=hp_env)
    if docker_gid():
        hp["group_add"] = [str(docker_gid())]
    services["homepage"] = hp
    # babysitarr: your fork, built from source (vendor/babysitarr)
    services["babysitarr"] = {
        "build": f"{stack}/vendor/babysitarr", "image": f"{pre}babysitarr:local",
        "container_name": f"{pre}babysitarr", "restart": "unless-stopped", "mem_limit": MEM["babysitarr"],
        "ports": [f"{ports['babysitarr']}:8284"],
        "environment": {
            "CHECK_INTERVAL": "120", "RD_API_KEY": "${RD_API_KEY:-}",
            "DECYPHARR_URL": "http://decypharr:8282", "DECYPHARR_API_KEY": S["decypharr_token"],
            "EXPECTED_DECYPHARR_TAG": "2.5-stable",
            "DOWNLOAD_DIRS": "/downloads/movies,/downloads/shows",
            "SONARR": f"sonarr|sonarr|sonarr|8989|{S['sonarr']}",
            "RADARR": f"radarr|radarr|radarr|7878|{S['radarr']}",
            "STUCK_DOWNLOAD_TIMEOUT": "600", "LOOP_THRESHOLD": "5", "IMPORT_STALL_TIMEOUT": "300",
            "STUCK_QUEUE_TIMEOUT": "1800", "MAX_WORKERS": "3",
            "BROKEN_SYMLINK_CHECK": "true", "BROKEN_SYMLINK_DRY_RUN": "false",
            "WEB_PORT": "8284", "TZ": tz},
        "volumes": ["/var/run/docker.sock:/var/run/docker.sock", f"{stack}/babysitarr:/data",
                    f"{stack}/decypharr:/decypharr-config:ro", f"{a['media_root']}/symlinks:/downloads:ro",
                    media_mount, f"{stack}/radarr:/arr-configs/radarr", f"{stack}/sonarr:/arr-configs/sonarr",
                    f"{stack}/prowlarr:/arr-configs/prowlarr"]}
    if a.get("enable_arabarr"):
        services["arabarr"] = {
            "image": "python:3.12-slim", "container_name": f"{pre}arabarr", "restart": "unless-stopped",
            "mem_limit": MEM["arabarr"], "command": ["python", "-u", "/app/app.py"],
            "env_file": ["./arabarr.env"], "ports": [f"{ports['arabarr']}:5011"],
            "volumes": [f"{stack}/vendor/arabarr/app.py:/app/app.py:ro",
                        f"{stack}/vendor/arabarr/name_overrides.json:/app/name_overrides.json:ro"]}
    if a.get("selinux_enforcing", selinux_enforcing()):
        # Fedora/RHEL: with SELinux enforcing, containers can't read bind-mounted config folders ("permission denied"). Relabelling
        # a whole media library is slow and fights the FUSE mount, so these containers opt out of SELinux confinement instead.
        for d in services.values():
            d["security_opt"] = list(d.get("security_opt", [])) + ["label=disable"]      # keep options a service already has
    return {"name": a.get("project", "media-stack"), "services": services}


def selinux_enforcing():
    try:
        return open("/sys/fs/selinux/enforce").read().strip() == "1"
    except OSError:
        return False


# ------------------------------------------------------------------ files
def write_once(path, text, force=False, **kw):
    """App-owned state (API-key config.xml, auth.json, config.json...) is only written on the first run.
    A re-run must never clobber what an app has since saved; `--reconfigure` (force) opts in explicitly."""
    if os.path.exists(path) and not force:
        return False
    write_file(path, text, **kw)
    return True


ARR_XML = """<Config>
  <BindAddress>*</BindAddress>
  <Port>{port}</Port>
  <SslPort>{ssl}</SslPort>
  <EnableSsl>False</EnableSsl>
  <LaunchBrowser>False</LaunchBrowser>
  <ApiKey>{key}</ApiKey>
  <AuthenticationMethod>None</AuthenticationMethod>
  <AuthenticationRequired>Enabled</AuthenticationRequired>
  <Branch>{branch}</Branch>
  <LogLevel>info</LogLevel>
  <UpdateMechanism>Docker</UpdateMechanism>
  <InstanceName>{inst}</InstanceName>
</Config>
"""


def render_all(a, state_dir, force=False):
    puid, pgid = ids()
    S = load_secrets(state_dir)
    pins = seed_json("images.json")
    stack = ensure_dir(a["stack_dir"])
    own = dict(uid=puid, gid=pgid)

    # directories the containers write to must exist and be owned by the user *before* first start;
    # a root-owned auto-created bind mount is the classic cause of "the app came up unconfigured".
    for d in ("decypharr", "prowlarr", "sonarr", "radarr", "bazarr", "jellyseerr", "recyclarr", "pearlarr",
              "babysitarr", "homepage", "homepage/icons", "jellyfin/config", "jellyfin/cache",
              "prowlarr/Definitions/Custom", "bazarr/config", "recyclarr"):
        ensure_dir(os.path.join(stack, d))
    cache = a.get("cache", {})
    if cache.get("path"):
        ensure_dir(cache["path"])
    # symlinks/<category> is where decypharr puts finished downloads; Sonarr/Radarr warn "directory does not appear to exist" until it exists
    for sub in ("symlinks/movies", "symlinks/shows", "symlinks/tv-sonarr", "symlinks/radarr"):
        ensure_dir(os.path.join(a["media_root"], sub))
    ensure_dir(os.path.join(a["media_root"], "decypharr"))
    if os.geteuid() == 0:
        for root, dirs, files in os.walk(stack):
            os.chown(root, puid, pgid)
            for f in files:
                os.chown(os.path.join(root, f), puid, pgid)
        for d in (a["media_root"], os.path.join(a["media_root"], "symlinks"),
                  *(os.path.join(a["media_root"], "symlinks", x) for x in ("movies", "shows", "tv-sonarr", "radarr"))):
            os.chown(d, puid, pgid)

    # the two non-image services live inside the stack dir, so the stack keeps working if the kit is deleted
    for v in ("babysitarr", "arabarr"):
        shutil.copytree(os.path.join(KIT, "vendor", v), os.path.join(stack, "vendor", v), dirs_exist_ok=True)

    # compose + env
    compose = build_compose(a, S, pins, puid, pgid)
    write_file(os.path.join(stack, "docker-compose.yml"),
               "# Generated by the media-stack installer. Settings live in the per-app folders next to this file.\n"
               + to_yaml(compose) + "\n", **own)
    first_rd = next((k["api_key"] for k in a["debrid"] if k["provider"] == "realdebrid"), "")
    write_file(os.path.join(stack, ".env"), f"RD_API_KEY={first_rd}\n", mode=0o600, **own)

    # pinned API keys for the three *arr apps
    for n, port, ssl, branch, inst in (("sonarr", 8989, 9898, "main", "Sonarr"),
                                       ("radarr", 7878, 9898, "master", "Radarr"),
                                       ("prowlarr", 9696, 6969, "master", "Prowlarr")):
        write_once(os.path.join(stack, n, "config.xml"),
                   ARR_XML.format(port=port, ssl=ssl, key=S[n], branch=branch, inst=inst), force, mode=0o600, **own)

    # custom Prowlarr indexer definitions (Torrentio)
    cdir = os.path.join(SEED, "prowlarr/Definitions/Custom")
    for fn in os.listdir(cdir):
        shutil.copy2(os.path.join(cdir, fn), os.path.join(stack, "prowlarr/Definitions/Custom", fn))

    # bazarr: only the pinned API key pre-start; everything else is applied via its settings API
    write_once(os.path.join(stack, "bazarr/config/config.yaml"),
               f"auth:\n  apikey: {S['bazarr']}\n", force, mode=0o600, **own)

    # decypharr: pin its API token (it would otherwise invent one that Babysitarr can't know)
    write_once(os.path.join(stack, "decypharr/auth.json"), json.dumps({"api_token": S["decypharr_token"]}),
               force and False, mode=0o600, **own)       # never regenerated: it holds the registered login
    render_decypharr(a, S, stack, own, force)
    render_recyclarr(a, S, stack, own, force)
    render_pearlarr(a, S, stack, own, force)
    if a.get("enable_arabarr"):
        idx = a["indexer_accounts"]["arabp2p"]
        write_file(os.path.join(stack, "arabarr.env"),
                   "\n".join([
                       "PROWLARR_URL=http://prowlarr:9696", f"PROWLARR_API_KEY={S['prowlarr']}",
                       "ARABP2P_INDEXER_ID=__SET_BY_INSTALLER__",
                       "SONARR_URL=http://sonarr:8989", f"SONARR_API_KEY={S['sonarr']}",
                       "RADARR_URL=http://radarr:7878", f"RADARR_API_KEY={S['radarr']}",
                       f"PROXY_API_KEY={S['arabarr_proxy']}", f"TMDB_API_KEY={a['tmdb_api_key']}",
                       "PORT=5011", "ARABARR_BASE_URL=http://arabarr:5011",
                       f"ARABP2P_USERNAME={idx['username']}", f"ARABP2P_PASSWORD={idx['password']}", ""]),
                   mode=0o600, **own)
    # homepage static look; services.yaml is generated last, from what is actually running
    hp = os.path.join(stack, "homepage")
    for fn in ("settings.yaml", "widgets.yaml", "bookmarks.yaml", "docker.yaml"):
        write_file(os.path.join(hp, fn), seed_text(f"homepage/{fn}").replace("__HOST__", a["host_ip"]), **own)
    shutil.copy2(os.path.join(SEED, "homepage/icons/arabarr.png"), os.path.join(hp, "icons/arabarr.png"))
    for f in ("custom.css", "custom.js"):
        write_file(os.path.join(hp, f), "", **own)
    info(f"Rendered stack into {stack}")
    return S


def render_decypharr(a, S, stack, own, force=False):
    c = seed_json("decypharr/config.template.json")
    by = {}
    for k in a["debrid"]:
        by.setdefault(k["provider"], []).append(k["api_key"])
    live_names = {"realdebrid": "realdebrid", "alldebrid": "Alldebrid", "torbox": "torbox"}
    folders = {"realdebrid": "/mnt/decypharr/realdebrid/__all__/", "alldebrid": "/mnt/decypharr/alldebrid/",
               "torbox": "/mnt/decypharr/torbox/"}
    tmpl = {"rate_limit": "250/minute", "minimum_free_slot": 1, "torrents_refresh_interval": "10m",
            "download_links_refresh_interval": "40m", "workers": 200, "auto_expire_links_after": "3d",
            "use_webdav": True}
    deb = []
    for prov in dict.fromkeys(k["provider"] for k in a["debrid"]):   # keeps the order the user gave
        d = dict(tmpl)
        if prov == "realdebrid":
            d["download_links_refresh_interval"] = "5m"
        d.update(provider=prov, name=live_names[prov], api_key=by[prov][0], download_api_keys=by[prov],
                 folder=folders[prov])
        deb.append(d)
    c["debrids"] = deb
    for arr in c["arrs"]:
        arr["token"] = S[arr["name"]]
    # Mount engine: DFS when the friend enabled the cache on a suitable SSD (measured faster than rclone on cold reads,
    # cache hits, seeks, concurrent streams, RAM and restarts). DFS always keeps a disk cache, so a machine with no
    # suitable drive (e.g. a slow hard drive) uses rclone with its cache switched OFF instead.
    cache = a.get("cache", {})
    m = c["mount"]
    rc = m["rclone"]
    if cache.get("path"):
        m["type"] = "dfs"
        m["dfs"].update(cache_dir="/app/cache/dfs", disk_cache_size=f"{cache['size_gb']}GB", cache_expiry="72h")
    else:
        m["type"] = "rclone"
        rc["vfs_cache_mode"] = "off"
        rc.pop("vfs_cache_max_size", None)
        rc.pop("vfs_cache_max_age", None)
    write_once(os.path.join(stack, "decypharr/config.json"), json.dumps(c, indent=2) + "\n", force, mode=0o600, **own)


def render_recyclarr(a, S, stack, own, force=False):
    t = seed_text("recyclarr/recyclarr.template.yml")
    parts = t.split("\nradarr:\n")
    t = parts[0].replace("__KEY__", S["sonarr"]) + "\nradarr:\n" + parts[1].replace("__KEY__", S["radarr"])
    write_once(os.path.join(stack, "recyclarr/recyclarr.yml"), t, force, mode=0o600, **own)


def render_pearlarr(a, S, stack, own, force=False):
    t = seed_text("pearlarr/config.template.yml")
    t = t.replace("__ARR_KEY__", S["sonarr"], 1).replace("__ARR_KEY__", S["radarr"], 1)
    t = t.replace("__QBIT_USERNAME__", a["admin_user"]).replace("__QBIT_PASSWORD__", a["admin_pass"])
    write_once(os.path.join(stack, "pearlarr/config.yml"), t, force, mode=0o600, **own)
