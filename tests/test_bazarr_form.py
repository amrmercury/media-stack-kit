#!/usr/bin/env python3
"""Regression test: the settings form posted to Bazarr must never repeat a scalar key (Bazarr then reads it as a list and
rejects the whole save with HTTP 406, e.g. "use_sonarr must be bool but it is ['true','true']").   python3 tests/test_bazarr_form.py"""
import collections, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import bazarr

LIST_KEYS = {"settings-general-enabled_providers", "languages-enabled"}
S = {"sonarr": "a" * 32, "radarr": "b" * 32}
CASES = {
    "no providers": {},
    "all three providers": {"opensubtitlescom": {"username": "u", "password": "p"}, "subsource": {"apikey": "k"}, "subdl": {"api_key": "k"}},
    "one provider": {"subdl": {"api_key": "k"}},
}
bad = 0
for name, subs in CASES.items():
    sent = []
    b = bazarr.Bazarr("http://x", "key")
    b.must = lambda method, path, form=None: sent.append(form)
    b.configure({"admin_user": "alex", "admin_pass": "Passw0rd!x", "subtitles": subs}, S)
    form = sent[0]
    counts = collections.Counter(k for k, _ in form)
    dups = {k: n for k, n in counts.items() if n > 1 and k not in LIST_KEYS}
    keys = dict(form)
    ok = not dups and keys.get("settings-general-use_sonarr") == "true" and keys.get("settings-general-use_radarr") == "true" \
        and counts["settings-auth-password"] == 1 and (("languages-profiles" in keys) == bool(subs))
    print(("  ok  " if ok else "FAIL  ") + f"{name}: {len(form)} fields" + (f", duplicates: {dups}" if dups else ""))
    bad += 0 if ok else 1
sys.exit(1 if bad else 0)
