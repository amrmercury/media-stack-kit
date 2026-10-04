#!/usr/bin/env python3
"""End-to-end test of the browser installer, run on a clean CI machine (never on a real server).

Starts `./install.sh --web`, drives the page in headless Chromium exactly as a person would (including wrong input),
lets the REAL install run with fake credentials, and checks the success screen. Saves a screenshot of every screen.
Any failure or hang is captured (screenshot + page text + the installer's log) and fails the run quickly.

  python3 tests/e2e_web.py --state-dir DIR --shots DIR
"""
import argparse, os, re, subprocess, sys, threading, time

from playwright.sync_api import sync_playwright, expect

ap = argparse.ArgumentParser()
ap.add_argument("--state-dir", required=True)
ap.add_argument("--shots", default="shots")
ap.add_argument("--install-timeout-min", type=int, default=20)
args = ap.parse_args()
os.makedirs(args.shots, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
N = 0


def stage(msg):
    print(f"[e2e] {msg}", flush=True)


def shot(page, name):
    global N
    N += 1
    page.screenshot(path=os.path.join(args.shots, f"{N:02d}-{name}.png"), full_page=True)


def start_server():
    srv = subprocess.Popen(["./install.sh", "--web", "--no-browser", "--state-dir", args.state_dir], cwd=ROOT,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                           env={**os.environ, "PYTHONUNBUFFERED": "1"})
    logf = open(os.path.join(args.shots, "server.log"), "w")
    url, t0 = None, time.time()
    while url is None and time.time() - t0 < 300:
        line = srv.stdout.readline()
        if not line:
            break
        logf.write(line); logf.flush()
        m = re.search(r"(http://127\.0\.0\.1:\d+/\?t=\S+)", line)
        url = m.group(1) if m else None
    if not url:
        print("FAIL: the installer never printed its page link", flush=True); srv.kill(); sys.exit(1)

    def drain():
        for l in srv.stdout:
            logf.write(l); logf.flush()
    threading.Thread(target=drain, daemon=True).start()
    return srv, url


def flow(page, srv, url):
    stage("open page")
    page.goto(url)
    expect(page.locator("h1")).to_have_text("Set up your media server")
    assert "Media Stack Installer" in page.title()
    shot(page, "welcome")
    page.get_by_role("button", name="Start").click()

    stage("login: wrong input first")
    page.get_by_role("button", name="Next").click()
    expect(page.locator("#card .err")).to_contain_text("Username")
    shot(page, "login-errors")
    page.get_by_label("Username").fill("ciuser")
    page.get_by_label("Password").fill("short")
    page.get_by_role("button", name="Next").click()
    expect(page.locator("#card .err")).to_contain_text("at least 8")
    page.get_by_label("Password").fill("Passw0rd!ci-test")
    assert page.get_by_label("Password").get_attribute("type") == "text", "password should be visible by default"
    page.get_by_role("button", name="Hide what I type").click()
    assert page.get_by_label("Password").get_attribute("type") == "password"
    page.get_by_role("button", name="Show what I type").click()
    assert page.get_by_label("Password").get_attribute("type") == "text"
    shot(page, "login")
    page.get_by_role("button", name="Next").click()

    stage("library: folder browser")
    page.get_by_role("button", name="Next").click()
    expect(page.locator("#card .err")).to_contain_text("library")
    page.get_by_role("button", name="Browse for another folder").click()
    expect(page.locator("#modal")).to_be_visible()
    page.locator("#newname").fill("bad/name")
    page.get_by_role("button", name="Create").click()
    expect(page.locator("#merr")).to_be_visible()
    page.locator("#newname").fill("ci-library")
    page.get_by_role("button", name="Create").click()
    expect(page.locator("#crumb")).to_contain_text("ci-library")
    shot(page, "folder-picker")
    page.get_by_role("button", name="Use this folder").click()
    expect(page.locator(".note code")).to_contain_text("/ci-library/Media")
    shot(page, "library")
    page.get_by_role("button", name="Next").click()

    stage("debrid keys")
    page.get_by_role("button", name="Next").click()
    expect(page.locator("#card .err")).to_contain_text("debrid key")
    page.get_by_placeholder("paste your key").first.fill("FAKE" + "R" * 48)
    page.get_by_role("button", name="+ Add another key").click()
    page.locator("select").nth(1).select_option("torbox")
    page.get_by_placeholder("paste your key").nth(1).fill("FAKETORBOXKEY0123456789")
    page.get_by_role("button", name="Back").click()           # re-enter the page so the earlier error box is gone for the picture
    page.get_by_role("button", name="Next").click()
    shot(page, "debrid")
    page.get_by_role("button", name="Next").click()

    stage("Arabic series and movies (one step)")
    expect(page.locator("h1")).to_contain_text("Arabic series")
    assert page.get_by_role("button", name="Skip").count() == 1
    box = page.locator(".opt", has_text="ArabicSource")
    box.locator("input[type=checkbox]").check()
    page.get_by_role("button", name="Next").click()
    expect(page.locator("#card .err")).to_contain_text("ArabicSource")
    box.locator("input[type=text]").fill("FAKEARABICSOURCEKEY")
    box = page.locator(".opt", has_text="Arabarr")
    box.locator("input[type=checkbox]").check()
    page.get_by_role("button", name="Next").click()
    expect(page.locator("#card .err")).to_contain_text("Arabic title matching")
    fields = box.locator("input[type=text]")
    fields.nth(0).fill("cifake"); fields.nth(1).fill("fakepass1"); fields.nth(2).fill("0123456789abcdef0123456789abcdef")
    page.get_by_role("button", name="Back").click()
    page.get_by_role("button", name="Next").click()
    shot(page, "arabic")
    page.get_by_role("button", name="Next").click()

    stage("subtitles")
    for name, vals in (("OpenSubtitles", ["fakeuser", "fakepass"]), ("Subsource", ["FAKESUBSOURCE"]), ("SubDL", ["FAKESUBDL"])):
        b = page.locator(".opt", has_text=name)
        b.locator("input[type=checkbox]").first.check()
        for i, v in enumerate(vals):
            b.locator("input[type=text]").nth(i).fill(v)
    shot(page, "subtitles")
    page.get_by_role("button", name="Next").click()

    stage("cache (drive check)")
    expect(page.get_by_text("Checking your drives")).to_have_count(0, timeout=150000)
    radios = page.locator("input[type=radio]")
    if radios.count() > 1:
        radios.first.check()
        page.locator("input[type=number]").fill("5")
    shot(page, "cache")
    page.get_by_role("button", name="Next").click()

    stage("review")
    expect(page.locator("h1")).to_have_text("Review")
    expect(page.locator("table")).to_contain_text("ciuser")
    expect(page.locator("table")).to_contain_text("Real-Debrid, TorBox")
    shot(page, "review")
    page.get_by_role("button", name="Install").click()
    expect(page.locator("h1")).to_have_text("Installing…")
    stage("installing (real install, fake keys)")
    time.sleep(20)
    shot(page, "installing")

    t0 = time.time()
    while time.time() - t0 < args.install_timeout_min * 60:
        h = page.inner_text("h1")
        if h.startswith("All done") or h.startswith("Something went wrong"):
            break
        if int(time.time() - t0) % 60 < 5:
            last = page.evaluate("document.getElementById('log') ? (document.getElementById('log').lastElementChild || {}).textContent : ''")
            stage(f"still installing ({int(time.time() - t0)}s) last log line: {last}")
        time.sleep(5)
    if not page.inner_text("h1").startswith("All done"):
        raise AssertionError("install did not succeed: " + page.inner_text("h1") + " | " + page.inner_text("#pstat"))
    shot(page, "done")
    links = page.locator(".links a")
    names = [links.nth(i).inner_text().split("\n")[0] for i in range(links.count())]
    assert names[0] == "Homepage" and "Jellyfin" in names and "Jellyseerr" in names, names
    assert ":3001" in links.first.get_attribute("href")
    expect(page.locator("#pstat")).to_contain_text("ciuser")
    stage(f"success screen links: {names}")

    stage("Finish")
    page.get_by_role("button", name="Finish").click()
    expect(page.locator("h1")).to_have_text("Finished")
    shot(page, "finished")


def main():
    srv, url = start_server()
    stage("page ready")
    code = 1
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 1100})
        page.set_default_timeout(45000)             # no single click/fill/expect may hang longer than this
        page.on("pageerror", lambda e: print("PAGE JS ERROR:", e, flush=True))
        page.on("console", lambda m: print("console error:", m.text, flush=True) if m.type == "error" else None)
        try:
            flow(page, srv, url)
        except Exception as e:                       # any error or timeout: capture the screen + what the page says
            print(f"[e2e] FAIL: {type(e).__name__}: {str(e)[:700]}", flush=True)
            try:
                shot(page, "FAILURE")
                open(os.path.join(args.shots, "failure-page-text.txt"), "w").write(page.inner_text("body"))
                if page.locator("#log").count():
                    open(os.path.join(args.shots, "failure-install-log.txt"), "w").write(page.inner_text("#log"))
            except Exception as e2:
                print("could not capture the failure state:", e2, flush=True)
            srv.kill()
            browser.close()
            sys.exit(1)
        browser.close()
    try:
        code = srv.wait(timeout=30)
    except subprocess.TimeoutExpired:
        srv.kill(); print("[e2e] FAIL: server did not stop after Finish", flush=True); sys.exit(1)
    print(f"[e2e] server exited with {code}", flush=True)
    sys.exit(0 if code == 0 else 1)


if __name__ == "__main__":
    main()
