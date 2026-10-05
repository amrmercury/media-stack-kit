#!/usr/bin/env bash
# Runs in the background during a CI install: every so often it restarts one of the stack's app containers, the way apps restart
# themselves after plugin installs / config changes on a slow machine. The installer must ride through all of it.
# (CI machines only. Never run this on a real server.)   usage: ci_chaos.sh <stop-file> <log>
STOP="$1"; LOG="$2"
APPS=(jellyfin sonarr radarr prowlarr bazarr jellyseerr flaresolverr homepage)
RANDOM=${CHAOS_SEED:-1}
sleep 45
n=0
while [ ! -e "$STOP" ]; do
  a="${APPS[$((RANDOM % ${#APPS[@]}))]}"
  if docker ps --format '{{.Names}}' | grep -qx "$a"; then
    echo "$(date +%T) chaos: restarting $a" >> "$LOG"
    docker restart "$a" >/dev/null 2>&1; n=$((n + 1))
  fi
  sleep 20
done
echo "chaos done after $n restarts" >> "$LOG"
