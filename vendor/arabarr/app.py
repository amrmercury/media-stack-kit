import os, re, json, time, urllib.request, urllib.parse, http.cookiejar, datetime, threading
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

E = os.environ.get
PROWLARR = E("PROWLARR_URL", "").rstrip("/")
PROWLARR_KEY = E("PROWLARR_API_KEY", "")
INDEXER_ID = E("ARABP2P_INDEXER_ID", "")
SONARR = E("SONARR_URL", "").rstrip("/")
SONARR_KEY = E("SONARR_API_KEY", "")
TMDB_KEY = E("TMDB_API_KEY", "")
PROXY_KEY = E("PROXY_API_KEY", "")
PORT = int(E("PORT", "5010"))
OVERRIDES_PATH = E("NAME_OVERRIDES_PATH", "/app/name_overrides.json")
ARABP2P_USER = E("ARABP2P_USERNAME", "")
ARABP2P_PASS = E("ARABP2P_PASSWORD", "")
ARABP2P_DUB_CAT = E("ARABP2P_DUB_CATEGORY", "100")
ARABP2P_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
ARABARR_BASE = E("ARABARR_BASE_URL", f"http://arabarr:{E('PORT', '5010')}")
XMLT = "application/rss+xml; charset=utf-8"
ET.register_namespace("torznab", "http://torznab.com/schemas/2015/feed")
ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

CAPS = b"""<?xml version="1.0" encoding="UTF-8"?>
<caps><server title="Arabarr"/><limits default="100" max="100"/>
<searching><search available="yes" supportedParams="q"/>
<tv-search available="yes" supportedParams="q,season,ep,tvdbid"/>
<movie-search available="no" supportedParams="q"/></searching>
<categories><category id="5000" name="TV"/></categories></caps>"""
EMPTY = b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Arabarr</title></channel></rss>'

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def fetch(url, headers=None, raw=False):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read() if raw else json.load(r)

_cache = {}
def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val

# ---------- Arabic text ----------
def norm(s):
    s = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u0640]", "", s or "")
    s = re.sub("[أإآٱ]", "ا", s)
    for a, b in (("ى", "ي"), ("ة", "ه"), ("ؤ", "و"), ("ئ", "ي")):
        s = s.replace(a, b)
    s = re.sub(r"[^\w\s]|_", " ", s.lower())
    return " " + " ".join(s.split()) + " "

def has_ar(s):
    return bool(re.search(r"[\u0600-\u06FF]", s or ""))

ORDINALS = [("الاول", "الاولي"), ("الثاني", "الثانيه"), ("الثالث", "الثالثه"), ("الرابع", "الرابعه"),
            ("الخامس", "الخامسه"), ("السادس", "السادسه"), ("السابع", "السابعه"), ("الثامن", "الثامنه"),
            ("التاسع", "التاسعه"), ("العاشر", "العاشره")]
ORD = {w: i for i, pair in enumerate(ORDINALS, 1) for w in pair}

def parse(t, single_season=False):
    n = norm(t)
    full = " كامل " in n
    season = None
    m = re.search(r"(?<![a-z])s(\d{1,2})(?!\d)", t, re.I)
    if m:
        season = int(m.group(1))
    if season is None:
        m = re.search(r"\[\s*(?:(\d{1,2})\s*م|م\s*(\d{1,2}))\s*\]", t)
        if m:
            season = int(m.group(1) or m.group(2))
    if season is None:
        m = re.search(r" (?:الجزء|الموسم) (\S+) ", n)
        if m:
            w = m.group(1)
            season = int(w) if w.isdigit() else ORD.get(w)
    eps = []
    m = (re.search(r"(?<![a-z])s\d{1,2}((?:\s*[-,]?\s*e\d{1,4})+)", t, re.I)
         or re.search(r"\[\s*(e\d{1,4}(?:\s*[-, ]\s*e\d{1,4})*)\s*\]", t, re.I))
    if m:
        eps = [int(x) for x in re.findall(r"e(\d{1,4})", m.group(1), re.I)]
        if len(eps) == 2 and "," not in m.group(1):
            eps = list(range(min(eps), max(eps) + 1))
    if not eps and not full:
        m = re.search(r" (?:ال)?حلقه (\d{1,4}) ", n) or re.search(r"\[\s*(\d{1,3})\s*\]", t)
        if m:
            eps = [int(m.group(1))]
    if full:
        eps = []
    if season is None and (eps or full):
        season = 1
    # Last resort: no season/episode/"complete" marker at all (e.g. just "<show> [480p]").
    # Normally that's too ambiguous to guess, but if the show only has one season, any
    # such pack can only be that season in full - unambiguous, so treat it as "كامل".
    if season is None and not eps and single_season:
        full = True
        season = 1
    if season is None or len(eps) > 100:
        return None
    return season, sorted(set(eps))

