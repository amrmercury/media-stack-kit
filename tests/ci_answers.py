#!/usr/bin/env python3
"""Writes a fake-credential answers file for CI.   usage: ci_answers.py <basic|full> <out.json>
Fake keys are used on purpose: nothing real ever goes near a public repo's logs."""
import json, os, sys

case, out = sys.argv[1], sys.argv[2]
home = os.path.expanduser("~")
a = {"admin_user": "ciuser", "admin_pass": "Passw0rd!ci-test", "media_root": f"{home}/ci-media", "stack_dir": f"{home}/media-stack",
     "timezone": "UTC", "debrid": [{"provider": "realdebrid", "api_key": "FAKE" + "R" * 48}],
     "indexer_accounts": {}, "subtitles": {}, "tmdb_api_key": "", "enable_arabarr": False, "cache": {"path": None}}
if case == "full":
    a["debrid"] += [{"provider": "torbox", "api_key": "FAKETORBOXKEY0123456789"}, {"provider": "alldebrid", "api_key": "FAKEALLDEBRIDKEY0123"}]
    a["indexer_accounts"] = {"arabicsource": {"apikey": "FAKEARABICSOURCEKEY"}, "arabp2p": {"username": "cifake", "password": "fakepass1"}}
    a["tmdb_api_key"], a["enable_arabarr"] = "0123456789abcdef0123456789abcdef", True
    a["subtitles"] = {"opensubtitlescom": {"username": "fakeuser", "password": "fakepass"}, "subsource": {"apikey": "FAKESUBSOURCE"},
                      "subdl": {"api_key": "FAKESUBDL"}}
    a["cache"] = {"path": f"{home}/ci-cache/media-stack-cache", "size_gb": 5}      # DFS engine + cache
json.dump(a, open(out, "w"), indent=1)
print(f"wrote {out} ({case})")
