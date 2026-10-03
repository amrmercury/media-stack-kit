#!/usr/bin/env bash
# Everyday control of the installed stack.
#   ./stack.sh status | up | down | restart [service] | logs [service] | update
set -euo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${STACK_STATE_DIR:-$KIT/state}"
read -r STACK MEDIA < <(python3 -c "import json,sys;a=json.load(open('$STATE_DIR/answers.json'));print(a['stack_dir'],a['media_root'])") \
  || { echo "No install found. Run ./install.sh first." >&2; exit 1; }
dc() { docker compose --project-directory "$STACK" "$@"; }

# After containers are removed, the debrid mount can linger as a dead mount that blocks the next start.
clear_mount() {
  local m="$MEDIA/decypharr"
  if ! ls "$m" >/dev/null 2>&1 && mount | grep -q " $m "; then
    echo "Clearing a leftover debrid mount..."
    fusermount3 -uz "$m" 2>/dev/null || fusermount -uz "$m" 2>/dev/null || sudo umount -l "$m"
  fi
}

case "${1:-status}" in
  status)  dc ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}';;
  up)      clear_mount; dc up -d;;
  down)    dc down; clear_mount;;
  restart) shift; [ "${1:-}" = decypharr ] && { dc stop decypharr; clear_mount; dc start decypharr; } || dc restart "$@";;
  logs)    shift; dc logs --tail 100 -f "$@";;
  update)
    echo "Images are pinned to the exact versions this setup was tested with, so there is nothing to auto-update."
    echo "To move to newer versions, ask whoever shared this kit for a refreshed copy.";;
  *) echo "Usage: $0 status|up|down|restart [service]|logs [service]|update"; exit 1;;
esac