# Same policy as the sonarr-dub Release Profile / Custom Formats, enforced here
# because rename() below would otherwise strip these words before Sonarr ever
# sees the title, making that profile's rules unreachable for arabarr results.
DUB_MARKERS = ["بالفصحى", "مدبلج", "سبيستون"]  # scored terms, checked on raw title
DUB_MARKERS_RE = ["AWAAN", "SHAHID"]            # scored terms, English, case-insensitive

def dub_tag(orig):
    if "تونسي" in orig:
        return None  # Must Not Contain: Tunisian dialect
    if "مترجم" in orig and "مدبلج" not in orig:
        return None  # Must Not Contain: sub-only, no dub
    hits = [kw for kw in DUB_MARKERS if kw in orig]
    hits += [kw for kw in DUB_MARKERS_RE if re.search(kw, orig, re.I)]
    return " ".join(hits) if hits else "ARABIC"

def rename(orig, names, single_season=False):
    n = norm(orig)
    if " خلف الكواليس " in n:
        return None
    for ar, en in names:
        if ar.strip() and ar in n:
            tag = dub_tag(orig)
            if tag is None:
                return None
            p = parse(orig, single_season=single_season)
            if not p:
                return None
            season, eps = p
            se = "S%02d" % season + "".join("E%02d" % e for e in eps)
            res = re.search(r"(2160|1080|720|576|480)p", orig, re.I)
            codec = "H265" if re.search(r"[hx]\.?265|hevc", orig, re.I) else "H264" if re.search(r"[hx]\.?264", orig, re.I) else ""
            parts = [re.sub(r"[\[\]:/\\]", " ", en), se, res.group(0).lower() if res else "",
                     "BluRay" if re.search(r"blu-?ray", orig, re.I) else "WEB-DL",
                     "HDR" if re.search(r"\bHDR\b", orig) else "", codec, tag]
            return " ".join(" ".join(parts).split()) + "-ArabP2P"
    return None

# ---------- lookups ----------
def sonarr(path):
    return fetch(f"{SONARR}/api/v3/{path}", {"X-Api-Key": SONARR_KEY})

def tmdb(path, **q):
    q["api_key"] = TMDB_KEY
    return fetch(f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(q)}")

def find_series(tvdbid=None, q=None):
    key = re.sub(r"[^a-z0-9]", "", (q or "").lower())
    for attempt in range(2):
        if attempt:
            _cache.pop("series", None)
        for s in cached("series", 600, lambda: sonarr("series")):
            if tvdbid and str(s.get("tvdbId")) == str(tvdbid):
                return s
            if key and not tvdbid:
                titles = [s.get("title", "")] + [a.get("title", "") for a in s.get("alternateTitles") or []]
                if any(re.sub(r"[^a-z0-9]", "", x.lower()) == key for x in titles):
                    return s
    return None

