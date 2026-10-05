#!/usr/bin/env bash
# CI only: kill the installer at several points mid-install (like a crash, a closed terminal, a laptop that shut off), then re-run it
# each time. Every re-run must pick up and the last one must finish and verify. usage: ci_resume.sh <answers.json> <state-dir>
set -u
ANS="$1"; STATE="$2"; LOG="$STATE/install.log"
run_until() {            # run_until <phrase>: start the installer, kill it ~4s after <phrase> appears in the log
  local phrase="$1" n0 pid
  n0=$(grep -c "$phrase" "$LOG" 2>/dev/null || echo 0)
  setsid ./install.sh --answers "$ANS" --state-dir "$STATE" > /dev/null 2>&1 &
  pid=$!
  for _ in $(seq 1 600); do
    [ "$(grep -c "$phrase" "$LOG" 2>/dev/null || echo 0)" -gt "$n0" ] && break
    kill -0 "$pid" 2>/dev/null || { echo "installer ended before '$phrase' appeared"; return 0; }
    sleep 1
  done
  sleep 4
  echo "killing the installer (process group $pid) after '$phrase'"
  kill -9 -- "-$pid" 2>/dev/null; pkill -9 -f "lib/deploy.py" 2>/dev/null; wait "$pid" 2>/dev/null
  return 0
}
mkdir -p "$STATE"
for phrase in "Starting containers" "Jellyfin: setup" "Prowlarr: indexers" "Sonarr + Radarr" "Bazarr: subtitles" "Jellyseerr" "Recyclarr: quality" "Homepage (last"; do
  echo "=== kill point: $phrase"
  run_until "$phrase"
done
echo "=== final run (must succeed)"
./install.sh --answers "$ANS" --state-dir "$STATE" 2>&1 | tee "$STATE/resume-final.log"
test "${PIPESTATUS[0]}" -eq 0
