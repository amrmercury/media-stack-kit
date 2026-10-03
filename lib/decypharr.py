"""decypharr: create its login (bcrypt-hashed by the app) and verify both its web and qBittorrent-API logins."""
import json, os, sys, urllib.parse, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import StackError, http, info, ok, wait_ready, load_json


class Decypharr:
    def __init__(self, base, stack_dir, token):
        self.base, self.stack_dir, self.token = base.rstrip("/"), stack_dir, token

    def wait_ready(self, timeout=240):
        wait_ready("decypharr", lambda: http("GET", f"{self.base}/api/v2/app/version").status == 200, timeout)

    def _auth_file(self):
        return load_json(os.path.join(self.stack_dir, "decypharr/auth.json"), {})

    def registered(self):
        return bool(self._auth_file().get("username"))

    def register(self, user, password):
        if self.registered():
            info("decypharr already has a login; verifying it matches yours")
            return
        boundary = "----stack" + uuid.uuid4().hex
        parts = []
        for k, v in (("username", user), ("password", password), ("confirmPassword", password)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
        body = ("".join(parts) + f"--{boundary}--\r\n").encode()
        r = http("POST", f"{self.base}/register", data=body,
                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if r.status not in (200, 303):
            raise StackError(f"decypharr registration failed: HTTP {r.status} {r.body[:200]}")
        ok("decypharr login created")

    def verify_web_login(self, user, password):
        r = http("POST", f"{self.base}/login", json_body={"username": user, "password": password})
        return r.status in (200, 303)

    def verify_qbit_login(self, user, password):
        """What Sonarr/Radarr/Pearlarr use to talk to it."""
        r = http("POST", f"{self.base}/api/v2/auth/login",
                 form={"username": user, "password": password})
        return r.status == 200 and r.body.strip().startswith("Ok")

    def mounted(self, media_root):
        """The debrid FUSE mount must be visible on the host through shared mount propagation."""
        try:
            with open("/proc/self/mountinfo") as f:
                return any(os.path.join(media_root, "decypharr") + " " in line for line in f)
        except OSError:
            return False