def load_overrides():
    def load():
        try:
            with open(OVERRIDES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:
            log("overrides file error:", e)
            return {}
    return cached("overrides", 30, load)

def arabic_names(s):
    def load():
        # Manual overrides (name_overrides.json, keyed by tvdbId): use these when
        # TMDB's Arabic title/translation is missing or worded differently from
        # real release titles (e.g. TMDB gives "X الجزئين 1-2" but releases just say "X").
        manual = load_overrides().get(str(s["tvdbId"]), [])
        tid = s.get("tmdbId") or 0
        tmdb_cands = []
        if not tid:
            hits = tmdb(f"find/{s['tvdbId']}", external_source="tvdb_id").get("tv_results") or []
            tid = hits[0]["id"] if hits else 0
        if tid:
            d = tmdb(f"tv/{tid}", append_to_response="alternative_titles,translations")
            tmdb_cands = [d.get("original_name"), d.get("name")]
            tmdb_cands += [x.get("title") for x in (d.get("alternative_titles") or {}).get("results", [])]
            tmdb_cands += [(x.get("data") or {}).get("name") for x in (d.get("translations") or {}).get("translations", [])
                            if x.get("iso_639_1") == "ar"]

        def uniqify(cands):
            uniq = {}
            for c in cands:
                if has_ar(c) and norm(c).strip():
                    uniq.setdefault(norm(c), c)
            return sorted(uniq.values(), key=len, reverse=True)

        manual_names = uniqify(manual)
        seen = {norm(n) for n in manual_names}
        tmdb_names = [n for n in uniqify(tmdb_cands) if norm(n) not in seen]
        return manual_names + tmdb_names  # manual overrides take priority
    return cached(("names", s["tvdbId"]), 21600, load)

def prowlarr(q):
    qs = urllib.parse.urlencode({"apikey": PROWLARR_KEY, "t": "search", "q": q, "limit": 100})
    return ET.fromstring(fetch(f"{PROWLARR}/{INDEXER_ID}/api?{qs}", raw=True))

# ---------- ArabP2P direct (dubbed-anime category only) ----------
# Prowlarr's Cardigann engine won't honor a fixed category override for this site
# (every attempt to restrict it there got silently reset to "all categories" - confirmed
# via Prowlarr's own request log). So for the one category ArabP2P actually splits by dub
# status (100 = مسلسلات مدبلج / dubbed series), arabarr logs into the site itself and
# queries it directly, bypassing Prowlarr entirely for this lookup. Everything returned
# here is dubbed regardless of whether the title carries a مدبلج marker.
_ap2p_jar = http.cookiejar.CookieJar()
_ap2p_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_ap2p_jar))
_ap2p_last_login = 0
# ThreadingHTTPServer runs each request in its own thread, but _ap2p_jar/_ap2p_opener are
# shared mutable global state - concurrent Sonarr requests (e.g. S01 and S02 searched back
# to back) could otherwise race on login/cookies. Serialize all ArabP2P site access with
# one lock rather than debug intermittent corruption from interleaved requests.
_ap2p_lock = threading.Lock()

def ap2p_login():
    global _ap2p_last_login
    data = urllib.parse.urlencode({"uid": ARABP2P_USER, "pwd": ARABP2P_PASS}).encode()
    req = urllib.request.Request("https://www.arabp2p.net/index.php?page=login", data=data,
                                  headers={"User-Agent": ARABP2P_UA})
    with _ap2p_opener.open(req, timeout=30) as r:
        r.read()
    _ap2p_last_login = time.time()

