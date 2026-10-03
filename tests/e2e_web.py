#!/usr/bin/env python3
"""End-to-end test of the browser installer, run on a clean CI machine (never on a real server).

Starts `./install.sh --web`, drives the page in headless Chromium exactly as a person would (including wrong input),
lets the REAL install run with fake credentials, and checks the success screen. Saves a screenshot of every screen.

  python3 tests/e2e_web.py --state-dir DIR --shots DIR
"""
import argparse, os, re, subprocess, sys, threading, time

from playwright.sync_api import sync_playwright, expect

ap = argparse.ArgumentParser()
ap.add_argument("--state-dir", required=True)
ap.add_argument("--shots", default="shots")
ap.add_argument("--install-timeout-min", type=int, default=30)
args = ap.parse_args()
os.makedirs(args.shots, exist_ok=True)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- start the installer page
srv = subprocess.Popen(["./install.sh", "--web", "--no-browser", "--state-dir", args.state_dir], cwd=ROOT,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
url, logf = None, open(os.path.join(args.shots, "server.log"), "w")
t0 = time.time()
while url is None and time.time() - t0 < 300:
    line = srv.stdout.readline()
    if not line:
        break
    logf.write(line); logf.flush()
    m = re.search(r"(http://127\.0\.0\.1:\d+/\?t=\S+)", line)
    url = m.group(1) if m else None
if not url:
    print("FAIL: the installer never printed its page link"); srv.kill(); sys.exit(1)
threading.Thread(target=lambda: [logf.write(l) or logf.flush() for l in srv.stdout], daemon=True).start()
print("page:", url.split("?")[0])

n = 0
def shot(page, name):
    global n
    n += 1
    page.screenshot(path=os.path.join(args.shots, f"{n:02d}-{name}.png"), full_page=True)

def fail(page, msg):
    shot(page, "FAILURE")
    try:
        open(os.path.join(args.shots, "failure-log.txt"), "w").write(page.inner_text("#log"))
    except Exception:
        pass
    print("FAIL:", msg); srv.kill(); sys.exit(1)

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1000, "height": 1100})
    page.on("pageerror", lambda e: print("PAGE JS ERROR:", e))
    page.goto(url)
    expect(page.locator("h1")).to_have_text("Set up your media server")
    assert "Media Stack Installer" in page.title()
    shot(page, "welcome")
    page.get_by_role("button", name="Start").click()

    # ---- login: wrong input first, then right; password must be visible by default
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".err")).to_contain_text("Username")
    shot(page, "login-errors")
    page.get_by_label("Username").fill("ciuser")
    page.get_by_label("Password").fill("short")
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".err")).to_contain_text("at least 8")
    page.get_by_label("Password").fill("Passw0rd!ci-test")
    assert page.get_by_label("Password").get_attribute("type") == "text", "password should be visible by default"
    page.get_by_role("button", name="Hide what I type").click()
    assert page.get_by_label("Password").get_attribute("type") == "password"
    page.get_by_role("button", name="Show what I type").click()
    assert page.get_by_label("Password").get_attribute("type") == "text"
    shot(page, "login")
    page.get_by_role("button", name="Next").click()

    # ---- library: must choose a folder; use the folder browser and create a new folder
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".err")).to_contain_text("library")
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

    # ---- debrid: need one key; add a second of another service
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".err")).to_contain_text("debrid key")
    page.get_by_placeholder("paste your key").first.fill("FAKE" + "R" * 48)
    page.get_by_role("button", name="+ Add another key").click()
    page.locator("select").nth(1).select_option("torbox")
    page.get_by_placeholder("paste your key").nth(1).fill("FAKETORBOXKEY0123456789")
    shot(page, "debrid")
    page.get_by_role("button", name="Next").click()

    # ---- ArabicSource: switching it on requires a key
    box = page.locator(".opt", has_text="ArabicSource")
    box.locator("input[type=checkbox]").check()
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".err")).to_contain_text("ArabicSource")
    box.locator("input[type=text]").fill("FAKEARABICSOURCEKEY")
    page.get_by_role("button", name="Next").click()

    # ---- Arabic series (Arabarr)
    box = page.locator(".opt", has_text="Set up Arabarr")
    box.locator("input[type=checkbox]").check()
    page.get_by_role("button", name="Next").click()
    expect(page.locator(".err")).to_contain_text("Arabic series")
    fields = box.locator("input[type=text]")
    fields.nth(0).fill("cifake"); fields.nth(1).fill("fakepass1"); fields.nth(2).fill("0123456789abcdef0123456789abcdef")
    shot(page, "arabic")
    page.get_by_role("button", name="Next").click()

    # ---- subtitles
    for name, vals in (("OpenSubtitles", ["fakeuser", "fakepass"]), ("Subsource", ["FAKESUBSOURCE"]), ("SubDL", ["FAKESUBDL"])):
        b = page.locator(".opt", has_text=name)
        b.locator("input[type=checkbox]").first.check()
        for i, v in enumerate(vals):
            b.locator("input[type=text]").nth(i).fill(v)
    shot(page, "subtitles")
    page.get_by_role("button", name="Next").click()

    # ---- cache: wait for the drive check; use the first suitable drive if there is one
    expect(page.get_by_text("Checking your drives")).to_have_count(0, timeout=120000)
    radios = page.locator("input[type=radio]")
    if radios.count() > 1:
        radios.first.check()
        page.locator("input[type=number]").fill("5")
    shot(page, "cache")
    page.get_by_role("button", name="Next").click()

    # ---- review, then install for real
    expect(page.locator("h1")).to_have_text("Review")
    expect(page.locator("table")).to_contain_text("ciuser")
    expect(page.locator("table")).to_contain_text("Real-Debrid, TorBox")
    shot(page, "review")
    page.get_by_role("button", name="Install").click()
    expect(page.locator("h1")).to_have_text("Installing…")
    time.sleep(20)
    shot(page, "installing")

    done = "h1:has-text('All done'), h1:has-text('Something went wrong')"
    page.wait_for_selector(done, timeout=args.install_timeout_min * 60 * 1000)
    if "Something went wrong" in page.inner_text("h1"):
        fail(page, "the install failed: " + page.inner_text("#pstat"))
    shot(page, "done")
    links = page.locator(".links a")
    names = [links.nth(i).inner_text().split("\n")[0] for i in range(links.count())]
    assert names[0] == "Homepage" and "Jellyfin" in names and "Jellyseerr" in names, names
    assert ":3001" in links.first.get_attribute("href")
    expect(page.locator("#pstat")).to_contain_text("ciuser")
    print("success screen links:", names)

    # ---- Finish stops the server
    page.get_by_role("button", name="Finish").click()
    expect(page.locator("h1")).to_have_text("Finished")
    shot(page, "finished")
    browser.close()

try:
    code = srv.wait(timeout=30)
except subprocess.TimeoutExpired:
    srv.kill(); print("FAIL: server did not stop after Finish"); sys.exit(1)
print("server exited with", code)
sys.exit(0 if code == 0 else 1)
