#!/usr/bin/env bash
# Media stack installer. Run as your normal user (not root):   ./install.sh
#
#   ./install.sh                       asks a few questions (in your browser on a desktop, else in the terminal),
#                                      then installs by itself
#   ./install.sh --cli                 force the terminal questions
#   ./install.sh --web [--no-browser]  force the browser page (prints a link; --no-browser = don't auto-open)
#   ./install.sh --web --host 0.0.0.0  browser page reachable from another computer (e.g. when installing over SSH)
#   ./install.sh --diag                collect what's needed to debug a failed install into ~/media-stack-diag.txt
#   ./install.sh --answers file.json   no questions (for repeat installs / testing)
#   ./install.sh --reconfigure         re-apply your answers to the app config files
#
# Works on Debian/Ubuntu/Mint, Fedora/RHEL, Arch/Manjaro, openSUSE. Everything else runs inside Docker,
# so the distro doesn't matter beyond installing Docker itself.
set -euo pipefail

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$KIT"

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
say()  { printf '%s\n' "${B}$*${N}"; }
ok()   { printf '%s\n' "${G}✔${N} $*"; }
warn() { printf '%s\n' "${Y}!${N} $*"; }
die()  { printf '%s\n' "${R}✘ $*${N}" >&2; exit 1; }

ANSWERS=""; RECONFIGURE=""; STATE_DIR="$KIT/state"; FRESH=""; UI="auto"; NOBROWSER=""; UIHOST="127.0.0.1"
while [ $# -gt 0 ]; do
  case "$1" in
    --answers)     ANSWERS="${2:?--answers needs a file}"; shift 2;;
    --reconfigure) RECONFIGURE="--reconfigure"; shift;;
    --state-dir)   STATE_DIR="${2:?}"; shift 2;;
    --fresh)       FRESH=1; shift;;
    --diag)        exec bash "$KIT/diag.sh";;
    --cli)         UI=cli; shift;;
    --web)         UI=web; shift;;
    --no-browser)  NOBROWSER=1; shift;;
    --host)        UIHOST="${2:?--host needs an address}"; shift 2;;
    -h|--help)     sed -n '2,16p' "$0"; exit 0;;
    *) die "Unknown option: $1 (try --help)";;
  esac
done

[ "$(id -u)" -ne 0 ] || die "Please run this as your normal user, not as root (it uses sudo only where needed)."
[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux."

# ---------------------------------------------------------------- package manager
PM=""
if   command -v apt-get >/dev/null; then PM=apt
elif command -v dnf     >/dev/null; then PM=dnf
elif command -v pacman  >/dev/null; then PM=pacman
elif command -v zypper  >/dev/null; then PM=zypper
fi

SUDO=""
need_sudo() {
  [ -n "$SUDO" ] && return 0
  if command -v sudo >/dev/null; then SUDO="sudo"; else die "I need to install something but 'sudo' isn't available. Install it, or run the missing pieces as root."; fi
}

pkg_install() {   # pkg_install <apt-name> <dnf-name> <pacman-name> <zypper-name>
  need_sudo
  case "$PM" in
    apt)    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$1" ;;
    dnf)    $SUDO dnf install -y -q "$2" ;;
    pacman) $SUDO pacman -S --noconfirm --needed "$3" ;;
    zypper) $SUDO zypper --non-interactive install "$4" ;;
    *)      die "Unsupported distro: please install '$1' manually and re-run." ;;
  esac
}

say "Checking what this machine needs"

# ---------------------------------------------------------------- python3 (>= 3.8)
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
  warn "Python 3.8+ not found; installing"
  pkg_install python3 python3 python python3
fi
ok "Python $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"

# ---------------------------------------------------------------- small tools the installer uses
command -v curl    >/dev/null || pkg_install curl curl curl curl
command -v findmnt >/dev/null || pkg_install util-linux util-linux util-linux util-linux
command -v ip      >/dev/null || pkg_install iproute2 iproute iproute2 iproute2
# fusermount3: lets the installer clear a dead debrid mount without root
command -v fusermount3 >/dev/null || command -v fusermount >/dev/null || pkg_install fuse3 fuse3 fuse3 fuse3
ok "Helper tools present"

# ---------------------------------------------------------------- docker
if ! command -v docker >/dev/null; then
  warn "Docker isn't installed; installing it now"
  need_sudo
  if [ "$PM" = pacman ]; then
    $SUDO pacman -S --noconfirm --needed docker docker-compose
  elif [ "$PM" = zypper ]; then
    $SUDO zypper --non-interactive install docker docker-compose
  else
    curl -fsSL https://get.docker.com | $SUDO sh      # Docker's official convenience script
  fi
  $SUDO systemctl enable --now docker 2>/dev/null || $SUDO service docker start || true
fi

if ! docker compose version >/dev/null 2>&1; then
  warn "Docker Compose plugin missing; installing"
  pkg_install docker-compose-plugin docker-compose-plugin docker-compose docker-compose
fi

# the user must be able to talk to the Docker daemon without sudo
if ! docker info >/dev/null 2>&1; then
  if [ -z "${STACK_REEXEC:-}" ] && ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    warn "Adding you to the 'docker' group"
    need_sudo
    $SUDO usermod -aG docker "$USER"
  fi
  if [ -z "${STACK_REEXEC:-}" ] && command -v sg >/dev/null; then
    # pick up the new group right now instead of asking you to log out and in
    export STACK_REEXEC=1
    exec sg docker -c "$(printf '%q ' "$0" "$@")"
  fi
  die "Docker is installed but this user can't use it. Log out and back in (or reboot), then run ./install.sh again."
fi
ok "Docker $(docker --version | sed -E 's/Docker version ([^,]+).*/\1/')"

# ---------------------------------------------------------------- questions
mkdir -p "$STATE_DIR"
ANS_FILE="$STATE_DIR/answers.json"

# Browser page by default when this is a desktop session; terminal questions otherwise (e.g. over SSH).
if [ "$UI" = auto ]; then
  if [ -n "$ANSWERS" ]; then UI=cli
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then UI=web
  else UI=cli; fi
fi

if [ "$UI" = web ] && [ -z "$ANSWERS" ]; then
  say "Starting the installer page"
  ARGS=(--state-dir "$STATE_DIR" --host "$UIHOST")
  [ -n "$NOBROWSER" ] && ARGS+=(--no-browser)
  [ -n "$RECONFIGURE" ] && ARGS+=(--reconfigure)
  # the page collects the answers, then runs the same unattended install and shows its progress
  exec python3 -u lib/webui.py "${ARGS[@]}"
fi

if [ -n "$ANSWERS" ]; then
  python3 lib/wizard.py --answers "$ANSWERS" --out "$ANS_FILE"
elif [ -f "$ANS_FILE" ] && [ -z "$FRESH" ] && [ -t 0 ]; then
  say "Found your answers from a previous run."
  read -r -p "Use them again? (Y/n; n = answer the questions again): " yn
  case "${yn:-y}" in [Nn]*) python3 lib/wizard.py --out "$ANS_FILE";; esac
else
  python3 lib/wizard.py --out "$ANS_FILE"
fi

# ---------------------------------------------------------------- install
say "Installing. You don't need to do anything until it finishes."
exec python3 lib/deploy.py --answers "$ANS_FILE" --state-dir "$STATE_DIR" $RECONFIGURE