def ap2p_dub_items(q):
    if not (ARABP2P_USER and ARABP2P_PASS):
        return []
    # Never let a transient site error (429, timeout, session hiccup) escape and crash the
    # whole feed - Sonarr's RSS parser treats one bad response as a total indexer failure
    # and temporarily disables it after repeated occurrences.
    try:
        with _ap2p_lock:
            if time.time() - _ap2p_last_login > 1800:
                ap2p_login()
            qs = urllib.parse.urlencode({"page": "torrents", "search": q, "category": ARABP2P_DUB_CAT, "active": "0"})
            req = urllib.request.Request(f"https://www.arabp2p.net/index.php?{qs}", headers={"User-Agent": ARABP2P_UA})
            with _ap2p_opener.open(req, timeout=30) as r:
                html = r.read().decode("utf-8", "replace")
            if "tor_link" not in html and 'name="pwd"' in html:
                ap2p_login()  # session expired, retry once
                with _ap2p_opener.open(req, timeout=30) as r:
                    html = r.read().decode("utf-8", "replace")
    except Exception as e:
        log("ArabP2P direct dub search error:", repr(e))
        return []

    items = []
    for chunk in re.split(r'(?=<div class="file-header">)', html):
        tm = re.search(r'<span class="tor_link">([^<]+)</span>', chunk)
        # magnet, when the site offers one for this torrent - self-contained, no proxy needed
        mm = re.search(r"magnet:\?xt=urn:btih:[^'\"<> ]+", chunk)
        # .torrent file link otherwise - needs our authenticated session, so proxy it via /dl
        dm = re.search(r'href="(download\.php\?id=\d+&amp;f=[^"]+)"', chunk)
        if not tm or not (mm or dm):
            continue
        title = tm.group(1).strip()
        if mm:
            link = mm.group(0).replace("&amp;", "&")
        else:
            rel = dm.group(1).replace("&amp;", "&")
            link = f"{ARABARR_BASE}/dl?apikey={urllib.parse.quote(PROXY_KEY)}&u={urllib.parse.quote(rel, safe='')}"
        # Sonarr's RSS parser requires every item to have a valid pubDate, or it throws
        # and treats the WHOLE feed as failed - repeated failures got Spacetoonarr
        # temporarily disabled by Sonarr's "unavailable due to failures" backoff.
        dtm = re.search(r"<span\s+title='(\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [AP]M)'", chunk)
        pubdate = None
        if dtm:
            try:
                dt = datetime.datetime.strptime(dtm.group(1), "%y-%m-%d %I:%M:%S %p")
                pubdate = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except ValueError:
                pubdate = None
        if not pubdate:
            pubdate = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
        it = ET.Element("item")
        ET.SubElement(it, "title").text = title
        ET.SubElement(it, "guid").text = link
        ET.SubElement(it, "link").text = link
        ET.SubElement(it, "pubDate").text = pubdate
        enc = ET.SubElement(it, "enclosure")
        enc.set("url", link)
        enc.set("type", "application/x-bittorrent")
        items.append(it)
    return items

def ap2p_download(rel):
    with _ap2p_lock:
        if time.time() - _ap2p_last_login > 1800:
            ap2p_login()
        req = urllib.request.Request(f"https://www.arabp2p.net/{rel}", headers={"User-Agent": ARABP2P_UA})
        with _ap2p_opener.open(req, timeout=60) as r:
            return r.read()

# ---------- torznab ----------
def feed(queries, names, keep_unmatched, label, single_season=False):
    root = chan = None
    seen, kept = set(), 0
    for q in queries:
        r = prowlarr(q)
        ch = r.find("channel")
        items = ch.findall("item")
        if root is None:
            root, chan = r, ch
            for it in items:
                ch.remove(it)
        if q.strip():
            try:
                items = items + ap2p_dub_items(q)
            except Exception as e:
                log("ArabP2P direct dub search error:", e)
        for it in items:
            key = it.findtext("guid") or it.findtext("link") or it.findtext("title")
            if key in seen:
                continue
            seen.add(key)
            orig = it.findtext("title") or ""
            new = rename(orig, names, single_season=single_season)
            if new:
                it.find("title").text = new
                chan.append(it)
                kept += 1
                log(f"[{label}] {orig}  =>  {new}")
            elif keep_unmatched:
                # Mangle the guid so this unmatched item can never collide with the
                # same underlying torrent's guid when a later *targeted* search finds
                # and correctly renames it - Sonarr caches releases by guid and keeps
                # whichever it saw first, so an unmangled leak here would permanently
                # shadow the correct renamed title. Keeping the item (rather than
                # dropping it) matters too: Sonarr's indexer add/save flow runs a test
                # query and rejects the indexer if it comes back empty.
                g = it.find("guid")
                if g is not None and g.text:
                    g.text = "rss-unmatched:" + g.text
                chan.append(it)
            else:
                log(f"[{label}] skip: {orig}")
    log(f"[{label}] {kept} release(s) renamed")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

def rss_map():
    def load():
        m = []
        for s in cached("series", 600, lambda: sonarr("series")):
            if (s.get("originalLanguage") or {}).get("name") != "Arabic":
                continue
            try:
                m += [(norm(n), s["title"]) for n in arabic_names(s)]
            except Exception as e:
                log("TMDB error for", s.get("title"), e)
        return sorted(m, key=lambda x: -len(x[0]))
    return cached("rssmap", 3600, load)

