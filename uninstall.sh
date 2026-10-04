#!/usr/bin/env bash
# Removes this stack completely, so the next ./install.sh starts from nothing.
#   ./uninstall.sh            shows what it will remove and asks first
#   ./uninstall.sh --yes      no question
#   ./uninstall.sh --images   also delete the downloaded Docker images (the next install downloads them again)
# Removes: the stack's containers + networks, the debrid mount, the settings folder (~/media-stack, including root-owned files),
# the library folder's decypharr/ and symlinks/ folders (and the library folder itself if that leaves it empty), the cache folder,
# and this kit's saved answers. It never deletes anything else in your library folder.
set -uo pipefail
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${STACK_STATE_DIR:-$KIT/state}"; YES=""; IMAGES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=1; shift;;
    --images) IMAGES=1; shift;;
    --state-dir) STATE_DIR="${2:?}"; shift 2;;
    -h|--help) sed -n '2,9p' "$0"; exit 0;;
    *) echo "Unknown option: $1"; exit 1;;
  esac
done
[ "$(id -u)" -ne 0 ] || { echo "Please run this as your normal user, not root."; exit 1; }

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
ok()   { printf '%s\n' "${G}✔${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*"; }
LEFT=0

# ---- what to remove, from the saved answers (or sensible defaults)
read_ans() { python3 - "$STATE_DIR/answers.json" "$1" <<'PY' 2>/dev/null
import json, sys
try:
    a = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
k = sys.argv[2]
v = (a.get("cache") or {}).get("path") if k == "cache" else a.get(k)
print(v or "")
PY
}
STACK="$(read_ans stack_dir)";  [ -n "$STACK" ] || STACK="$HOME/media-stack"
MEDIA="$(read_ans media_root)"
CACHE="$(read_ans cache)"
PRE="$(read_ans container_prefix)"
# no answers file (e.g. it was deleted)? find the library folder from the debrid mount, which has a recognisable name
if [ -z "$MEDIA" ]; then
  MEDIA="$(awk '$3 ~ /^fuse/ && $2 ~ /\/decypharr$/ {print $2; exit}' /proc/mounts | sed 's,/decypharr$,,')"
fi
NAMES="decypharr prowlarr sonarr radarr bazarr flaresolverr jellyseerr jellyfin recyclarr pearlarr homepage babysitarr arabarr"

safe_path() {   # refuse anything that could be a system or home folder
  case "$1" in ""|/|/home|/root|/mnt|/media|"$HOME") return 1;; esac
  [ "$(printf '%s' "$1" | tr -cd / | wc -c)" -ge 2 ]
}

echo "${B}This will remove:${N}"
echo "  - the stack's containers and networks (names: $NAMES${PRE:+, prefix $PRE})"
echo "  - settings folder:    $STACK"
[ -n "$MEDIA" ] && echo "  - library folder bits: $MEDIA/decypharr and $MEDIA/symlinks (the folder itself too if it ends up empty)"
[ -n "$CACHE" ] && echo "  - cache folder:       $CACHE"
echo "  - saved answers:      $STATE_DIR"
[ -n "$IMAGES" ] && echo "  - downloaded Docker images of this stack"
if [ -z "$YES" ]; then
  read -r -p "Continue? [y/N] " ans; case "$ans" in y|Y|yes) ;; *) echo "Nothing was changed."; exit 0;; esac
fi

# ---- containers + networks
if command -v docker >/dev/null 2>&1; then
  [ -f "$STACK/docker-compose.yml" ] && docker compose --project-directory "$STACK" down -v --remove-orphans >/dev/null 2>&1
  for n in $NAMES; do
    docker ps -a --format '{{.Names}}' | grep -qx "$PRE$n" && docker rm -f "$PRE$n" >/dev/null 2>&1
  done
  docker network ls --format '{{.Name}}' | grep -E "^($(basename "$STACK")|media-stack)(_default)?$" | xargs -r docker network rm >/dev/null 2>&1 || true
  if [ -n "$IMAGES" ]; then
    python3 -c "import json;print('\n'.join(v['tag'] for v in json.load(open('$KIT/seed/images.json')).values()))" 2>/dev/null | xargs -r docker rmi -f >/dev/null 2>&1 || true
  fi
  ok "containers and networks removed"
else
  warn "Docker isn't installed, so there are no containers to remove"
fi

# ---- unmount everything the stack mounted (before deleting anything: never delete through a debrid mount)
unmount_under() {
  local base="$1" mp
  [ -n "$base" ] || return 0
  for _ in 1 2 3; do
    awk -v b="$base/" 'index($2 "/", b) == 1 {print $2}' /proc/mounts | sort -r | while read -r mp; do
      fusermount3 -uz "$mp" 2>/dev/null || fusermount -uz "$mp" 2>/dev/null || sudo umount -l "$mp" 2>/dev/null
    done
  done
  ! awk -v b="$base/" 'index($2 "/", b) == 1 {found=1} END {exit !found}' /proc/mounts
}
MOUNT_OK=1
for base in "$MEDIA" "$STACK" "$CACHE"; do
  if [ -n "$base" ] && ! unmount_under "$base"; then MOUNT_OK=0; warn "a mount under $base would not unmount, so I left that folder alone"; LEFT=1; fi
done
[ "$MOUNT_OK" = 1 ] && ok "debrid mount cleared"

rmtree() {   # delete a folder; use sudo for the root-owned files containers create
  local d="$1"
  [ -e "$d" ] || return 0
  safe_path "$d" || { warn "won't delete $d (looks like a system or home folder)"; LEFT=1; return 0; }
  rm -rf --one-file-system "$d" 2>/dev/null
  if [ -e "$d" ]; then sudo rm -rf --one-file-system "$d"; fi
  if [ -e "$d" ]; then warn "could not delete $d"; LEFT=1; else ok "removed $d"; fi
}

[ "$MOUNT_OK" = 1 ] && rmtree "$STACK"
if [ -n "$MEDIA" ] && safe_path "$MEDIA" && ! awk -v b="$MEDIA/" 'index($2 "/", b) == 1 {found=1} END {exit !found}' /proc/mounts; then
  rmtree "$MEDIA/decypharr"; rmtree "$MEDIA/symlinks"
  rmdir "$MEDIA" 2>/dev/null && ok "removed the empty library folder $MEDIA"
fi
if [ -n "$CACHE" ] && [ "$(basename "$CACHE")" = "media-stack-cache" ]; then rmtree "$CACHE"; fi

rm -rf "$STATE_DIR" "$HOME/media-stack-diag.txt" 2>/dev/null
ok "saved answers removed"

# ---- check that nothing is left
if command -v docker >/dev/null 2>&1; then
  for n in $NAMES; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$PRE$n"; then warn "container $PRE$n is still there"; LEFT=1; fi
  done
fi
[ -e "$STACK" ] && { warn "$STACK is still there"; LEFT=1; }
if [ "$LEFT" = 0 ]; then echo; ok "${B}Everything is removed.${N} Run ./install.sh to start from scratch."; else echo; warn "Something is left (see above)."; exit 1; fi
