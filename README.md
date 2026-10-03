# Media stack kit

One command installs a complete, pre-configured media server on any Linux PC with Docker:
Jellyfin, Jellyseerr, Sonarr, Radarr, Prowlarr, Bazarr, Recyclarr, decypharr (debrid), FlareSolverr,
Babysitarr, Pearlarr, Arabarr (optional) and a Homepage dashboard that lists all of them.

You get the **settings** (quality profiles, indexers, plugins, subtitle setup, connections between the apps).
You do **not** get anyone's library, history or accounts: the library starts empty and fills from *your* debrid account.

## Install

```bash
./install.sh
```

On a desktop it opens a **page in your browser** with the questions (and shows live progress while it installs). With no
screen, for example over SSH, it asks in the terminal instead. Force either with `./install.sh --web` or `--cli`.
To use the page from another computer: `./install.sh --web --host 0.0.0.0`, then open the link it prints.

It asks a few questions up front, then runs by itself:

1. **A username + password.** Used in *every* app (Jellyfin, Jellyseerr, Sonarr, Radarr, Prowlarr, Bazarr, decypharr).
   Everything you type or paste is shown on screen, so you can check it.
2. **Where your library should live**: pick a drive from a numbered menu, or browse to a folder. No paths to type.
3. **Debrid keys**: Real-Debrid, AllDebrid and/or TorBox. Add as many as you like.
4. **Other indexer accounts** (optional): ArabTorrents, ArabicSource. Skip any you don't have.
5. **Arabic series** (optional): installs Arabarr. Needs your own ArabP2P account and a free TMDB API key (links are shown).
6. **Subtitle providers** (optional): OpenSubtitles (needs your *username*, not your email), Subsource, SubDL.
   Bazarr is always connected to Sonarr and Radarr; skipping all providers just leaves it with no providers.
7. **Cache** (optional): only offered if a fast SSD with enough free space is found; shows how much space it will use.
   Enabling it also switches the debrid mount to decypharr's faster DFS engine. Machines without a suitable drive
   (for example a slow hard drive) run the standard rclone mount with its cache switched off.

Needs: a 64-bit Linux PC, 4 GB RAM or more, internet. Docker is installed for you if missing.
Works on Debian/Ubuntu/Mint, Fedora, Arch, openSUSE (anything else with Docker + `sudo` should also work).

## What you have afterwards

Open **Homepage** (`http://<this-pc>:3001`): it links to everything. Then:

- **Jellyseerr** (`:5055`): request a show or movie. It flows to Sonarr/Radarr, decypharr fetches it from
  your debrid account, and it appears in **Jellyfin** (`:8096`).
- Every app is already connected to the others, and the installer *signs in to each one* at the end to prove
  your login works (and that a wrong password is refused).
- Playback is **direct play** by default (the server doesn't transcode video, so even an old CPU is fine).
  A client that can't play a file will show an error; the owner can allow transcoding per user in Jellyfin
  (Dashboard → Users → Playback). 4K remux is best on a wired connection; fast Wi-Fi also works.
- Symlink repair runs for everyone: decypharr's scheduled repair plus Babysitarr's broken-link watchdog.

## Everyday use

```bash
./stack.sh status        # what's running
./stack.sh down / up     # stop / start (clears the debrid mount cleanly)
./stack.sh restart sonarr
./stack.sh logs decypharr
./install.sh             # safe to re-run any time; nothing is duplicated or reset
./install.sh --reconfigure   # re-apply your answers to the app config files
```

Image versions are pinned to the exact builds this setup was tested with.

## If something looks wrong

- **Everything reports ✔ at the end but a page won't load**: give it a minute after a reboot, then `./stack.sh status`.
- **"Transport endpoint is not connected" on the media folder**: a leftover dead mount; `./stack.sh down && ./stack.sh up`
  (or just re-run `./install.sh`) clears it.
- **Small machine (4 GB)**: expect ~1.5 GB used when idle, growing with your library. A swap file is a good idea.
- Your answers are saved in `state/answers.json` (private to your user). Delete it to answer the questions again.

---

### For the person who maintains the kit

```bash
node tests/test_ui_logic.js && python3 tests/test_webui.py   # installer-page tests (no Docker, no network)
python3 tools/export_from_live.py          # snapshot your live settings into seed/ (secrets stripped)
AUDIT_EXTRA=you@example.com python3 tools/audit_seed.py   # fails if ANY real secret/personal value is in the kit
bash tools/package.sh                      # builds media-stack-kit.tar.gz (runs the audit first; omits tools/, state/)
```

**Testing the kit:** do it on a different machine or VM, not beside a live stack that uses the same
debrid FUSE mount (shared mounts are not isolated from each other).

`seed/` holds the settings, `lib/` the installer, `vendor/` your two non-image services (Babysitarr fork, Arabarr).