def search(p):
    if int(p.get("offset") or 0) > 0:
        return EMPTY
    tvdbid, q = p.get("tvdbid"), (p.get("q") or "").strip()
    if tvdbid or q:
        s = find_series(tvdbid=tvdbid) if tvdbid else find_series(q=q)
        if not s:
            log(f"search {tvdbid or q}: not in Sonarr")
            return EMPTY
        names = arabic_names(s)
        if not names:
            log(f"search {s['title']}: no Arabic title on TMDB")
            return EMPTY
        real_seasons = [se for se in (s.get("seasons") or []) if se.get("seasonNumber", 0) > 0]
        single_season = len(real_seasons) == 1
        return feed(names, [(norm(n), s["title"]) for n in names], False, s["title"], single_season=single_season)
    # keep_unmatched=True: a blank/no-tvdbid/no-q request is Sonarr's preliminary
    # probe before it issues the real per-series query, and also what Sonarr's own
    # indexer add/save flow tests with - it rejects an indexer whose test query comes
    # back empty. feed() mangles unmatched items' guids so they can't collide with the
    # same torrent's guid once a later targeted search finds and correctly renames it.
    return feed([""], rss_map(), True, "RSS")

def debug(p):
    lines = []
    if p.get("tvdbid"):
        s = find_series(tvdbid=p["tvdbid"])
        lines.append(f"Sonarr: {s['title'] if s else 'NOT FOUND'}")
        if s:
            lines.append("TMDB Arabic titles: " + (" | ".join(arabic_names(s)) or "NONE"))
    if p.get("name"):
        names = [(norm(p["name"]), p.get("title") or "English Title")]
        for it in prowlarr(p["name"]).find("channel").findall("item"):
            o = it.findtext("title") or ""
            lines.append(f"{o}\n    => {rename(o, names) or 'SKIP'}")
    return "\n".join(lines) or "Use /debug?name=ARABIC&title=ENGLISH or /debug?tvdbid=ID"

def err(code, desc):
    return f'<?xml version="1.0" encoding="UTF-8"?><error code="{code}" description="{desc}"/>'.encode()

class Handler(BaseHTTPRequestHandler):
    def reply(self, body, ctype=XMLT, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path.rstrip("/")
        try:
            if path == "/status":
                try:
                    n = len(cached("series", 600, lambda: sonarr("series")))
                    ar = len([1 for x in rss_map()])
                except Exception:
                    n, ar = "?", "?"
                html = ("<html><head><title>Arabarr</title>"
                        "<meta http-equiv=refresh content=30>"
                        "<style>body{font-family:sans-serif;background:#1a1a1a;color:#eee;padding:2em}"
                        "h1{color:#e8a03c}.ok{color:#4caf50}</style></head><body>"
                        "<h1>Arabarr</h1><p class=ok>&#9679; running</p>"
                        f"<p>Series in Sonarr: {n}</p>"
                        f"<p>Arabic title mappings loaded: {ar}</p>"
                        "<p>Tail the log for live matches: <code>docker logs -f arabarr</code></p>"
                        "</body></html>")
                return self.reply(html.encode(), "text/html; charset=utf-8")
            if path == "/debug":
                return self.reply(debug(p).encode(), "text/plain; charset=utf-8")
            if path == "/dl":
                if PROXY_KEY and p.get("apikey") != PROXY_KEY:
                    return self.reply(err(100, "Incorrect API key"))
                if not p.get("u"):
                    return self.reply(err(201, "Missing u param"))
                data = ap2p_download(p["u"])
                return self.reply(data, "application/x-bittorrent")
            if path != "/api":
                return self.reply(b"Arabarr is running", "text/plain")
            if PROXY_KEY and p.get("apikey") != PROXY_KEY:
                return self.reply(err(100, "Incorrect API key"))
            log("REQ", {k: v for k, v in p.items() if k != "apikey"})
            t = p.get("t")
            if t == "caps":
                return self.reply(CAPS)
            if t in ("search", "tvsearch"):
                return self.reply(search(p))
            return self.reply(err(202, "Not supported"))
        except Exception as e:
            log("ERROR:", repr(e))
            return self.reply(err(900, "Arabarr error, see docker logs"))

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    log(f"Arabarr on port {PORT}, ArabP2P indexer {INDEXER_ID}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
