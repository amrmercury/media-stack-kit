#!/usr/bin/env python3
"""Jellyfin answers 503 while it restarts (slow laptops): the installer must wait and retry, not report a refused login. python3 tests/test_jellyfin_retry.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import jellyfin
from common import Resp, StackError

calls = []
def fake_http(seq):
    it = iter(seq)
    def f(method, url, **kw):
        calls.append(url); return next(it)
    return f

class FakeTime:                                   # only jellyfin.py sees this; the real time module stays untouched
    def __init__(self): self.now = 0
    def sleep(self, s): self.now += s
    def time(self): self.now += 1; return self.now
ok_login = Resp(200, '{"AccessToken": "tok", "User": {"Id": "u1"}}', {"Content-Type": "application/json"})

jellyfin.time = FakeTime()
jellyfin.http = fake_http([Resp(503, "", {}), Resp(503, "", {}), ok_login])
j = jellyfin.Jellyfin("http://x", "/s"); j.login("alex", "pw")
assert j.token == "tok" and len(calls) == 3, "should retry through 503s and then log in"
print("  ok  503 while starting is retried until the login works")

jellyfin.http = fake_http([Resp(401, "", {})]); j = jellyfin.Jellyfin("http://x", "/s")
try: j.login("alex", "bad"); raise SystemExit("401 must raise")
except StackError as e: assert "rejected the login" in str(e)
print("  ok  a real 401 is still reported as a refused login")

jellyfin.time = FakeTime()
jellyfin.http = lambda *a, **k: Resp(503, "", {})
j = jellyfin.Jellyfin("http://x", "/s")
try: j.login("alex", "pw"); raise SystemExit("persistent 503 must raise")
except StackError as e: assert "still starting up" in str(e) and "rejected" not in str(e)
print("  ok  a Jellyfin that never wakes up gives a clear 'still starting' message")
def flaky(seq):
    it = iter(seq)
    def f(method, url, **kw):
        v = next(it)
        if isinstance(v, Exception): raise v
        return v
    return f

jellyfin.time = FakeTime()
jellyfin.http = flaky([StackError("cannot reach x: [Errno 104] Connection reset by peer"), StackError("cannot reach x: refused"), ok_login])
j = jellyfin.Jellyfin("http://x", "/s"); j.login("alex", "pw")
assert j.token == "tok"
print("  ok  a connection reset while Jellyfin restarts is waited out too")

jellyfin.time = FakeTime()
jellyfin.http = lambda *a, **k: (_ for _ in ()).throw(StackError("cannot reach x: reset"))
try: jellyfin.Jellyfin("http://x", "/s").login("alex", "pw"); raise SystemExit("must raise")
except StackError as e: assert "cannot reach" in str(e)
print("  ok  a Jellyfin that never comes back still fails clearly")
print("\n5 jellyfin retry checks passed")
