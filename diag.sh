#!/usr/bin/env bash
# Collects what's needed to understand a failed or interrupted install, prints it, and saves it to
# ~/media-stack-diag.txt. No secrets are included (keys and passwords are never printed).
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${STACK_STATE_DIR:-$KIT/state}"
OUT="$HOME/media-stack-diag.txt"
dk() { docker "$@" 2>/dev/null || sudo -n docker "$@" 2>&1; }
{
  echo "== kit: $(git -C "$KIT" log --oneline -1 2>/dev/null || echo 'not a git checkout')"
  echo "== system: $(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}") | kernel $(uname -r) | $(nproc) cpu"
  echo "== memory and disk"; free -h | head -2; df -h / "$HOME" 2>/dev/null | sort -u
  echo; echo "== last 80 lines of the install log ($STATE/install.log)"; tail -n 80 "$STATE/install.log" 2>&1
  echo; echo "== containers"; dk ps -a --format '{{.Names}}  {{.Status}}'
  for c in $(dk ps -a --filter status=exited --filter status=restarting --filter health=unhealthy --format '{{.Names}}'); do
    echo; echo "-- last 25 log lines of $c"; dk logs --tail 25 "$c" 2>&1
  done
  echo; echo "== answers (no secrets)"
  python3 - "$STATE/answers.json" <<'PY' 2>&1
import json, sys
try:
    a = json.load(open(sys.argv[1]))
except Exception as e:
    print("no answers file:", e); sys.exit()
print("user:", a.get("admin_user"), "| library:", a.get("media_root"), "| stack:", a.get("stack_dir"))
print("debrid:", [d["provider"] for d in a.get("debrid", [])], "| subtitles:", list(a.get("subtitles", {})),
      "| arabarr:", a.get("enable_arabarr"), "| cache:", (a.get("cache") or {}).get("path"))
PY
  echo; echo "== mounts / FUSE"; ls -l /dev/fuse 2>&1; findmnt -o TARGET,FSTYPE,PROPAGATION -T "$HOME" 2>&1 | tail -2
  echo; echo "== why the machine last stopped (previous boot, tail)"; journalctl -b -1 -n 40 --no-pager 2>&1 | tail -40
  echo; echo "== errors this boot"; journalctl -b -p err -n 25 --no-pager 2>&1 | tail -25
} 2>&1 | tee "$OUT"
echo; echo "Saved to $OUT"
