#!/usr/bin/env python3
"""YakTube - a tiny self-hosted YouTube portal.

One file, Python standard library only. Data lives in SQLite.
Subscriptions come from YouTube's public RSS feeds; playback uses the
privacy-enhanced embedded player. No Google account, no API key.
"""
import base64, csv, functools, html, io, json, math, os, random, re, sqlite3, struct, sys, threading, time, urllib.parse, urllib.request, zipfile, zlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("YAK_DB", os.path.join(HERE, "yaktube.db"))


def _version():
    """YakTube's version, worked out from this file: the day it was last updated (copied to the NAS) and a short code
    that changes with every change to it, e.g. "2026.11.12 · 3f9c2a1". Shown in Settings."""
    import hashlib
    with open(os.path.abspath(__file__), "rb") as f:
        code = hashlib.sha1(f.read()).hexdigest()[:7]
    return time.strftime("%Y.%m.%d", time.localtime(os.path.getmtime(os.path.abspath(__file__)))) + " · " + code


VERSION = _version()
HOST = os.environ.get("YAK_HOST", "0.0.0.0")
PORT = int(os.environ.get("YAK_PORT", "8080"))
REFRESH_MIN = int(os.environ.get("YAK_REFRESH_MIN", "30"))
DISCOVER_HOURS = float(os.environ.get("YAK_DISCOVER_HOURS", "6"))   # how often to look for new suggestions; 0 = never
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="
VID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CID_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
COLORS = ["#d9480f", "#1971c2", "#2f9e44", "#9c36b5", "#e8590c", "#0c8599", "#c2255c", "#5f3dc4"]
PROFILE_COLORS = COLORS + ["#f59f00", "#495057"]          # a profile can be any of these (channels keep COLORS)
AVATARS = "🐱 🐶 🦊 🐼 🐨 🐸 🦁 🐵 🦄 🐙 🐢 🐝 🚀 🍕 ⚽ 🎮 🎸 🎨 🌟 🌈 🌙 🔥 👻".split()  # a profile's picture
PAGE_SIZE = 48
e = html.escape

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS channels(id TEXT PRIMARY KEY, title TEXT NOT NULL, fetched_at INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS subs(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  channel_id TEXT NOT NULL REFERENCES channels(id),
  added_at INTEGER, PRIMARY KEY(profile_id, channel_id));
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, channel_id TEXT, channel_title TEXT, channel_url TEXT,
  title TEXT, published INTEGER DEFAULT 0, views INTEGER DEFAULT 0, short INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS videos_chan ON videos(channel_id, published DESC);
CREATE TABLE IF NOT EXISTS history(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL, watched_at INTEGER, times INTEGER DEFAULT 1,
  PRIMARY KEY(profile_id, video_id));
CREATE INDEX IF NOT EXISTS history_time ON history(profile_id, watched_at DESC);
CREATE TABLE IF NOT EXISTS later(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL, added_at INTEGER, PRIMARY KEY(profile_id, video_id));
CREATE TABLE IF NOT EXISTS hidden(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, ref TEXT NOT NULL, added_at INTEGER, PRIMARY KEY(profile_id, kind, ref));
CREATE TABLE IF NOT EXISTS related(src TEXT NOT NULL, video_id TEXT NOT NULL, pos INTEGER, PRIMARY KEY(src, video_id));
CREATE TABLE IF NOT EXISTS related_src(src TEXT PRIMARY KEY, fetched_at INTEGER);
CREATE TABLE IF NOT EXISTS shown(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL, first_at INTEGER, PRIMARY KEY(profile_id, video_id));
CREATE TABLE IF NOT EXISTS searches(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  q TEXT NOT NULL, at INTEGER, n INTEGER DEFAULT 1, auto INTEGER DEFAULT 0, PRIMARY KEY(profile_id, q));
CREATE TABLE IF NOT EXISTS impressions(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL, src TEXT, first_at INTEGER, last_at INTEGER, n INTEGER DEFAULT 1, PRIMARY KEY(profile_id, video_id));
CREATE TABLE IF NOT EXISTS ratings(
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL, val INTEGER NOT NULL, at INTEGER, PRIMARY KEY(profile_id, video_id));
CREATE TABLE IF NOT EXISTS outside(pos INTEGER PRIMARY KEY, video_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS scout(video_id TEXT PRIMARY KEY, score INTEGER, farm INTEGER, why TEXT, at INTEGER, deep INTEGER DEFAULT 0,
  kept INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS scout_chan(key TEXT PRIMARY KEY, name TEXT, kind TEXT, at INTEGER);
"""


def connect():
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=15000")
    return c


def init_db():
    c = connect()
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    if "feed_shorts" not in [r[1] for r in c.execute("PRAGMA table_info(profiles)")]:  # added Nov 2026
        c.execute("ALTER TABLE profiles ADD COLUMN feed_shorts INTEGER DEFAULT 1")       # Feed shows subs' Shorts
    if "pos" not in [r[1] for r in c.execute("PRAGMA table_info(history)")]:            # added Nov 2026
        c.execute("ALTER TABLE history ADD COLUMN pos INTEGER DEFAULT 0")                # seconds in, to resume from
    if "length" not in [r[1] for r in c.execute("PRAGMA table_info(videos)")]:          # added Nov 2026: seconds long,
        c.execute("ALTER TABLE videos ADD COLUMN length INTEGER DEFAULT 0")              # for the duration on pictures
    if "best" not in [r[1] for r in c.execute("PRAGMA table_info(history)")]:           # added Nov 2026: how much of it
        c.execute("ALTER TABLE history ADD COLUMN best INTEGER DEFAULT 0")               # was watched (furthest point)
        c.execute("ALTER TABLE history ADD COLUMN dur INTEGER DEFAULT 0")                # and how long it is
    if "avatar" not in [r[1] for r in c.execute("PRAGMA table_info(profiles)")]:        # added Nov 2026: a picture
        c.execute("ALTER TABLE profiles ADD COLUMN avatar TEXT DEFAULT ''")              # instead of the first letter
        c.execute("ALTER TABLE profiles ADD COLUMN pos INTEGER DEFAULT 0")               # and the order they're shown in
        c.execute("UPDATE profiles SET pos=id")
    for k, v in c.execute("SELECT k, v FROM settings WHERE k LIKE 'scout_%'"):        # Settings: Scout
        SCOUT_CFG[k[6:]] = v
    for k, v in c.execute("SELECT k, v FROM settings WHERE k IN ('gl','hl')"):        # Settings: region and language
        REGION[k] = v
    c.close()


# ---------------------------------------------------------------- youtube
# Region and language (Settings): which country's and language's Shorts, suggestions and search results YouTube gives
# us, and the language captions turn on in. One for the whole of YakTube, as the Shorts list and the "up next" lists
# it keeps are shared by every profile. Until it's changed, what YakTube always asked for: the US, in English.
REGIONS = (("CA", "Canada"), ("US", "United States"), ("MX", "Mexico"), ("GB", "United Kingdom"), ("AU", "Australia"),
           ("ES", "Spain"), ("FR", "France"))
LANGS = (("en", "English"), ("es", "Español"), ("fr", "Français"))
REGION = {"gl": "US", "hl": "en"}


def yt_cookie():
    """SOCS / CONSENT skip the EU consent wall; PREF is YouTube's own region and language choice."""
    return f"SOCS=CAI; CONSENT=YES+1; PREF=hl={REGION['hl']}&gl={REGION['gl']}"


def fetch(url, timeout=10):
    hl, gl = REGION["hl"], REGION["gl"]
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": f"{hl}-{gl},{hl};q=0.9", "Cookie": yt_cookie()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def video_id_from(s):
    s = s.strip()
    if VID_RE.match(s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})", s)
    return m.group(1) if m else None


def _ts(iso):
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "m": "http://search.yahoo.com/mrss/"}


def parse_feed(text):
    root = ET.fromstring(text)
    ch_title = root.findtext("a:title", "", NS)
    vids = []
    for en in root.findall("a:entry", NS):
        stats = en.find("m:group/m:community/m:statistics", NS)
        link = en.find("a:link", NS)
        vids.append({
            "id": en.findtext("yt:videoId", "", NS),
            "channel_id": en.findtext("yt:channelId", "", NS),
            "channel_title": en.findtext("a:author/a:name", ch_title, NS),
            "title": en.findtext("a:title", "", NS),
            "published": _ts(en.findtext("a:published", "", NS)),
            "views": int(stats.get("views", 0)) if stats is not None else 0,
            "short": 1 if link is not None and "/shorts/" in link.get("href", "") else 0,
        })
    return ch_title, vids


# YouTube pages embed their content as JSON (ytInitialData). Search results, channel
# /videos pages and watch-page "up next" lists all come from here.
YTD_RE = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.S)
UNITS = {"s": 1, "second": 1, "min": 60, "minute": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400,
         "w": 604800, "week": 604800, "mo": 2592000, "month": 2592000, "y": 31536000, "year": 31536000}


def yt_data(url):
    page = fetch(url)
    m = YTD_RE.search(page)
    return page, (json.loads(m.group(1)) if m else {})


def walk(o, key):
    """Yield every value stored under `key` anywhere in a JSON tree, in document order."""
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                yield v
            elif isinstance(v, (dict, list)):
                yield from walk(v, key)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v, key)


def rel_ts(text, now=None):
    """'2d ago' / '3 weeks ago' -> approximate unix time (0 if unknown)."""
    m = re.search(r"(\d+)\s*(mo|y|w|d|h|min|s|second|minute|hour|day|week|month|year)s?\b", text or "")
    return int(now or time.time()) - int(m.group(1)) * UNITS[m.group(2)] if m else 0


def num(text):
    """'7.4M' / '3,582,218 views' -> int."""
    m = re.search(r"(\d[\d.,]*)\s*([KMB])?", text or "")
    if not m:
        return 0
    return int(float(m.group(1).replace(",", "")) * {"K": 1e3, "M": 1e6, "B": 1e9}.get(m.group(2), 1))


LEN_RE = re.compile(r"\d{1,2}(?::\d{2}){1,2}")          # a video's length as YouTube writes it: 4:05, 1:02:33


def secs(text):
    """'1:02:33' -> 3753 seconds (0 if it isn't a length)."""
    if not LEN_RE.fullmatch(text or ""):
        return 0
    n = 0
    for part in text.split(":"):
        n = n * 60 + int(part)
    return n


def parse_items(d):
    """Pull plain video dicts out of ytInitialData (both old and new layouts). Shorts are skipped."""
    out, seen = [], set()

    def add(vid, title, cid, ctitle, when, views_, length=""):
        if vid and VID_RE.match(vid) and vid not in seen:
            seen.add(vid)
            out.append({"id": vid, "title": title, "channel_id": cid if cid and CID_RE.fullmatch(cid) else None,
                        "channel_title": ctitle, "published": rel_ts(when), "views": num(views_), "short": 0,
                        "length": secs(length)})
    for r in walk(d, "videoRenderer"):               # search results
        by = ((r.get("longBylineText") or r.get("ownerText") or {}).get("runs") or [{}])[0]
        add(r.get("videoId"), "".join(x.get("text", "") for x in r.get("title", {}).get("runs", [])),
            by.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId"), by.get("text", ""),
            r.get("publishedTimeText", {}).get("simpleText", ""), r.get("viewCountText", {}).get("simpleText", ""),
            r.get("lengthText", {}).get("simpleText", ""))
    for lk in walk(d, "lockupViewModel"):            # channel pages, up-next lists
        if lk.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        md = lk.get("metadata", {}).get("lockupMetadataViewModel", {})
        rows = [[p.get("text", {}).get("content", "") for p in row.get("metadataParts", [])]
                for row in md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])]
        parts = [p for row in rows for p in row]
        when = next((p for p in parts if "ago" in p), "")
        vws = next((p for p in parts if re.fullmatch(r"[\d.,]+\s*[KMB]?( views)?", p)), "")
        ctitle = rows[0][0] if len(rows) > 1 and rows[0] else ""
        add(lk.get("contentId"), md.get("title", {}).get("content", ""), next(walk(lk, "browseId"), None),
            ctitle, when, vws, next((t for t in walk(lk, "text") if isinstance(t, str) and LEN_RE.fullmatch(t)), ""))
    for i, v in enumerate(out):                      # keep page order for equal "2d ago" dates
        if v["published"]:
            v["published"] -= i
    return out


def store_videos(c, vids, exact=False):
    """Upsert videos. Approximate dates ("2d ago") never overwrite a date we already have."""
    rows = [dict({"channel_id": None, "channel_title": "", "title": "", "published": 0, "views": 0, "short": 0, "length": 0},
                 **v, exact=int(exact)) for v in vids if VID_RE.match(v.get("id", ""))]
    c.execute("BEGIN")
    try:
        c.executemany(
            "INSERT INTO videos(id,channel_id,channel_title,title,published,views,short,length) "
            "VALUES(:id,:channel_id,:channel_title,:title,:published,:views,:short,:length) ON CONFLICT(id) DO UPDATE SET "
            "length=CASE WHEN excluded.length>0 THEN excluded.length ELSE videos.length END, "
            "channel_id=COALESCE(excluded.channel_id, videos.channel_id), "
            "channel_title=COALESCE(NULLIF(excluded.channel_title,''), videos.channel_title), "
            "title=COALESCE(NULLIF(excluded.title,''), videos.title), "
            "published=CASE WHEN :exact OR videos.published=0 THEN excluded.published ELSE videos.published END, "
            "views=MAX(videos.views, excluded.views), short=MAX(videos.short, excluded.short)", rows)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


CHANNEL_AT = {}   # channel id -> when its channel page last read YouTube (so browsing doesn't refetch every click)


def channel_videos(cid):
    """A channel's normal /videos page: its title and newest ~30 videos (approximate dates)."""
    page, d = yt_data(f"https://www.youtube.com/channel/{cid}/videos")
    m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    title = html.unescape(m.group(1)) if m else ""
    if not d:
        raise ValueError("Couldn't load that channel's videos.")
    return title, [dict(v, channel_id=cid, channel_title=title) for v in parse_items(d)]


def refresh_channel(c, cid, deep=False):
    """RSS first (exact dates); if YouTube's RSS is down, read the channel's /videos page instead.
    deep=True (the channel page) reads the /videos page as well: RSS only lists the newest 15."""
    try:
        title, vids = parse_feed(fetch(FEED + cid))
        exact = True
    except Exception:
        title, vids = channel_videos(cid)
        exact = deep = False
    c.execute("INSERT INTO channels(id,title,fetched_at) VALUES(?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET title=excluded.title, fetched_at=excluded.fetched_at",
              (cid, title or cid, int(time.time())))
    store_videos(c, vids, exact)
    if deep:
        try:
            store_videos(c, channel_videos(cid)[1])
        except Exception as ex:
            print(f"[channel] {cid}: {ex}")
    return title


def post_json(url, body, timeout=10):
    req = urllib.request.Request(url, json.dumps(body).encode(), {
        "User-Agent": UA, "Content-Type": "application/json", "Cookie": yt_cookie()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


OUTSIDE = []      # Shorts YouTube shows a visitor who isn't logged in, newest batch first (kept in the outside table)
OUTSIDE_AT = [0]  # when we last asked
OUTSIDE_LOCK = threading.Lock()   # one batch at a time (the hourly refresh and a player asking for more)


def load_outside():
    """The Shorts list as it was before a restart."""
    c = connect()
    try:
        OUTSIDE[:] = [r[0] for r in c.execute("SELECT video_id FROM outside ORDER BY pos")]
    finally:
        c.close()


def refresh_outside(max_age=3600, pages=5, keep=600):
    """Get a new batch of Shorts, unless the last one is less than max_age seconds old. About 40 a time; the newest
    600 are kept (an hour of Shorts is roughly 60-120)."""
    with OUTSIDE_LOCK:
        if OUTSIDE_AT[0] > time.time() - max_age:
            return
        OUTSIDE_AT[0] = time.time()
        fetch_outside(pages, keep)


def fetch_outside(pages, keep):
    """Open youtube.com/shorts/ like a visitor with no account, then ask for the Shorts that follow
    (8 per request, as YouTube's own page does). Saved as Shorts, so they never steer Home / New to you."""
    page, d = yt_data("https://www.youtube.com/shorts/")
    seed = next(walk(d, "reelWatchEndpoint"), {}).get("videoId", "")
    ver = (re.findall(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page) or ["2.20261105.05.01"])[0]
    if not VID_RE.match(seed):
        return
    ctx = {"client": {"clientName": "WEB", "clientVersion": ver, "hl": REGION["hl"], "gl": REGION["gl"]}}
    body, ids = {"sequenceParams": base64.urlsafe_b64encode(b"\n\x0b" + seed.encode()).decode()}, [seed]  # field 1 = start
    for _ in range(pages):
        r = post_json("https://www.youtube.com/youtubei/v1/reel/reel_watch_sequence?prettyPrint=false", dict(body, context=ctx))
        ids += [e.get("command", {}).get("reelWatchEndpoint", {}).get("videoId", "") for e in r.get("entries", [])]
        token = r.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
        if not token:
            break
        body = {"continuation": token}
    ids = list(dict.fromkeys(v for v in ids if VID_RE.match(v)))
    c = connect()
    try:
        known = {r[0] for r in c.execute(f"SELECT id FROM videos WHERE title!='' AND id IN ({','.join('?' * len(ids))})", ids)}

        def info(v):  # title and channel; YouTube refuses this for Shorts that can't be embedded, which drops them too
            try:
                o = json.loads(fetch("https://www.youtube.com/oembed?format=json&url="
                                     + urllib.parse.quote("https://www.youtube.com/shorts/" + v), timeout=5))
                return {"id": v, "title": o.get("title", ""), "channel_title": o.get("author_name", ""), "published": int(time.time())}
            except Exception:
                return None
        with ThreadPoolExecutor(4) as pool:
            got = [r for r in pool.map(info, [v for v in ids if v not in known]) if r]
        store_videos(c, [dict(r, short=1) for r in got] + [{"id": v, "short": 1} for v in known])
        ok = known | {r["id"] for r in got}
        new = [v for v in ids if v in ok]
        OUTSIDE[:] = (new + [v for v in OUTSIDE if v not in ok])[:keep]
        scout_outside(c)                         # content-farm ones come out before anyone sees them
        c.execute("DELETE FROM outside")
        c.executemany("INSERT INTO outside(pos, video_id) VALUES(?,?)", enumerate(OUTSIDE))
    finally:
        c.close()


# ---------------------------------------------------------------- scout
# Scout keeps content-farm Shorts out of the Shorts list: the faceless channels that mass-produce Shorts from one
# formula (an "In this world, everyone..." script, the same synthetic voice, a pasted description) to farm views and
# ad money. Being made with AI isn't the problem, and on its own it never gets a Short dropped: plenty of people make
# clever, funny things with AI. Scout reads each Short's title, description and auto-captions and adds up the signs of
# a farm; nothing is downloaded and nothing needs installing. The weights are a starting guess: `python3 app.py scout <link or id>...` shows what
# Scout makes of any Short and why, and the log says what was dropped. It only ever judges Shorts from outside your
# subscriptions, and when YouTube won't answer it lets the Short through.
SCOUT = os.environ.get("YAK_SCOUT", "1") != "0"                 # the title/caption check's default (Settings can change it)
SCOUT_CFG = {"text": "1" if SCOUT else "0",                     # Settings > YakScout, saved as scout_<k> in settings:
             "on": "0", "addr": "", "port": "8095",             #   the YakScout plugin: on/off, where it is
             "seen": "0",                                       #   and when it last answered
             "min": "", "days": "60"}                           # Advanced: drop at (blank: SCOUT_MIN), remember for
SCOUT_MIN = int(os.environ.get("YAK_SCOUT_MIN", "50"))          # score (0-100) at which a Short counts as a farm's
SCOUT_PER_RUN = 80                                              # Shorts judged per batch (the rest wait for the next)


def scout_min():
    """The score that drops a Short: Settings > YakScout > Advanced, or YAK_SCOUT_MIN."""
    v = SCOUT_CFG.get("min") or ""
    return int(v) if v.isdigit() else SCOUT_MIN

_I = re.I
SCOUT_AI_TOOLS = re.compile(                                    # made with AI: farms often say so, but so do artists,
    r"#(?:ai|ai(?:art|video|videos|story|stories|generated|animation|cat|cats|dog|dogs|animals?|slop|voice|images?)"
    r"|sora|veo3?|midjourney|kling|runwayml|aigenerated|aivideo)\b"       # so it's only ever a small clue
    r"|\bai[- ](?:generated|made|created|animation|story|video)\b"
    r"|\b(?:made|created|generated|produced|animated) (?:with|using|by) (?:ai|sora|veo ?3?|midjourney|chatgpt|runway|kling)\b", _I)
SCOUT_TITLE = [re.compile(p, _I) for p in (                     # clickbait the farms stamp on everything (weak on its own)
    r"\bpov\b", r"\bpart \d+\b", r"\bwould you survive\b", r"\bwhat if\b", r"\bwait (?:till|until|for) the end\b")]
SCOUT_TAGS = re.compile(r"#(?:storytime|whatif|pov|scarystories|animation)\b", _I)
SCOUT_HOOKS = [re.compile(p, _I) for p in (                     # how the script opens (first ~20 words)
    r"\b(?:in a|in this|imagine a) world where\b",
    r"\bin this (?:world|town|city|school|village|kingdom|society|country),? (?:everyone|every|nobody|no one|if|when|people|you)\b",
    r"\b(?:every|everyone|nobody|no one) (?:is born with|has a|must|can only)\b",
    r"\b(?:if you|when you) (?:break|touch|say|look at|reach age)\b",
    r"\bthere is (?:only one|a strict) rule\b")]
SCOUT_TROPE = re.compile(r"\bit wasn'?t just\b.{3,60}\bit was\b|\blittle did (?:he|she|they|i|we) know\b"
                         r"|\bnobody (?:understood|knew|expected) (?:why|what)\b|\bthe reason behind it was\b", _I)
SCOUT_BOILER = [re.compile(p, _I) for p in (                    # description boilerplate pasted on every upload
    r"\bwelcome to our [\w &]{2,25} channel\b", r"\beducational (?:&|and) entertainment (?:notice|purposes)\b")]
SCOUT_SLOP = re.compile(r"\b(forbidden|outcast|hierarchy|pathetic|shivered|whispered|unbeknownst|destiny|awakened|"
                        r"level zero|system|rank)\b", _I)
SCOUT_DANGLING = {"and", "because", "until", "but", "so", "when", "then", "which", "as", "while", "that's", "to", "the", "a"}


def scout_score(title="", desc="", transcript="", seconds=0):
    """(score 0-100, reasons) from what a Short says about itself. Pure: no network, so it is easy to test and tune."""
    hits = []

    def add(n, why):
        hits.append((n, why))
    head = f"{title}\n{desc}"
    if SCOUT_AI_TOOLS.search(head):
        add(10, "made with AI")
    n = sum(1 for r in SCOUT_BOILER if r.search(desc))
    if n:
        add(30 * n, "farm description")
    n = sum(1 for r in SCOUT_TITLE if r.search(title))
    if n:
        add(min(20, 8 * n), "clickbait title")
    n = len(set(t.lower() for t in SCOUT_TAGS.findall(head)))
    if n:
        add(min(15, 5 * n), "farm hashtags")
    words = transcript.split()
    if len(words) >= 25:                                         # a real script, not just a few words of music
        low = " ".join(words).lower()
        opening = " ".join(words[:20]).lower()
        if any(r.search(opening) for r in SCOUT_HOOKS):
            add(30, "world-rule opening")
        if SCOUT_TROPE.search(low):
            add(15, "'it wasn't just X, it was Y'")
        slop = {x.lower() for x in SCOUT_SLOP.findall(low)}
        if len(slop) >= 2:
            add(min(24, 8 * len(slop)), "melodrama words: " + ", ".join(sorted(slop)))
        if len(words) >= 60 and words[-1].lower().strip(".,!?…") in SCOUT_DANGLING:
            add(10, "ends mid-sentence (loop)")
        pace = len(words) / seconds if seconds else 0
        if (seconds >= 60 and pace >= 4.0) or (seconds >= 90 and pace >= 3.4):   # people rarely keep this up for minutes
            add(20, "non-stop fast narration")
    return min(100, sum(n for n, _ in hits)), [w for _, w in hits]


def scout_text(xml):
    """(words, seconds) from YouTube's auto-caption file."""
    root = ET.fromstring(xml)
    out, end = [], 0
    for p in root.iter("p"):
        out.append("".join(p.itertext()))
        end = max(end, (int(p.get("t", 0)) + int(p.get("d", 0))) // 1000)
    return " ".join(re.sub(r"\[[^\]]*\]", " ", " ".join(out)).split()), end


def scout_check(vid):
    """Look one Short up (YouTube's app interface, no account) and judge it. None if YouTube has nothing on it;
    raises if it can't be reached."""
    r = post_json("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", {"videoId": vid, "context": {"client": {
        "clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 34, "hl": REGION["hl"], "gl": REGION["gl"]}}}, 6)
    d = r.get("videoDetails")
    if not d:
        return None
    text, secs_ = "", int(d.get("lengthSeconds") or 0)
    tracks = r.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    track = next((t for t in tracks if t.get("languageCode", "")[:2] == REGION["hl"] and t.get("kind") == "asr"), None)
    if track:
        try:
            text, _ = scout_text(fetch(track["baseUrl"] + "&fmt=json3", timeout=8))
        except Exception:
            pass                                                 # no captions: judge it on what it says about itself
    score, why = scout_score(d.get("title", ""), d.get("shortDescription", ""), text, secs_)
    return {"score": score, "farm": int(score >= scout_min()), "why": why}


def scout_found(c):
    """Channels Scout has mostly judged to be farms (3 or more Shorts seen, over half of them): {name: Shorts dropped}."""
    return {r[0]: r[1] for r in c.execute(
        "SELECT v.channel_title, SUM(s.farm) FROM scout s JOIN videos v ON v.id=s.video_id WHERE v.channel_title!='' "
        "GROUP BY v.channel_title HAVING COUNT(*)>=3 AND SUM(s.farm)*2>COUNT(*)")}


def scout_lists(c):
    """The channels you've set in Settings > YakScout, by kind: farm (always drop), keep (never drop) and ok (one Scout
    found that you took off the farm list). {kind: {lower-case name: name}}."""
    out = {"farm": {}, "keep": {}, "ok": {}}
    for key, name, kind in c.execute("SELECT key, name, kind FROM scout_chan ORDER BY at DESC"):
        out.setdefault(kind, {})[key] = name
    return out


def scout_channels(c):
    """Farm channels (lower-case names): the ones Scout found, less the ones you took off, plus the ones you added.
    Their other Shorts are dropped without being looked up."""
    ls = scout_lists(c)
    return ({n.lower() for n in scout_found(c)} - set(ls["ok"]) - set(ls["keep"])) | set(ls["farm"])


def scout_never(c, vid):
    """Is this Short from a channel on your Never drop list?"""
    r = c.execute("SELECT 1 FROM videos v JOIN scout_chan k ON k.key=lower(v.channel_title) AND k.kind='keep' "
                  "WHERE v.id=?", (vid,)).fetchone()
    return bool(r)


def scout_outside(c):
    """Judge the Shorts in OUTSIDE that Scout hasn't seen yet and take the content-farm ones out of the list."""
    if not OUTSIDE:
        return
    marks = ",".join("?" * len(OUTSIDE))
    known = {r[0]: r[1] for r in c.execute(f"SELECT video_id, farm FROM scout WHERE video_id IN ({marks})", OUTSIDE)}
    todo = [v for v in OUTSIDE if v not in known][:SCOUT_PER_RUN] if SCOUT_CFG["text"] == "1" else []
    if todo:
        names = {r[0]: r[1] for r in c.execute(
            f"SELECT id, channel_title FROM videos WHERE id IN ({','.join('?' * len(todo))})", todo)}
        farms, keep = scout_channels(c), set(scout_lists(c)["keep"])

        def one(v):
            name = (names.get(v) or "").lower()
            if name in keep:
                return v, {"score": 0, "farm": 0, "why": ["never drop"]}
            if name in farms:
                return v, {"score": 100, "farm": 1, "why": ["a farm channel"]}
            try:
                return v, scout_check(v) or {"score": 0, "farm": 0, "why": ["no data"]}
            except Exception:
                return v, None                                   # couldn't ask: let it through, ask again next batch
        with ThreadPoolExecutor(8) as pool:
            got = [r for r in pool.map(one, todo) if r[1]]
        now = int(time.time())
        for i, (v, r) in enumerate(got):
            old = c.execute("SELECT score, why, kept FROM scout WHERE video_id=?", (v,)).fetchone()
            if old:     # the YakScout plugin got to it while this check ran: add to its verdict, don't replace it
                total = min(100, old["score"] + r["score"])
                why = [x for x in r["why"] if x != "no data"] + [x for x in (old["why"] or "").split("; ") if x]
                got[i] = (v, {"score": total, "farm": int(total >= scout_min() and not old["kept"]
                                                          and "never drop" not in why), "why": why})
                c.execute("UPDATE scout SET score=?, farm=?, why=? WHERE video_id=?",
                          (total, got[i][1]["farm"], "; ".join(why), v))
            else:
                c.execute("INSERT INTO scout(video_id,score,farm,why,at) VALUES(?,?,?,?,?)",
                          (v, r["score"], r["farm"], "; ".join(r["why"]), now))
        known.update({v: r["farm"] for v, r in got})
        for v, r in got:
            if r["farm"]:
                print(f"[scout] dropped {v} ({r['score']}): {names.get(v) or '?'} - {'; '.join(r['why'])}")
    drop = {v for v, farm in known.items() if farm}
    OUTSIDE[:] = [v for v in OUTSIDE if v not in drop]


def scout_queue(c, n=10):
    """Shorts in the list that scout.py hasn't looked at yet, newest first: [{id, title, channel}]."""
    ids = OUTSIDE[:]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    done = {r[0] for r in c.execute(f"SELECT video_id FROM scout WHERE (deep=1 OR kept=1) AND video_id IN ({marks})", ids)}
    todo = [v for v in ids if v not in done][:n]
    info = {r["id"]: r for r in c.execute(
        f"SELECT id, title, channel_title FROM videos WHERE id IN ({','.join('?' * len(todo))})", todo)} if todo else {}
    return [{"id": v, "title": info[v]["title"] if v in info else "", "channel": info[v]["channel_title"] if v in info else ""}
            for v in todo]


def scout_verdict(c, vid, score, why):
    """The YakScout plugin's verdict on the video itself. Adds to what the title/caption check already found; if the
    total reaches the drop score the Short leaves the list at once (never one you brought back, or from a channel on
    your Never drop list). Returns whether it was dropped."""
    old = c.execute("SELECT score, why, kept FROM scout WHERE video_id=?", (vid,)).fetchone()
    total = min(100, (old["score"] if old else 0) + score)
    reasons = [x for x in ((old["why"] if old else "") or "").split("; ") if x and x != "no data"] + why
    kept = old["kept"] if old else 0
    farm = int(total >= scout_min() and not kept and not scout_never(c, vid))
    c.execute("INSERT OR REPLACE INTO scout(video_id,score,farm,why,at,deep,kept) VALUES(?,?,?,?,?,1,?)",
              (vid, total, farm, "; ".join(reasons), int(time.time()), kept))
    if farm:
        OUTSIDE[:] = [v for v in OUTSIDE if v != vid]
        c.execute("DELETE FROM outside WHERE video_id=?", (vid,))
        print(f"[scout] dropped {vid} ({total}) after scanning it - {'; '.join(reasons)}")
    return bool(farm)


def scout_set_channel(c, kind, name, add=True):
    """Settings > YakScout: add a channel to (or take it off) the Farm channels or Never drop list. Adding a farm channel
    drops its Shorts from the list now; adding one to Never drop brings its dropped Shorts back. Taking a channel
    Scout found off the farm list stops it being treated as one. Returns False if the name is no good."""
    name = " ".join(name.split())[:100]
    key = name.lower()
    if not key or kind not in ("farm", "keep"):
        return False
    if not add:
        c.execute("DELETE FROM scout_chan WHERE key=? AND kind=?", (key, kind))
        if kind == "farm" and key in {n.lower() for n in scout_found(c)}:
            c.execute("INSERT OR REPLACE INTO scout_chan(key, name, kind, at) VALUES(?,?,?,?)", (key, name, "ok", int(time.time())))
        return True
    c.execute("INSERT OR REPLACE INTO scout_chan(key, name, kind, at) VALUES(?,?,?,?)", (key, name, kind, int(time.time())))
    ids = [r[0] for r in c.execute("SELECT id FROM videos WHERE lower(channel_title)=?", (key,))]
    if kind == "keep":
        for vid in ids:
            if c.execute("SELECT 1 FROM scout WHERE video_id=? AND farm=1", (vid,)).fetchone():
                scout_keep(c, vid)
    else:
        now = int(time.time())
        for vid in ids:
            if vid in OUTSIDE:
                c.execute("INSERT INTO scout(video_id,score,farm,why,at) VALUES(?,100,1,'a farm channel',?) ON CONFLICT(video_id) "
                          "DO UPDATE SET farm=1, kept=0, why='a farm channel'", (vid, now))
        drop = set(ids)
        if drop & set(OUTSIDE):
            OUTSIDE[:] = [v for v in OUTSIDE if v not in drop]
            c.execute("DELETE FROM outside")
            c.executemany("INSERT INTO outside(pos, video_id) VALUES(?,?)", enumerate(OUTSIDE))
    return True


def scout_keep(c, vid):
    """Bring back: a dropped Short isn't a farm's after all. It goes back at the front of the Shorts list and is never
    dropped again."""
    if not c.execute("UPDATE scout SET farm=0, kept=1 WHERE video_id=?", (vid,)).rowcount:
        return False
    if vid not in OUTSIDE:
        OUTSIDE.insert(0, vid)
        c.execute("DELETE FROM outside")
        c.executemany("INSERT INTO outside(pos, video_id) VALUES(?,?)", enumerate(OUTSIDE))
    return True


# The YakScout plugin (Settings > YakScout): `scout.py` running on this NAS (Docker) or another computer on the network; it listens to the start of each Short
# (it hears the synthetic voices farms reuse). YakTube connects to it at the address set in Settings, asks how it is
# (GET /status) and hands it Shorts one at a time (POST /look); what it says is added to the title/caption check's
# score. When it's off, asleep or can't be reached, YakTube carries on without it and tries again a minute later.
SCOUT_LINK = {"state": "", "error": "", "voices": [], "version": "", "up": 0, "lists": [],   # state: "" (not tried yet), ok, down, off
              "secs": 0.0, "last": 0}                             # seconds a Short (averaged), when it last scanned one
SCOUT_FAILS = {}                                                # Shorts it couldn't scan: given up on after 2 tries
SCOUT_WAKE = threading.Event()                                  # Settings changed: try now, not in a minute
SCOUT_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")


def scout_call(path, body=None, timeout=10):
    req = urllib.request.Request(f"http://{SCOUT_CFG['addr']}:{SCOUT_CFG['port']}{path}",
                                 json.dumps(body).encode() if body is not None else None, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def scout_save(c, k, v):
    SCOUT_CFG[k] = v
    c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", ("scout_" + k, v))


def scout_link_once():
    """One step of talking to the YakScout plugin. Returns how many seconds to wait before the next."""
    if SCOUT_CFG["on"] != "1" or not SCOUT_CFG["addr"]:
        SCOUT_LINK.update(state="off", error="")
        return 30
    try:
        st = scout_call("/status", timeout=5)
    except Exception as ex:
        SCOUT_LINK.update(state="down", error=str(ex)[:120])
        return 60
    SCOUT_LINK.update(state="ok", error="", voices=[v for v in st.get("voices", []) if isinstance(v, dict)][:50],
                      version=str(st.get("scout", ""))[:20], up=st.get("up") if isinstance(st.get("up"), int) else 0,
                      lists=[l for l in st.get("lists", []) if isinstance(l, dict)][:10] if isinstance(st.get("lists"), list) else [])
    c = connect()
    try:
        now = int(time.time())
        if now - int(SCOUT_CFG["seen"] or 0) >= 60:
            scout_save(c, "seen", str(now))
        q = scout_queue(c, 1)
        if not q:
            return 30
        vid = q[0]["id"]
        t = time.time()
        try:
            r = scout_call("/look", {"id": vid}, timeout=300)
        except urllib.error.HTTPError:                               # it answered, but couldn't scan this one
            SCOUT_FAILS[vid] = SCOUT_FAILS.get(vid, 0) + 1
            if SCOUT_FAILS[vid] >= 2:
                scout_verdict(c, vid, 0, ["couldn't scan"])
            return 5
        except Exception as ex:
            SCOUT_LINK.update(state="down", error=str(ex)[:120])
            return 60
        took = time.time() - t
        SCOUT_LINK.update(secs=took if not SCOUT_LINK["secs"] else 0.9 * SCOUT_LINK["secs"] + 0.1 * took, last=int(time.time()))
        score = r.get("score")
        if isinstance(score, int) and 0 <= score <= 100:
            scout_verdict(c, vid, score, [str(x)[:80] for x in r.get("why", [])[:8]])
        return 0
    finally:
        c.close()


def scout_link_loop():
    while True:
        try:
            wait = scout_link_once()
        except Exception as ex:
            print("[scout link]", repr(ex))
            wait = 60
        if wait:
            SCOUT_WAKE.wait(wait)
        SCOUT_WAKE.clear()


# Settings > YakScout > Submissions: the open pull requests on the shared voice list, each checked twice at home:
# the YakScout plugin listens to its example Shorts (same voice?), and the titles & captions check reads them (farm
# clues?). GitHub's servers can't do the listening (YouTube blocks them), so it happens here. Merging stays on GitHub.
SCOUT_LIST_REPO = os.environ.get("YAK_SCOUT_LIST_REPO", "glenndall/yakscout-voices")
SCOUT_REVIEW = {"at": 0, "error": "", "items": [], "synced": 0, "sync_ok": False}
SCOUT_FARM_CLUE = 30                                            # a titles & captions score this high is a farm clue


def gh_get(url, timeout=15):
    """Public GitHub data as JSON (no sign-in, and nothing of YouTube's sent along)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"YakTube/{VERSION}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def scout_submissions(get=gh_get, repo=None):
    """Open pull requests on the shared voice list and the voice files each adds or changes:
    [{number, title, url, user, files: [{name, entry} or {name, error}]}]."""
    api = f"https://api.github.com/repos/{repo or SCOUT_LIST_REPO}"
    out = []
    for pr in get(f"{api}/pulls?state=open&per_page=10")[:10]:
        files = []
        for f in get(f"{api}/pulls/{int(pr['number'])}/files?per_page=30")[:30]:
            name = str(f.get("filename", ""))
            if not (name.startswith("voices/") and name.endswith(".json")) or f.get("status") == "removed":
                continue
            try:
                files.append({"name": name, "entry": get(f["raw_url"])})
            except Exception as ex:
                files.append({"name": name, "error": str(ex)[:80]})
        out.append(dict(scout_pr_info(pr), files=files))
    return out


def scout_pr_info(pr):
    return {"number": int(pr["number"]), "title": str(pr.get("title", ""))[:120], "url": str(pr.get("html_url", "")),
            "user": str(pr.get("user", {}).get("login", ""))[:40], "sha": str(pr.get("head", {}).get("sha", ""))[:40]}


def scout_sync_open(get=None, now=None):
    """When Settings > YakScout opens (at most once a minute): ask GitHub which submissions are still open, so merged
    or closed ones leave the list, and new or changed ones show "Not checked yet". Quick: no listening."""
    now = now or int(time.time())
    if now - SCOUT_REVIEW["synced"] < 60:
        return
    SCOUT_REVIEW["synced"] = now
    try:
        pulls = (get or (lambda u: gh_get(u, timeout=4)))(
            f"https://api.github.com/repos/{SCOUT_LIST_REPO}/pulls?state=open&per_page=10")[:10]
    except Exception:
        SCOUT_REVIEW["sync_ok"] = False                         # offline or rate-limited: keep what we had
        return
    SCOUT_REVIEW["sync_ok"] = True
    had = {it["number"]: it for it in SCOUT_REVIEW["items"]}
    items = []
    for pr in pulls:
        info = scout_pr_info(pr)
        old = had.get(info["number"])
        items.append(old if old and old.get("sha") == info["sha"] else dict(info, files=[], verdict="unchecked"))
    SCOUT_REVIEW["items"] = items


def scout_judge_entry(entry, verify=None, text=None):
    """Both checks on one submitted voice, and a plain verdict:
    ok (looks good to merge), look (same voice but few farm clues: look at the channels), no (don't merge),
    wait (the voice couldn't be checked)."""
    verify = verify or (lambda en: scout_call("/verify", {"entry": en}, timeout=300))
    text = text or scout_check
    try:
        heard = verify(entry).get("examples", [])
    except Exception:
        heard = []
    limit = 0.25
    same = sum(1 for x in heard if isinstance(x.get("gap"), (int, float)) and x["gap"] <= limit)
    diff = sum(1 for x in heard if isinstance(x.get("gap"), (int, float)) and x["gap"] > limit)
    examples = [x for x in entry.get("examples", []) if isinstance(x, str) and VID_RE.fullmatch(x)][:10]
    clues, why = 0, {}
    for vid in examples:
        try:
            r = text(vid) or {}
        except Exception:
            r = {}
        if r.get("score", 0) >= SCOUT_FARM_CLUE:
            clues += 1
            for w in r.get("why", []):
                why[w] = why.get(w, 0) + 1
    if diff:
        verdict = "no"
    elif not same:
        verdict = "wait"
    elif clues * 2 >= max(1, len(examples)):
        verdict = "ok"
    else:
        verdict = "look"
    return {"id": str(entry.get("id", "?"))[:40], "channels": [str(c) for c in entry.get("channels", [])][:20],
            "verdict": verdict, "same": same, "diff": diff, "heard": len(heard), "examples": len(examples),
            "clues": clues, "why": [w for w, _ in sorted(why.items(), key=lambda x: -x[1])][:3]}


def scout_review(get=gh_get, verify=None, text=None):
    """Check every open submission (Settings > YakScout > Submissions > Check)."""
    try:
        subs = scout_submissions(get)
        items = []
        for sub in subs:
            judged = [scout_judge_entry(f["entry"], verify, text) if "entry" in f and isinstance(f["entry"], dict)
                      else {"id": f["name"], "verdict": "wait", "error": f.get("error", "not a voice file")} for f in sub["files"]]
            order = ["no", "wait", "look", "ok"]
            items.append(dict(sub, files=judged,
                              verdict=min((j["verdict"] for j in judged), key=order.index) if judged else "none"))
        SCOUT_REVIEW.update(at=int(time.time()), error="", items=items, synced=int(time.time()), sync_ok=True)
    except Exception as ex:
        SCOUT_REVIEW.update(at=int(time.time()), error=str(ex)[:120])
    return SCOUT_REVIEW


SCOUT_VERDICT = {"unchecked": "Not checked yet: press Check", "ok": "✅ Looks good to merge", "look": "⚠️ Same voice, few farm clues: check the channels",
                 "no": "❌ Don't merge: a different voice in its examples", "wait": "⏸ Couldn't check the voice yet",
                 "none": "No voice files in it"}


def scout_badge(why):
    """What caught a dropped Short, in a word, for its picture in Settings."""
    for word, b in (("voice", "Voice"), ("farm channel", "Channel"), ("description", "Pasted"), ("opening", "Story")):
        if word in why:
            return b
    return "Text"


def scout_stats(c):
    """The live part of Settings > YakScout (the page asks for it every few seconds): the connection badge, today's
    counts and the plugin's facts line."""
    on = SCOUT_CFG["on"] == "1" and SCOUT_CFG["addr"]
    seen = int(SCOUT_CFG["seen"] or 0)
    st = SCOUT_LINK["state"] if on else "off"
    chip = {"ok": ("ok", "Connected"), "down": ("down", "Can't reach" + (f" · seen {ago(seen)}" if seen else "")),
            "": ("", "Connecting…")}.get(st, ("off", "Off" if SCOUT_CFG["addr"] else "Not set up"))
    today = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
    scanned, dropped = c.execute("SELECT COALESCE(SUM(deep),0), COALESCE(SUM(farm),0) FROM scout WHERE at>=?", (today,)).fetchone()

    def dur(sec):
        for n, u in ((86400, "day"), (3600, "hour"), (60, "min")):
            if sec >= n:
                k = sec // n
                return f"{k} {u}{'s' if k > 1 and u != 'min' else ''}"
        return "a moment"
    facts = "".join(f"<span>{x}</span>" for x in (
        f'Plugin <b>v{e(SCOUT_LINK["version"])}</b>' if SCOUT_LINK["version"] else "",
        f'<b>{SCOUT_LINK["secs"]:.1f} s</b> a Short' if SCOUT_LINK["secs"] else "",
        f'Last scan <b>{ago(SCOUT_LINK["last"])}</b>' if SCOUT_LINK["last"] else "",
        f'Up <b>{dur(SCOUT_LINK["up"])}</b>' if SCOUT_LINK["up"] else "") if x) if st == "ok" else ""
    return {"state": st, "chip": chip[0], "text": chip[1], "error": SCOUT_LINK["error"] if st == "down" else "",
            "scanned": scanned, "dropped": dropped, "waiting": len(scout_queue(c, 1000)) if on else "–", "facts": facts}


def scout_pane(c):
    """Settings > YakScout: everything YakScout does, in one place. The lists fold away; each can be searched."""
    live = scout_stats(c)
    st = live["state"]

    def switch(k, label, sub):
        v = SCOUT_CFG[k] == "1"
        return (f'<div class="chk"><div>{label}<small>{sub}</small></div><button type="button" class="btn ghost small sw'
                f'{" on" if v else ""}" role="switch" aria-checked="{"true" if v else "false"}" data-k="{k}">'
                f'{"On" if v else "Off"}<i></i></button></div>')

    def fold(key, title, n, body, tools=""):
        return (f'<details class="card fold" id="sf-{key}"><summary>{title} <span class="n">· {n}</span></summary>'
                f'{tools}<div class="sclist">{body}</div></details>')

    def search(key):
        return f'<div class="tools"><input type="search" placeholder="Search" data-filter="{key}" aria-label="Search"></div>'

    def add(kind, ph, label="Add"):
        return (f'<form class="tools" data-add="{kind}"><input name="v" placeholder="{ph}" autocapitalize="off" '
                f'autocorrect="off" spellcheck="false" aria-label="{ph}"><button class="btn small">{label}</button>'
                '<span class="msg" role="status"></span></form>')

    def item(q, main, sub, btn, pic=""):
        return f'<div class="item" data-q="{e(q.lower())}">{pic}<div><span class="t">{main}</span><small>{sub}</small></div>{btn}</div>'
    none = '<div class="empty">None yet.</div>'
    # dropped Shorts
    rows = c.execute("SELECT s.video_id, s.why, s.at, v.title, v.channel_title FROM scout s LEFT JOIN videos v ON v.id=s.video_id "
                     "WHERE s.farm=1 ORDER BY s.at DESC LIMIT 200").fetchall()
    n_drop = c.execute("SELECT COUNT(*) FROM scout WHERE farm=1").fetchone()[0]
    drops = "".join(item(f'{r["title"] or ""} {r["channel_title"] or ""} {r["why"] or ""}', e(r["title"] or "?"),
                         f'<span class="tag" title="{e(r["why"] or "")}">{scout_badge(r["why"] or "")}</span>{e(r["channel_title"] or "")}',
                         f'<button type="button" class="btn ghost small" data-keep="{r["video_id"]}">Bring back</button>',
                         f'<img src="https://i.ytimg.com/vi/{r["video_id"]}/oar2.jpg" alt="" loading="lazy">') for r in rows)
    # channels
    ls, found = scout_lists(c), scout_found(c)
    farm = [(n, f"Found by YakScout · {k} dropped") for n, k in sorted(found.items(), key=lambda x: -x[1])
            if n.lower() not in ls["ok"] and n.lower() not in ls["keep"] and n.lower() not in ls["farm"]]
    farm = [(n, "Added by you") for n in ls["farm"].values()] + farm
    x = lambda kind, n: f'<button type="button" class="x" data-rm="{kind}" data-name="{e(n)}" aria-label="Remove {e(n)}">×</button>'
    farms = "".join(item(n, e(n), sub, x("farm", n)) for n, sub in farm)
    keeps = "".join(item(n, e(n), "Added by you", x("keep", n)) for n in ls["keep"].values())
    # voices (Scout keeps them; shown while it's connected)
    voices = SCOUT_LINK["voices"] if st == "ok" else []

    def vsub(v):
        n = v.get("channels") if isinstance(v.get("channels"), int) else 0
        names = [str(x) for x in v.get("channel_names", [])][:6] if isinstance(v.get("channel_names"), list) else []
        bits = [f'{n} channel{"s" if n != 1 else ""}' + (": " + ", ".join(names) if names else "")] if n else []
        return " · ".join(bits + ([f'learned {v["learned"]}'] if v.get("learned") else []))
    def vbtn(v):
        n = e(str(v.get("name", "")))
        share = (f'<a class="btn ghost small" href="/scout/share?name={urllib.parse.quote(str(v.get("name", "")))}" download '
                 'title="A file to send to the shared voice list">Share</a>') if v.get("source") != "shared" else ""
        return share + (f'<button type="button" class="x" data-forget="{n}" aria-label="'
                        f'{"Hide here" if v.get("source") == "shared" else "Forget"}">×</button>')
    vlist = "".join(item(f'{v.get("name", "")} {vsub(v)} {v.get("source", "")}', e(str(v.get("name", "?"))),
                         f'<span class="tag">{"Shared" if v.get("source") == "shared" else "Learned"}</span>{e(vsub(v))}',
                         vbtn(v)) for v in voices)
    lists = SCOUT_LINK["lists"] if st == "ok" else []
    vinfo = "".join(f'<div class="empty">Shared list: {l.get("count", 0)} voices · '
                    f'{"updated " + ago(l["fetched"]) if l.get("fetched") else "not downloaded yet"}'
                    f'{" · " + e(str(l["error"])[:60]) if l.get("error") else ""}</div>' for l in lists)
    # submissions to the shared voice list, checked at home
    scout_sync_open()

    def sline(f):
        if f.get("error"):
            return f'{e(f["id"])}: {e(f["error"])}'
        bits = [f'voice {f["same"]}/{f["examples"]} same' + (f', {f["diff"]} different' if f["diff"] else ""),
                f'farm clues {f["clues"]}/{f["examples"]}' + (f' ({", ".join(f["why"])})' if f["why"] else "")]
        return f'{e(f["id"])} · ' + " · ".join(e(b) for b in bits)
    subs = "".join(
        f'<div class="item sub" data-q="{e((it["title"] + " " + it["user"]).lower())}"><div><span class="t">'
        f'{SCOUT_VERDICT.get(it["verdict"], "")}</span><small>#{it["number"]} {e(it["title"])}'
        f'{" · by " + e(it["user"]) if it["user"] else ""}</small>'
        + "".join(f'<small>{sline(f)}</small>' for f in it["files"]) +
        f'</div><a class="btn ghost small" href="{e(it["url"])}" target="_blank" rel="noopener">Open</a></div>'
        for it in SCOUT_REVIEW["items"])
    stext = (f'Checked {ago(SCOUT_REVIEW["at"])}' if SCOUT_REVIEW["at"] and SCOUT_REVIEW["items"] else
             "" if SCOUT_REVIEW["sync_ok"] and not SCOUT_REVIEW["items"] else "Not checked yet") + \
        (f' · {e(SCOUT_REVIEW["error"])}' if SCOUT_REVIEW["error"] else "")
    sinfo = f'<div class="empty">{stext}</div>' if stext else ""
    # the log: what was judged lately
    log = c.execute("SELECT s.video_id, s.score, s.farm, s.why, s.at, s.deep, v.title, v.channel_title FROM scout s "
                    "LEFT JOIN videos v ON v.id=s.video_id ORDER BY s.at DESC LIMIT 100").fetchall()
    logs = "".join(item(f'{r["title"] or ""} {r["channel_title"] or ""} {r["why"] or ""}',
                        e(r["title"] or r["video_id"]),
                        f'{"Dropped" if r["farm"] else "Kept"} · {r["score"]} · {e(r["why"] or "-")} · {ago(r["at"])}',
                        f'<span class="tag">{"Scanned" if r["deep"] else "Read"}</span>') for r in log)
    lo = scout_min()
    days = SCOUT_CFG["days"] if SCOUT_CFG["days"] in ("30", "60", "90") else "60"
    return (f'<section class="spane scp" id="p-scout" hidden><div class="sch"><h2>YakScout</h2><span class="chip {live["chip"]}" '
            f'id="sc-chip" title="{e(live["error"])}"><i></i><span>{live["text"]}</span></span></div>'
            '<p>Keeps content-farm Shorts out. AI-made is fine.</p>'
            f'<div class="stats"><div class="stat"><b id="sc-scanned">{live["scanned"]}</b><span>Scanned today</span></div>'
            f'<div class="stat"><b id="sc-dropped">{live["dropped"]}</b><span>Dropped today</span></div>'
            f'<div class="stat"><b id="sc-waiting">{live["waiting"]}</b><span>Waiting</span></div></div>'
            '<h3>Checks</h3><div class="card">' + switch("text", "Titles &amp; captions", "Built in") +
            switch("on", "Scan voices", "Needs the YakScout plugin") + '</div>'
            '<h3>Connection</h3><div class="card"><form class="chk addr" id="scaddr"><label class="fld wide"><span>Address</span>'
            f'<input name="addr" value="{e(SCOUT_CFG["addr"])}" placeholder="192.168.1.31" inputmode="url" autocapitalize="off" '
            'autocorrect="off" spellcheck="false"></label><label class="fld port"><span>Port</span>'
            f'<input name="port" value="{e(SCOUT_CFG["port"])}" placeholder="8095" inputmode="numeric"></label>'
            f'<button class="btn small save">Save</button><span class="msg" role="status"></span></form>'
            f'<div class="facts" id="sc-facts"{"" if live["facts"] else " hidden"}>{live["facts"]}</div></div>'
            '<h3>Lists</h3>' +
            fold("drop", "Dropped", n_drop, drops or none, search("drop")) +
            fold("farm", "AI-Farm channels", len(farm), farms or none, add("farm", "Channel name") + search("farm")) +
            fold("keep", "Never drop", len(ls["keep"]), keeps or none, add("keep", "Channel name")) +
            fold("voice", "Voices", len(voices), vlist or ('<div class="empty">None learned yet.</div>' if st == "ok" else
                 '<div class="empty">Shows when YakScout is connected.</div>'),
                 (add("voice", "Link to a farm Short", "Learn") + vinfo) if st == "ok" else "") +
            fold("subs", "Submissions", len(SCOUT_REVIEW["items"]),
                 subs or ('<div class="empty">None open.</div>' if SCOUT_REVIEW["sync_ok"] else ""),
                 '<div class="tools"><button type="button" class="btn small" data-review>Check</button>'
                 '<span class="msg" role="status"></span></div>' + sinfo) +
            fold("log", "Log", len(log), logs or none,
                 '<div class="tools"><input type="search" placeholder="Search" data-filter="log" aria-label="Search">'
                 '<a class="btn ghost small" href="/scout/log.csv" download>Download CSV</a></div>') +
            '<details class="card fold" id="sf-adv"><summary>Advanced</summary>'
            f'<div class="chk"><div>Drop at<small>Lower drops more</small></div><div class="slider"><input type="range" min="30" '
            f'max="90" step="5" value="{lo}" data-set="min" aria-label="Drop at"><b>{lo}</b></div></div>'
            '<div class="chk"><div>Remember for<small>Then YakScout looks again</small></div><select class="btn ghost small" '
            'data-set="days" aria-label="Remember for">' + "".join(
                f'<option value="{d}"{" selected" if d == days else ""}>{d} days</option>' for d in ("30", "60", "90")) +
            '</select></div><div class="chk"><div>Backup<small>Lists, voices, settings &amp; log (.zip)</small></div>'
            '<a class="btn ghost small" href="/scout/backup" download>Download</a></div>'
            '<div class="chk"><div>Restore<small>From a backup; adds to what\'s here</small><span class="msg" id="sc-rmsg" '
            'role="status"></span></div><label class="btn ghost small" tabindex="0">Choose file<input type="file" '
            'accept=".zip,.json,application/zip,application/json" id="sc-restore" hidden></label></div></details></section>')


def shorts_for(c, pid, first=None, limit=300):
    """Shorts from outside your subscriptions (what YouTube shows a visitor who isn't logged in),
    newest batch first, minus ones you've watched. Your thumbs steer it by channel (a thumb down outweighs
    a thumb up): channels you've mostly thumbed down drop out, and ones you've liked move up the list."""
    ids = OUTSIDE[:]
    found = {r["id"]: r for r in c.execute(
        f"SELECT id, title, channel_title FROM videos WHERE id IN ({','.join('?' * len(ids))}) AND id NOT IN "
        "(SELECT video_id FROM history WHERE profile_id=?)", ids + [pid])} if ids else {}
    score = {}      # by channel name: Shorts from outside your subscriptions come with a name but no channel id
    for name, val in c.execute("SELECT v.channel_title, r.val FROM ratings r JOIN videos v ON v.id=r.video_id "
                               "WHERE r.profile_id=? AND v.channel_title!=''", (pid,)):
        score[name] = score.get(name, 0) + (1 if val > 0 else -1.5)
    no_vids = hidden_sets(c, pid)[0]
    out = [found[v] for v in ids if v in found and v not in no_vids and score.get(found[v]["channel_title"], 0) >= 0]
    lift = lambda r: 30 * min(score.get(r["channel_title"], 0), 3)     # up to 90 places for a favourite
    out = [r for _, r in sorted(enumerate(out), key=lambda p: p[0] - lift(p[1]))]
    if first:
        out = [ensure_video(c, first)] + [r for r in out if r["id"] != first]
    return [{"id": r["id"], "title": r["title"] or "", "channel": r["channel_title"] or ""} for r in out[:limit]]


def home_shorts(c, pid, n=16):
    """Shorts for the shelves on Home: unwatched ones from your subscriptions (newest first) taking turns with
    YouTube's own (the Shorts tab's list). Skips hidden and thumbed-down ones."""
    skip = {r[0] for r in c.execute("SELECT video_id FROM history WHERE profile_id=? UNION "
                                    "SELECT video_id FROM ratings WHERE profile_id=? AND val<0", (pid, pid))}
    no_vids, no_chans = hidden_sets(c, pid)
    skip |= no_vids
    mine = c.execute("SELECT v.* FROM videos v JOIN subs s ON s.channel_id=v.channel_id AND s.profile_id=? "
                     "WHERE v.short=1 ORDER BY v.published DESC LIMIT 60", (pid,)).fetchall()
    ids = OUTSIDE[:]
    found = {r["id"]: r for r in c.execute(
        f"SELECT * FROM videos WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    theirs = [found[v] for v in ids if v in found]
    out, got = [], set()
    for i in range(max(len(mine), len(theirs))):
        for src in (mine, theirs):
            if i < len(src):
                r = src[i]
                if r["id"] not in skip and r["id"] not in got and r["channel_id"] not in no_chans:
                    got.add(r["id"])
                    out.append(r)
    return out[:n]


def mark_watched(c, pid, vid):
    c.execute("INSERT INTO history(profile_id,video_id,watched_at) VALUES(?,?,?) ON CONFLICT(profile_id,video_id) DO UPDATE "
              "SET watched_at=excluded.watched_at, times=times+1", (pid, vid, int(time.time())))


def search(c, q):
    # sp=EgIQAQ== limits results to videos (no channels/playlists/shorts shelves)
    _, d = yt_data("https://www.youtube.com/results?sp=EgIQAQ%253D%253D&search_query=" + urllib.parse.quote_plus(q))
    vids = parse_items(d)
    store_videos(c, vids)
    return vids


def parse_channels(d, limit=3):
    """Channels in a search page: id, name and "@handle · 399K subscribers"."""
    out = {}
    for r in walk(d, "channelRenderer"):
        cid = r.get("channelId", "")
        # YouTube keeps the handle under "subscriberCountText" and the subscribers under "videoCountText"
        info = [r.get(k, {}).get("simpleText", "") for k in ("subscriberCountText", "videoCountText")]
        if CID_RE.fullmatch(cid) and cid not in out:
            out[cid] = {"id": cid, "title": r.get("title", {}).get("simpleText", "") or cid,
                        "info": " · ".join(sorted((t for t in info if t), key=lambda t: "subscriber" in t))}
    return list(out.values())[:limit]


def search_channels(q):
    # sp=EgIQAg== limits results to channels
    _, d = yt_data("https://www.youtube.com/results?sp=EgIQAg%253D%253D&search_query=" + urllib.parse.quote_plus(q))
    return parse_channels(d)


def hidden_sets(c, pid):
    """What this profile marked "not interested": (video ids, channel ids)."""
    vids, chans = set(), set()
    for kind, ref in c.execute("SELECT kind, ref FROM hidden WHERE profile_id=?", (pid,)):
        (vids if kind == "v" else chans).add(ref)
    return vids, chans


def fetch_related(c, vid, max_age=86400):
    """Fill in a video's details and cache its 'up next' list (also feeds the For You page)."""
    row = c.execute("SELECT fetched_at FROM related_src WHERE src=?", (vid,)).fetchone()
    if not row or row[0] < time.time() - max_age:
        page, d = yt_data("https://www.youtube.com/watch?v=" + vid)
        got = lambda k: (re.findall(r'"' + k + r'":"([^"]*)"', page) or [""])[0]
        if CID_RE.fullmatch(got("channelId")):
            c.execute("UPDATE videos SET channel_id=COALESCE(channel_id,?), views=MAX(views,?), "
                      "published=CASE WHEN published=0 THEN ? ELSE published END WHERE id=?",
                      (got("channelId"), int(got("viewCount") or 0), _ts(got("publishDate")), vid))
        if got("lengthSeconds").isdigit():
            c.execute("UPDATE videos SET length=? WHERE id=?", (int(got("lengthSeconds")), vid))
        rel = [v for v in parse_items(d) if v["id"] != vid][:20]
        store_videos(c, rel)
        c.execute("DELETE FROM related WHERE src=?", (vid,))
        c.executemany("INSERT OR IGNORE INTO related(src,video_id,pos) VALUES(?,?,?)",
                      [(vid, v["id"], i) for i, v in enumerate(rel)])
        c.execute("INSERT OR REPLACE INTO related_src(src,fetched_at) VALUES(?,?)", (vid, int(time.time())))
    return c.execute("SELECT v.* FROM related r JOIN videos v ON v.id=r.video_id WHERE r.src=? ORDER BY r.pos",
                     (vid,)).fetchall()


def resolve_channel(s):
    """Turn a channel URL, @handle, channel id or video link into a UC... id."""
    s = s.strip()
    m = CID_RE.search(s)
    if m and (s.startswith("UC") or "/channel/" in s):
        return m.group(0)
    vid = video_id_from(s)
    if vid:
        url = "https://www.youtube.com/watch?v=" + vid
    elif s.startswith("@"):
        url = "https://www.youtube.com/" + urllib.parse.quote(s)
    elif re.match(r"^(https?://)?(www\.|m\.)?youtube\.com/", s):
        url = s if s.startswith("http") else "https://" + s
    elif re.match(r"^[\w.-]+$", s):
        url = "https://www.youtube.com/@" + s
    else:
        raise ValueError("That doesn't look like a YouTube channel.")
    page = fetch(url)
    for pat in (r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"',
                r'"externalId":"(UC[\w-]{22})"', r'"channelId":"(UC[\w-]{22})"'):
        m = re.search(pat, page)
        if m:
            return m.group(1)
    raise ValueError("Couldn't find that channel.")


def ensure_video(c, vid):
    """Make sure we know a video's title/channel (for links pasted from outside)."""
    row = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    if row:
        return row
    try:
        o = json.loads(fetch("https://www.youtube.com/oembed?format=json&url="
                             + urllib.parse.quote("https://www.youtube.com/watch?v=" + vid), timeout=5))
        c.execute("INSERT OR IGNORE INTO videos(id,title,channel_title,channel_url) VALUES(?,?,?,?)",
                  (vid, o.get("title", ""), o.get("author_name", ""), o.get("author_url", "")))
    except Exception:
        c.execute("INSERT OR IGNORE INTO videos(id,title) VALUES(?,?)", (vid, ""))
    return c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()


def new_to_you(c, pid, limit=48, now=None, taste=None):
    """Channels this profile has never watched or subscribed to, reached through what it does watch.
    Hop 1: 'up next' of your watches (the last few hours count most). Hop 2: 'up next' of those suggestions (cached
    in the background). Titles on your topics rank higher; anything you thumbed down, and its "up next", is out.
    A channel you've been shown and passed over sinks (half as strong every 3 days), so the page turns over."""
    now = now or time.time()
    t = taste or Taste(c, pid, now)
    hist = c.execute("SELECT h.video_id, h.watched_at FROM history h LEFT JOIN videos v ON v.id=h.video_id WHERE h.profile_id=? "
                     "AND COALESCE(v.short,0)=0 ORDER BY h.watched_at DESC LIMIT 100", (pid,)).fetchall()
    watched = {r[0] for r in c.execute("SELECT video_id FROM history WHERE profile_id=?", (pid,))}
    known = {r[0] for r in c.execute("SELECT channel_id FROM subs WHERE profile_id=?", (pid,))}
    known_titles = set()
    for cid, title in c.execute("SELECT v.channel_id, v.channel_title FROM history h JOIN videos v ON v.id=h.video_id "
                                "WHERE h.profile_id=?", (pid,)):
        known.add(cid)
        known_titles.add(title)
    no_vids, no_chans = hidden_sets(c, pid)   # "not interested"
    watched |= no_vids | {v for v, val in t.rate.items() if val < 0}
    known |= no_chans | {ch for ch, a in t.chan.items() if a < 0}
    known.discard(None)
    known_titles.discard("")
    known_titles.discard(None)

    def spread(sources, into):
        for src, w in sources.items():
            for vid, pos in c.execute("SELECT video_id, pos FROM related WHERE src=?", (src,)):
                into[vid] = into.get(vid, 0) + w / (1 + 0.1 * pos)
    hop1 = {}
    spread({h[0]: (.5 + .5 * .5 ** ((now - (h[1] or 0)) / (6 * 3600))) / (1 + 0.05 * i)
            for i, h in enumerate(hist) if t.rate.get(h[0], 0) >= 0}, hop1)
    cached = {r[0] for r in c.execute("SELECT src FROM related_src")}
    score = dict(hop1)
    spread({v: 0.5 * min(s, 1) for v, s in hop1.items() if v in cached}, score)
    ids = [v for v in sorted(score, key=score.get, reverse=True) if v not in watched][:800]
    if not ids:
        return []
    shown_at = {}                             # when each video / its channel first appeared on this page
    for vid, cid, at in c.execute("SELECT s.video_id, v.channel_id, s.first_at FROM shown s LEFT JOIN videos v "
                                  "ON v.id=s.video_id WHERE s.profile_id=?", (pid,)):
        for k in (vid, cid):
            if k:
                shown_at[k] = min(shown_at.get(k, at), at)

    rows = c.execute(f"SELECT * FROM videos WHERE short=0 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()
    topic = t.topics(rows)

    def rank(r):
        at = min(shown_at.get(r["id"], now), shown_at.get(r["channel_id"], now))
        return score[r["id"]] * 0.5 ** (max(0, now - at) / (3 * 86400)) * (.7 + .6 * max(-.5, topic.get(r["id"], 0)))
    out, seen = [], set()
    for r in sorted(rows, key=rank, reverse=True):
        k = r["channel_id"] or r["channel_title"]
        if k and k not in seen and r["channel_id"] not in known and r["channel_title"] not in known_titles:
            seen.add(k)                       # one video per new channel
            out.append(r)
            if len(out) >= limit:
                break
    return out


def warm_explore(pid):
    """Background: look one step further out from the best 'new to you' picks."""
    c = connect()
    try:
        cached = {r[0] for r in c.execute("SELECT src FROM related_src")}
        for r in [r for r in new_to_you(c, pid, 12) if r["id"] not in cached][:4]:
            fetch_related(c, r["id"])
    except Exception as ex:
        print("[explore]", repr(ex))
    finally:
        c.close()


def warm_related(pid):
    """Background: make sure recent watches have their 'up next' lists cached."""
    c = connect()
    try:
        for (vid,) in c.execute("SELECT h.video_id FROM history h LEFT JOIN related_src r ON r.src=h.video_id "
                                "LEFT JOIN videos v ON v.id=h.video_id WHERE h.profile_id=? AND r.src IS NULL "
                                "AND COALESCE(v.short,0)=0 ORDER BY h.watched_at DESC LIMIT 5",
                                (pid,)).fetchall():
            fetch_related(c, vid)
    except Exception as ex:
        print("[warm]", repr(ex))
    finally:
        c.close()


def discover(c, pid, n=6):
    """On a schedule: fetch fresh 'up next' lists so Home / New to you keep changing even if nobody opens them.
    Per profile: lists missing for recent watches, the oldest lists of the last 25 watches (older than 2 days),
    then one step further out from the best 'new to you' picks. At most 3*n pages from YouTube."""
    def grab(vids):
        got = 0
        for vid in vids:
            try:
                fetch_related(c, vid, max_age=0)
                got += 1
            except Exception as ex:
                print("[discover]", vid, repr(ex))
        return got
    age = {r[0]: r[1] for r in c.execute("SELECT src, fetched_at FROM related_src")}
    recent = [r[0] for r in c.execute(
        "SELECT h.video_id FROM history h LEFT JOIN videos v ON v.id=h.video_id WHERE h.profile_id=? "
        "AND COALESCE(v.short,0)=0 ORDER BY h.watched_at DESC LIMIT 25", (pid,))]
    old = time.time() - 2 * 86400
    done = grab([v for v in recent if v not in age][:n]
                + sorted((v for v in recent if age.get(v, old) < old), key=age.get)[:n])
    cached = {r[0] for r in c.execute("SELECT src FROM related_src")}
    return done + grab([r["id"] for r in new_to_you(c, pid) if r["id"] not in cached][:n])


def discover_all():
    c = connect()
    try:
        for (pid,) in c.execute("SELECT id FROM profiles").fetchall():
            n = discover(c, pid) + discover_more(c, pid)
            if n:
                print(f"[discover] profile {pid}: {n} lists fetched")
    finally:
        c.close()


# ---------------------------------------------------------------- Home: the recommendation engine
# All of it is worked out here from this profile's own activity. YouTube only supplies lists of videos ("up next"
# lists, search results, channel pages), the same ones it shows any visitor who isn't logged in.
#
# 1. Candidates come from several sources:
#      watch    "up next" of what you watched lately; the last few hours count most, so Home follows your mood
#      liked    "up next" of videos you gave a thumbs up (your lasting tastes)
#      subs     fresh uploads from your subscriptions
#      back     older videos from your subscriptions that you haven't seen
#      search   results of searches you made (recent and repeated ones count more), re-run in the background
#      topic    searches YakTube makes by itself from the words in the titles you watch and like
#      explore  New to you: channels you haven't tried, reached through ones you like
# 2. Each candidate gets a score: how strongly its sources point at it, x how much you like its channel, x how well
#    its title matches the topics you watch, x how new it is to you (every visit where it was on screen and you
#    passed it over, it sinks; it recovers over a few days).
# 3. Each source's weight is learned from what you do: of the videos it put on your screen, how many you went on
#    to watch (a thumbs up counts double, a thumbs down as a miss). Sources that work for you get more of Home;
#    a little randomness (Thompson sampling) keeps giving the others a chance.
# 4. Home is filled from the top: at most 2 per channel in the first 24 and 3 overall (more only once nothing else
#    is left), one "up next" list or search can't take over the page, and every 6th place goes to something new (explore, topic or back catalogue).
#    It reshuffles after every video you watch and every 15 minutes.
SOURCES = {"watch": .12, "liked": .10, "subs": .14, "back": .05, "search": .08, "topic": .05, "explore": .05}  # prior hit rates
WHY = {"watch": "Because you watched", "liked": "Because you liked", "subs": "New from your subscriptions",
       "back": "From a channel you subscribe to", "search": "From your search", "topic": "A topic you watch",
       "explore": "A channel new to you"}
STOP = set("""the and for with from that this these those you your yours are was were been being its it's into out
about than then what how why when where who which not can will just all any more most very via get got one two
new video videos official full part episode live shorts short trailer review reaction vlog best top ever first last
how-to don't i'm you're we're they're here there have has had our ours their them his her she him""".split())
WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)?")
DAY = 86400


def words(title):
    return [w for w in WORD_RE.findall((title or "").lower()) if len(w) > 2 and not w.isdigit() and w not in STOP]


class Taste:
    """What one profile likes, from its history (how much of each video it watched, how often, how lately) and its
    thumbs: a score per channel, per word and per word pair, all fading with a 45-day half-life."""

    def __init__(self, c, pid, now):
        self.rate = dict(c.execute("SELECT video_id, val FROM ratings WHERE profile_id=?", (pid,)).fetchall())
        self.chan, self.word, self.pair, self.eng = {}, {}, {}, {}
        for r in c.execute("SELECT h.video_id, h.watched_at, h.times, h.best, h.dur, v.channel_id, v.title FROM history h "
                           "JOIN videos v ON v.id=h.video_id WHERE h.profile_id=? AND v.short=0 "
                           "ORDER BY h.watched_at DESC LIMIT 400", (pid,)):
            done = min(1, (r["best"] or 0) / r["dur"]) if r["dur"] else .5    # unknown (older history): call it half
            e = .4 + done + .3 * min((r["times"] or 1) - 1, 3)
            val = self.rate.get(r["video_id"], 0)
            e = e + 1.5 if val > 0 else -2 if val < 0 else e
            self.eng[r["video_id"]] = e
            self.add(r["channel_id"], r["title"], e * .5 ** ((now - (r["watched_at"] or now)) / (45 * DAY)))
        for r in c.execute("SELECT v.channel_id, v.title, r.val FROM ratings r JOIN videos v ON v.id=r.video_id "
                           "WHERE r.profile_id=? AND (v.short=1 OR r.video_id NOT IN (SELECT video_id FROM history WHERE profile_id=?))",
                           (pid, pid)):     # rated without watching, and any rated Short (watching one counts for nothing)
            self.add(r["channel_id"], r["title"], 1 if r["val"] > 0 else -1.5)
        self.top = max([v for v in self.chan.values() if v > 0], default=1)

    def add(self, ch, title, w):
        if ch:
            self.chan[ch] = self.chan.get(ch, 0) + w
        ws = words(title)
        k = w / max(1, len(ws)) ** .5
        for x in set(ws):
            self.word[x] = self.word.get(x, 0) + k
        for x in set(zip(ws, ws[1:])):
            self.pair[x] = self.pair.get(x, 0) + k

    def channel(self, ch, subbed=False):
        """x1 for a channel you know nothing about, up to x2 for your favourite, down to x0.25 if you thumb it down."""
        a = self.chan.get(ch, 0)
        f = 1 + a / self.top if a >= 0 else max(.25, 1 / (1 - a))
        return f * (1.15 if subbed else 1)

    def topics(self, rows):
        """How well each title matches your words (rarer words count more), from -1 to 1."""
        df, toks = {}, {}
        for r in rows:
            toks[r["id"]] = set(words(r["title"]))
            for w in toks[r["id"]]:
                df[w] = df.get(w, 0) + 1
        n = len(rows) or 1
        raw = {vid: sum(self.word.get(w, 0) * math.log(1 + n / df[w]) for w in ws) / max(1, len(ws)) ** .5
               for vid, ws in toks.items()}
        top = max([abs(v) for v in raw.values()], default=0) or 1
        return {vid: v / top for vid, v in raw.items()}

    def queries(self, n=2, skip=()):
        """Searches to try for you: the word pairs that sum up what you watch (e.g. "quantum mechanics")."""
        best = sorted(((w, " ".join(p)) for p, w in self.pair.items() if w > 0 and min(self.word.get(x, 0) for x in p) > 0),
                      reverse=True) + sorted(((w, x) for x, w in self.word.items() if w > 0), reverse=True)  # then single words
        return [q for _, q in best if q not in skip][:n]


def source_weights(c, pid, now, rng):
    """How well each source has worked for this profile lately, as a multiplier (1 = as expected). Each is a draw from
    its Beta distribution, so a source that's barely been tried sometimes gets a go."""
    seen = {s: [0, 0] for s in SOURCES}                       # shown, watched
    for src, won, val in c.execute(
            "SELECT i.src, h.watched_at >= i.first_at, COALESCE(r.val,0) FROM impressions i LEFT JOIN history h ON "
            "h.profile_id=i.profile_id AND h.video_id=i.video_id LEFT JOIN ratings r ON r.profile_id=i.profile_id AND "
            "r.video_id=i.video_id WHERE i.profile_id=? AND i.first_at>?", (pid, now - 30 * DAY)):
        if src in seen:
            hit = 1 if won and val >= 0 else 0
            seen[src][0] += 1 + (val > 0)
            seen[src][1] += hit * (2 if val > 0 else 1)
    out = {}
    for s, p in SOURCES.items():
        shown, won = seen[s]
        draw = rng.betavariate(won + 40 * p, shown - won + 40 * (1 - p)) / p   # 1 = doing as well as expected
        out[s] = min(2.5, max(.4, draw ** .5))                # square root: it steers Home, it doesn't flip it
    return out


def home_feed(c, pid, limit=60, now=None, seed=None):
    """The Home page: a list of (video row, source, score, why). See the notes above."""
    now = now or time.time()
    last = c.execute("SELECT MAX(watched_at) FROM history WHERE profile_id=?", (pid,)).fetchone()[0] or 0
    rng = random.Random(f"{pid}:{last}:{int(now // 900)}" if seed is None else seed)   # same until you watch something
    t = Taste(c, pid, now)
    subs = {r[0] for r in c.execute("SELECT channel_id FROM subs WHERE profile_id=?", (pid,))}
    no_vids, no_chans = hidden_sets(c, pid)
    skip = {r[0] for r in c.execute("SELECT video_id FROM history WHERE profile_id=?", (pid,))} | no_vids
    skip |= set(t.rate)                                       # rated = seen it (even if not played here)
    cand = {}                                                 # video -> {source: [weight, strongest root, its weight]}

    def add(vid, src, w, root):
        if vid in skip or w <= 0:
            return
        d = cand.setdefault(vid, {}).setdefault(src, [0, root, 0])
        d[0] += w                                             # several lists pointing at one video add up
        if w > d[2]:
            d[1], d[2] = root, w

    def lists(src, roots):                                    # roots: (list key, weight, label)
        for key, w, root in roots:
            for vid, pos in c.execute("SELECT video_id, pos FROM related WHERE src=?", (key,)):
                add(vid, src, w / (1 + .1 * pos), root)
    recent = c.execute("SELECT h.video_id, h.watched_at FROM history h JOIN videos v ON v.id=h.video_id WHERE "
                       "h.profile_id=? AND v.short=0 ORDER BY h.watched_at DESC LIMIT 25", (pid,)).fetchall()
    lists("watch", [(v, (.3 + 1.0 * .5 ** ((now - at) / (4 * 3600))) / (1 + .1 * i) * min(t.eng.get(v, 1), 2) / 1.4, v)
                    for i, (v, at) in enumerate(recent) if t.eng.get(v, 1) > 0])
    lists("liked", [(v, .2 + .4 * .5 ** ((now - at) / (14 * DAY)), v) for v, at in c.execute(
        "SELECT video_id, at FROM ratings WHERE profile_id=? AND val>0 ORDER BY at DESC LIMIT 50", (pid,))])
    for q, at, n, auto in c.execute("SELECT q, at, n, auto FROM searches WHERE profile_id=? ORDER BY at DESC LIMIT 40", (pid,)):
        w = (.15 + .55 * .5 ** ((now - at) / (7 * DAY))) * (1 + .3 * math.log(n or 1))
        lists("topic" if auto else "search", [("q:" + q, w * (.7 if auto else 1), "q:" + q)])
    for vid, ch, pub in c.execute("SELECT v.id, v.channel_id, v.published FROM videos v JOIN subs s ON s.channel_id="
                                  "v.channel_id AND s.profile_id=? WHERE v.short=0 ORDER BY v.published DESC LIMIT 1500", (pid,)):
        age = (now - (pub or 0)) / DAY
        if age < 21:
            add(vid, "subs", .1 + .8 * (1 - age / 21), "subs")
        else:
            add(vid, "back", .3 * .5 ** (age / 365), "back:" + (ch or ""))
    for i, r in enumerate(new_to_you(c, pid, 24, now, taste=t)):   # built from the same lists: only adds what's new
        if r["id"] not in cand:
            add(r["id"], "explore", .35 * (1 - i / 48), "explore")
    if not cand:
        return []
    W = source_weights(c, pid, now, rng)
    raw = {vid: sum(W[s] * d[0] for s, d in srcs.items()) for vid, srcs in cand.items()}
    ids = sorted(raw, key=raw.get, reverse=True)[:1500]
    rows = c.execute(f"SELECT * FROM videos WHERE short=0 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()
    topic = t.topics(rows)
    shown = {r[0]: (r[1], r[2]) for r in c.execute("SELECT video_id, n, last_at FROM impressions WHERE profile_id=?", (pid,))}
    items = []
    for r in rows:
        if r["channel_id"] in no_chans:
            continue
        srcs = cand[r["id"]]
        src = max(srcs, key=lambda s: W[s] * srcs[s][0])
        n, at = shown.get(r["id"], (0, now))
        score = (raw[r["id"]] * t.channel(r["channel_id"], r["channel_id"] in subs)
                 * (.55 + .9 * max(-.5, topic.get(r["id"], 0)))          # x0.1 (off-topic) .. x1.45 (right on it)
                 * .6 ** (n * .5 ** ((now - at) / (3 * DAY)))           # passed over before: sinks, recovers in days
                 * rng.uniform(.92, 1.08))
        items.append([score, r, src, srcs[src][1]])
    out, per, used = [], {}, {}
    while items and len(out) < limit:
        cap = 2 if len(out) < 24 else 3
        pool = [it for it in items if per.get(it[1]["channel_id"] or it[1]["channel_title"], 0) < cap] or items
        if len(out) % 6 == 5:                                 # every 6th place: something new
            pool = [it for it in pool if it[2] in ("explore", "topic", "back")] or pool
        it = max(pool, key=lambda it: it[0] * .7 ** used.get(it[3], 0))
        items.remove(it)
        k = it[1]["channel_id"] or it[1]["channel_title"]
        per[k] = per.get(k, 0) + 1
        used[it[3]] = used.get(it[3], 0) + 1
        out.append((it[1], it[2], it[0], why(c, it[2], it[3])))
    return out


def why(c, src, root):
    """A short reason for a pick, e.g. 'Because you watched: <title>'."""
    if src in ("watch", "liked"):
        r = c.execute("SELECT title FROM videos WHERE id=?", (root,)).fetchone()
        return f"{WHY[src]}: {r[0]}" if r and r[0] else WHY[src]
    if root.startswith("q:"):
        return f"{WHY[src]}: “{root[2:]}”"
    return WHY[src]


def save_search(c, pid, q, vids, auto=0):
    """Remember a search (for Home) and its results, as the list 'q:<words>'."""
    q = " ".join(q.lower().split())[:120]
    if not q:
        return
    c.execute("INSERT INTO searches(profile_id,q,at,n,auto) VALUES(?,?,?,1,?) ON CONFLICT(profile_id,q) DO UPDATE SET "
              "at=excluded.at, n=n+1-excluded.auto, auto=MIN(auto,excluded.auto)", (pid, q, int(time.time()), auto))
    c.execute("DELETE FROM related WHERE src=?", ("q:" + q,))
    c.executemany("INSERT OR IGNORE INTO related(src,video_id,pos) VALUES(?,?,?)",
                  [("q:" + q, v["id"], i) for i, v in enumerate(vids[:20])])
    c.execute("INSERT OR REPLACE INTO related_src(src,fetched_at) VALUES(?,?)", ("q:" + q, int(time.time())))


def discover_more(c, pid, n=3, now=None):
    """On the same schedule as discover(): keep Home's other sources topped up. Per profile, at most:
    n thumbed-up videos' "up next" lists, n past searches re-run, 2 topic searches, 2 subscriptions read in depth."""
    now = now or time.time()
    age = {r[0]: r[1] for r in c.execute("SELECT src, fetched_at FROM related_src")}
    done = 0

    def stale(key, days):
        return age.get(key, 0) < now - days * DAY
    for (vid,) in c.execute("SELECT video_id FROM ratings WHERE profile_id=? AND val>0 ORDER BY at DESC LIMIT 30",
                            (pid,)).fetchall():
        if done < n and stale(vid, 3):
            try:
                fetch_related(c, vid, max_age=0)
                done += 1
            except Exception as ex:
                print("[discover] liked", vid, repr(ex))
    t = Taste(c, pid, now)
    have = {r[0]: r[1] for r in c.execute("SELECT q, auto FROM searches WHERE profile_id=?", (pid,))}
    for q in t.queries(2, skip={q for q, auto in have.items() if not auto}):
        c.execute("INSERT INTO searches(profile_id,q,at,n,auto) VALUES(?,?,?,1,1) ON CONFLICT DO NOTHING", (pid, q, int(now)))
    redo = [r[0] for r in c.execute("SELECT q FROM searches WHERE profile_id=? ORDER BY auto, at DESC LIMIT 12", (pid,))]
    for q in [q for q in redo if stale("q:" + q, .5)][:n + 2]:
        try:
            save_search(c, pid, q, search(c, q), auto=have.get(q, 1))
            done += 1
        except Exception as ex:
            print("[discover] search", q, repr(ex))
    favs = sorted((cid for (cid,) in c.execute("SELECT channel_id FROM subs WHERE profile_id=?", (pid,))),
                  key=lambda ch: t.chan.get(ch, 0), reverse=True)
    for cid in [ch for ch in favs if stale("c:" + ch, 7)][:2]:   # newest ~30 on the channel page, beyond RSS's 15
        try:
            refresh_channel(c, cid, deep=True)
            c.execute("INSERT OR REPLACE INTO related_src(src,fetched_at) VALUES(?,?)", ("c:" + cid, int(now)))
            done += 1
        except Exception as ex:
            print("[discover] channel", cid, repr(ex))
    return done


def warm_one(vid):
    c = connect()
    try:
        fetch_related(c, vid)
    except Exception as ex:
        print("[warm]", vid, repr(ex))
    finally:
        c.close()


def after_watch(pid, vid):
    """Background, the moment a video counts as watched: make sure its "up next" list is in, so Home can follow
    straight away, and look one step past it for New to you."""
    c = connect()
    try:
        rel = fetch_related(c, vid)
        cached = {r[0] for r in c.execute("SELECT src FROM related_src")}
        for r in [r for r in rel if r["id"] not in cached][:2]:
            fetch_related(c, r["id"])
    except Exception as ex:
        print("[after watch]", vid, repr(ex))
    finally:
        c.close()


def refresh_stale(force=False):
    """Refresh every subscribed channel whose feed is older than REFRESH_MIN."""
    c = connect()
    cutoff = 0 if force else int(time.time()) - REFRESH_MIN * 60
    ids = [r[0] for r in c.execute(
        "SELECT DISTINCT ch.id FROM channels ch JOIN subs s ON s.channel_id=ch.id "
        "WHERE ch.fetched_at < ? OR ?", (cutoff, 1 if force else 0))]
    c.close()

    def one(cid):
        cc = connect()
        try:
            refresh_channel(cc, cid)
        except Exception as ex:
            print(f"[refresh] {cid}: {ex}")
        finally:
            cc.close()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(one, ids))
    return len(ids)


def tidy(c, now=None):
    """Keep the database small: forget old "Up next" lists and videos nobody needs any more.
    Never touches profiles, subscriptions or watch history. Returns how many videos were removed."""
    now = now or int(time.time())
    # stale lists go, except those of each profile's last 25 watches (Home / New to you are built from them)
    stale = ("SELECT src FROM related_src WHERE fetched_at < ? AND src NOT IN (SELECT video_id FROM history h WHERE "
             "(SELECT COUNT(*) FROM history h2 WHERE h2.profile_id=h.profile_id AND h2.watched_at>h.watched_at) < 25)")
    old = now - 30 * 86400
    c.execute(f"DELETE FROM related WHERE src IN ({stale})", (old,))
    c.execute(f"DELETE FROM related_src WHERE src IN ({stale})", (old,))
    c.execute("DELETE FROM shown WHERE first_at < ?", (old,))
    c.execute("DELETE FROM impressions WHERE last_at < ?", (now - 90 * 86400,))
    days = int(SCOUT_CFG["days"]) if SCOUT_CFG["days"].isdigit() else 60
    c.execute("DELETE FROM scout WHERE at < ? AND kept=0", (now - days * 86400,))   # then Scout looks again
    c.execute("DELETE FROM searches WHERE auto=1 AND at < ?", (old,))   # YakTube's own topic searches, once stale   # passed-over picks get another chance after a month
    n = c.execute(
        "DELETE FROM videos WHERE id NOT IN (SELECT video_id FROM history) AND id NOT IN (SELECT video_id FROM later) "
        "AND id NOT IN (SELECT video_id FROM ratings) AND id NOT IN (SELECT video_id FROM outside) "
        "AND id NOT IN (SELECT video_id FROM related) AND id NOT IN (SELECT src FROM related_src) "
        "AND (CASE WHEN channel_id IN (SELECT channel_id FROM subs) THEN published < ? ELSE published < ? END)",
        (now - 365 * 86400, now - 60 * 86400)).rowcount
    c.execute("DELETE FROM channels WHERE id NOT IN (SELECT channel_id FROM subs)")
    c.execute("VACUUM")  # hand the freed space back to the drive
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return n


def refresher_loop():
    last_tidy = last_discover = 0
    while True:
        try:
            refresh_stale()
            if DISCOVER_HOURS and time.time() - last_discover > DISCOVER_HOURS * 3600:   # and once at start-up
                last_discover = time.time()
                try:
                    discover_all()
                except Exception as ex:
                    print("[discover]", ex)
            try:
                refresh_outside()
            except Exception as ex:
                print("[outside shorts]", ex)
            if time.time() - last_tidy > 86400:  # once a day (and once at start-up)
                last_tidy = time.time()
                c = connect()
                try:
                    print(f"[tidy] removed {tidy(c)} old videos; db now {os.path.getsize(DB_PATH) // 1024} KB")
                finally:
                    c.close()
        except Exception as ex:
            print("[refresh loop]", ex)
        time.sleep(60)


# ---------------------------------------------------------------- views
CSS = """
:root{--bg:#f7f6f3;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e4e2dd;--accent:#d9480f;color-scheme:light}
/* dark follows the system unless the header toggle chose one (html data-theme, kept in localStorage) */
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#111;--card:#1c1c1e;--fg:#f2f2f2;--muted:#9a9aa0;--line:#2c2c2e;--accent:#ff6b2c;color-scheme:dark}}
:root[data-theme=dark]{--bg:#111;--card:#1c1c1e;--fg:#f2f2f2;--muted:#9a9aa0;--line:#2c2c2e;--accent:#ff6b2c;color-scheme:dark}
.theme{background:none;border:0;color:var(--fg);font:inherit;cursor:pointer;flex:none}
.theme svg{width:22px;height:22px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round}
.theme .sun,:root[data-theme=dark] .theme .moon{display:none}:root[data-theme=dark] .theme .sun{display:contents}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .theme .moon{display:none}:root:not([data-theme=light]) .theme .sun{display:contents}}
.theme .moon{display:contents}
header .theme{display:none;padding:6px;border-radius:50%;place-items:center}header .theme:hover{background:var(--card)}header .theme small{display:none}
.side .theme{display:flex;align-items:center;gap:20px;width:100%;padding:9px 14px;border-radius:10px;text-align:left;white-space:nowrap}
.side .theme small{font-size:inherit}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:5;display:flex;gap:14px;align-items:center;padding:10px 20px;
  background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.logo{display:inline-flex;align-items:center;gap:.17em;font-weight:800;font-size:20px;letter-spacing:-.6px;white-space:nowrap}
.logo b{color:var(--accent)}.logo svg{width:1.42em;height:1.08em;flex:none}
header .logo{width:150px;flex:none}
.burger{background:none;border:0;color:var(--fg);padding:8px;margin-left:-4px;border-radius:50%;cursor:pointer;display:grid;place-items:center;flex:none}
.burger:hover{background:var(--card)}.burger svg{width:24px;height:24px;fill:currentColor}
form.go{flex:1;max-width:620px;margin:0 auto;display:flex}
nav svg,.rate svg{width:22px;height:22px;flex:none;fill:currentColor}
.rib,.bot,.tvside{display:none}
.shell{display:flex;align-items:flex-start}
.side{position:sticky;top:57px;width:228px;flex:none;max-height:calc(100vh - 57px);overflow-y:auto;padding:10px 10px 20px;scrollbar-width:thin}
.side a{display:flex;align-items:center;gap:20px;padding:9px 14px;border-radius:10px;white-space:nowrap}
.side a:hover,.side .theme:hover{background:var(--card)}.side a.on{background:var(--card);font-weight:650;box-shadow:0 0 0 1px var(--line)}
.side a.on svg{color:var(--accent)}
.side hr{border:0;border-top:1px solid var(--line);margin:10px 6px}
/* the icon rail: by default between 721 and 1100px; the burger flips it (html data-side=mini|full, kept in localStorage) */
@media(min-width:1101px){:root[data-side=mini] header .logo{width:auto}:root[data-side=mini] .side{width:80px;padding:6px 4px}:root[data-side=mini] .side a,:root[data-side=mini] .side .theme{flex-direction:column;gap:5px;padding:13px 2px 10px;font-size:10.5px;text-align:center;white-space:normal;line-height:1.15}:root[data-side=mini] .side a.on{box-shadow:none}:root[data-side=mini] .side hr{margin:8px 12px}}
@media(max-width:1100px) and (min-width:721px){:root:not([data-side=full]) header .logo{width:auto}:root:not([data-side=full]) .side{width:80px;padding:6px 4px}:root:not([data-side=full]) .side a,:root:not([data-side=full]) .side .theme{flex-direction:column;gap:5px;padding:13px 2px 10px;font-size:10.5px;text-align:center;white-space:normal;line-height:1.15}:root:not([data-side=full]) .side a.on{box-shadow:none}:root:not([data-side=full]) .side hr{margin:8px 12px}}
input[type=text]{width:100%;padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--fg);font:inherit}
input[type=text]:focus{outline:2px solid var(--accent);outline-offset:-1px}
.btn{display:inline-block;padding:7px 16px;border-radius:999px;border:0;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.btn.ghost{background:var(--card);color:var(--fg);box-shadow:0 0 0 1px var(--line)}
.btn.small{padding:4px 12px;font-size:13px}
.sw i{display:inline-block;position:relative;width:26px;height:14px;margin-left:8px;vertical-align:-2px;border-radius:9px;background:var(--line);transition:.15s}
.sw i:after{content:"";position:absolute;top:2px;left:2px;width:10px;height:10px;border-radius:50%;background:#fff;transition:.15s}
.sw.on i{background:var(--accent)}.sw.on i:after{left:14px}
.acts{display:flex;gap:8px}
.av{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700;flex:none}
main{flex:1;min-width:0;max-width:1720px;margin:0 auto;padding:22px 24px 60px}
.shelf{grid-column:1/-1;margin:4px 0 6px}
.shelf h2{display:flex;align-items:center;gap:8px;font-size:18px;margin:0 0 12px}.shelf h2 svg{width:22px;height:22px;fill:var(--accent)}
.sgrid{display:grid;grid-template-columns:repeat(4,minmax(0,218px));gap:16px}   /* YouTube's own: 218x326 */
.scard img{width:100%;aspect-ratio:2/3;object-fit:cover;border-radius:12px;background:var(--line);display:block}
.scard h3{font-size:14px;font-weight:600;margin:8px 0 2px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.scard:hover h3{color:var(--accent)}
.rate{display:inline-flex;border-radius:999px;background:var(--card);box-shadow:0 0 0 1px var(--line);overflow:hidden}
.rate button{display:flex;align-items:center;padding:4px 14px;border:0;background:none;color:var(--muted);cursor:pointer}
.rate button+button{border-left:1px solid var(--line)}.rate button:hover{color:var(--fg)}
.rate svg{width:19px;height:19px}.rate .dn svg{transform:rotate(180deg)}.rate button.on{color:var(--accent)}
h1{font-size:22px;margin:0 0 18px;letter-spacing:-.3px}
.bar{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}.bar h1{margin:0;margin-right:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:24px 16px}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:var(--line);display:block}
.card h3{font-size:14.5px;font-weight:600;margin:8px 0 2px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card:hover h3{color:var(--accent)}
[data-ch]:hover,a.ch:hover{color:var(--fg);text-decoration:underline}
.th{position:relative;display:block}
.dur{position:absolute;right:6px;bottom:6px;padding:1px 5px;border-radius:4px;background:rgba(0,0,0,.8);color:#fff;
  font-size:12px;font-weight:600;line-height:1.45;pointer-events:none}
.note{background:var(--card);box-shadow:0 0 0 1px var(--line);padding:10px 14px;border-radius:10px;margin-bottom:16px}
.imp{margin:-6px 0 18px}.imp summary{cursor:pointer}.imp form{max-width:560px;margin-top:10px}
.imp textarea{display:block;width:100%;margin:10px 0;padding:10px 14px;border:1px solid var(--line);border-radius:12px;
  background:var(--card);color:var(--fg);font:13px/1.4 ui-monospace,Menlo,monospace}
.meta{color:var(--muted);font-size:13px}
.seen img{opacity:.45}
.empty{padding:60px 20px;text-align:center;color:var(--muted)}.empty p{max-width:460px;margin:8px auto 18px}
.watch{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:28px}
@media(max-width:1050px){.watch{grid-template-columns:1fr}}
.player{width:100%;aspect-ratio:16/9;border:0;border-radius:12px;background:#000;display:block}
.pw{position:relative}
.watch h1{margin:14px 0 10px;font-size:20px}
.row{display:flex;gap:10px;margin-bottom:12px}.row img{width:168px;aspect-ratio:16/9;object-fit:cover;border-radius:8px;flex:none;background:var(--line)}
.row h3{font-size:14px;margin:0 0 3px;font-weight:600;line-height:1.3}
/* lists (Channels, History): like the TV's, each row a rounded box, with a thin outline always showing (a phone has no
   pointer to highlight them); the row under the pointer or holding the keyboard focus fills in */
.list{display:flex;flex-direction:column;gap:8px}
.list>div{display:flex;align-items:center;gap:12px;padding:10px 14px;border-radius:14px;box-shadow:0 0 0 1px var(--line);transition:background .12s}
.list>div:hover,.list>div:focus-within{background:var(--card)}
.list .grow{flex:1;min-width:0}
.err{background:#fff0e6;color:#a33a00;padding:10px 14px;border-radius:10px;margin-bottom:16px}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]) .err{background:#3a1f10;color:#ffb38a}}
:root[data-theme=dark] .err{background:#3a1f10;color:#ffb38a}
.profiles{display:flex;gap:28px;justify-content:center;flex-wrap:wrap;margin:40px 0}
.profiles button{background:none;border:0;color:var(--fg);font:inherit;cursor:pointer;text-align:center}
.profiles .av{width:110px;height:110px;font-size:44px;border-radius:24px;margin-bottom:10px;transition:transform .12s}
.profiles button:hover .av,.profiles a.tile:hover .av{transform:scale(1.05)}
.av .emo{font-style:normal;font-size:1.15em;line-height:1}
.profiles a.tile{display:block;color:var(--fg);text-align:center;border-radius:24px}
.profiles .tile .av{position:relative}
.profiles .pen{position:absolute;right:-8px;bottom:-8px;width:38px;height:38px;border-radius:50%;background:var(--fg);box-shadow:0 0 0 3px var(--bg);display:grid;place-items:center}
.profiles .pen svg{width:20px;height:20px;stroke:var(--bg)!important;fill:none;stroke:#fff;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.profiles .mv{display:flex;justify-content:center;gap:10px;margin-top:6px}
.profiles .mv button{width:40px;height:32px;border-radius:999px;background:var(--line);font-size:14px}
.profiles .mv button.no{visibility:hidden}
.pedit{max-width:460px;margin:30px auto}
.pedit .top{display:flex;gap:16px;align-items:center;margin-bottom:22px}
.pedit .top .av{width:84px;height:84px;font-size:36px;border-radius:20px}
.pedit .top input{flex:1;font-size:18px}
.pedit h3{font-size:14px;color:var(--muted);margin:18px 0 8px;font-weight:600}
.pick{display:flex;flex-wrap:wrap;gap:8px}
.pick button{border:0;cursor:pointer;font:inherit;display:grid;place-items:center}
.pick.cols button{width:38px;height:38px;border-radius:50%}
.pick.faces button{width:46px;height:46px;border-radius:12px;background:var(--line);font-size:24px;color:var(--fg)}
.pick button.on{box-shadow:0 0 0 3px var(--bg),0 0 0 5px var(--fg)}
.pedit .acts{display:flex;gap:14px;align-items:center;margin-top:26px}
.pedit .rm{display:block;margin-top:36px;color:#c92a2a}
.center{text-align:center}
.pback{position:fixed;top:max(16px,env(safe-area-inset-top));left:max(16px,env(safe-area-inset-left));display:inline-flex;align-items:center;
  padding:7px 16px;border-radius:999px;background:var(--card);box-shadow:0 0 0 1px var(--line);font-weight:600}.pback:hover{color:var(--accent)}@media(max-width:720px){.pback+.center>.logo{margin-top:68px!important}}   /* room for Back on a phone */
.why{color:var(--accent);font-size:12px;margin-top:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media(max-width:720px){header{flex-wrap:wrap;padding:10px 16px;row-gap:10px}header>.av,.side,.burger{display:none}header .theme{display:grid;margin-left:auto}
  header .logo{width:auto;padding:0}
  .rib{display:flex;gap:8px;order:2;flex-basis:calc(100% + 32px);margin:0 -16px;padding:0 16px;overflow-x:auto;scrollbar-width:none}
  .rib::-webkit-scrollbar{display:none}
  /* the chips along the top, like the YouTube app's: flat grey, slightly rounded, medium-weight text */
  .rib a{display:flex;align-items:center;gap:6px;height:32px;padding:0 12px;border-radius:8px;
    background:color-mix(in srgb,var(--fg) 9%,var(--bg));font-size:15px;font-weight:500;white-space:nowrap}
  .rib a.on{background:var(--fg);color:var(--bg);box-shadow:none}
  .rib .av{width:20px;height:20px;font-size:11px}
  form.go{order:3;flex-basis:100%;max-width:none}
  main{padding:16px 16px calc(80px + env(safe-area-inset-bottom))}
  .bot{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:6;padding-bottom:env(safe-area-inset-bottom);
    background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(10px);border-top:1px solid var(--line)}
  .bot a{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:6px 0 5px;font-size:11px;font-weight:700;color:var(--fg)}
  .bot a svg{width:34px;height:34px;padding:7px;border-radius:50%;box-shadow:0 0 0 2px currentColor;   /* bold icon in a ring */
    stroke:currentColor;stroke-width:.9;stroke-linejoin:round;overflow:visible}
  .bot a.on{font-weight:800}.bot a.on svg{color:var(--accent);background:color-mix(in srgb,var(--accent) 14%,transparent)}
  .grid{grid-template-columns:1fr 1fr;gap:16px 10px}.row img{width:130px}
  .sgrid{grid-template-columns:1fr 1fr;gap:12px 10px}}
@media(max-width:440px){.grid{grid-template-columns:1fr}}
/* Home on a computer's screen: the TV's rows (see home_rows), moved along with the mouse: arrows at the ends, or a
   sideways scroll. A phone keeps the grid. A card's reason shows in Recommended for you and More for you (elsewhere the
   row's title says it); the picture gets the TV's ring on hover. */
.hrows{display:none}
@media(min-width:721px){.hgrid{display:none}.hrows{display:block;margin-top:-6px}
  .hrows .trow{position:relative;margin:0 0 6px}.hrows .trow h2{font-size:19px;font-weight:600;margin:0}
  .hrows .strip{display:flex;gap:16px;overflow-x:auto;scrollbar-width:none;scroll-behavior:smooth;padding:12px 8px 16px;margin:0 -8px}
  .hrows .strip::-webkit-scrollbar{display:none}
  .hrows .card{flex:0 0 max(250px,calc((100% - 48px) / 3.4));min-width:0}
  .hrows .scard{flex:0 0 max(150px,calc((100% - 96px) / 6.6));min-width:0}
  .hrows .scard img{aspect-ratio:9/16}
  .hrows .card h3,.hrows .scard h3{font-size:16px;font-weight:500;height:2.6em;margin:10px 0 2px}
  .hrows .meta,.hrows .why{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .hrows .why{display:none;font-size:13px}.trow[data-row=rec] .why,.trow[data-row=more] .why{display:block}
  .hrows .strip>a img{transition:box-shadow .12s}
  .hrows .strip>a:hover img,.hrows .strip>a:focus-visible img{box-shadow:0 0 0 3px var(--bg),0 0 0 6px var(--fg)}
  .hrows .strip>a:focus-visible{outline:none}.hrows .strip>a:hover h3{color:var(--fg)}
  .rarr{position:absolute;z-index:2;top:40%;width:44px;height:44px;border-radius:50%;border:0;
    display:grid;place-items:center;background:var(--card);color:var(--fg);box-shadow:0 0 0 1px var(--line),0 4px 14px rgba(0,0,0,.35);
    cursor:pointer;opacity:0;transition:opacity .15s}
  .rarr svg{width:24px;height:24px;fill:currentColor}.rarr.l{left:-14px}.rarr.r{right:-14px}.rarr.l svg{transform:rotate(180deg)}
  .hrows .trow:hover .rarr:not([hidden]){opacity:1}.rarr:hover{background:var(--line)}.rarr[hidden]{display:none}}
/* Settings, like the YouTube TV app's: names down the left in groups; the selected one centred on the right with a big
   title, one short line and wide round buttons (a phone puts them one under the other) */
.sets{display:grid;grid-template-columns:minmax(0,280px) minmax(0,1fr);gap:24px;align-items:start;max-width:1040px}
.slist{display:flex;flex-direction:column;gap:2px;padding-right:12px;border-right:1px solid var(--line)}
.slist h4{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:14px 14px 6px;font-weight:600}
.slist h4:first-child{margin-top:2px}
.srow{display:block;width:100%;text-align:left;border:0;border-radius:10px;padding:9px 14px;background:none;color:var(--fg);font:inherit;font-weight:500;cursor:pointer}
.srow:hover,.srow.sel{background:var(--card)}
.spane{display:flex;flex-direction:column;align-items:center;text-align:center;padding:40px 16px 0;min-width:0}
.spane h2{font-size:26px;line-height:1.15;margin:0 0 10px}.spane>p{margin:0 0 24px;max-width:46ch;color:var(--muted)}
.sopts,.spane .sbtns,.spane .sask{display:flex;flex-direction:column;gap:8px;width:300px;max-width:100%}
.sopt,.spane .btn{display:flex;justify-content:space-between;align-items:center;width:100%;min-height:44px;padding:0 22px;border:0;border-radius:999px;
  background:var(--card);box-shadow:0 0 0 1px var(--line);color:var(--fg);font:inherit;font-weight:500;cursor:pointer;text-align:left}
.spane .btn{justify-content:center}.sopt.on:after{content:"✓";color:var(--accent);font-weight:700}
.sedit{width:300px;max-width:100%}.sedit i{font-style:normal;color:var(--muted)}.sedit span{margin-right:auto;padding-left:.3em}
.spane .sask p{margin:0 0 4px;color:var(--muted)}.spane .sask b{font-weight:500}
.shid{display:flex;align-items:center;gap:12px;width:380px;max-width:100%;padding:8px 0;border-top:1px solid var(--line);text-align:left}
.shid:first-of-type{border-top:0}
.shid .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.shid.back .nm{color:var(--muted);text-decoration:line-through}
.shid .btn{width:auto;min-height:34px;padding:0 16px;font-size:13px}
.sdone{color:#37b24d;font-weight:600}
.sver{display:grid;grid-template-columns:auto auto;gap:12px 44px;margin:4px 0;text-align:left;font-variant-numeric:tabular-nums}
.sver span{color:var(--muted)}.sver b{font-weight:500;overflow-wrap:anywhere}
.sbtns[hidden],.sask[hidden],.sopts[hidden],.sopt[hidden],.spane[hidden]{display:none}.bar .sask{flex-basis:100%}.bar .sask p{margin:0 0 10px}
form.sure button,form.sure button:focus{background:#c92a2a!important;color:#fff!important;box-shadow:none!important}
@media(max-width:720px){.sets{grid-template-columns:1fr}.slist{border:0;padding:0}.spane{padding-top:16px}}
/* Settings > YakScout: a wider page than the others, left-aligned, in sections */
.spane.scp{align-items:stretch;text-align:left;max-width:640px;padding-top:28px}
.scp .sch{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.scp .sch h2{margin:0;flex:1}
.scp .chip{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:999px;background:var(--card);border:1px solid var(--line);font-size:13px;color:var(--muted)}
.scp .chip i{width:8px;height:8px;border-radius:50%;background:#f59f00}.scp .chip.ok i{background:#2f9e44}
.scp .chip.down i{background:#e03131}.scp .chip.off i{background:var(--muted)}
.scp>p{margin:6px 0 20px;color:var(--muted)}
.scp .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:26px}
.scp .stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px}
.scp .stat b{display:block;font-size:24px;line-height:1.1}.scp .stat span{font-size:13px;color:var(--muted)}
.scp h3{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 10px;font-weight:600}
.scp .card{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:26px}
.scp .chk{display:flex;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid var(--line)}
.scp .chk:first-child{border-top:0}.scp .chk>div{flex:1;min-width:0}.scp .chk small{display:block;color:var(--muted);font-size:13px}
.scp .chk .sw{flex:none;width:auto;min-height:34px;padding:0 14px;gap:0}
.scp .addr{flex-wrap:wrap;align-items:flex-end}
.scp .fld{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted);min-width:0}
.scp .fld.wide{flex:1 1 180px}.scp .fld.port{flex:0 0 90px}
.scp .fld input{font:inherit;font-size:15px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:9px 12px;width:100%;box-sizing:border-box}
.scp .fld input:focus{outline:2px solid var(--accent);outline-offset:-1px}
.scp .addr .save{width:auto;flex:none;min-height:40px;padding:0 18px}.scp .addr .msg{flex-basis:100%;font-size:13px;color:var(--muted)}
.scp .addr .msg:empty{display:none}.scp .addr .msg.bad{color:#e03131}
.scp .facts{display:flex;flex-wrap:wrap;gap:4px 16px;font-size:13px;color:var(--muted);padding:10px 16px;border-top:1px solid var(--line)}
.scp .facts b{color:var(--fg);font-weight:500}.scp .facts[hidden]{display:none}
.scp details.fold{margin-bottom:10px;overflow:hidden}.scp details.fold:last-of-type{margin-bottom:26px}
.scp details.fold>summary{list-style:none;display:flex;align-items:center;gap:6px;padding:14px 16px;cursor:pointer;font-weight:500}
.scp details.fold>summary::-webkit-details-marker{display:none}
.scp details.fold>summary .n{color:var(--muted);font-weight:400;flex:1}
.scp details.fold>summary:after{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(45deg);margin:-4px 4px 0 auto}
.scp details.fold[open]>summary:after{transform:rotate(-135deg);margin-top:4px}
.scp details.fold>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px;border-radius:14px}
.scp .tools{display:flex;flex-wrap:wrap;gap:8px;padding:0 16px 12px}
.scp .tools input{flex:1 1 160px;min-width:0;font:inherit;font-size:15px;color:var(--fg);background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px 12px}
.scp .tools input:focus{outline:2px solid var(--accent);outline-offset:-1px}
.scp .tools .btn{width:auto;flex:none;min-height:38px;padding:0 16px}
.scp .tools .msg{flex-basis:100%;font-size:13px;color:var(--muted)}.scp .tools .msg:empty{display:none}.scp .tools .msg.bad{color:#e03131}
.scp .sclist{max-height:340px;overflow:auto;border-top:1px solid var(--line)}
.scp .item{display:flex;align-items:center;gap:12px;padding:10px 16px;border-top:1px solid var(--line)}
.scp .item:first-child{border-top:0}.scp .item[hidden]{display:none}
.scp .item img{width:36px;height:64px;object-fit:cover;border-radius:6px;background:var(--line);flex:none}
.scp .item>div{flex:1;min-width:0}
.scp .item .t{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scp .item small{display:block;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scp .item .btn{width:auto;flex:none;min-height:32px;padding:0 12px;font-size:13px}
.scp .item.back img{opacity:.45}.scp .sdone{font-size:13px;color:var(--muted);flex:none}
.scp .tag{font-size:11px;font-weight:600;padding:1px 7px;border-radius:6px;background:var(--line);color:var(--fg);margin-right:6px;flex:none}
.scp .x{flex:none;border:0;background:none;color:var(--muted);font-size:22px;line-height:1;cursor:pointer;padding:4px 8px;border-radius:8px}
.scp .x:hover,.scp .x:focus-visible{color:var(--fg);background:var(--line)}
.scp .empty{padding:12px 16px;color:var(--muted);font-size:13px}
.scp .item.sub small{white-space:normal}.scp .item.sub .t{white-space:normal;font-weight:500}
.scp .slider{display:flex;align-items:center;gap:10px;flex:none}.scp .slider input{width:140px;accent-color:var(--accent)}
.scp .slider b{min-width:2ch;text-align:right}
.scp select.btn{width:auto;flex:none;min-height:34px;padding:0 10px}.scp a.btn,.scp label.btn{width:auto;flex:none;min-height:34px;padding:0 14px;cursor:pointer}
.scp .chk .msg{display:block;font-size:13px;color:var(--muted)}.scp .chk .msg:empty{display:none}.scp .chk .msg.bad{color:#e03131}
.scp .hint{font-size:13px;color:var(--muted);margin:-14px 0 26px}.scp h3+.hint{margin-top:0}.scp code{font-size:12px}
"""

# Extra styles when the page is shown by the YakTube TV app (its browser says "YakTubeTV").
TV_CSS = """<style>
:root,:root[data-theme],:root:not([data-theme=light]){--bg:#111;--card:#1c1c1e;--fg:#f2f2f2;--muted:#9a9aa0;--line:#2c2c2e;--accent:#ff6b2c}
body{font-size:17px}main{padding:18px 28px 40px 104px;max-width:none}
header,.side,.rib,.bot{display:none!important}.shell{display:block}
/* the rail: icons down the left; it widens with names when the remote moves into it. Sized like the YouTube app on
   Google TV: about 80 wide closed and 240 open, names at 15px and the profile name at 17px, rows 40 apart with
   22px icons (a selected row's highlight fills it). Icons and pictures sit centred on the closed rail. */
.tvside{display:flex;flex-direction:column;gap:2px;position:fixed;left:0;top:0;bottom:0;z-index:20;width:80px;padding:28px 12px;
  overflow:hidden;background:linear-gradient(90deg,#0a0a0a 70%,rgba(10,10,10,.92));transition:width .15s}
.tvside:focus-within{width:240px;box-shadow:30px 0 80px rgba(0,0,0,.7)}
.tvside a{display:flex;align-items:center;gap:14px;min-height:38px;padding:0 17px;border-radius:999px;white-space:nowrap;color:#aaa;flex:none;font-size:15px;font-weight:500}
.tvside a span{min-width:0;overflow:hidden;text-overflow:ellipsis}
.tvside a svg{width:22px;height:22px;flex:none}.tvside a.on{color:#fff}.tvside a.on svg{color:var(--accent)}
.tvside a span,.tvside h4{opacity:0;transition:opacity .12s}.tvside:focus-within a span,.tvside:focus-within h4{opacity:1}
.tvside a:focus{background:#f2f2f2;color:#111;outline:none!important}.tvside a:focus svg{color:#111}
.tvside hr{border:0;border-top:1px solid #2a2a2a;margin:5px 13px;flex:none}.tvside .gap{display:block;height:8px;flex:none}
.tvside h4{margin:2px 13px 4px;font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#777;white-space:nowrap}
.tvside .av{width:22px;height:22px;font-size:11px;font-style:normal;flex:none}.tvside .me .av{width:30px;height:30px;font-size:14px}
.tvside .me{min-height:48px;margin-bottom:6px;padding:0 13px;gap:10px}.tvside .me span{line-height:1.2;font-size:17px}.tvside .me small{display:block;font-size:12px;color:#888}
.tvside a.me:focus small{color:#555}
.bare .tvside{display:none}#rt #sr{display:none!important}   /* Shorts: no Share button on the TV (beats Shorts' own button style) */.bare main{padding:0}
.go.big{max-width:720px;margin:12vh auto 0}.go.big input{font-size:26px;padding:16px 26px}
/* Home: rows you move along with Left / Right, the focused video's details above them */
/* Home's top line, like the YouTube TV app's: a wide grey search pill on the left (it opens Search), the logo on the right */
.tvhead{display:flex;align-items:center;justify-content:space-between;margin:4px 0 14px 8px}
.tvsearch{display:flex;align-items:center;gap:12px;width:calc(100vw * 728 / 1920);height:38px;padding:0 18px;border-radius:999px;
  background:#8c8c8c;color:#1a1a1a;font-size:18px;font-weight:500}.tvsearch svg{width:20px;height:20px;fill:currentColor}
.tvsearch:focus{background:#f2f2f2;outline:none!important}
.tvhead .logo{font-size:24px;color:#f2f2f2}.tvhead .logo b{color:#ff6b2c}
.trow{margin:0 0 2px;scroll-margin-top:6px}.trow h2{font-size:17px;margin:0 0 0 8px;font-weight:500;color:#f1f1f1}   /* like the YouTube TV app's: 17px, medium, near-white */
/* each row has room around it, so the selected card's border and zoom aren't cut off */
.strip{position:relative;display:flex;gap:18px;overflow-x:auto;scrollbar-width:none;padding:26px 30px 30px;margin:0 -22px}
.strip::-webkit-scrollbar{display:none}
/* Every row the same: video tiles 532 x 299 on a 1080p screen (between YouTube's 426 and its featured 640); Shorts tall
   (9:16). Sizes follow the screen width, so a 4K TV gets the same layout, twice as sharp. */
.strip .card{flex:0 0 calc(100vw * 532 / 1920)}
.strip .scard{flex:0 0 calc(100vw * 240 / 1920)}
.strip>a{min-width:0}   /* every card exactly that wide, however long its text */
/* selected, like the YouTube TV app: a white ring round the picture only (with a thin dark gap), and the title turns white;
   the tile also gets 10% wider, pushing the next ones along (same height: see below) */
.strip>a{position:relative;transition:flex-basis .15s}
.strip>a:focus{transform:none!important;outline:none!important;box-shadow:none}
.strip>a:focus img{box-shadow:0 0 0 3px #111,0 0 0 6px #fff}
.strip .card:focus{flex-basis:calc(100vw * 532 * 1.1 / 1920)}
.strip .scard:focus{flex-basis:calc(100vw * 240 * 1.1 / 1920)}
.strip .dur{font-size:11px;padding:0 5px}
.strip .scard img{aspect-ratio:9/16;border-radius:10px}
/* every picture keeps one height, so a selected tile only gets wider (its picture trimmed a little top and bottom) and
   the rows below never move */
.strip .card img{aspect-ratio:auto;height:calc(100vw * 532 / 1920 * 9 / 16)}.strip .scard img{aspect-ratio:auto;height:calc(100vw * 240 / 1920 * 16 / 9)}
/* the details sit under each picture, always the same height, so nothing moves when the selection changes */
.strip .card h3,.strip .scard h3{-webkit-line-clamp:2;height:2.6em;line-height:1.3;font-size:18px;font-weight:500;margin:10px 0 2px;color:#aaa}   /* YouTube TV: titles 18, details 14 */
.strip .card .meta,.strip .scard .meta,.strip .why{display:block;font-size:14px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#999}
.strip .why{visibility:hidden;color:var(--accent)}.strip .card:focus .why{visibility:visible}
.strip .card:focus h3,.strip .scard:focus h3{color:#fff}.strip>a:focus .meta{color:#bbb}
.scard{transition:transform .1s}.scard:focus{transform:scale(1.06)}.scard:focus h3{color:var(--accent)}
a:focus,button:focus,input:focus,iframe:focus{outline:4px solid #fff!important;outline-offset:3px;border-radius:10px}
/* Channels and History lists: the whole row is the selection, a rounded box with a white ring (drawn with box-shadow,
   as some TV browsers and older Safari draw outlines square). Its picture and title both open it, so they need no ring
   of their own; the button on the right (✕, Unsubscribe) turns white when it's the one selected. */
.list{background:none;box-shadow:none;gap:2px}.list>div{border-top:0;border-radius:14px;padding:10px 14px;box-shadow:none;background:none}
.list>div:focus-within{background:#262628;box-shadow:0 0 0 3px #fff}
.list a:focus{outline:none!important;box-shadow:none!important}
.list button:focus{outline:none!important;box-shadow:none!important;background:#f2f2f2!important;color:#111!important;border-radius:999px}
.card{transition:transform .1s}.card:focus{transform:scale(1.05)}.card:focus h3{color:var(--accent)}
.grid{grid-template-columns:repeat(4,1fr)}
a[target=_blank],.imp{display:none}
.watch{grid-template-columns:1fr}.player{aspect-ratio:auto;height:100vh;overflow:hidden}.tvp{position:relative}
/* The player's controls, laid out like the YouTube app on a TV: title and channel top left; the times over the progress
   bar; under it Subscribe on the left, back 10s / play / forward 10s in the middle, and thumbs, Watch later and ⚙ on
   the right. Round dark buttons; the selected one turns white. */
.tvbar{position:absolute;left:0;right:0;bottom:0;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;row-gap:16px;
  padding:80px 92px 30px;border-radius:0 0 12px 12px;background:linear-gradient(transparent,rgba(0,0,0,.7));transition:opacity .5s}.tvbar.hide{opacity:0}
.tvsk{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;row-gap:6px;font-size:13px;font-weight:600;color:#eee;text-shadow:0 1px 2px #000}
#t1{grid-row:1;grid-column:2;justify-self:end}#sk{grid-column:1/-1}
#sk{height:18px;display:flex;align-items:center;border-radius:9px;outline:none!important}#sk i{position:relative;display:block;width:100%;height:4px;border-radius:2px;background:rgba(255,255,255,.3)}
#sk b{position:absolute;left:0;top:0;bottom:0;border-radius:2px;background:#f03}
#sk b:after{content:"";position:absolute;right:-8px;top:50%;width:16px;height:16px;margin-top:-8px;border-radius:50%;background:#f03;transform:scale(0);transition:transform .12s}
#sk:focus i{height:8px}#sk:focus b:after{transform:scale(1);box-shadow:0 0 0 4px rgba(255,255,255,.85)}
.tvl,.tvc,.tvr{display:flex;align-items:center;gap:10px}.tvl{justify-self:start}.tvc{gap:14px}.tvr{justify-self:end}
.tvtop{position:absolute;left:0;right:0;top:0;padding:30px 92px 70px;background:linear-gradient(rgba(0,0,0,.7),transparent);transition:opacity .5s;pointer-events:none}
.tvtop b{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:22px;line-height:1.25;max-width:72%;text-shadow:0 1px 3px #000}
.tvtop span{display:block;margin-top:6px;font-size:13px;color:#ddd;text-shadow:0 1px 2px #000}
.tvbar.hide~.tvtop{opacity:0}
.tvbar button,.tvbar button.btn,.tvnx button{font:inherit;font-size:14px;font-weight:600;height:36px;padding:0 18px;border-radius:999px;border:0;box-shadow:none;
  background:rgba(50,50,50,.85);color:#fff;display:inline-grid;place-items:center;grid-auto-flow:column;gap:6px}
.tvbar button:focus,.tvbar button.btn:focus,.tvnx button:focus{background:#f2f2f2;color:#111;outline:none!important}
.tvbar svg{width:20px;height:20px;fill:currentColor}
.tvc button,#gs{width:36px;padding:0}#pp{width:46px;height:46px}#pp svg{width:24px;height:24px}
.tvbar form{display:inline}
/* Settings on the TV: dark round buttons; the selected setting and button turn white, like everywhere else */
.slist{border-color:#262626}.srow{font-size:16px}.srow.sel{background:#333}.spane h2{font-size:30px}.spane>p{color:#aaa}
.sopt,.spane .btn{background:#2c2c2e;box-shadow:none;color:#f2f2f2}
.srow:focus,.sopt:focus,.spane .btn:focus{background:#f2f2f2!important;color:#111!important;outline:none!important}.sopt:focus i{color:#111}
.tvbar .rate{display:flex;background:rgba(50,50,50,.85);border-radius:999px;box-shadow:none;overflow:hidden}
.tvbar .rate button{width:44px;padding:0;border-radius:999px;background:none}.tvbar .rate button.on{color:var(--accent)}
.tvbar .rate button:focus{background:#f2f2f2;color:#111}.tvbar .rate button.on:focus{color:var(--accent)}
.tvgm{position:absolute;right:92px;bottom:84px;display:flex;flex-direction:column;gap:6px;min-width:230px;padding:12px;border-radius:16px;
  background:rgba(22,22,22,.96);box-shadow:0 0 0 1px var(--line),0 12px 40px rgba(0,0,0,.6)}.tvgm[hidden]{display:none}
.tvbar .tvgm button{display:block;width:auto;height:auto;text-align:left;font-size:15px;padding:9px 16px;border-radius:10px;background:none}
.tvbar .tvgm button:focus{background:var(--fg);color:var(--bg)}.tvgm .meta{padding:4px 16px 0;font-size:12px;color:#aaa}
#gs{position:relative}#gs.dot:after{content:"";position:absolute;top:2px;right:2px;width:8px;height:8px;border-radius:50%;background:var(--accent)}
.tvnx{position:absolute;right:92px;bottom:150px;max-width:440px;padding:16px 18px;border-radius:14px;background:rgba(20,20,20,.94);box-shadow:0 0 0 1px var(--line)}
.tvnx>b{display:block;margin:4px 0 12px;font-size:16px;line-height:1.3}.tvnx button{display:inline-grid;margin-right:8px}
/* the channel: its letter in a circle on the left of the controls; OK opens a panel on the right (like the YouTube app's)
   with Subscribe, Go to channel and Don't recommend channel */
.tvbar #chb{width:42px;height:42px;padding:0;border-radius:50%;background:none}.tvbar #chb .av{width:36px;height:36px;font-size:16px;font-style:normal}
.tvbar #chb:focus{background:#f2f2f2}
.tvcp{position:fixed;top:24px;right:24px;bottom:24px;width:300px;z-index:30;display:flex;flex-direction:column;align-items:center;gap:8px;
  padding:34px 18px;border-radius:20px;background:#1c1c1e;box-shadow:0 12px 40px rgba(0,0,0,.6)}.tvcp[hidden]{display:none}
.tvcp>.av{width:120px;height:120px;font-size:54px;font-style:normal;margin-bottom:12px}
.tvcp>b{font-size:18px;line-height:1.3;text-align:center;margin-bottom:14px}
.tvcp form{display:block;width:100%}
.tvbar .tvcp button,.tvbar .tvcp button.btn,.tvbar .tvcp a{display:block;width:100%;height:auto;padding:12px 16px;border-radius:10px;text-align:left;
  font-size:15px;font-weight:600;line-height:1.3;background:#2c2c2e;color:#fff;box-shadow:none}
.tvbar .tvcp button:focus,.tvbar .tvcp button.btn:focus,.tvbar .tvcp a:focus{background:#f2f2f2;color:#111;outline:none!important}
/* hold OK on a video: a menu of things to do with it, over a dimmed screen */
.tvlm{position:fixed;inset:0;z-index:50;display:grid;place-items:center;background:rgba(0,0,0,.6)}
.tvlm>div{display:flex;flex-direction:column;gap:4px;width:420px;padding:14px;border-radius:18px;background:#1c1c1e;
  box-shadow:0 0 0 1px var(--line),0 16px 50px rgba(0,0,0,.7)}
.tvlm b{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;padding:4px 18px 10px;font-size:15px;
  line-height:1.3;font-weight:600;color:#aaa}
.tvlm button{text-align:left;font:inherit;font-size:18px;padding:11px 18px;border-radius:12px;border:0;background:none;color:var(--fg)}
.tvlm button:focus{background:var(--fg);color:var(--bg);outline:none!important}
.tvtoast{position:fixed;left:50%;bottom:48px;transform:translateX(-50%);z-index:51;padding:10px 22px;border-radius:999px;
  background:#1c1c1e;color:#fff;font-size:16px;box-shadow:0 0 0 1px var(--line),0 8px 24px rgba(0,0,0,.6)}

/* Shorts, laid out like the YouTube app on a TV: the Short in the middle with rounded corners, its title and channel
   in a column to the right, the thumbs as plain icons stacked near the bottom, and a "Press down" hint top right
   (in place of the ▲ ▼ buttons). "#sh" in front of each so these beat the Shorts page's own styles. */
#sh #pl{border-radius:16px;overflow:hidden;box-shadow:0 0 0 3px #3a3a3c}
#sh #info,#sh #rt{left:calc(50% + 28.125vh + 44px);right:32px;width:auto;transform:none}
#sh #info{top:43%;bottom:auto;padding:0;background:none}
#sh #info b{font-size:18px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
#sh #info span{display:block;margin-top:10px;font-size:15px;opacity:1;color:#ddd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#sh #rt{top:auto;bottom:8%;gap:8px;margin-left:-11px}
#sh #rt button{width:46px;height:46px;background:none;color:#fff}#sh #rt svg{width:24px;height:24px}
#sh #rt button.on{background:none;color:var(--accent)}
#sh #rt button:focus{background:#f2f2f2;color:#111;outline:none!important}#sh #rt button.on:focus{color:var(--accent)}
#sh #ud{display:none}
#sh #hint{display:flex;position:absolute;top:32px;right:32px;align-items:center;gap:14px;padding:14px 20px 14px 16px;border-radius:12px;
  background:#1f1f21;font-size:15px;font-weight:600;z-index:2;pointer-events:none}
#sh #hint svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2.4;stroke-linecap:round;stroke-linejoin:round}
</style>"""
# Both players call the function yakTrack() returns on every update from YouTube. A video goes into History once it
# has really played for 10 seconds (not when its page opens); after that the position is saved every 15 seconds,
# on pause and on leaving the page, so the video can pick up where you left off.
TRACK_JS = """<script>function yakTrack(V){var seen=0,acc=0,prev=0,sent=-99,T=0,D=0;
function send(n){sent=T;try{fetch("/seen",{method:"POST",keepalive:true,headers:{"Content-Type":"application/x-www-form-urlencoded"},
body:"v="+V+"&n="+n+"&t="+Math.floor(T)+"&d="+Math.floor(D)})}catch(x){}}
addEventListener("pagehide",function(){if(seen&&T!=sent)send(0)});
return function(t,d,playing){var now=Date.now();if(playing&&prev)acc+=Math.min(now-prev,2000);prev=playing?now:0;
if(typeof t=="number")T=t;if(d)D=d;
if(!seen){if(acc>=Math.min(10000,(D||60)*400)){seen=1;send(1)}}
else if(Math.abs(T-sent)>=15||(!playing&&Math.abs(T-sent)>=1))send(0)}}</script>"""
# TV player: the YouTube player never takes the remote's focus (it lives in a shadow root the remote
# navigation can't see). Our own buttons drive it through YouTube's embed commands (enablejsapi).
TV_PLAYER = """<div class="tvp"><div id="pl" class="player"></div>
<div class="tvbar"><div class="tvsk"><span id="t0">0:00</span><div id="sk" tabindex="0" role="slider" aria-label="Seek"><i><b></b></i></div><span id="t1"></span></div>
<div class="tvl">__CHAN__</div>
<div class="tvc"><button data-k="-10" aria-label="Back 10 seconds"><svg viewBox="0 0 24 24"><path d="M12 5V2L7 6l5 4V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z"/><text x="12" y="16" font-size="7" font-weight="700" text-anchor="middle">10</text></svg></button>
<button id="pp" aria-label="Pause"><svg viewBox="0 0 24 24"><path d="M6 5h4v14H6zm8 0h4v14h-4z"/></svg></button>
<button data-k="10" aria-label="Forward 10 seconds"><svg viewBox="0 0 24 24"><path d="M12 5V2l5 4-5 4V7a6 6 0 1 0 6 6h2a8 8 0 1 1-8-8z"/><text x="12" y="16" font-size="7" font-weight="700" text-anchor="middle">10</text></svg></button></div>
<div class="tvr">__ACTS__<button id="gs" aria-haspopup="menu" aria-expanded="false" aria-label="Settings"><svg viewBox="0 0 24 24"><path fill-rule="evenodd" d="M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.6 7.6 0 0 0-1.7-1L15 3.3h-4l-.4 2.6a7.6 7.6 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.6 7.6 0 0 0 1.7 1l.4 2.6h4l.4-2.6a7.6 7.6 0 0 0 1.7-1l2.5 1 2-3.5zM12 15.3a3.3 3.3 0 1 1 0-6.6 3.3 3.3 0 0 1 0 6.6z"/></svg></button>
<div id="gm" class="tvgm" role="menu" hidden><button id="cc">Captions off</button><button id="sp">Speed 1×</button><button id="ql">Quality Auto</button>
<span id="tm" class="meta"></span></div></div></div>
<div class="tvtop">__TOP__</div>
<div id="nx" class="tvnx" hidden><span class="meta">Up next in <span id="nxn">10</span></span><b id="nxt"></b>
<button id="nxg">Play now</button><button id="nxc">Cancel</button></div></div>
<script>(function(){var V="__VID__",st={t:0,d:0,s:-1},cc=false,rates=[1,1.25,1.5,2,0.75],ri=0,$=function(i){return document.getElementById(i)};
// Quality: YouTube picks the picture quality from the player's size and there is no command to change it, so
// "720p" / "480p" lay the player out smaller and stretch it back to full screen (for TVs that play the sound
// of a 4K or 60-frames-a-second video but show no picture). The choice is remembered on this TV.
var qw=[0,1280,854],qn=["Auto","720p","480p"],qi=0,t0=__T0__,back=0,tr=yakTrack(V);
try{qi=+localStorage.yakq||0;if(!qn[qi])qi=0;var o=(sessionStorage.yakt||"").split(":");if(o[0]==V){t0=+o[1]||0;back=1}sessionStorage.removeItem("yakt")}catch(x){}
var f=document.createElement("iframe");f.allow="autoplay; encrypted-media; fullscreen";f.referrerPolicy="strict-origin-when-cross-origin";
f.style.cssText="width:100%;height:100%;border:0;display:block;background:#000;transform-origin:0 0";
$("pl").attachShadow({mode:"open"}).appendChild(f);
function fit(){var w=$("pl").clientWidth,h=$("pl").clientHeight,k=qw[qi]?w*(window.devicePixelRatio||1)/qw[qi]:1;
if(k>1){f.style.width=w/k+"px";f.style.height=h/k+"px";f.style.transform="scale("+k+")"}else{f.style.width=f.style.height="100%";f.style.transform=""}}
fit();addEventListener("resize",fit);addEventListener("load",fit);
f.src="https://www.youtube-nocookie.com/embed/"+V+"?autoplay=1&rel=0&modestbranding=1&controls=0&enablejsapi=1"+(t0?"&start="+t0:"")+"&origin="+encodeURIComponent(location.origin);
$("ql").textContent="Quality "+qn[qi];
$("ql").onclick=function(){qi=(qi+1)%qn.length;try{localStorage.yakq=qi;sessionStorage.yakt=V+":"+Math.floor(st.t)}catch(x){}location.reload()};  // reload, carry on from the same spot
var QL={tiny:"144p",small:"240p",medium:"360p",large:"480p",hd720:"720p",hd1080:"1080p",hd1440:"1440p",hd2160:"4K",hd2880:"5K",highres:"8K"};
function cmd(fn,a){f.contentWindow.postMessage(JSON.stringify({event:"command",func:fn,args:a||[],id:1,channel:"widget"}),"*")}
var hello=setInterval(function(){f.contentWindow&&f.contentWindow.postMessage(JSON.stringify({event:"listening",id:1,channel:"widget"}),"*")},400);
function tf(x){x=Math.max(0,Math.floor(x));var h=Math.floor(x/3600),m=Math.floor(x/60)%60,s=("0"+x%60).slice(-2);return(h?h+":"+("0"+m).slice(-2):m)+":"+s}
function upd(){var on=st.s==1||st.s==3,pp=$("pp");if(pp.dataset.on!=String(on)){pp.dataset.on=on;pp.setAttribute("aria-label",on?"Pause":"Play");
pp.innerHTML='<svg viewBox="0 0 24 24"><path d="'+(on?"M6 5h4v14H6zm8 0h4v14h-4z":"M8 5v14l11-7z")+'"/></svg>'}$("tm").textContent=st.q?"playing "+(QL[st.q]||st.q):"";
$("t0").textContent=tf(st.t);$("t1").textContent=st.d?tf(st.d):"";st.at=Date.now()}
// Progress bar: moves smoothly between the player's reports. Select it with the remote and Left / Right jump 10s
// (hold to keep going), like the YouTube app on a TV. Its keys are handled with the Settings menu's, below.
(function draw(){var t=st.t+(st.s==1&&st.at?(Date.now()-st.at)/1000:0);$("sk").firstChild.firstChild.style.width=(st.d?Math.min(100,t/st.d*100):0)+"%";requestAnimationFrame(draw)})();
addEventListener("message",function(e){if(!/youtube/.test(e.origin))return;var m;try{m=JSON.parse(e.data)}catch(x){return}if(m.event=="onReady"||m.event=="initialDelivery")clearInterval(hello);
var i=m.info||{},was=st.s;if("currentTime" in i)st.t=i.currentTime;if(i.duration)st.d=i.duration;if("playerState" in i)st.s=i.playerState;
if(i.playbackQuality&&i.playbackQuality!="unknown")st.q=i.playbackQuality;
if(st.s!=was){wake();if(st.s==0)ended();else if(nx)stopnx()}tr(st.t,st.d,st.s==1);
if(!cc&&(m.event=="onReady"||(st.s==1&&!st.off))){ccoff();if(st.s==1){st.off=1;setTimeout(ccoff,1500);setTimeout(ccoff,4000)}}upd()});
function ccoff(){if(!cc)cmd("setOption",["captions","track",{}])}  // off for good: unloadModule came back after every seek
function toggle(){if(st.s==1||st.s==3){cmd("pauseVideo");st.s=2}else{cmd("playVideo");st.s=1}upd()}
function seek(d){st.t=Math.min(Math.max(0,st.t+d),st.d||1e9);cmd("seekTo",[st.t,true]);upd()}
$("pp").onclick=toggle;[].forEach.call(document.querySelectorAll("[data-k]"),function(b){b.onclick=function(){seek(+b.dataset.k)}});
$("cc").onclick=function(){cc=!cc;if(cc){cmd("loadModule",["captions"]);cmd("setOption",["captions","track",{languageCode:"__HL__"}])}else ccoff();this.textContent="Captions "+(cc?"on":"off")};
$("sp").onclick=function(){ri=(ri+1)%rates.length;cmd("setPlaybackRate",[rates[ri]]);this.textContent="Speed "+rates[ri]+"×"};
addEventListener("keydown",function(k){var c=k.key;if(c=="MediaPlayPause"||c=="MediaPlay"||c=="MediaPause"){toggle();k.preventDefault()}
else if(c=="MediaFastForward"){seek(10);k.preventDefault()}else if(c=="MediaRewind"){seek(-10);k.preventDefault()}});
var bar=document.querySelector(".tvbar");[].forEach.call(document.querySelectorAll(".tvbar button,.tvnx button,#sk"),function(b){b.scrollIntoView=function(){}});
function home(){var y=$("pl").getBoundingClientRect().top+scrollY;if(Math.abs(scrollY-y)>2)scrollTo(0,y)}
bar.addEventListener("focusin",home);$("nx").addEventListener("focusin",home);addEventListener("load",home);
// Autoplay (off unless switched on in Settings; remembered on this TV): when a video ends, count down 10s, then play the
// first "Up next" video you haven't seen. Cancel stops it.
var ap=0;try{ap=localStorage.yakap=="1"?1:0}catch(x){}
var nx=0;function stopnx(){clearTimeout(nx);nx=0;$("nx").hidden=true}
function ended(){var a=document.querySelector("#upnext a.row:not(.seen)")||document.querySelector("#upnext a.row"),n=10;if(!ap||!a||nx)return;
$("nxt").textContent=a.querySelector("h3").textContent;$("nx").hidden=false;$("nxg").onclick=function(){location.href=a.href};$("nxg").focus();
(function tick(){$("nxn").textContent=n;if(n--<=0)location.href=a.href;else nx=setTimeout(tick,1000)})()}
$("nxc").onclick=function(){stopnx();$("pp").focus()};
// The buttons fade out after 5s of no remote presses while playing; the next press only brings them back
// (so OK doesn't also pause). Play/pause and fast-forward/rewind keys always act straight away.
var tmr;function wake(){bar.classList.remove("hide");clearTimeout(tmr);tmr=setTimeout(function(){if(st.s==1&&$("gm").hidden&&(!$("cp")||$("cp").hidden)&&bar.contains(document.activeElement))bar.classList.add("hide")},5000)}
addEventListener("keydown",function(k){var hid=bar.classList.contains("hide");wake();
if(hid&&!/^Media/.test(k.key)){k.preventDefault();k.stopImmediatePropagation()}},true);
// Settings (⚙): captions, speed and quality, in a small menu above the button. Up / Down move, OK changes
// one, Left or Back closes it. While it's open the controls don't fade. A dot on ⚙ means something isn't the usual.
var gm=$("gm"),gs=$("gs"),gl=[].slice.call(gm.querySelectorAll("button"));
function mark(){gs.classList.toggle("dot",!!(cc||ri||qi))}
function hookb(){if(!window.__yakm&&window.yakBack){window.__yakm=1;var ob=window.yakBack;
  window.yakBack=function(){if(!gm.hidden){shut();return true}if(cp&&!cp.hidden){shutc();return true}return ob()}}}
function openm(){gm.hidden=false;gs.setAttribute("aria-expanded","true");gl[0].focus();wake();hookb()}
function shut(){gm.hidden=true;gs.setAttribute("aria-expanded","false");gs.focus();wake()}
gs.onclick=function(){gm.hidden?openm():shut()};
gm.addEventListener("click",function(){setTimeout(mark)});
// The progress bar's and the menu's keys are caught on the window before anything else sees them: the TV app's
// arrow-key navigation also listens there, and would otherwise move the selection as well (off the progress bar,
// or one extra step in the menu, skipping every second item).
addEventListener("keydown",function(k){var d=k.key,a=document.activeElement,i;
  function stop(){k.preventDefault();k.stopImmediatePropagation()}
  if(a==$("sk")){if(d=="ArrowLeft"||d=="ArrowRight"){stop();wake();seek(d=="ArrowRight"?10:-10)}
    else if(d=="Enter"){stop();toggle()}return}
  // the control row (channel, back 10s, play, forward 10s, thumbs, Watch later, ⚙): Left / Right move along it and stop
  // at its ends, rather than jumping to the nearest thing in that direction (which can be in the page below)
  if((d=="ArrowLeft"||d=="ArrowRight")&&a.closest&&a.closest(".tvl,.tvc,.tvr")&&!a.closest("#gm,#cp")){stop();wake();
    var row=[].filter.call(bar.querySelectorAll(".tvl button,.tvc button,.tvr button"),function(b){return b.offsetParent&&!b.closest("#gm,#cp")}),
      n=row[row.indexOf(a)+(d=="ArrowRight"?1:-1)];if(n)n.focus();return}
  if(gm.hidden||!gm.contains(a))return;i=gl.indexOf(a);
  if(d=="ArrowUp"||d=="ArrowDown"){stop();var n=gl[i+(d=="ArrowDown"?1:-1)];if(n)n.focus();wake()}
  else if(d=="ArrowLeft"||d=="Escape"||d=="GoBack"||d=="BrowserBack"){stop();shut()}
  else if(d=="ArrowRight")stop()},true);
gm.addEventListener("focusout",function(e){if(!gm.hidden&&!gm.contains(e.relatedTarget)&&e.relatedTarget!=gs){gm.hidden=true;gs.setAttribute("aria-expanded","false")}});
// Channel panel: OK on the channel's circle opens it, Up / Down move, Left or Back closes it. Don't recommend channel
// keeps it out of Home and New to you; pressed again it takes that back.
var cp=$("cp"),chb=$("chb");function cl(){return[].slice.call(cp.querySelectorAll("button,a"))}
function openc(){cp.hidden=false;chb.setAttribute("aria-expanded","true");cl()[0].focus();wake();hookb()}
function shutc(){cp.hidden=true;chb.setAttribute("aria-expanded","false");chb.focus();wake()}
if(cp){chb.onclick=function(){cp.hidden?openc():shutc()};
  addEventListener("keydown",function(k){if(cp.hidden||!cp.contains(document.activeElement))return;var d=k.key,l=cl(),i=l.indexOf(document.activeElement);
    function stop(){k.preventDefault();k.stopImmediatePropagation()}
    if(d=="ArrowUp"||d=="ArrowDown"){stop();var n=l[i+(d=="ArrowDown"?1:-1)];if(n)n.focus();wake()}
    else if(d=="ArrowLeft"||d=="Escape"||d=="GoBack"||d=="BrowserBack"){stop();shutc()}else if(d=="ArrowRight")stop()},true);
  cp.addEventListener("focusout",function(e){if(!cp.hidden&&e.relatedTarget&&!cp.contains(e.relatedTarget)&&e.relatedTarget!=chb){cp.hidden=true;chb.setAttribute("aria-expanded","false")}});
  var nr=$("cpn");if(nr)nr.onclick=function(){var on=nr.dataset.on=="1";
    fetch("/hide",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"c="+nr.dataset.c+"&js=1"+(on?"&undo=1":"")});
    nr.dataset.on=on?"0":"1";nr.textContent=on?"Don't recommend channel":"Recommend channel again"}}
mark();if(back){openm();$("ql").focus()}else $("pp").focus()})()</script>"""
APK = os.path.join(os.path.dirname(DB_PATH), "yaktube-tv.apk")

# Web players (not the TV app). yakPlayer() talks to YouTube's embed: captions start off, and (Shorts only) if the
# browser won't autoplay with sound (iPhone, Safari, Firefox), it plays muted instead and says so. The Feed's player
# passes plain=1: no muted fallback, so a blocked autoplay just shows YouTube's own play button.
YT_JS = """<script>function yakPlayer(f,on,plain){var st={s:-1,muted:false,off:0},go=0;
function post(o){f.contentWindow&&f.contentWindow.postMessage(JSON.stringify(o),"*")}
function cmd(fn,a){post({event:"command",func:fn,args:a||[],id:1,channel:"widget"})}
function quiet(){if(st.s!=1&&st.s!=3){cmd("mute");cmd("playVideo");st.muted=true;on&&on(st,{})}}
var hello=setInterval(function(){post({event:"listening",id:1,channel:"widget"})},400);
addEventListener("message",function(e){if(e.source!==f.contentWindow)return;var m;try{m=JSON.parse(e.data)}catch(x){return}
if(m.event=="onReady"||m.event=="initialDelivery")clearInterval(hello);
var i=m.info||{};if("playerState" in i)st.s=i.playerState;if("muted" in i)st.muted=i.muted;
if(m.event=="onReady"&&!go){go=1;cc();if(!plain){cmd("addEventListener",["onAutoplayBlocked"]);cmd("playVideo");setTimeout(quiet,2500)}}
if(m.event=="onAutoplayBlocked"&&!plain)quiet();
if(st.s==1&&!st.off){st.off=1;cc();setTimeout(cc,1500);setTimeout(cc,4000)}
on&&on(st,m)});
function cc(){cmd("setOption",["captions","track",{}])}  // captions off; unlike unloadModule this survives seeking
return{st:st,cmd:cmd}}</script>"""
EMBED = "https://www.youtube-nocookie.com/embed/"
WEB_PLAYER = """<div class="pw"><iframe id="yt" class="player" allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
""" + YT_JS + TRACK_JS + """<script>(function(){var f=document.getElementById("yt");
f.src="https://www.youtube-nocookie.com/embed/__VID__?autoplay=1&rel=0&modestbranding=1&playsinline=1&enablejsapi=1&start=__T0__&origin="+encodeURIComponent(location.origin);
var tr=yakTrack("__VID__");yakPlayer(f,function(st,m){var i=m.info||{};tr(i.currentTime,i.duration,st.s==1)},1)})()</script>"""

# Shorts: full screen, one at a time, loops. Swipe up (or wheel / Down arrow / the TV remote's Down) for the next one,
# tap (or OK) to pause. A see-through layer over the player catches the swipes; the player itself never gets touched.
SHORTS_CSS = """<style>html,body{overflow:hidden;overscroll-behavior:none;background:#000}
#sh{position:fixed;inset:0;background:#000;color:#fff;z-index:10;-webkit-user-select:none;user-select:none}
#pl,#info,#pb{position:absolute;left:50%;transform:translateX(-50%);width:min(100vw,56.25vh);width:min(100vw,56.25dvh)}
#pl{top:0;bottom:0}#tap{position:absolute;inset:0;touch-action:none;cursor:pointer}
#x{position:absolute;top:12px;left:12px;width:44px;height:44px;border-radius:50%;background:rgba(0,0,0,.5);
  display:grid;place-items:center;font-size:24px;line-height:1;z-index:2}
#info{bottom:0;padding:48px 16px 28px;background:linear-gradient(transparent,rgba(0,0,0,.7));pointer-events:none}
#info b{display:block;font-size:15px;line-height:1.3}#info span{font-size:13px;opacity:.8}
#snd{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.7);padding:16px 28px;
  border-radius:999px;font-size:20px;font-weight:600;pointer-events:none;white-space:nowrap}
#pz{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;opacity:.85;pointer-events:none}
#ud{position:absolute;right:24px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:14px;z-index:2}
#ud button{width:52px;height:52px;border-radius:50%;border:0;background:rgba(255,255,255,.16);color:#fff;font-size:20px;cursor:pointer}
@media (hover:none){#ud{display:none}}
#sh .empty{position:absolute;top:30%;left:0;right:0;color:#ccc}
#rt[hidden],#pb[hidden]{display:none}
#rt{position:absolute;bottom:120px;left:min(calc(50% + 28.125vh + 14px),calc(100% - 64px));display:flex;flex-direction:column;gap:16px;z-index:2}
#rt button{width:48px;height:48px;border-radius:50%;border:0;background:rgba(40,40,40,.75);color:#fff;cursor:pointer;display:grid;place-items:center}
#rt svg{width:22px;height:22px;fill:currentColor}#rd svg{transform:rotate(180deg)}#rt button.on{background:#fff;color:#000}
#pb{bottom:0;height:18px;z-index:3;cursor:pointer;touch-action:none;display:flex;align-items:flex-end}
#pb i{position:relative;display:block;width:100%;height:3px;background:rgba(255,255,255,.3);transition:height .12s}
#pb b{position:absolute;left:0;top:0;bottom:0;width:0;background:#f03}
#pb b:after{content:"";position:absolute;right:-6px;top:50%;width:12px;height:12px;margin-top:-6px;border-radius:50%;background:#f03;
  transform:scale(0);transition:transform .12s}
#pb:hover i,#pb.drag i{height:6px}#pb:hover b:after,#pb.drag b:after{transform:scale(1)}
#tm{position:absolute;left:50%;bottom:34px;transform:translateX(-50%);font-size:15px;font-weight:600;
  text-shadow:0 1px 3px #000;pointer-events:none;z-index:3}
#hint{display:none}
#toast{position:absolute;left:50%;top:72px;transform:translateX(-50%);background:rgba(30,30,30,.92);padding:10px 18px;
  border-radius:999px;font-size:14px;pointer-events:none;z-index:4;white-space:nowrap}</style>"""
SHORTS_JS = """<script>(function(){var L=__LIST__,i=0,seen={},$=function(x){return document.getElementById(x)};
function close(){var r=document.referrer;if(r&&r.indexOf(location.origin+"/")==0&&history.length>1)history.back();else location.href="/"}
$("x").onclick=function(k){k.preventDefault();close()};
// Remote / keyboard. With nothing selected: Up / Down change Short, OK pauses, Right goes to the buttons on the side
// (thumbs, share), Left to ✕. On a button: OK presses it, Up / Down move between the side buttons, Left (or Down
// from ✕) goes back to the Short. If the buttons are ever laid out in a row instead, Left / Right move along it (Left
// off the first one goes back to the Short) and Up / Down still change Short.
addEventListener("keydown",function(k){var d=k.key,a=document.activeElement,g;
  function stop(){k.preventDefault();k.stopImmediatePropagation()}
  if(a&&a!=document.body&&/^(A|BUTTON)$/.test(a.tagName)){
    if(d=="Enter"||d==" ")return;
    g=a.closest("#rt,#ud");var row=g&&getComputedStyle(g).flexDirection=="row";   // a row of buttons on the TV
    if(row&&(d=="ArrowUp"||d=="ArrowDown")){stop();a.blur();(d=="ArrowDown"?next:prev)();return}   // still changes Short
    if(g&&(row?d=="ArrowLeft"||d=="ArrowRight":d=="ArrowUp"||d=="ArrowDown")){stop();var l=[].filter.call(g.querySelectorAll("button"),function(b){return b.offsetParent}),
      n=l[l.indexOf(a)+(d=="ArrowDown"||d=="ArrowRight"?1:-1)];if(n)n.focus();else if(row&&d=="ArrowLeft")a.blur();return}
    if((g&&g.id=="rt"&&d=="ArrowLeft")||(a.id=="x"&&(d=="ArrowDown"||d=="ArrowRight"))){stop();a.blur();return}
    if(d!="Escape"&&d!="MediaPlayPause")return}
  else if(d=="ArrowRight"&&!$("rt").hidden){stop();$("ru").focus();return}
  else if(d=="ArrowLeft"){stop();$("x").focus();return}
  var h={ArrowDown:next,PageDown:next,ArrowUp:prev,PageUp:prev,Enter:tap," ":tap,MediaPlayPause:tap,Escape:close}[d];
  if(h){stop();h()}},true);
// More Shorts: when 8 are left (or the list runs out), ask YakTube for the next batch and add it on the end.
var busy=0,dry=0,want=0;
function more(){if(busy)return;if(Date.now()<dry){if(want){want=0;toast("No more Shorts right now");go(0)}return}busy=1;
  fetch("/shorts/more",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:"have="+L.map(function(x){return x.id}).join(",")}).then(function(r){return r.json()}).then(function(a){
    var have={},n=0;L.forEach(function(x){have[x.id]=1});a.forEach(function(x){if(!have[x.id]){L.push(x);have[x.id]=1;n++}});
    busy=0;if(!n)dry=Date.now()+60000;done()},function(){busy=0;dry=Date.now()+30000;done()})}
function done(){if(!want)return;want=0;if(i+1<L.length)go(i+1);else{toast("No more Shorts right now");go(0)}}
if(!L.length){["rt","pb"].forEach(function(x){$(x).hidden=true});
  fetch("/shorts/more",{method:"POST"}).then(function(r){return r.json()}).then(function(a){if(a.length)location.reload()});return}
var f=document.createElement("iframe");f.allow="autoplay; encrypted-media";f.referrerPolicy="strict-origin-when-cross-origin";
f.style.cssText="width:100%;height:100%;border:0;display:block;pointer-events:none";
f.src="__EMBED__"+L[0].id+"?autoplay=1&controls=0&rel=0&playsinline=1&modestbranding=1&iv_load_policy=3&enablejsapi=1&origin="+encodeURIComponent(location.origin);
$("pl").attachShadow({mode:"open"}).appendChild(f);
var pt={t:0,d:0,at:0};
var p=yakPlayer(f,function(st,m){var o=m.info||{};if(typeof o.currentTime=="number"){pt.t=o.currentTime;pt.at=Date.now()}if(o.duration)pt.d=o.duration;
  if(st.s==0){p.cmd("seekTo",[0,true]);p.cmd("playVideo")}
  if(st.s==1&&!seen[L[i].id]){seen[L[i].id]=1;fetch("/seen",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"v="+L[i].id})}
  $("snd").hidden=!st.muted;$("pz").hidden=st.s!=2});
function rt(){$("ru").className=L[i].r==1?"on":"";$("rd").className=L[i].r==-1?"on":""}
function rate(n){var it=L[i];fetch("/rate",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"v="+it.id+"&r="+n})
  .then(function(x){return x.text()}).then(function(s){it.r=+s;rt()})}
$("ru").onclick=function(){rate(1)};$("rd").onclick=function(){rate(-1)};
// Share: the phone's own share sheet where there is one, otherwise the YouTube link is copied.
var tt;function toast(s){$("toast").textContent=s;$("toast").hidden=false;clearTimeout(tt);tt=setTimeout(function(){$("toast").hidden=true},2000)}
$("sr").onclick=function(){var it=L[i],u="https://youtube.com/shorts/"+it.id;
  if(navigator.share){navigator.share({title:it.title,url:u}).catch(function(){});return}
  function old(){var a=document.createElement("textarea");a.value=u;a.style.cssText="position:fixed;opacity:0";document.body.appendChild(a);a.select();
    var ok=false;try{ok=document.execCommand("copy")}catch(x){}a.remove();ok?toast("Link copied"):prompt("Copy this link",u)}
  if(navigator.clipboard&&window.isSecureContext)navigator.clipboard.writeText(u).then(function(){toast("Link copied")},old);else old()};
// Progress bar along the bottom, like YouTube's: the player reports the time a few times a second, in between it's
// worked out from the clock so the bar moves smoothly. Drag (or tap) it to jump; the time shows while dragging.
var bar=$("pb"),drag=null;
function tf(x){x=Math.max(0,Math.floor(x));return Math.floor(x/60)+":"+("0"+x%60).slice(-2)}
function now(){return Math.min(pt.d||0,pt.t+(p.st.s==1&&pt.at?(Date.now()-pt.at)/1000:0))}
(function draw(){var k=drag!=null?drag:pt.d?now()/pt.d:0;bar.firstChild.firstChild.style.width=(k*100)+"%";requestAnimationFrame(draw)})();
function at(e){var r=bar.getBoundingClientRect();return Math.min(1,Math.max(0,(e.clientX-r.left)/r.width))}
bar.addEventListener("pointerdown",function(e){if(!pt.d)return;e.stopPropagation();bar.setPointerCapture(e.pointerId);drag=at(e);bar.classList.add("drag");
  $("tm").hidden=false;$("tm").textContent=tf(drag*pt.d)+" / "+tf(pt.d)});
bar.addEventListener("pointermove",function(e){if(drag==null)return;drag=at(e);$("tm").textContent=tf(drag*pt.d)+" / "+tf(pt.d)});
function drop(e){if(drag==null)return;pt.t=drag*pt.d;pt.at=Date.now();p.cmd("seekTo",[pt.t,true]);drag=null;bar.classList.remove("drag");$("tm").hidden=true}
bar.addEventListener("pointerup",drop);bar.addEventListener("pointercancel",drop);
["click","touchstart","touchend"].forEach(function(n){bar.addEventListener(n,function(e){e.stopPropagation()})});
function show(){rt();$("ti").textContent=L[i].title;$("ch").textContent=L[i].channel;history.replaceState(null,"","/shorts?v="+L[i].id)}
function go(n){pt.t=pt.d=pt.at=0;i=(n+L.length)%L.length;p.st.off=0;p.st.s=-1;p.cmd("loadVideoById",[L[i].id,0]);show();
  if(L.length-i<=8)more()}
function next(){if(i+1<L.length)go(i+1);else{want=1;toast("Finding more Shorts\u2026");more()}}function prev(){go(i-1)}
function tap(){if(p.st.muted){p.cmd("unMute");p.cmd("playVideo");p.st.muted=false;$("snd").hidden=true}
  else if(p.st.s==2){p.cmd("playVideo");p.st.s=1;$("pz").hidden=true}else{p.cmd("pauseVideo");p.st.s=2;$("pz").hidden=false}}
var t=$("tap"),y0=null,w=0;
t.addEventListener("touchstart",function(e){y0=e.touches[0].clientY},{passive:true});
t.addEventListener("touchend",function(e){if(y0==null)return;var d=e.changedTouches[0].clientY-y0;y0=null;e.preventDefault();
  if(d<-50)next();else if(d>50)prev();else tap()});
t.addEventListener("click",tap);
addEventListener("wheel",function(e){if(Math.abs(e.deltaY)<20||Date.now()-w<700)return;w=Date.now();e.deltaY>0?next():prev()},{passive:true});
$("up").onclick=prev;$("dn").onclick=next;show();if(L.length<=8)more()})()</script>"""


# ---------------------------------------------------------------- icons
def png(w, h, rows):
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + bytes(r) for r in rows), 9)) + chunk(b"IEND", b""))


@functools.lru_cache(None)
def icon_png(n, tile=True):
    """The app icon, drawn here (no image files to ship), matching the TV app: a white play triangle on orange.
    tile=True fills the square (phones round it themselves); False rounds the corners (browser tab)."""
    r = 0 if tile else .19
    tl, tr, th = .35, .76, .22                               # triangle: left x, tip x, half-height
    k = 4 if n <= 64 else 2 if n <= 200 else 1               # samples per pixel side (smooth edges on small icons)

    def color(x, y):
        dx, dy = max(r - x, 0, x - 1 + r), max(r - y, 0, y - 1 + r)
        if r and dx * dx + dy * dy > r * r:
            return None
        if x >= tl and abs(y - .5) <= th * (tr - x) / (tr - tl):
            return (255, 255, 255, 255)
        return (217, 72, 15, 255)
    rows = []
    for py in range(n):
        row = bytearray()
        for px in range(n):
            got = [c for c in (color((px + (a + .5) / k) / n, (py + (b + .5) / k) / n) for a in range(k) for b in range(k)) if c]
            row += bytes([sum(c[j] for c in got) // len(got) for j in range(3)] + [255 * len(got) // (k * k)]) if got else b"\0\0\0\0"
        rows.append(row)
    return png(n, n, rows)


# The YakTube logo: the orange play badge (the same as the app icon and the TV app's banner), then "Yak" and "Tube" in
# orange. Used wherever the name is shown; it scales with the font size.
LOGO_BADGE = ('<svg viewBox="0 0 92 70" aria-hidden="true"><rect width="92" height="70" rx="16" fill="#d9480f"/>'
              '<path d="M36 18v34l28-17z" fill="#fff"/></svg>')


def logo(tag="span", extra=""):
    end = tag.split()[0]
    return f'<{tag} class="logo"{extra}>{LOGO_BADGE}<span>Yak<b>Tube</b></span></{end}>'


MANIFEST = json.dumps({"name": "YakTube", "short_name": "YakTube", "start_url": "/", "display": "standalone",
                       "background_color": "#111111", "theme_color": "#111111",
                       "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"} for n in (192, 512)]})
STATIC = {"/favicon.png": lambda: icon_png(64, False), "/favicon.ico": lambda: icon_png(64, False),
          "/apple-touch-icon.png": lambda: icon_png(180), "/icon-192.png": lambda: icon_png(192),
          "/icon-512.png": lambda: icon_png(512)}


def ago(ts):
    if not ts:
        return ""
    d = max(0, int(time.time()) - ts)
    for n, u in ((31536000, "year"), (2592000, "month"), (604800, "week"), (86400, "day"), (3600, "hour"), (60, "minute")):
        if d >= n:
            k = d // n
            return f"{k} {u}{'s' if k > 1 else ''} ago"
    return "just now"


def views(n):
    if not n:
        return ""
    for d, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= d:
            return f"{n / d:.1f}".rstrip("0").rstrip(".") + s + " views"
    return f"{n} views"


def clock(n):
    """3753 -> '1:02:33', 245 -> '4:05'."""
    n = int(n or 0)
    return (f"{n // 3600}:{n // 60 % 60:02d}:{n % 60:02d}" if n >= 3600 else f"{n // 60}:{n % 60:02d}") if n > 0 else ""


def thumb(vid):
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def card(v, seen=False, src=None, why=None):
    short = "short" in v.keys() and v["short"]
    meta = " · ".join(x for x in ("Short" if short else "", views(v["views"]), ago(v["published"])) if x)
    href = ("/shorts?v=" if short else "/watch?v=") + v["id"]   # Shorts open in the full-screen Shorts player
    # the channel name opens its channel page on a click or tap (see layout); it's not a link of its own,
    # so the TV remote still steps from video to video
    cid = v["channel_id"] if "channel_id" in v.keys() else None
    ch = f'<span data-ch="{cid}">{e(v["channel_title"] or "")}</span>' if cid and CID_RE.fullmatch(cid) else e(v["channel_title"] or "")
    tag = f' data-v="{v["id"]}" data-s="{src}"' if src else ""     # Home: reports when it's been on screen
    length = "" if short or "length" not in v.keys() else clock(v["length"])
    extra = f'<span class="dur">{length}</span>' if length else ""   # how long, bottom right
    return (f'<a class="card{" seen" if seen else ""}" href="{href}"{tag}>'
            f'<span class="th"><img loading="lazy" src="{thumb(v["id"])}" alt="">{extra}</span><h3>{e(v["title"] or v["id"])}</h3>'
            f'<div class="meta">{ch}</div><div class="meta">{meta}</div>'
            + (f'<div class="why">{e(why)}</div>' if why else "") + '</a>')


def short_card(v):
    """A Short on Home's shelves: tall picture (YouTube's portrait one, or the usual one cropped if there isn't one)."""
    meta = views(v["views"]) if "views" in v.keys() else ""
    return (f'<a class="scard" href="/shorts?v={v["id"]}"><img loading="lazy" src="https://i.ytimg.com/vi/{v["id"]}/oar2.jpg" '
            f'data-f="{thumb(v["id"])}" onerror="this.onerror=null;this.src=this.dataset.f" alt="">'
            f'<h3>{e(v["title"] or "")}</h3><div class="meta">{meta}</div></a>')


def sub_button(cid, subbed, nxt, vid=None):
    """Subscribe / Subscribed button for a channel; `nxt` is the page to come back to. `cid` may also be a channel
    link (a video pasted from outside), with `vid` that video. The page script sends it without reloading the page
    (so a playing video carries on); the plain form is the fallback."""
    if subbed:
        return (f'<form class="js" method="post" action="/subs/remove"><input type="hidden" name="id" value="{e(cid)}">'
                f'<input type="hidden" name="next" value="{e(nxt)}"><button class="btn ghost small">Subscribed ✓</button></form>')
    return (f'<form class="js" method="post" action="/subs/add"><input type="hidden" name="url" value="{e(cid)}">'
            + (f'<input type="hidden" name="v" value="{vid}">' if vid else "")
            + f'<input type="hidden" name="next" value="{e(nxt)}"><button class="btn small">Subscribe</button></form>')


def later_button(vid, saved):
    return (f'<form class="js" method="post" action="/later/toggle"><input type="hidden" name="v" value="{vid}">'
            f'<input type="hidden" name="next" value="/watch?v={vid}">'
            f'<button class="btn ghost small">{"Saved ✓" if saved else "Watch later"}</button></form>')


def ask_first(action, fields, label, sure, cls="btn"):
    """A button for something that can't be undone. Pressed, it's replaced by "Are you sure?" with Clear and Cancel
    (the page script, PAGE_JS); only Clear sends the form. `fields`: the form's hidden values."""
    hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in fields.items())
    return (f'<div class="sbtns"><button type="button" class="{cls}" data-ask>{label}</button></div>'
            f'<form class="sask" method="post" action="{action}" hidden>{hidden}'
            f'<p><b>{sure + " " if sure else ""}This can\'t be undone.</b></p><div class="sbtns"><button class="btn">Clear</button>'
            f'<button type="button" class="btn ghost" data-no>Cancel</button></div></form>')


def side_row(r, seen=False):
    meta = " · ".join(x for x in (r["channel_title"] or "", views(r["views"]), ago(r["published"])) if x)
    return (f'<a class="row{" seen" if seen else ""}" href="/watch?v={r["id"]}"><img loading="lazy" src="{thumb(r["id"])}" alt="">'
            f'<div><h3>{e(r["title"] or r["id"])}</h3><div class="meta">{e(meta)}</div></div></a>')


# One small script on every page: "/" jumps to the search box, and the bits of a video card that aren't links of
# their own (so the TV remote still steps from video to video) react to a click or tap: the channel name opens its
# page.
THEME_BTN = ('<button type="button" class="theme" title="Light or dark" aria-label="Switch light or dark">'
             '<span class="moon"><svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg><small>Dark mode</small></span>'
             '<span class="sun"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
             'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg><small>Light mode</small></span></button>')

PAGE_JS = """<script>document.querySelectorAll(".theme").forEach(function(b){b.onclick=function(){var r=document.documentElement,
d=(r.dataset.theme||(matchMedia("(prefers-color-scheme:dark)").matches?"dark":"light"))=="dark"?"light":"dark";
r.dataset.theme=d;try{localStorage.yaktheme=d}catch(x){}}});
document.querySelectorAll(".burger").forEach(function(b){b.onclick=function(){var r=document.documentElement,
d=(r.dataset.side||(innerWidth>1100?"full":"mini"))=="full"?"mini":"full";r.dataset.side=d;try{localStorage.yakside=d}catch(x){}}});
addEventListener("keydown",k=>{if(k.key=="/"&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName))
{k.preventDefault();document.querySelector(".go input").focus()}});
addEventListener("click",k=>{var t=k.target,c;if(!t.closest)return;
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b}).then(r=>r.text())}
if(c=t.closest("[data-rate]")){k.preventDefault();var g=c.parentNode;
post("/rate","v="+g.dataset.v+"&r="+c.dataset.rate).then(s=>{g.querySelectorAll("[data-rate]").forEach(b=>{b.classList.toggle("on",b.dataset.rate==s);b.setAttribute("aria-pressed",b.dataset.rate==s)})})}
else if(c=t.closest("[data-ch]")){k.preventDefault();location.href="/channel?id="+c.dataset.ch}});
// Subscribe / Watch later buttons (form class="js") change in place, so a playing video isn't interrupted.
addEventListener("submit",k=>{var f=k.target;if(!f.classList.contains("js"))return;k.preventDefault();
var b=new URLSearchParams(new FormData(f)),had=f.contains(document.activeElement);b.set("inline","1");f.querySelector("button").disabled=true;
fetch(f.action,{method:"POST",body:b}).then(r=>r.ok?r.text():Promise.reject()).then(h=>{var d=document.createElement("div");
d.innerHTML=h;var n=d.firstChild;f.replaceWith(n);if(had)n.querySelector("button").focus()}).catch(()=>{f.classList.remove("js");f.submit()})});
// Things that can't be undone ask first. A [data-ask] button swaps itself for its "Are you sure?" form (Clear / Cancel).
// A small form with data-sure (✕ on a History video) needs a second press: the first turns its button into data-sure.
document.querySelectorAll("[data-ask]").forEach(b=>{var w=b.parentNode,q=w.nextElementSibling;
b.onclick=()=>{q.hidden=false;w.hidden=true;q.querySelector("button").focus()};
q.querySelector("[data-no]").onclick=()=>{q.hidden=true;w.hidden=false;b.focus()}});
addEventListener("submit",k=>{var f=k.target,b=f.querySelector("button");if(!f.dataset.sure||f.classList.contains("sure"))return;
k.preventDefault();f.classList.add("sure");var t=b.textContent;b.textContent=f.dataset.sure;
b.addEventListener("blur",()=>{f.classList.remove("sure");b.textContent=t},{once:true})},true);
var t=document.querySelector(".rib .on");if(t)t.parentNode.scrollLeft=t.offsetLeft-40</script>"""

# Home only: note which cards were on screen for a second (sent in batches), and when you come back to Home with
# the browser's Back button, load it fresh so it shows what you've just watched.
HOME_JS = """<script>(function(){var q={},sent={};
function flush(){var v=Object.keys(q);if(!v.length)return;var b="v="+v.join(",")+"&s="+v.map(function(k){return q[k]}).join(",");q={};
try{navigator.sendBeacon("/impr",new URLSearchParams(b))}catch(x){fetch("/impr",{method:"POST",body:b,keepalive:true,headers:{"Content-Type":"application/x-www-form-urlencoded"}})}}
if(window.IntersectionObserver){var io=new IntersectionObserver(function(es){es.forEach(function(e){var a=e.target;
if(e.isIntersecting)a._t=setTimeout(function(){if(!sent[a.dataset.v]){sent[a.dataset.v]=1;q[a.dataset.v]=a.dataset.s}},1000);else clearTimeout(a._t)})},{threshold:.6});
document.querySelectorAll(".card[data-s]").forEach(function(a){io.observe(a)})}
setInterval(flush,5000);addEventListener("pagehide",flush);
addEventListener("pageshow",function(e){if(e.persisted)location.reload()})})()</script>"""

# TV pages: the remote. On Home the rows work like the YouTube app on Google TV: Left / Right along a row, Up / Down
# to the row above or below (each row remembers where you were), Left at the start of a row opens the rail.
# In the rail, Up / Down move and Right goes back to where you were. Anything else is left to the TV app's own
# arrow-key handling (it jumps to the nearest thing in that direction).
TV_JS = """<script>(function(){var rail=document.querySelector(".tvside"),last=null;
if(!rail)return;
function rows(){return[].slice.call(document.querySelectorAll(".trow"))}
function items(r){return[].slice.call(r.querySelectorAll(".strip>a"))}
function put(a){var r=a.closest(".trow"),s=a.parentNode;r.dataset.i=items(r).indexOf(a);s.scrollLeft=Math.max(0,a.offsetLeft-a.offsetWidth*.6);
a.focus({preventScroll:true});if(rows()[0]==r)scrollTo(0,0);else r.scrollIntoView({block:"start"})}
function open(){(rail.querySelector("a.on")||rail.querySelector("a")).focus()}
function stop(k){k.preventDefault();k.stopImmediatePropagation()}
addEventListener("focusin",function(e){var t=e.target;if(rail.contains(t)){if(!rail.contains(e.relatedTarget)){var on=rail.querySelector("a.on");if(on&&on!=t)on.focus()}}
else if(t!=document.body)last=t});
addEventListener("keydown",function(k){var d=k.key,a=document.activeElement,r;if(!/^Arrow/.test(d))return;
if(rail.contains(a)){stop(k);if(d=="ArrowRight"){if(last&&document.contains(last)){if(last.closest(".trow"))put(last);else last.focus()}else{var f=document.querySelector(".trow .strip>a");f?put(f):rail.blur()}}
else if(d!="ArrowLeft"){var l=[].slice.call(rail.querySelectorAll("a")),n=l[l.indexOf(a)+(d=="ArrowDown"?1:-1)];if(n){n.focus();n.scrollIntoView({block:"nearest"})}}return}
if(a&&a.closest&&(r=a.closest(".trow"))){stop(k);var l=items(r),i=l.indexOf(a);
if(d=="ArrowRight"){if(l[i+1])put(l[i+1])}else if(d=="ArrowLeft"){if(i>0)put(l[i-1]);else open()}
else{var R=rows(),n=R[R.indexOf(r)+(d=="ArrowDown"?1:-1)],q=document.querySelector(".tvsearch");if(n){var m=items(n);put(m[Math.min(+n.dataset.i||0,m.length-1)])}
else if(d=="ArrowUp"&&q){q.focus();scrollTo(0,0)}}}},true);
var f=document.querySelector(".trow .strip>a");if(f&&(!document.activeElement||document.activeElement==document.body))put(f)})()</script>"""

# On the TV, holding OK on a video (for half a second) opens a menu: Not interested, Watch later and its channel.
# A short press still opens the video. In a browser, OK on a video waits for the key to come back up before acting;
# the TV app does that part itself and calls yakHold().
TV_MENU_JS = """<script>(function(){var held=null,tm=0,menu=null,from=null,tt=0;
function card(a){return a&&a.closest?a.closest("a.card,a.scard"):null}
function vid(a){var m=/[?&]v=([A-Za-z0-9_-]{11})/.exec(a.getAttribute("href")||"");return m?m[1]:null}
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b}).then(function(r){return r.text()})}
function stop(k){k.preventDefault();k.stopImmediatePropagation()}
function toast(t){var o=document.querySelector(".tvtoast");if(!o){o=document.createElement("div");o.className="tvtoast";document.body.appendChild(o)}
  o.textContent=t;o.hidden=false;clearTimeout(tt);tt=setTimeout(function(){o.hidden=true},2200)}
function shut(){if(!menu)return;menu.remove();menu=null;if(from&&document.contains(from))from.focus({preventScroll:true})}
function show(a){var v=vid(a);if(!v)return;from=a;
  fetch("/vstate?v="+v).then(function(r){return r.json()}).then(function(s){if(menu||document.activeElement!=a)return;
    var ch=a.querySelector("[data-ch]"),l=[["no","Not interested"],["lt",s.later?"Remove from Watch later":"Watch later"]];
    if(ch&&ch.dataset.ch)l.push(["ch","Go to channel"],["nc","Don't recommend channel"]);
    menu=document.createElement("div");menu.className="tvlm";menu.innerHTML="<div><b></b>"+l.map(function(x){
      return '<button type="button" data-a="'+x[0]+'">'+x[1]+"</button>"}).join("")+"</div>";
    menu.querySelector("b").textContent=(a.querySelector("h3")||{}).textContent||"";
    menu.addEventListener("click",function(e){var b=e.target.closest("button");if(b)act(b.dataset.a,a,v,ch)});
    document.body.appendChild(menu);hook();menu.querySelector("button").focus({preventScroll:true})})}
function act(w,a,v,ch){shut();
  if(w=="lt")post("/later/toggle","v="+v).then(function(s){toast(s=="1"?"Saved to Watch later":"Removed from Watch later")});
  else if(w=="no"){post("/hide","v="+v+"&js=1");var n=a.nextElementSibling||a.previousElementSibling;a.remove();
    if(n)n.focus({preventScroll:true});toast("Hidden from your suggestions")}
  else if(w=="ch")location.href="/channel?id="+ch.dataset.ch;
  else if(w=="nc"){var id=ch.dataset.ch;post("/hide","c="+id+"&js=1");      // the whole channel: gone from every row
    var n=a.nextElementSibling||a.previousElementSibling;
    [].forEach.call(document.querySelectorAll('[data-ch="'+id+'"]'),function(x){var c=card(x);if(c){if(c==n)n=null;c.remove()}});
    if(!n||!document.contains(n))n=document.querySelector(".trow .strip>a,a.card");if(n)n.focus({preventScroll:true});
    toast("You won't see this channel in suggestions")}}
addEventListener("keydown",function(k){var d=k.key,a=document.activeElement,c;
  if(menu){if(k.repeat){stop(k);return}
    if(d=="ArrowUp"||d=="ArrowDown"){stop(k);var b=[].slice.call(menu.querySelectorAll("button")),i=b.indexOf(a);
      var n=b[i<0?0:i+(d=="ArrowDown"?1:-1)];if(n)n.focus()}
    else if(d=="ArrowLeft"||d=="ArrowRight")stop(k);else if(d=="Escape"||d=="Backspace"){stop(k);shut()}return}
  if(d=="Enter"&&(c=card(a))){stop(k);if(!k.repeat){clearTimeout(tm);held=c;tm=setTimeout(function(){tm=0;show(c)},500)}}},true);
addEventListener("keyup",function(k){if(k.key!="Enter"||!held)return;stop(k);var c=held;held=null;
  if(tm){clearTimeout(tm);tm=0;c.click()}},true);
// The TV app watches OK itself (its WebView only passes OK on once it's let go) and calls this when it's held.
window.yakHold=function(){var c=card(document.activeElement);if(!c||menu)return false;clearTimeout(tm);held=null;show(c);return true};
// Back closes the menu. The TV app sets its own yakBack once the page has loaded (replacing this one), so the menu
// puts itself back in front each time it opens.
function hook(){var ob=window.yakBack;if(ob&&ob.menu)return;
  var f=function(){if(menu){shut();return true}return ob?ob():false};f.menu=1;window.yakBack=f}
hook()})()</script>"""

# TV Home: when the selection gets within 5 videos of a row's end, ask for 12 more for that row and add them on, so
# the rows seem endless. Checked on each move and on Right at the very end (where the selection can't move); a row that
# comes back empty rests for a minute before asking again.
TV_ROWS_JS = """<script>(function(){var busy={};
function ids(l){return[].map.call(l,function(a){var m=/[?&]v=([A-Za-z0-9_-]{11})/.exec(a.getAttribute("href"));
  return m?m[1]:""}).join(",")}
function near(a){var r=a&&a.closest&&a.closest(".trow"),k;if(!r||!a.closest(".strip"))return;
  var l=r.querySelectorAll(".strip>a");if(l.length-[].indexOf.call(l,a)>5)return;k=r.dataset.row;
  if(!k||busy[k]&&busy[k]>Date.now())return;busy[k]=Date.now()+30000;
  fetch("/tv/more",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:"row="+encodeURIComponent(k)+"&have="+ids(document.querySelectorAll(".trow .strip>a"))+"&mine="+ids(l)}).then(function(x){return x.text()}).then(function(h){
    busy[k]=h.trim()?0:Date.now()+60000;if(h.trim())r.querySelector(".strip").insertAdjacentHTML("beforeend",h)},
    function(){busy[k]=Date.now()+30000})}
addEventListener("focusin",function(e){near(e.target)});
addEventListener("keydown",function(k){if(k.key=="ArrowRight")near(document.activeElement)},true)})()</script>"""


# Home's rows on a computer's screen: an arrow at each end of a row (shown on hover, and only where there's more to
# see) moves it most of a screen along; near a row's end it asks for more, as the TV's rows do (see tv_more).
DESK_ROWS_JS = """<script>(function(){var busy={},A='<svg viewBox="0 0 24 24"><path d="M8.6 5.4 15.2 12l-6.6 6.6L10 20l8-8-8-8z"/></svg>';
function ids(l){return[].map.call(l,function(a){var m=/[?&]v=([A-Za-z0-9_-]{11})/.exec(a.getAttribute("href"));return m?m[1]:""}).join(",")}
function more(r,s){var k=r.dataset.row;if(!k||busy[k]&&busy[k]>Date.now())return;busy[k]=Date.now()+30000;
  fetch("/tv/more",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:"row="+encodeURIComponent(k)+"&have="+ids(document.querySelectorAll(".hrows .strip>a"))+"&mine="+ids(s.children)}).then(function(x){return x.text()}).then(function(h){
    busy[k]=h.trim()?0:Date.now()+60000;if(h.trim()){s.insertAdjacentHTML("beforeend",h);s._u()}},
    function(){busy[k]=Date.now()+30000})}
[].forEach.call(document.querySelectorAll(".hrows .trow"),function(r){var s=r.querySelector(".strip"),b={};
  ["l","r"].forEach(function(d){var x=document.createElement("button");x.className="rarr "+d;x.type="button";x.tabIndex=-1;
    x.setAttribute("aria-label",d=="l"?"Back":"More");x.innerHTML=A;x.hidden=true;r.appendChild(x);b[d]=x;
    x.onclick=function(){s.scrollBy({left:(d=="l"?-1:1)*s.clientWidth*.85,behavior:"smooth"})}});
  function upd(){var end=s.scrollWidth-s.clientWidth,i=s.querySelector("img");b.l.hidden=s.scrollLeft<4;b.r.hidden=s.scrollLeft>end-4;
    if(i)b.l.style.top=b.r.style.top=i.getBoundingClientRect().top-r.getBoundingClientRect().top+i.offsetHeight/2-22+"px"}
  s._u=upd;s.addEventListener("scroll",function(){upd();if(s.scrollLeft+s.clientWidth*2>s.scrollWidth)more(r,s)},{passive:true});
  addEventListener("resize",upd);upd()})})()</script>"""


# The TV app's arrow-key navigation (NAV in android/src/yaktube/tv/MainActivity.java, word for word): arrows move to
# the nearest link or button that way. The TV pages carry it too, so the TV layout in a browser (?tv=1) gets around
# the same way; it adds itself once, so in the TV app the app's own copy then does nothing. Added last, after the
# page's own key handling (side panel, rows, player), which goes first. Change both copies together.
TV_NAV_JS = "<script>(function(){if(window.__yak)return;window.__yak=1;var S='a[href],button,input,select,textarea,iframe,[tabindex]:not([tabindex=\"-1\"])';function all(){return[].filter.call(document.querySelectorAll(S),function(el){var r=el.getBoundingClientRect();return!el.disabled&&r.width>0&&r.height>0&&getComputedStyle(el).visibility!='hidden'})}function gap(a1,a2,b1,b2){return b2<a1?a1-b2:b1>a2?b1-a2:0}function show(el){el.focus();el.scrollIntoView({block:'center',inline:'nearest'})}function first(){var a=document.activeElement;if(a&&a!=document.body)return;var f=document.querySelector('iframe')||document.querySelector('.card,.row,.profiles button')||all()[0];if(f)show(f)}function go(d){var c=document.activeElement;if(!c||c==document.body){first();return true}var a=c.getBoundingClientRect(),best=null,bs=1e9;all().forEach(function(el){if(el==c||el.contains(c)||c.contains(el))return;var b=el.getBoundingClientRect(),m,x;if(d=='ArrowRight'){if(b.left<a.left+1||b.left+b.width/2<=a.left+a.width/2)return;m=Math.max(0,b.left-a.right);x=gap(a.top,a.bottom,b.top,b.bottom)}else if(d=='ArrowLeft'){if(b.right>a.right-1||b.left+b.width/2>=a.left+a.width/2)return;m=Math.max(0,a.left-b.right);x=gap(a.top,a.bottom,b.top,b.bottom)}else if(d=='ArrowDown'){if(b.top<a.top+1||b.top+b.height/2<=a.top+a.height/2)return;m=Math.max(0,b.top-a.bottom);x=gap(a.left,a.right,b.left,b.right)}else{if(b.bottom>a.bottom-1||b.top+b.height/2>=a.top+a.height/2)return;m=Math.max(0,a.top-b.bottom);x=gap(a.left,a.right,b.left,b.right)}var s=m+x*3;if(s<bs){bs=s;best=el}});if(best){show(best);return true}return false}addEventListener('keydown',function(k){var d=k.key;if(!/^Arrow/.test(d))return;var t=document.activeElement;if(t&&/INPUT|TEXTAREA/.test(t.tagName)&&t.value&&((d=='ArrowLeft'&&t.selectionStart>0)||(d=='ArrowRight'&&t.selectionEnd<t.value.length)))return;if(go(d))k.preventDefault()},true);window.yakBack=function(){var t=document.activeElement;if(t&&t.tagName=='IFRAME'){var l=all(),i=l.indexOf(t);var n=l[i+1]||l[0];if(n&&n!=t){show(n)}else{t.blur()}return true}return false};first()})()</script>"

# Settings: selecting a setting (moving onto it with the remote, or clicking) shows it; Left comes back to it. Autoplay is kept on
# this TV (the player reads it); region and language, and "Recommend again", are sent straight away; the two Clear
# buttons ask first (PAGE_JS). Back from a clear, #watch / #search reopens that setting.
SETTINGS_JS = """<script>(function(){var rows=[].slice.call(document.querySelectorAll(".srow"));
function shut(){[].forEach.call(document.querySelectorAll(".sedit[data-open]"),function(b){b.hidden=false;b.nextElementSibling.hidden=true})}
function show(k){shut();rows.forEach(function(r){r.classList.toggle("sel",r.dataset.p==k)});
  [].forEach.call(document.querySelectorAll(".spane"),function(p){p.hidden=p.id!="p-"+k})}
rows.forEach(function(r){r.addEventListener("focus",function(){show(r.dataset.p)});r.addEventListener("click",function(){show(r.dataset.p)})});
// Left from the details goes back to the setting they belong to
addEventListener("keydown",function(k){var a=document.activeElement,p=a&&a.closest&&a.closest(".spane");if(k.key!="ArrowLeft"||!p)return;
  var x=a.getBoundingClientRect().left;if([].some.call(p.querySelectorAll("button,a"),function(b){var r=b.getBoundingClientRect();return r.width&&r.right<=x}))return;
  k.preventDefault();k.stopImmediatePropagation();document.getElementById("r-"+p.id.slice(2)).focus()},true);
var h=location.hash.slice(1),f=rows.filter(function(r){return r.dataset.p==h})[0]||rows[0];show(f.dataset.p);f.focus();
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b})}
function mark(g,v){[].forEach.call(g.children,function(x){x.classList.toggle("on",x.dataset.v==v)})}
var ap=document.querySelector('.sopts[data-k="ap"]'),on="0";
if(ap){try{on=localStorage.yakap=="1"?"1":"0"}catch(x){}mark(ap,on);
  ap.addEventListener("click",function(e){var b=e.target.closest("button");if(!b)return;try{localStorage.yakap=b.dataset.v}catch(x){}mark(ap,b.dataset.v)})}
var th=document.querySelector('.sopts[data-k="theme"]');
if(th){var r=document.documentElement;mark(th,r.dataset.theme||"");
  th.addEventListener("click",function(e){var b=e.target.closest("button");if(!b)return;var v=b.dataset.v;
    if(v)r.dataset.theme=v;else delete r.dataset.theme;try{if(v)localStorage.yaktheme=v;else localStorage.removeItem("yaktheme")}catch(x){}mark(th,v)})}
// Language and Location: OK on the round button shows the choices; choosing one saves it and folds them away again
[].forEach.call(document.querySelectorAll(".sedit[data-open]"),function(b){var g=b.nextElementSibling;
  b.onclick=function(){b.hidden=true;g.hidden=false;(g.querySelector(".on")||g.firstChild).focus()};
  g.addEventListener("click",function(e){var o=e.target.closest("button");if(!o)return;post("/settings/region","k="+g.dataset.k+"&v="+o.dataset.v);
    mark(g,o.dataset.v);b.querySelector("span").textContent=o.textContent;g.hidden=true;b.hidden=false;b.focus()})});
var hc=document.getElementById("p-hidden");if(hc)hc.addEventListener("click",function(e){var b=e.target.closest("button[data-c]");if(!b)return;
  post("/hide","c="+b.dataset.c+"&undo=1&js=1");var s=document.createElement("span");s.className="sdone";s.textContent="Back";
  b.parentNode.classList.add("back");b.replaceWith(s);var l=hc.querySelectorAll("button[data-c]");(l[0]||document.getElementById("r-hidden")).focus()})})()</script>"""

# Settings > YakScout: the switches save straight away; Save checks the address (YakTube then tries to connect, and the
# page comes back a few seconds later to show whether it could); Bring back returns a Short to the Shorts list. The
# lists fold away (the page remembers which were open) and can be searched; channels are added and removed in place.
SCOUT_JS = """<script>(function(){var p=document.getElementById("p-scout");if(!p)return;
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b})}
function again(){setTimeout(function(){location.replace("/settings#scout");location.reload()},2500)}
function now(){location.replace("/settings#scout");location.reload()}
var KEY="yak-scout-open";try{(sessionStorage.getItem(KEY)||"").split(",").forEach(function(id){var d=id&&document.getElementById(id);if(d)d.open=true})}catch(e){}
p.querySelectorAll("details.fold").forEach(function(d){d.addEventListener("toggle",function(){try{sessionStorage.setItem(KEY,
  [].map.call(p.querySelectorAll("details.fold[open]"),function(x){return x.id}).join(","))}catch(e){}})});
function count(d){var n=d.querySelector(".n");if(n)n.textContent="· "+d.querySelectorAll(".item").length}
p.addEventListener("input",function(ev){var k=ev.target.dataset.filter;if(k){var q=ev.target.value.toLowerCase();
  document.querySelectorAll("#sf-"+k+" .item").forEach(function(r){r.hidden=q&&r.dataset.q.indexOf(q)<0})}
  if(ev.target.dataset.set=="min")ev.target.nextElementSibling.textContent=ev.target.value});
p.addEventListener("change",function(ev){var k=ev.target.dataset.set;if(k)post("/settings/scout","k="+k+"&v="+ev.target.value)});
p.addEventListener("click",function(ev){var b=ev.target.closest("button");if(!b)return;
  if(b.dataset.k){var on=!b.classList.contains("on");b.classList.toggle("on",on);b.setAttribute("aria-checked",on);
    b.firstChild.nodeValue=on?"On":"Off";post("/settings/scout","k="+b.dataset.k+"&v="+(on?1:0));if(b.dataset.k=="on")again()}
  else if(b.dataset.keep){post("/scout/keep","v="+b.dataset.keep);var s=document.createElement("span");s.className="sdone";
    s.textContent="Back";b.closest(".item").classList.add("back");b.replaceWith(s)}
  else if(b.dataset.rm){var d=b.closest("details");post("/scout/channel","op=remove&kind="+b.dataset.rm+"&v="+encodeURIComponent(b.dataset.name));
    b.closest(".item").remove();count(d)}
  else if(b.hasAttribute("data-review")){var rm2=b.nextElementSibling;b.disabled=true;rm2.className="msg";
    rm2.textContent="Checking… (about 10 seconds a Short)";post("/scout/review","").then(function(r){return r.json()})
    .then(function(j){if(j.error){b.disabled=false;rm2.className="msg bad";rm2.textContent=j.error;return}now()})
    .catch(function(){b.disabled=false;rm2.className="msg bad";rm2.textContent="Couldn't check"})}
  else if(b.dataset.forget){var d2=b.closest("details");b.disabled=true;post("/scout/voice","op=forget&v="+encodeURIComponent(b.dataset.forget))
    .then(function(r){if(r.ok){b.closest(".item").remove();count(d2)}else b.disabled=false})}});
p.addEventListener("submit",function(ev){var f=ev.target,kind=f.dataset.add;if(!kind)return;ev.preventDefault();
  var v=f.v.value.trim(),m=f.querySelector(".msg");if(!v)return;m.className="msg";
  if(kind=="voice"){m.textContent="Listening…";f.querySelector("button").disabled=true;
    post("/scout/voice","op=learn&v="+encodeURIComponent(v)).then(function(r){return r.json()}).then(function(j){
      f.querySelector("button").disabled=false;if(j.error){m.className="msg bad";m.textContent=j.error;return}
      m.textContent=j.msg;setTimeout(now,1500)}).catch(function(){f.querySelector("button").disabled=false;m.className="msg bad";m.textContent="Couldn't reach YakTube"});return}
  post("/scout/channel","op=add&kind="+kind+"&v="+encodeURIComponent(v)).then(function(r){if(r.ok)now();else{m.className="msg bad";m.textContent="Type a channel name"}})});
function live(){if(document.hidden||p.hidden)return;fetch("/scout/stats").then(function(r){return r.json()}).then(function(j){
  ["scanned","dropped","waiting"].forEach(function(k){document.getElementById("sc-"+k).textContent=j[k]});
  var c=document.getElementById("sc-chip");c.className="chip "+j.chip;c.title=j.error;c.lastChild.textContent=j.text;
  var fa=document.getElementById("sc-facts");fa.innerHTML=j.facts;fa.hidden=!j.facts}).catch(function(){})}
setInterval(live,5000);document.addEventListener("visibilitychange",live);
var rf=document.getElementById("sc-restore"),rm=document.getElementById("sc-rmsg");
rf.parentNode.addEventListener("keydown",function(ev){if(ev.key=="Enter"||ev.key==" "){ev.preventDefault();rf.click()}});
rf.addEventListener("change",function(){var file=rf.files[0];if(!file)return;rm.className="msg";rm.textContent="Restoring…";
  file.arrayBuffer().then(function(t){return fetch("/scout/restore",{method:"POST",headers:{"Content-Type":"application/octet-stream"},body:t})})
  .then(function(r){return r.json()}).then(function(j){rf.value="";if(j.error){rm.className="msg bad";rm.textContent=j.error;return}
    rm.textContent=j.msg;setTimeout(now,2500)}).catch(function(){rm.className="msg bad";rm.textContent="Couldn't restore"})});
var f=document.getElementById("scaddr"),m=f.querySelector(".msg");
f.addEventListener("submit",function(ev){ev.preventDefault();ev.stopPropagation();m.className="msg";m.textContent="Saving…";
  post("/settings/scout","k=addr&v="+encodeURIComponent(f.addr.value)+"&port="+encodeURIComponent(f.port.value))
  .then(function(r){return r.json()}).then(function(j){if(j.error){m.className="msg bad";m.textContent=j.error;return}
    f.addr.value=j.addr;f.port.value=j.port;m.textContent=j.addr?"Saved. Connecting…":"Saved.";
    var s=p.querySelector('[data-k="on"]');if(j.on=="1"&&s){s.classList.add("on");s.setAttribute("aria-checked",true);s.firstChild.nodeValue="On"}
    again()})
  .catch(function(){m.className="msg bad";m.textContent="Couldn't save"})})})()</script>"""

# Navigation icons (24x24, filled with the text colour).
ICON = {k: f'<svg viewBox="0 0 24 24" aria-hidden="true">{v}</svg>' for k, v in {
    "home": '<path d="M12 3 2 12h3v8h5v-6h4v6h5v-8h3z"/>',
    "shorts": '<path d="M9 2h6a4 4 0 0 1 4 4v12a4 4 0 0 1-4 4H9a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4zm0 2a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h6'
              'a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm1 4.5 5 3.5-5 3.5z"/>',
    "feed": '<path d="M4 6h16v2H4zm2-4h12v2H6zm-2 8h16a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2zm6 2.7v6.6'
            'l6-3.3z"/>',
    "new": '<path d="m19 9 1.25-2.75L23 5l-2.75-1.25L19 1l-1.25 2.75L15 5l2.75 1.25zm-7.5.5L9 4 6.5 9.5 1 12l5.5 2.5L9 20l2.5-5.5'
           'L17 12zM19 15l-1.25 2.75L15 19l2.75 1.25L19 23l1.25-2.75L23 19l-2.75-1.25z"/>',
    "later": '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16zm.5-13H11v6l5.2 3.2.8-1.3-4.5-2.7z"/>',
    "subs": '<path d="M4 6H2v14a2 2 0 0 0 2 2h14v-2H4zm16-4H8a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zm-8 '
            '12.5v-9l6 4.5z"/>',
    "hist": '<path d="M13 3a9 9 0 0 0-9 9H1l3.9 3.9L9 12H6a7 7 0 1 1 2.05 4.95l-1.41 1.41A9 9 0 1 0 13 3zm-1 5v5l4.3 2.5.7-1.2'
            '-3.5-2.1V8z"/>',
    "up": '<path d="M1 21h4V9H1zm22-11a2 2 0 0 0-2-2h-6.3l.95-4.57.03-.32a1.5 1.5 0 0 0-.44-1.06L14.17 1 7.6 7.6A2 2 0 0 0 7 9v10'
          'a2 2 0 0 0 2 2h9a2 2 0 0 0 1.84-1.22l3.02-7.05A2 2 0 0 0 23 12z"/>',
    "share": '<path d="M14 4v5C7 10 4 15 3 20c2.5-3.5 6-5.1 11-5.1V20l8-8z"/>',
    "search": '<path d="M15.5 14h-.8l-.3-.3A6.5 6.5 0 1 0 14 15.5l.3.3v.8l5 5 1.5-1.5zm-6 0a4.5 4.5 0 1 1 0-9 4.5 4.5 0 0 1 0 9z"/>',
    "set": '<path fill-rule="evenodd" d="M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.6 7.6 0 0 0-1.7-1L15 3.3h-4l-.4 2.6a7.6 '
           '7.6 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.1 1.6 2 3.5 2.5-1a7.6 7.6 0 0 0 1.7 1l.4 2.6h4l.4-2.6a7.6 7.6 '
           '0 0 0 1.7-1l2.5 1 2-3.5zM12 15.3a3.3 3.3 0 1 1 0-6.6 3.3 3.3 0 0 1 0 6.6z"/>',
}.items()}
# Home, Shorts and Subscriptions are the main three (the bar along the bottom on a phone); the rest sit under them
# on a computer, or in the ribbon across the top on a phone.
MAIN_TABS = (("home", "/", "Home"), ("shorts", "/shorts", "Shorts"), ("feed", "/feed", "Subscriptions"))
MORE_TABS = (("new", "/new", "New to you"), ("later", "/later", "Later"), ("subs", "/subs", "Channels"),
             ("hist", "/history", "History"))


# The TV's rail down the left, in the order YouTube's Google TV app now uses: search, then the places you go most.
# The TV side panel, top to bottom: these four, a divider, the next three, a divider, Recent channels, then Settings
# last (after a small gap). New to you is a row on Home.
TV_TABS = (("search", "/search", "Search"), ("home", "/", "Home"), ("feed", "/feed", "Subscriptions"), ("shorts", "/shorts", "Shorts"))
TV_MORE = (("later", "/later", "Watch Later"), ("subs", "/subs", "Channels"), ("hist", "/history", "History"))


def rate_buttons(vid, val):
    """Thumbs up / down for a video; the page script sends a click to /rate."""
    return (f'<div class="rate" data-v="{vid}">' + "".join(
        f'<button type="button" class="{cls}{" on" if val == n else ""}" data-rate="{n}" aria-pressed="{str(val == n).lower()}" '
        f'title="{t}" aria-label="{t}">{ICON["up"]}</button>' for n, cls, t in ((1, "up", "I like this"), (-1, "dn", "Not for me")))
            + '</div>')


def face(p):
    """What's inside a profile's circle: its picture, or else the first letter of its name."""
    a = p["avatar"] or ""
    return f'<i class="emo">{a}</i>' if a in AVATARS else e(p["name"][:1].upper())


# The subscriptions import's file picker. A Takeout .zip can be gigabytes (it holds your own uploads too), so the page
# reads only the zip's table of contents and subscriptions.csv out of it, in the browser; nothing big is sent anywhere.
IMPORT_JS = r"""<script>(function(){var inp=document.querySelector(".imp input[type=file]"),t=document.querySelector(".imp textarea"),
msg=document.querySelector(".imp .zipmsg");
function u16(d,o){return d.getUint16(o,true)}function u32(d,o){return d.getUint32(o,true)}function u64(d,o){return u32(d,o)+u32(d,o+4)*4294967296}
function part(f,a,b){return f.slice(a,b).arrayBuffer().then(function(x){return new DataView(x)})}
function fromZip(f){var tail=Math.min(f.size,65557+20);
 return part(f,f.size-tail,f.size).then(function(d){var i=d.byteLength-22;while(i>=0&&u32(d,i)!=0x06054b50)i--;if(i<0)throw"zip";
  var size=u32(d,i+12),off=u32(d,i+16);
  if(off==0xFFFFFFFF&&i>=20&&u32(d,i-20)==0x07064b50){var z=u64(d,i-12);return part(f,z,z+56).then(function(e){return[u64(e,40),u64(e,48)]})}
  return[size,off]})
 .then(function(c){return part(f,c[1],c[1]+c[0])})
 .then(function(d){var o=0,hit=null;
  while(o+46<=d.byteLength&&u32(d,o)==0x02014b50){var n=u16(d,o+28),x=u16(d,o+30),k=u16(d,o+32),
   name=new TextDecoder().decode(new Uint8Array(d.buffer,o+46,n));
   if(/(^|\/)subscriptions\.csv$/i.test(name)){var csz=u32(d,o+20),usz=u32(d,o+24),off=u32(d,o+42),p=o+46+n,end=p+x;
    while(p+4<=end){var id=u16(d,p),l=u16(d,p+2),q=p+4;
     if(id==1){if(usz==0xFFFFFFFF)q+=8;if(csz==0xFFFFFFFF){csz=u64(d,q);q+=8}if(off==0xFFFFFFFF)off=u64(d,q)}p+=4+l}
    hit={m:u16(d,o+10),csz:csz,off:off};break}
   o+=46+n+x+k}
  if(!hit)throw"none";
  return part(f,hit.off,hit.off+30).then(function(h){var s=hit.off+30+u16(h,26)+u16(h,28),b=f.slice(s,s+hit.csz);
   if(hit.m==0)return b.text();if(hit.m!=8)throw"zip";
   return new Response(b.stream().pipeThrough(new DecompressionStream("deflate-raw"))).text()})})}
inp.onchange=function(){var f=this.files[0],zip;if(!f)return;zip=/\.zip$/i.test(f.name);msg.textContent=zip?"Looking in the zip…":"";
 (zip?fromZip(f):f.text()).then(function(s){t.value=s;
  msg.textContent=zip?"Found subscriptions.csv with "+(s.match(/(^|\n)UC[\w-]{22},/g)||[]).length+" channels. Press Import.":""})
 .catch(function(e){msg.textContent=e=="none"?"There’s no subscriptions.csv in this zip. If Takeout split your download "
  +"into several zips, try the others.":"Couldn’t read that zip. Unzip it and choose subscriptions/subscriptions.csv instead."})}})()</script>"""

def layout(title, body, prof=None, tab="", q="", bare=False):
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<title>{e(title)} · YakTube</title><link rel="icon" type="image/png" href="/favicon.png">'
            f'<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.json" crossorigin="use-credentials">'
            f'<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
            f'<meta name="apple-mobile-web-app-title" content="YakTube"><style>{CSS}</style>'
            f'<script>try{{var t=localStorage.yaktheme,s=localStorage.yakside,r=document.documentElement;if(t)r.dataset.theme=t;if(s)r.dataset.side=s}}catch(x){{}}</script></head><body{" class=bare" if bare else ""}>')
    if not prof:
        return head + f'<main>{body}</main></body></html>'
    def links(tabs, icons=True):
        return "".join(f'<a href="{h}" class="{"on" if tab == k else ""}">{ICON[k] if icons else ""}<span>{n}</span></a>'
                       for k, h, n in tabs)
    av = f'<span class="av" style="background:{prof["color"]}">{face(prof)}</span>'
    return head + (
        f'<header><button type="button" class="burger" title="Collapse or expand the menu" aria-label="Menu">'
        f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg></button>'
        + logo("a", ' href="/" aria-label="YakTube"') +
        f'<nav class="rib">{links(MORE_TABS, False)}<a href="/profiles">{av}<span>Profiles</span></a>'
        f'<a href="/settings" class="{"on" if tab == "set" else ""}"><span>Settings</span></a></nav>'
        f'<form class="go" action="/go"><input type="text" name="q" placeholder="Search, or paste a YouTube link…" '
        f'autocomplete="off" value="{e(q)}"></form>'
        f'<a href="/profiles" title="Switch profile ({e(prof["name"])})" class="av" style="background:{prof["color"]}">'
        f'{face(prof)}</a>{THEME_BTN}</header>'
        f'<div class="shell"><nav class="side">{links(MAIN_TABS)}<hr>{links(MORE_TABS)}<hr>'   # light or dark: in Settings (a phone keeps its header button)
        f'{links((("set", "/settings", "Settings"),))}</nav><main>{body}</main></div>'
        f'<nav class="bot">{links(MAIN_TABS)}</nav>'
        f'<nav class="tvside"><a class="me" href="/profiles">{av}<span>{e(prof["name"])}<small>Switch profile</small></span></a>'
        f'{links(TV_TABS)}<hr>{links(TV_MORE)}<!--tvrecent--><i class="gap"></i>{links((("set", "/settings", "Settings"),))}</nav>'
        + PAGE_JS + '</body></html>')


# ---------------------------------------------------------------- server
class App(BaseHTTPRequestHandler):
    server_version = "YakTube/1"

    # -- helpers
    def send(self, body, status=200, ctype="text/html; charset=utf-8", headers=()):
        if isinstance(body, str) and ctype.startswith("text/html") and self.tv():
            body = body.replace("</head>", TV_CSS + "</head>", 1)
            if "<!--tvrecent-->" in body:                       # the rail: channels you watched lately, and its script
                body = body.replace("<!--tvrecent-->", self.tv_recent(), 1).replace("</body>", TV_JS + TV_MENU_JS + "</body>", 1)
            body = body.replace("</body>", TV_NAV_JS + "</body>", 1)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")  # embeds need a referrer
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def tv(self):
        """The TV app (its browser says "YakTubeTV"), or a browser switched to the TV layout with ?tv=1 (?tv=0: back)."""
        return "YakTubeTV" in self.headers.get("User-Agent", "") or "yak_tv=1" in self.headers.get("Cookie", "")

    def tv_channel(self, v, sub):
        """The TV player's channel button (its first letter in a circle) and the panel it opens: Subscribe, Go to
        channel and Don't recommend channel. Without a channel, just Subscribe if there is one."""
        cid, ref = v["channel_id"], v["channel_id"] or v["channel_url"]
        if not ref:
            return sub
        name = v["channel_title"] or "Channel"
        av = f'<i class="av" style="background:{COLORS[sum(map(ord, ref)) % len(COLORS)]}">{e(name[:1].upper())}</i>'
        nr = ""
        if cid:
            hid = self.db.execute("SELECT 1 FROM hidden WHERE profile_id=? AND kind='c' AND ref=?", (self.prof["id"], cid)).fetchone()
            label = "Recommend channel again" if hid else "Don't recommend channel"
            nr = f'<button type="button" id="cpn" data-c="{e(cid)}" data-on="{1 if hid else 0}">{label}</button>'
        return (f'<button id="chb" aria-haspopup="dialog" aria-expanded="false" aria-label="{e(name)}">{av}</button>'
                f'<div id="cp" class="tvcp" role="dialog" aria-label="{e(name)}" hidden>{av}<b>{e(name)}</b>{sub}'
                f'<a href="/channel?id={urllib.parse.quote(ref, safe="")}">Go to channel</a>{nr}</div>')

    def tv_recent(self):
        """The TV rail's "Recent channels": the last 3 channels this profile watched, after a divider."""
        rows = self.db.execute(
            "SELECT v.channel_id, COALESCE(ch.title, MAX(v.channel_title)) AS title, MAX(h.watched_at) AS at FROM history h "
            "JOIN videos v ON v.id=h.video_id LEFT JOIN channels ch ON ch.id=v.channel_id WHERE h.profile_id=? "
            "AND v.channel_id IS NOT NULL GROUP BY v.channel_id ORDER BY at DESC LIMIT 3", (self.prof["id"],)).fetchall()
        if not rows:
            return ""
        return '<hr><h4>Recent channels</h4>' + "".join(
            f'<a href="/channel?id={r["channel_id"]}"><i class="av" style="background:{COLORS[sum(map(ord, r["channel_id"])) % len(COLORS)]}">'
            f'{e((r["title"] or "?")[:1].upper())}</i><span>{e(r["title"] or "")}</span></a>' for r in rows)

    def redirect(self, to, headers=()):
        self.send("", 303, headers=[("Location", to), *headers])

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

    def profile(self):
        ck = SimpleCookie(self.headers.get("Cookie", ""))
        if "yak_profile" in ck and ck["yak_profile"].value.isdigit():
            return self.db.execute("SELECT * FROM profiles WHERE id=?", (int(ck["yak_profile"].value),)).fetchone()
        return None

    def card(self, v, seen=False, src=None, why=None):
        return card(v, seen, src, why)

    def back(self, f, fallback=None):
        """After a form: go back to the page it came from (only ever a path on this site)."""
        nxt = f.get("next", "")
        if nxt.startswith("/") and not nxt.startswith("//"):
            return self.redirect(nxt)
        return self.redirect(fallback) if fallback else self.send("", 204)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def route(self, method):
        u = urllib.parse.urlsplit(self.path)
        self.q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.db = connect()
        try:
            if u.path == "/healthz":
                return self.send("ok", ctype="text/plain")
            if u.path in STATIC:                                    # icons: no profile needed
                return self.send(STATIC[u.path](), ctype="image/png", headers=[("Cache-Control", "max-age=604800")])
            if u.path == "/manifest.json":
                return self.send(MANIFEST, ctype="application/manifest+json")
            if u.path == "/yaktube-tv.apk" and os.path.isfile(APK):  # so a TV can download the app
                with open(APK, "rb") as f:
                    return self.send(f.read(), ctype="application/vnd.android.package-archive",
                                     headers=[("Content-Disposition", 'attachment; filename="yaktube-tv.apk"')])
            if self.q.get("tv") in ("0", "1"):                     # preview the TV layout in a normal browser
                return self.redirect(u.path, [("Set-Cookie", f"yak_tv={self.q['tv']}; Path=/; Max-Age=31536000; SameSite=Lax")])
            if u.path.startswith("/profiles"):
                return getattr(self, method.lower() + "_profiles")(u.path)
            self.prof = self.profile()
            if not self.prof:
                return self.redirect("/profiles")
            h = {("GET", "/"): self.home, ("GET", "/feed"): self.feed, ("POST", "/rate"): self.rate, ("POST", "/impr"): self.impr, ("GET", "/watch"): self.watch, ("GET", "/go"): self.go,
                 ("GET", "/subs"): self.subs, ("POST", "/subs/add"): self.sub_add, ("GET", "/channel"): self.channel,
                 ("POST", "/subs/import"): self.sub_import, ("GET", "/subs/export"): self.sub_export,
                 ("GET", "/later"): self.later, ("GET", "/vstate"): self.vstate, ("POST", "/later/toggle"): self.later_toggle, ("POST", "/hide"): self.hide,
                 ("POST", "/subs/remove"): self.sub_remove, ("POST", "/refresh"): self.refresh, ("POST", "/feed/shorts"): self.feed_shorts,
                 ("GET", "/foryou"): lambda: self.redirect("/"), ("GET", "/shorts"): self.shorts, ("POST", "/tv/more"): self.tv_more, ("POST", "/shorts/more"): self.shorts_more, ("POST", "/seen"): self.seen, ("GET", "/new"): self.newtoyou, ("GET", "/search"): self.search, ("GET", "/related"): self.related,
                 ("GET", "/history"): self.history, ("GET", "/settings"): self.settings,
                 ("POST", "/settings/region"): self.settings_region, ("POST", "/settings/scout"): self.settings_scout, ("POST", "/scout/keep"): self.scout_keep, ("POST", "/scout/channel"): self.scout_channel, ("POST", "/scout/voice"): self.scout_voice, ("GET", "/scout/backup"): self.scout_backup, ("POST", "/scout/restore"): self.scout_restore, ("GET", "/scout/log.csv"): self.scout_log_csv, ("GET", "/scout/share"): self.scout_share, ("POST", "/scout/review"): self.scout_review_route, ("GET", "/scout/stats"): self.scout_stats, ("POST", "/settings/clear"): self.settings_clear, ("POST", "/history/clear"): self.history_clear,
                 ("POST", "/history/remove"): self.history_remove}.get((method, u.path))
            if not h:
                return self.send(layout("Not found", '<div class="empty"><h1>Not found</h1></div>', self.prof), 404)
            h()
        except Exception as ex:
            print("[error]", self.path, repr(ex))
            self.send(layout("Error", f'<div class="empty"><h1>Something broke</h1><p>{e(str(ex))}</p></div>'), 500)
        finally:
            self.db.close()

    # -- profiles
    PEN = '<svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16zM14 6l4 4"/></svg>'

    def get_profiles(self, path):
        if path == "/profiles/edit":
            return self.profile_edit()
        if path == "/profiles/remove":
            return self.profile_remove()
        manage = self.q.get("manage") == "1"
        ps = self.db.execute("SELECT * FROM profiles ORDER BY pos, id").fetchall()
        moved, d = self.q.get("moved", ""), self.q.get("d", "")
        tiles = []
        for i, p in enumerate(ps):
            av = f'<div class="av" style="background:{p["color"]}">{face(p)}'
            if not manage:
                tiles.append(f'<form method="post" action="/profiles/select"><input type="hidden" name="id" value="{p["id"]}">'
                             f'<button>{av}</div>{e(p["name"])}</button></form>')
                continue
            def mv(step, label, arrow):   # move buttons; after a move, the same button keeps the focus
                hid = " no" if not 0 <= i + step < len(ps) else ""
                af = " autofocus" if moved == str(p["id"]) and d == str(step) and not hid else ""
                return (f'<form method="post" action="/profiles/move"><input type="hidden" name="id" value="{p["id"]}">'
                        f'<input type="hidden" name="d" value="{step}"><button class="{hid}" title="{label}"{af}>{arrow}</button></form>')
            first = " autofocus" if i == 0 and not moved else ""
            tiles.append(f'<div><a class="tile" href="/profiles/edit?id={p["id"]}" title="Edit {e(p["name"])}"{first}>'
                         f'{av}<span class="pen">{self.PEN}</span></div>{e(p["name"])}</a>'
                         f'<div class="mv">{mv(-1, "Move left", "◀")}{mv(1, "Move right", "▶")}</div></div>')
        add = ('' if manage else
               f'<form class="center" method="post" action="/profiles/add" style="display:flex;gap:8px;max-width:360px;margin:0 auto">'
               f'<input type="text" name="name" placeholder="New profile name" maxlength="30" required>'
               f'<button class="btn">Add</button></form>')
        big = logo("div", ' style="font-size:34px;margin-top:40px"')
        # Back: from Manage profiles to the profiles; otherwise to the page you came to Profiles from (once a profile is
        # in use). That page is remembered in a cookie, so Back still works after Manage profiles, adding a profile or
        # opening Profiles fresh, where the browser's own history would only bounce between Profiles pages.
        ref, cookies = urllib.parse.urlsplit(self.headers.get("Referer", "")), SimpleCookie(self.headers.get("Cookie", ""))
        came = ref.path + (f"?{ref.query}" if ref.query else "")
        if ref.netloc == self.headers.get("Host", "") and came.startswith("/") and not came.startswith(("//", "/profiles")):
            went, keep = came, [("Set-Cookie", f"yak_back={urllib.parse.quote(came, safe='')}; Path=/profiles; SameSite=Lax")]
        else:
            got = urllib.parse.unquote(cookies["yak_back"].value) if "yak_back" in cookies else "/"
            went, keep = (got if got.startswith("/") and not got.startswith("//") else "/"), []
        back = (f'<a class="pback" href="{"/profiles" if manage else e(went)}">← Back</a>' if self.profile() else "")
        body = (f'{back}<div class="center">{big}'
                f'<h1 style="margin-top:28px">{"Manage profiles" if manage else "Who’s watching?"}</h1>'
                f'{"<p class=meta>Pick a profile to change it, or move it with the arrows.</p>" if manage else ""}</div>'
                f'<div class="profiles">{"".join(tiles)}</div>{add}<p class="center meta" style="margin-top:22px">'
                f'<a href="/profiles{"" if manage else "?manage=1"}">{"Done" if manage else "Manage profiles"}</a></p>')
        self.send(layout("Profiles", body), headers=keep)

    def one_profile(self, key="id", src=None):
        v = (src or self.q).get(key, "")
        return self.db.execute("SELECT * FROM profiles WHERE id=?", (int(v),)).fetchone() if v.isdigit() else None

    def profile_edit(self):
        p = self.one_profile()
        if not p:
            return self.redirect("/profiles?manage=1")
        cur = p["avatar"] if p["avatar"] in AVATARS else ""
        cols = "".join(f'<button type="button" data-c="{c}" title="Colour" style="background:{c}"'
                       f'{" class=on" if c == p["color"] else ""}></button>' for c in PROFILE_COLORS)
        faces = "".join(f'<button type="button" data-a="{a}" title="{"First letter" if not a else "Picture"}"'
                        f'{" class=on" if a == cur else ""}>{a or "Aa"}</button>' for a in [""] + AVATARS)
        err = '<div class="err">Another profile already has that name.</div>' if self.q.get("err") == "taken" else ""
        body = (f'<form class="pedit" method="post" action="/profiles/save"><h1>Edit profile</h1>{err}'
                f'<input type="hidden" name="id" value="{p["id"]}"><input type="hidden" name="color" value="{p["color"]}">'
                f'<input type="hidden" name="avatar" value="{cur}">'
                f'<div class="top"><div class="av" id="pv" style="background:{p["color"]}">{face(p)}</div>'
                f'<input type="text" name="name" value="{e(p["name"])}" maxlength="30" required aria-label="Name"></div>'
                f'<h3>Colour</h3><div class="pick cols">{cols}</div>'
                f'<h3>Picture</h3><div class="pick faces">{faces}</div>'
                f'<div class="acts"><button class="btn">Save</button><a href="/profiles?manage=1">Cancel</a></div>'
                f'<a class="rm" href="/profiles/remove?id={p["id"]}">Remove this profile…</a></form>'
                '<script>(function(){var f=document.querySelector(".pedit"),pv=f.querySelector("#pv");'
                'function draw(){var a=f.avatar.value,n=f.name.value.trim();pv.style.background=f.color.value;'
                'pv.innerHTML=a?"<i class=emo>"+a+"</i>":"";if(!a)pv.textContent=(n[0]||"?").toUpperCase();'
                'f.querySelectorAll("[data-c]").forEach(function(b){b.classList.toggle("on",b.dataset.c==f.color.value)});'
                'f.querySelectorAll("[data-a]").forEach(function(b){b.classList.toggle("on",b.dataset.a==a)})}'
                'f.addEventListener("click",function(ev){var b=ev.target.closest("[data-c],[data-a]");if(!b)return;'
                'if(b.dataset.c)f.color.value=b.dataset.c;else f.avatar.value=b.dataset.a;draw()});'
                'f.name.addEventListener("input",draw)})()</script>')
        self.send(layout("Edit profile", body))

    def profile_remove(self):
        p = self.one_profile()
        if not p:
            return self.redirect("/profiles?manage=1")
        n = e(p["name"])
        body = (f'<div class="empty"><div class="profiles" style="margin:0 0 10px"><div class="av" style="background:{p["color"]}">'
                f'{face(p)}</div></div><h1>Remove {n}?</h1><p>This deletes {n}’s watch history, subscriptions, Watch later '
                f'and likes. It can’t be undone.</p>'
                f'<form method="post" action="/profiles/delete" style="display:flex;gap:14px;justify-content:center;align-items:center">'
                f'<input type="hidden" name="id" value="{p["id"]}"><a class="btn" href="/profiles/edit?id={p["id"]}" autofocus>Keep it</a>'
                f'<button style="background:none;border:0;color:#c92a2a;font:inherit;cursor:pointer">Remove {n}</button></form></div>')
        self.send(layout("Remove profile", body))

    def post_profiles(self, path):
        f = self.form()
        if path == "/profiles/add" and f.get("name", "").strip():
            n, top = self.db.execute("SELECT COUNT(*), COALESCE(MAX(pos),0) FROM profiles").fetchone()
            self.db.execute("INSERT OR IGNORE INTO profiles(name,color,pos) VALUES(?,?,?)",
                            (f["name"].strip()[:30], COLORS[n % len(COLORS)], top + 1))
        elif path == "/profiles/select" and f.get("id", "").isdigit():
            return self.redirect("/", [("Set-Cookie", f"yak_profile={int(f['id'])}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly")])
        elif path == "/profiles/save" and (p := self.one_profile(src=f)):
            name = f.get("name", "").strip()[:30] or p["name"]
            color = f.get("color") if f.get("color") in PROFILE_COLORS else p["color"]
            avatar = f.get("avatar") if f.get("avatar") in AVATARS else ""
            try:
                self.db.execute("UPDATE profiles SET name=?, color=?, avatar=? WHERE id=?", (name, color, avatar, p["id"]))
            except sqlite3.IntegrityError:                                      # the name is taken
                return self.redirect(f"/profiles/edit?id={p['id']}&err=taken")
            return self.redirect("/profiles?manage=1")
        elif path == "/profiles/move" and (p := self.one_profile(src=f)) and f.get("d") in ("-1", "1"):
            ids = [r[0] for r in self.db.execute("SELECT id FROM profiles ORDER BY pos, id")]
            i, j = ids.index(p["id"]), ids.index(p["id"]) + int(f["d"])
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
                self.db.executemany("UPDATE profiles SET pos=? WHERE id=?", list(enumerate(ids)))
            return self.redirect(f"/profiles?manage=1&moved={p['id']}&d={f['d']}")
        elif path == "/profiles/delete" and f.get("id", "").isdigit():
            self.db.execute("DELETE FROM profiles WHERE id=?", (int(f["id"]),))
            return self.redirect("/profiles?manage=1")
        self.redirect("/profiles")

    # -- feed
    def feed(self):
        pid = self.prof["id"]
        page = max(1, int(self.q.get("page", "1")) if self.q.get("page", "").isdigit() else 1)
        on = 1 if self.prof["feed_shorts"] else 0
        nsubs = self.db.execute("SELECT COUNT(*) FROM subs WHERE profile_id=?", (pid,)).fetchone()[0]
        if not nsubs:
            return self.send(layout("Feed", '<div class="empty"><h1>Your feed is empty</h1><p>Subscribe to a few channels '
                                    'and their new videos will show up here, newest first.</p>'
                                    '<a class="btn" href="/subs">Add subscriptions</a></div>', self.prof, "feed"))
        rows = self.db.execute(
            "SELECT v.*, h.video_id AS seen FROM videos v JOIN subs s ON s.channel_id=v.channel_id AND s.profile_id=? "
            "LEFT JOIN history h ON h.video_id=v.id AND h.profile_id=? WHERE (v.short=0 OR ?) "   # Shorts toggle
            "ORDER BY v.published DESC LIMIT ? OFFSET ?",
            (pid, pid, on, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)).fetchall()
        more = len(rows) > PAGE_SIZE
        grid = "".join(self.card(v, v["seen"]) for v in rows[:PAGE_SIZE])
        body = (f'<div class="bar"><h1>Latest from your subscriptions</h1>'
                f'<div class="acts"><form method="post" action="/feed/shorts"><button class="btn ghost small sw{" on" if on else ""}" role="switch" '
                f'aria-checked="{"true" if on else "false"}" title="Show Shorts from your subscriptions">Shorts<i></i></button></form>'
                f'<form method="post" action="/refresh"><button class="btn ghost small">Refresh</button></form></div></div>'
                f'<div class="grid">{grid or "<p class=meta>Fetching videos… refresh in a moment.</p>"}</div>'
                + (f'<p class="center" style="margin-top:30px"><a class="btn ghost" href="/feed?page={page + 1}">Load more</a></p>' if more else ""))
        self.send(layout("Subscriptions", body, self.prof, "feed"))

    def home(self):
        """Home: the recommendation engine's picks (see home_feed), with a group of 4 Shorts every few rows.
        ?why=1 shows why each one is there. Never cached: coming back to it shows the picks as they are now."""
        pid = self.prof["id"]
        threading.Thread(target=warm_related, args=(pid,), daemon=True).start()
        picks = home_feed(self.db, pid, 90)
        if self.tv() and picks:
            return self.send(layout("Home", self.tv_home(picks), self.prof, "home"), headers=[("Cache-Control", "no-store")])
        rows = self.home_rows(picks) if picks else ""   # a computer's screen gets the TV's rows (.hrows in CSS); a phone, the grid
        picks = picks[:60]
        shorts = home_shorts(self.db, pid)
        show_why = self.q.get("why") == "1"

        def shelf():
            got, shorts[:] = shorts[:4], shorts[4:]
            return (f'<section class="shelf"><h2>{ICON["shorts"]}Shorts</h2><div class="sgrid">'
                    + "".join(short_card(v) for v in got) + '</div></section>')
        parts = []
        for i, (r, src, score, reason) in enumerate(picks):
            if i in (8, 20, 32, 44) and shorts:   # groups of 4, spread through the feed
                parts.append(shelf())
            parts.append(self.card(r, src=src, why=f"{reason} · {score:.2f}" if show_why else None))
        if shorts and len(picks) <= 8:
            parts.append(shelf())
        if not picks:
            body = ('<div class="empty"><h1>Nothing here yet</h1><p>Watch a few videos (search for something you like) '
                    'or subscribe to some channels, and suggestions based on what you watch will show up here. It\'s all '
                    'worked out on this computer from your own history.</p><a class="btn" href="/subs">Add subscriptions</a></div>')
            body += f'<div class="grid">{"".join(parts)}</div>' if parts else ""
        else:
            body = (f'<h1 class="sr">Home</h1><div class="grid hgrid">{"".join(parts)}</div><div class="hrows">{rows}</div>'
                    + HOME_JS + DESK_ROWS_JS)
        self.send(layout("Home", body, self.prof, "home"), headers=[("Cache-Control", "no-store")])

    def tv_home(self, picks):
        """Home on the TV: rows like the YouTube app on Google TV (see home_rows), under a search pill and the logo."""
        head = (f'<div class="tvhead"><a class="tvsearch" href="/search">{ICON["search"]}<span>Search</span></a>'
                + logo() + '</div>')   # the YakTube logo, as on the TV app's banner
        return '<h1 class="sr">Home</h1>' + head + self.home_rows(picks) + HOME_JS + TV_ROWS_JS

    def home_rows(self, picks):
        """Home's rows, on the TV and on a computer's screen: Recommended for you (the top picks), New from your
        subscriptions (newest first), New to you, then a row for each reason with 3 or more videos ("Because you
        watched …", "From your search …"), then the rest. Shorts get a row after New to you (tall cards, like YouTube's)."""
        pid = self.prof["id"]
        ntu = new_to_you(self.db, pid, 20)
        self.db.executemany("INSERT OR IGNORE INTO shown(profile_id,video_id,first_at) VALUES(?,?,?)",   # so it turns over
                            [(pid, r["id"], int(time.time())) for r in ntu])
        ntu_ids = {r["id"] for r in ntu}
        picks = [p for p in picks if p[0]["id"] not in ntu_ids and p[1] != "explore"]
        subs = [p for p in picks if p[1] == "subs"]
        rest = [p for p in picks if p[1] != "subs"]
        if len(subs) < 8:                                     # top up with the newest unwatched uploads
            have = {p[0]["id"] for p in picks} | ntu_ids
            no_vids, no_chans = hidden_sets(self.db, pid)
            for r in self.db.execute(
                    "SELECT v.* FROM videos v JOIN subs s ON s.channel_id=v.channel_id AND s.profile_id=? WHERE v.short=0 "
                    "AND v.id NOT IN (SELECT video_id FROM history WHERE profile_id=?) ORDER BY v.published DESC LIMIT 60",
                    (pid, pid)):
                if r["id"] not in have and r["id"] not in no_vids and r["channel_id"] not in no_chans:
                    subs.append((r, "subs", 0, WHY["subs"]))
                    if len(subs) >= 12:
                        break
        subs.sort(key=lambda p: p[0]["published"] or 0, reverse=True)
        groups = {}
        for p in rest[10:]:
            groups.setdefault(p[3], []).append(p)
        rows = ([("Recommended for you", rest[:10]), ("New from your subscriptions", subs),
                 ("New to you", [(r, "explore", 0, WHY["explore"]) for r in ntu]), ("Shorts", home_shorts(self.db, pid, 20))]
                + [(k, v) for k, v in groups.items() if len(v) >= 3]
                + [("More for you", [p for v in groups.values() if len(v) < 3 for p in v])])
        key = {"Recommended for you": "rec", "New from your subscriptions": "subs", "New to you": "ntu",
               "Shorts": "shorts", "More for you": "more"}
        return "".join(
            f'<section class="trow" data-row="{e(key.get(title, title))}"><h2>{e(title.replace(": ", " "))}</h2><div class="strip">'
            + "".join(short_card(p) if title == "Shorts" else self.card(p[0], src=p[1], why=p[3]) for p in row)
            + '</div></section>' for title, row in rows if row)

    def tv_more(self):
        """More videos for a TV Home row that's nearly at its end, so the rows seem endless. row is rec, subs, ntu,
        shorts, more, or a reason ("Because you watched: …"); have is every video already on the page, mine the ones
        in this row. Answers with up to 12 cards: the row's own kind first, then borrowed from the others, and once
        everything known is showing, YouTube's "up next" for the last few videos in the row."""
        f, pid = self.form(), self.prof["id"]
        row, have, cards = f.get("row", ""), set(f.get("have", "").split(",")), []
        def add(items, make, vid_of=lambda it: it["id"]):
            for it in items:
                if len(cards) >= 12:
                    return
                vid = vid_of(it)
                if vid not in have:
                    have.add(vid)
                    cards.append(make(it))
        if row == "shorts":
            add(home_shorts(self.db, pid, 60) + shorts_for(self.db, pid), short_card)
            if len(cards) < 12:
                try:
                    refresh_outside(max_age=90)                  # a new batch from YouTube
                except Exception as ex:
                    print("[outside shorts]", ex)
                add(shorts_for(self.db, pid), short_card)
            return self.send("".join(cards))
        picks = [p for p in home_feed(self.db, pid, 300) if p[1] != "explore"]
        pick = lambda p: self.card(p[0], src=p[1], why=p[3])
        no_vids, no_chans = hidden_sets(self.db, pid)
        uploads = lambda: [r for r in self.db.execute(       # subscriptions' uploads, newest first, going back in time
            "SELECT v.* FROM videos v JOIN subs s ON s.channel_id=v.channel_id AND s.profile_id=? WHERE v.short=0 "
            "AND v.id NOT IN (SELECT video_id FROM history WHERE profile_id=?) ORDER BY v.published DESC LIMIT 400",
            (pid, pid)) if r["id"] not in no_vids and r["channel_id"] not in no_chans]
        sub = lambda r: self.card(r, src="subs", why=WHY["subs"])
        new = lambda r: self.card(r, src="explore", why=WHY["explore"])
        if row == "subs":
            add(uploads(), sub)
        elif row == "ntu":
            add(new_to_you(self.db, pid, 120), new)
        elif row not in ("rec", "more"):
            add([p for p in picks if p[3] == row], pick, lambda p: p[0]["id"])
        add(picks, pick, lambda p: p[0]["id"])                    # then anything the engine picks
        if len(cards) < 12:
            add(new_to_you(self.db, pid, 120), new)
        if len(cards) < 12:
            add(uploads(), sub)
        if len(cards) < 12:              # everything known is on the page: ask YouTube for "up next" of this row's last few
            watched = {r[0] for r in self.db.execute("SELECT video_id FROM history WHERE profile_id=?", (pid,))}
            mine = [v for v in f.get("mine", "").split(",") if VID_RE.fullmatch(v)]
            for src in reversed(mine[-6:]):
                if len(cards) >= 12:
                    break
                try:
                    rel = fetch_related(self.db, src)
                except Exception as ex:
                    print("[tv more]", src, repr(ex))
                    continue
                add([r for r in rel if not r["short"] and r["id"] not in watched and r["id"] not in no_vids
                     and r["channel_id"] not in no_chans], lambda r: self.card(r, src="explore"))
        self.send("".join(cards))

    def impr(self):
        """Home reports the videos that were really on screen (a second or more); passed-over ones sink next time,
        and each source's hit rate is learned from them. A new visit (30+ minutes later) counts again."""
        f, pid, now = self.form(), self.prof["id"], int(time.time())
        rows = [(pid, v, s, now, now) for v, s in zip(f.get("v", "").split(","), f.get("s", "").split(","))
                if VID_RE.match(v) and s in SOURCES][:100]
        self.db.executemany(
            "INSERT INTO impressions(profile_id,video_id,src,first_at,last_at,n) VALUES(?,?,?,?,?,1) ON CONFLICT DO UPDATE "
            "SET n=n+(last_at<excluded.last_at-1800), last_at=excluded.last_at", rows)
        self.send("", 204)

    def rate(self):
        """Thumbs up (r=1) or down (r=-1); the same again takes it back. Answers with the video's rating now."""
        f, pid = self.form(), self.prof["id"]
        vid, r = f.get("v", ""), f.get("r", "")
        if not VID_RE.match(vid) or r not in ("1", "-1"):
            return self.send("", 400)
        val = int(r)
        if self.db.execute("DELETE FROM ratings WHERE profile_id=? AND video_id=? AND val=?", (pid, vid, val)).rowcount:
            val = 0
        else:
            ensure_video(self.db, vid)
            self.db.execute("INSERT OR REPLACE INTO ratings(profile_id,video_id,val,at) VALUES(?,?,?,?)",
                            (pid, vid, val, int(time.time())))
            if val > 0:                                       # its "up next" list feeds Home ("Because you liked")
                threading.Thread(target=warm_one, args=(vid,), daemon=True).start()
        self.send(str(val), ctype="text/plain")

    def newtoyou(self):
        threading.Thread(target=warm_related, args=(self.prof["id"],), daemon=True).start()
        threading.Thread(target=warm_explore, args=(self.prof["id"],), daemon=True).start()
        rows = new_to_you(self.db, self.prof["id"])
        if not rows:
            body = ('<div class="empty"><h1>Nothing new yet</h1><p>Watch a few videos and this page will find '
                    'channels you haven\'t tried, reached through the ones you like.</p>'
                    '<a class="btn" href="/">Back to your feed</a></div>')
        else:
            self.db.executemany("INSERT OR IGNORE INTO shown(profile_id,video_id,first_at) VALUES(?,?,?)",
                                [(self.prof["id"], r["id"], int(time.time())) for r in rows])
            body = (f'<div class="bar"><h1>New to you</h1><span class="meta">Channels you haven\'t watched yet · '
                    f'new picks through the day</span></div><div class="grid">{"".join(self.card(r) for r in rows)}</div>')
        self.send(layout("New to you", body, self.prof, "new"))

    def rated(self, rows):
        """Adds this profile's thumb (r) to each Short, for the player's buttons."""
        mine = dict(self.db.execute("SELECT video_id, val FROM ratings WHERE profile_id=?", (self.prof["id"],)).fetchall())
        for r in rows:
            r["r"] = mine.get(r["id"], 0)
        return rows

    def shorts_more(self):
        """For a Shorts player near the end of its list: asks YouTube for a new batch (at most once every 90 seconds,
        however many players ask) and answers with the Shorts the player doesn't have yet."""
        have = set(self.form().get("have", "").split(","))
        try:
            refresh_outside(max_age=90)
        except Exception as ex:
            print("[outside shorts]", ex)
        rows = [r for r in shorts_for(self.db, self.prof["id"]) if r["id"] not in have]
        self.send(json.dumps(self.rated(rows)), ctype="application/json")

    def shorts(self):
        first = self.q.get("v", "")
        rows = self.rated(shorts_for(self.db, self.prof["id"], first if VID_RE.match(first) else None))
        data = json.dumps(rows).replace("</", "<\\/")
        empty = ('' if rows else '<div class="empty"><h1>No Shorts right now</h1><p>Shorts from all over YouTube show up here. '
                 'A new batch arrives every hour (and a few minutes after a restart).</p></div>')
        body = (SHORTS_CSS + '<div id="sh"><div id="pl"></div><div id="tap"></div>'
                '<a id="x" href="/" aria-label="Close Shorts">✕</a>' + empty +
                '<div id="info"><b id="ti"></b><span id="ch"></span></div><div id="snd" hidden>🔇 Tap for sound</div>'
                f'<div id="pz" hidden>▶</div><div id="rt"><button id="ru" aria-label="I like this">{ICON["up"]}</button>'
                f'<button id="rd" aria-label="Not for me">{ICON["up"]}</button>'
                f'<button id="sr" aria-label="Share">{ICON["share"]}</button></div>'
                '<div id="pb" role="slider" aria-label="Seek"><i><b></b></i></div><div id="tm" hidden></div>'
                '<div id="toast" hidden></div><div id="hint"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>'
                'Press down for next Short</div><div id="ud"><button id="up" aria-label="Previous">▲</button>'
                '<button id="dn" aria-label="Next">▼</button></div></div>'
                + YT_JS + SHORTS_JS.replace("__LIST__", data).replace("__EMBED__", EMBED))
        self.send(layout("Shorts", body))

    def seen(self):
        """The players report here. n=1 (or nothing, as Shorts send) puts the video in History; t and d are how far in
        and how long, saved so the video can resume. A video watched to the end starts over (it stays in Watch later until you remove it)."""
        f, pid = self.form(), self.prof["id"]
        vid = f.get("v", "")
        if VID_RE.match(vid):
            t, d = (int(f[k]) if f.get(k, "").isdigit() else 0 for k in ("t", "d"))
            if f.get("n", "1") != "0":
                ensure_video(self.db, vid)
                mark_watched(self.db, pid, vid)
                threading.Thread(target=after_watch, args=(pid, vid), daemon=True).start()   # Home follows at once
            done = d > 0 and t >= d - max(10, d // 20)
            self.db.execute("UPDATE history SET pos=?, best=MAX(COALESCE(best,0),?), dur=CASE WHEN ?>0 THEN ? ELSE dur END "
                            "WHERE profile_id=? AND video_id=?", (0 if done or t < 5 else t, t, d, d, pid, vid))
            if d > 0:
                self.db.execute("UPDATE videos SET length=? WHERE id=? AND COALESCE(length,0)=0", (d, vid))
        self.send("", 204)

    # -- watch later, not interested
    def later(self):
        rows = self.db.execute(
            "SELECT v.*, h.video_id AS seen FROM later l JOIN videos v ON v.id=l.video_id "
            "LEFT JOIN history h ON h.video_id=v.id AND h.profile_id=l.profile_id WHERE l.profile_id=? "
            "ORDER BY l.added_at DESC, l.rowid DESC", (self.prof["id"],)).fetchall()
        if rows:
            body = (f'<div class="bar"><h1>Watch later</h1><span class="meta">Stays here until you remove it</span></div>'
                    f'<div class="grid">{"".join(self.card(r, r["seen"]) for r in rows)}</div>')
        else:
            body = ('<div class="empty"><h1>Nothing saved yet</h1><p>Use the Watch later button under a video to keep it here for another time.</p><a class="btn" href="/">Back to your feed</a></div>')
        self.send(layout("Watch later", body, self.prof, "later"))

    def later_toggle(self):
        f, pid, on = self.form(), self.prof["id"], 0
        vid = f.get("v", "")
        if VID_RE.match(vid) and not self.db.execute("DELETE FROM later WHERE profile_id=? AND video_id=?", (pid, vid)).rowcount:
            ensure_video(self.db, vid)
            self.db.execute("INSERT INTO later(profile_id,video_id,added_at) VALUES(?,?,?)", (pid, vid, int(time.time())))
            on = 1
        if f.get("inline"):
            return self.send(later_button(vid, on) if VID_RE.match(vid) else "", 200 if VID_RE.match(vid) else 400)
        if f.get("next"):
            return self.back(f, "/later")
        self.send(str(on), ctype="text/plain")              # the + on a video card asks for the new state

    def hide(self):
        """Not interested: v = a video, c = a whole channel; undo=1 takes it back. Only affects Home / New to you."""
        f, pid = self.form(), self.prof["id"]
        for kind, ref, ok in (("v", f.get("v", ""), VID_RE), ("c", f.get("c", ""), CID_RE)):
            if ok.fullmatch(ref):
                if f.get("undo"):
                    self.db.execute("DELETE FROM hidden WHERE profile_id=? AND kind=? AND ref=?", (pid, kind, ref))
                else:
                    self.db.execute("INSERT OR IGNORE INTO hidden(profile_id,kind,ref,added_at) VALUES(?,?,?,?)",
                                    (pid, kind, ref, int(time.time())))
        if f.get("js"):
            return self.send("", 204)                        # the TV's hold-OK menu needs no page back
        self.back(f)

    def vstate(self):
        """A video's thumb and Watch later state, for the TV's hold-OK menu."""
        vid, pid = self.q.get("v", ""), self.prof["id"]
        if not VID_RE.fullmatch(vid):
            return self.send("", 400)
        r = self.db.execute("SELECT val FROM ratings WHERE profile_id=? AND video_id=?", (pid, vid)).fetchone()
        later = self.db.execute("SELECT 1 FROM later WHERE profile_id=? AND video_id=?", (pid, vid)).fetchone()
        self.send(json.dumps({"r": r[0] if r else 0, "later": bool(later)}), ctype="application/json")

    def feed_shorts(self):
        self.db.execute("UPDATE profiles SET feed_shorts=1-COALESCE(feed_shorts,1) WHERE id=?", (self.prof["id"],))
        self.redirect("/feed")

    def refresh(self):
        refresh_stale(force=True)
        self.redirect("/feed")

    # -- watch
    def go(self):
        q = self.q.get("q", "").strip()
        vid = video_id_from(q) if "youtu" in q else None   # bare words are searches, even 11-letter ones
        if vid:
            return self.redirect(("/shorts?v=" if "/shorts/" in q else "/watch?v=") + vid)
        if q.startswith("@") or "youtube.com/" in q:        # a channel link: offer to subscribe
            return self.redirect("/subs?add=" + urllib.parse.quote(q))
        self.redirect("/search?q=" + urllib.parse.quote(q) if q else "/")

    def search(self):
        q = self.q.get("q", "").strip()[:200]
        if not q:
            return self.send(layout("Search", '<form class="go big" action="/go"><input type="text" name="q" autofocus '
                                    'placeholder="Search, or paste a YouTube link…" autocomplete="off"></form>', self.prof, "search"))
        pool = ThreadPoolExecutor(1)
        chans = pool.submit(search_channels, q)              # looked up while the videos load
        try:
            vids = search(self.db, q)
            save_search(self.db, self.prof["id"], q, vids)    # Home draws on what you search for
            err = ""
        except Exception as ex:
            print("[search]", repr(ex))
            vids, err = [], "Search isn't working right now (YouTube may have changed something). Pasting a link still works."
        try:
            chans = chans.result()
        except Exception as ex:
            print("[search channels]", repr(ex))
            chans = []
        pool.shutdown(wait=False)
        subbed = {r[0] for r in self.db.execute("SELECT channel_id FROM subs WHERE profile_id=?", (self.prof["id"],))}
        nxt = "/search?q=" + urllib.parse.quote(q)
        found = "".join(
            f'<div><div class="av" style="background:{COLORS[sum(map(ord, ch["id"])) % len(COLORS)]}">{e(ch["title"][:1].upper())}</div>'
            f'<div class="grow"><a class="ch" href="/channel?id={ch["id"]}"><strong>{e(ch["title"])}</strong></a>'
            f'<div class="meta">{e(ch["info"])}</div></div>{sub_button(ch["id"], ch["id"] in subbed, nxt)}</div>' for ch in chans)
        seen = {r[0] for r in self.db.execute("SELECT video_id FROM history WHERE profile_id=?", (self.prof["id"],))}
        grid = "".join(self.card(v, v["id"] in seen) for v in vids)
        body = (f'<h1>Results for “{e(q)}”</h1>' + (f'<div class="err">{e(err)}</div>' if err else "")
                + (f'<div class="list" style="margin-bottom:22px">{found}</div>' if found else "")
                + (f'<div class="grid">{grid}</div>' if grid else ("" if err else '<p class="meta">No videos found.</p>')))
        self.send(layout(q, body, self.prof, q=q))

    def related(self):
        vid = self.q.get("v", "")
        if not VID_RE.match(vid):
            return self.send("", 400)
        try:
            rows = fetch_related(self.db, vid)
        except Exception as ex:
            print("[related]", vid, repr(ex))
            rows = []
        seen = {r[0] for r in self.db.execute("SELECT video_id FROM history WHERE profile_id=?", (self.prof["id"],))}
        self.send("".join(side_row(r, r["id"] in seen) for r in rows))

    def watch(self):
        vid = self.q.get("v", "")
        if not VID_RE.match(vid):
            return self.redirect("/")
        pid = self.prof["id"]
        v = ensure_video(self.db, vid)
        at = self.db.execute("SELECT pos FROM history WHERE profile_id=? AND video_id=?", (pid, vid)).fetchone()
        t0 = str(at["pos"] or 0) if at else "0"              # pick up where this profile left off
        saved = self.db.execute("SELECT 1 FROM later WHERE profile_id=? AND video_id=?", (pid, vid)).fetchone()
        mine = self.db.execute("SELECT val FROM ratings WHERE profile_id=? AND video_id=?", (pid, vid)).fetchone()
        sub_later = later_button(vid, saved)
        subbed = v["channel_id"] and self.db.execute(
            "SELECT 1 FROM subs WHERE profile_id=? AND channel_id=?", (pid, v["channel_id"])).fetchone()
        nxt = e("/watch?v=" + vid)
        if subbed or v["channel_id"] or v["channel_url"]:
            sub = sub_button(v["channel_id"] or v["channel_url"], subbed, "/watch?v=" + vid, vid)
        else:
            sub = ""
        side = ""
        if v["channel_id"]:
            rows = self.db.execute("SELECT * FROM videos WHERE channel_id=? AND id<>? ORDER BY published DESC LIMIT 6",
                                   (v["channel_id"], vid)).fetchall()
            side = "".join(side_row(r) for r in rows)
        meta = " · ".join(x for x in (views(v["views"]), ago(v["published"])) if x)
        chan = f'<strong>{e(v["channel_title"] or "")}</strong>'
        if v["channel_id"] or v["channel_url"]:              # opens the channel page
            chan = f'<a class="ch" href="/channel?id={urllib.parse.quote(v["channel_id"] or v["channel_url"], safe="")}">{chan}</a>'
        if self.tv():
            top = f'<b>{e(v["title"] or "")}</b><span>{e(" · ".join(x for x in (v["channel_title"] or "", meta) if x))}</span>'
            acts = rate_buttons(vid, mine[0] if mine else 0) + sub_later
            player = TRACK_JS + (TV_PLAYER.replace("__VID__", vid).replace("__T0__", t0).replace("__TOP__", top)
                                 .replace("__CHAN__", self.tv_channel(v, sub)).replace("__ACTS__", acts)
                                 .replace("__HL__", REGION["hl"]))
        else:
            player = WEB_PLAYER.replace("__VID__", vid).replace("__T0__", t0)
        body = (f'<div class="watch"><div>{player}'
                f'<h1>{e(v["title"] or "Video")}</h1>'
                f'<div class="bar">{chan}{sub}{rate_buttons(vid, mine[0] if mine else 0)}{sub_later}'
                f'<span class="meta" style="margin-left:auto">{meta}</span>'
                f'<a class="meta" href="https://www.youtube.com/watch?v={vid}" target="_blank" rel="noopener">Open on YouTube ↗</a></div></div>'
                f'<aside><h3 style="margin-top:0">Up next</h3><div id="upnext"><p class="meta">Loading…</p></div>'
                f'{"<h3>More from this channel</h3>" + side if side else ""}</aside></div>'
                # one tiny script: load related videos after the player has started
                f'<script>fetch("/related?v={vid}").then(r=>r.text()).then(h=>{{document.getElementById("upnext")'
                f'.innerHTML=h||"<p class=meta>Nothing to suggest.</p>"}})</script>')
        self.send(layout(v["title"] or "Watch", body, self.prof, bare=True))

    # -- subscriptions
    def subs(self, err=""):
        pid = self.prof["id"]
        rows = self.db.execute(
            "SELECT ch.*, (SELECT MAX(published) FROM videos WHERE channel_id=ch.id) AS last "
            "FROM subs s JOIN channels ch ON ch.id=s.channel_id WHERE s.profile_id=? ORDER BY ch.title COLLATE NOCASE",
            (pid,)).fetchall()
        items = "".join(
            f'<div><div class="av" style="background:{COLORS[sum(map(ord, r["id"])) % len(COLORS)]}">{e(r["title"][:1].upper())}</div>'
            f'<div class="grow"><a class="ch" href="/channel?id={r["id"]}"><strong>{e(r["title"])}</strong></a><div class="meta">'
            f'{"Last upload " + ago(r["last"]) if r["last"] else "No videos fetched yet"}</div></div>'
            f'<a class="meta" href="https://www.youtube.com/channel/{r["id"]}" target="_blank" rel="noopener">YouTube ↗</a>'
            f'<form method="post" action="/subs/remove"><input type="hidden" name="id" value="{r["id"]}">'
            f'<button class="btn ghost small">Unsubscribe</button></form></div>' for r in rows)
        pre = e(self.q.get("add", ""))
        got = self.q.get("imported", "")
        note = (f'<div class="note">Added {int(got)} new subscription{"" if got == "1" else "s"} '
                f'({int(self.q["of"]) if self.q.get("of", "").isdigit() else got} channels found). '
                f'Their names and videos fill in over the next few minutes.</div>' if got.isdigit() else "")
        off = self.db.execute(
            "SELECT h.ref, COALESCE((SELECT title FROM channels WHERE id=h.ref), (SELECT channel_title FROM videos WHERE "
            "channel_id=h.ref AND channel_title!='' LIMIT 1), h.ref) AS title FROM hidden h WHERE h.profile_id=? AND h.kind='c' "
            "ORDER BY title COLLATE NOCASE", (pid,)).fetchall()
        hidden = "".join(
            f'<div><div class="grow"><a class="ch" href="/channel?id={r["ref"]}"><strong>{e(r["title"])}</strong></a></div>'
            f'<form method="post" action="/hide"><input type="hidden" name="c" value="{r["ref"]}"><input type="hidden" name="undo" value="1">'
            f'<input type="hidden" name="next" value="/subs"><button class="btn ghost small">Unhide</button></form></div>' for r in off)
        port = ('<details class="imp"><summary class="meta">Import or export subscriptions</summary>'
                '<form method="post" action="/subs/import"><p class="meta">From YouTube: Google Takeout → “YouTube and YouTube Music” → '
                'subscriptions → <b>subscriptions.csv</b>. Choose the Takeout .zip as it downloaded, or that file, or paste '
                'any list of channel links. Export saves this profile\'s list in the same format (handy for copying it to another profile).</p>'
                '<input type="file" accept=".zip,.csv,.txt,.opml,.xml,application/zip,text/*"><p class="meta zipmsg"></p>'
                '<textarea name="text" rows="5" required placeholder="Channel Id,Channel Url,Channel Title"></textarea>'
                '<button class="btn small">Import</button> <a class="btn ghost small" href="/subs/export">Export</a></form></details>'
                + IMPORT_JS)
        body = (f'<h1>Subscriptions <span class="meta">({len(rows)})</span></h1>'
                + (f'<div class="err">{e(err)}</div>' if err else "") + note
                + f'<form method="post" action="/subs/add" class="bar"><input type="text" name="url" value="{pre}" required '
                  f'placeholder="Channel link, @handle, or any video link from the channel" style="flex:1;max-width:560px">'
                  f'<button class="btn">Subscribe</button></form>' + port
                + (f'<div class="list">{items}</div>' if rows else '<p class="meta">No subscriptions yet.</p>')
                + (f'<h3 style="margin:28px 0 12px">Hidden from suggestions</h3><div class="list">{hidden}</div>' if hidden else ""))
        self.send(layout("Channels", body, self.prof, "subs"))

    def channel(self):
        """One channel's videos, newest first, whether or not you're subscribed."""
        cid, pid = self.q.get("id", "").strip(), self.prof["id"]
        if not CID_RE.fullmatch(cid):                        # a handle or channel link: look up its id first
            try:
                return self.redirect("/channel?id=" + resolve_channel(cid))
            except Exception as ex:
                print("[channel]", repr(ex))
                return self.send(layout("Channel", '<div class="empty"><h1>Channel not found</h1>'
                                        '<a class="btn" href="/">Back to your feed</a></div>', self.prof), 404)
        page = max(1, int(self.q.get("page", "1")) if self.q.get("page", "").isdigit() else 1)
        find = lambda: self.db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        ch, err = find(), ""
        if CHANNEL_AT.get(cid, 0) < time.time() - REFRESH_MIN * 60:
            CHANNEL_AT[cid] = time.time()
            try:
                refresh_channel(self.db, cid, deep=True)
            except Exception as ex:
                print("[channel]", cid, repr(ex))
                err = "Couldn't check this channel for new videos just now."
            ch = find()
        rows = self.db.execute(
            "SELECT v.*, h.video_id AS seen FROM videos v LEFT JOIN history h ON h.video_id=v.id AND h.profile_id=? "
            "WHERE v.channel_id=? ORDER BY v.published DESC LIMIT ? OFFSET ?",
            (pid, cid, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)).fetchall()
        title = ch["title"] if ch else next((r["channel_title"] for r in rows if r["channel_title"]), "Channel")
        nxt = f"/channel?id={cid}"
        sub = sub_button(cid, self.db.execute("SELECT 1 FROM subs WHERE profile_id=? AND channel_id=?", (pid, cid)).fetchone(), nxt)
        off = self.db.execute("SELECT 1 FROM hidden WHERE profile_id=? AND kind='c' AND ref=?", (pid, cid)).fetchone()
        sub += (f'<form method="post" action="/hide"><input type="hidden" name="c" value="{cid}">'
                f'<input type="hidden" name="next" value="{nxt}">{"<input type=hidden name=undo value=1>" if off else ""}'
                f'<button class="btn ghost small" title="Keeps this channel out of Home and New to you">'
                f'{"Hidden from suggestions ✓" if off else "Hide from suggestions"}</button></form>')
        more = len(rows) > PAGE_SIZE
        grid = "".join(self.card(v, v["seen"]) for v in rows[:PAGE_SIZE])
        body = (f'<div class="bar"><h1>{e(title)}</h1><div class="acts">{sub}</div>'
                f'<a class="meta" href="https://www.youtube.com/channel/{cid}" target="_blank" rel="noopener">YouTube ↗</a></div>'
                + (f'<div class="err">{e(err)}</div>' if err else "")
                + (f'<div class="grid">{grid}</div>' if grid else '<p class="meta">No videos found for this channel.</p>')
                + (f'<p class="center" style="margin-top:30px"><a class="btn ghost" href="/channel?id={cid}&page={page + 1}">Load more</a></p>' if more else ""))
        self.send(layout(title, body, self.prof))

    def sub_add(self):
        f = self.form()
        try:
            cid = resolve_channel(f.get("url", ""))
            refresh_channel(self.db, cid)
        except Exception as ex:
            if f.get("inline"):
                return self.send(f"Couldn't subscribe: {ex}", 400, ctype="text/plain")
            return self.subs(err=f"Couldn't subscribe: {ex}")
        self.db.execute("INSERT OR IGNORE INTO subs(profile_id,channel_id,added_at) VALUES(?,?,?)",
                        (self.prof["id"], cid, int(time.time())))
        if VID_RE.match(f.get("v", "")):
            self.db.execute("UPDATE videos SET channel_id=? WHERE id=? AND channel_id IS NULL", (cid, f["v"]))
        nxt = f.get("next", "")
        if f.get("inline"):                                  # the page script: answer with the new button
            return self.send(sub_button(cid, True, nxt))
        self.redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/subs")

    def sub_import(self):
        """Subscribe to every channel id in a pasted list: YouTube's Takeout subscriptions.csv, an OPML file, or plain links."""
        text, pid, now = self.form().get("text", ""), self.prof["id"], int(time.time())
        titles = {}
        try:
            for row in csv.reader(io.StringIO(text)):       # Takeout: Channel Id, Channel Url, Channel Title
                if len(row) > 2 and CID_RE.fullmatch(row[0].strip()):
                    titles[row[0].strip()] = row[2].strip()
        except csv.Error:
            pass
        ids = list(dict.fromkeys(re.findall(r"(?<![\w-])UC[\w-]{22}(?![\w-])", text)))[:2000]
        count = lambda: self.db.execute("SELECT COUNT(*) FROM subs WHERE profile_id=?", (pid,)).fetchone()[0]
        before = count()
        for cid in ids:                                     # fetched_at=0: the refresher picks them up straight away
            self.db.execute("INSERT OR IGNORE INTO channels(id,title,fetched_at) VALUES(?,?,0)", (cid, titles.get(cid) or cid))
            self.db.execute("INSERT OR IGNORE INTO subs(profile_id,channel_id,added_at) VALUES(?,?,?)", (pid, cid, now))
        threading.Thread(target=refresh_stale, daemon=True).start()
        self.redirect(f"/subs?imported={count() - before}&of={len(ids)}")

    def sub_export(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Channel Id", "Channel Url", "Channel Title"])
        for r in self.db.execute("SELECT ch.id, ch.title FROM subs s JOIN channels ch ON ch.id=s.channel_id WHERE s.profile_id=? "
                                 "ORDER BY ch.title COLLATE NOCASE", (self.prof["id"],)):
            w.writerow([r["id"], "http://www.youtube.com/channel/" + r["id"], r["title"]])
        self.send(out.getvalue(), ctype="text/csv; charset=utf-8",
                  headers=[("Content-Disposition", 'attachment; filename="subscriptions.csv"')])

    def sub_remove(self):
        f = self.form()
        self.db.execute("DELETE FROM subs WHERE profile_id=? AND channel_id=?", (self.prof["id"], f.get("id", "")))
        nxt = f.get("next", "")
        if f.get("inline"):
            return self.send(sub_button(f.get("id", ""), False, nxt))
        self.redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/subs")

    # -- history
    def history(self):
        rows = self.db.execute(
            "SELECT v.*, h.watched_at FROM history h JOIN videos v ON v.id=h.video_id WHERE h.profile_id=? "
            "ORDER BY h.watched_at DESC LIMIT 200", (self.prof["id"],)).fetchall()
        n = self.db.execute("SELECT COUNT(*) FROM history WHERE profile_id=?", (self.prof["id"],)).fetchone()[0]
        items = "".join(
            f'<div><a href="/watch?v={r["id"]}"><img src="{thumb(r["id"])}" loading="lazy" alt="" '
            f'style="width:120px;aspect-ratio:16/9;object-fit:cover;border-radius:6px;display:block"></a>'
            f'<a class="grow" href="/watch?v={r["id"]}"><strong>{e(r["title"] or r["id"])}</strong>'
            f'<div class="meta">{e(r["channel_title"] or "")} · watched {ago(r["watched_at"])}</div></a>'
            f'<form method="post" action="/history/remove" data-sure="Remove"><input type="hidden" name="v" value="{r["id"]}">'
            f'<button class="btn ghost small" title="Remove from history">✕</button></form></div>' for r in rows)
        body = ('<div class="bar"><h1>Watch history</h1>'
                + (ask_first("/history/clear", {}, "Clear all", f"Clear all {n} video{'s' if n != 1 else ''} from History?",
                             "btn ghost small") if rows else "")
                + "</div>" + (f'<div class="list">{items}</div>' if rows else '<p class="meta">Nothing watched yet.</p>'))
        self.send(layout("History", body, self.prof, "hist"))

    # -- settings
    def settings(self):
        """Settings, laid out like the YouTube TV app's: setting names down the left in groups, the selected one on the
        right, centred: a big title, one short line, wide round buttons. On the TV: server address (TV app only) and
        autoplay. For everyone: language and location (the whole of YakTube) and this profile's hidden channels, watch
        history and search history; then App version."""
        pid, name, tv = self.prof["id"], e(self.prof["name"]), self.tv()
        rows, panes = [], []

        def add(k, title, line, inner):
            rows.append(f'<button type="button" class="srow" data-p="{k}" id="r-{k}">{title}</button>')
            panes.append(f'<section class="spane" id="p-{k}" hidden><h2>{title}</h2>{f"<p>{line}</p>" if line else ""}{inner}</section>')

        def opts(k, items, cur, hidden=False):
            return (f'<div class="sopts" data-k="{k}"{" hidden" if hidden else ""}>' + "".join(
                f'<button type="button" class="sopt{" on" if v == cur else ""}" data-v="{v}">{n}</button>' for v, n in items) + '</div>')

        def pick(k, label, items, cur):   # one round button ("Location: Canada ✎"); OK on it shows the choices
            return (f'<button type="button" class="sopt sedit" data-open="{k}">{label}: <span>{dict(items).get(cur, cur)}</span>'
                    f'<i>✎</i></button>' + opts(k, items, cur, True))

        def ask(what, label):
            return ask_first("/settings/clear", {"what": what}, label, "")

        if tv:
            rows.append("<h4>This TV</h4>")
        if "YakTubeTV" in self.headers.get("User-Agent", ""):    # the TV app only: a browser has its own address bar
            add("addr", "Server address", "Where this TV finds YakTube.",
                f'<a class="sopt sedit" href="yaktube:setup">{e(self.headers.get("Host", ""))}<i>✎</i></a>')
        if tv:
            add("ap", "Autoplay next video", "When a video ends, the next one plays.", opts("ap", (("1", "On"), ("0", "Off")), ""))
        if not tv:                                            # light or dark: kept in this browser (the TV is always dark)
            rows.append("<h4>Appearance</h4>")
            add("theme", "Theme", "", opts("theme", (("", "System"), ("light", "Light"), ("dark", "Dark")), ""))
        rows.append("<h4>Language &amp; Location</h4>")
        add("lang", "Language", "For captions, search and suggestions. Menus stay in English.", pick("hl", "Language", LANGS, REGION["hl"]))
        add("loc", "Location", "Shapes the Shorts and suggestions you see.", pick("gl", "Location", REGIONS, REGION["gl"]))
        rows.append("<h4>History &amp; Data</h4>")
        hid = self.db.execute(
            "SELECT h.ref, COALESCE(ch.title, (SELECT channel_title FROM videos WHERE channel_id=h.ref AND channel_title<>'' LIMIT 1), "
            "h.ref) AS title FROM hidden h LEFT JOIN channels ch ON ch.id=h.ref WHERE h.profile_id=? AND h.kind='c' "
            "ORDER BY h.added_at DESC", (pid,)).fetchall()
        add("hidden", "Hidden channels", "Kept out of Home and New to you." if hid else "None hidden.",
            "".join(f'<div class="shid"><i class="av" style="background:{COLORS[sum(map(ord, r["ref"])) % len(COLORS)]}">'
                    f'{e((r["title"] or "?")[:1].upper())}</i><span class="nm">{e(r["title"])}</span>'
                    f'<button type="button" class="btn ghost small" data-c="{e(r["ref"])}">Recommend again</button></div>' for r in hid))
        n = self.db.execute("SELECT COUNT(*) FROM history WHERE profile_id=?", (pid,)).fetchone()[0]
        add("watch", "Clear watch history",
            f"Removes {name}'s {n} watched video{'s' if n != 1 else ''}." if n else f"{name}'s History is empty.",
            ask("watch", "Clear watch history") if n else "")
        n = self.db.execute("SELECT COUNT(*) FROM searches WHERE profile_id=? AND auto=0", (pid,)).fetchone()[0]
        add("search", "Clear search history",
            f"Removes {name}'s {n} search{'es' if n != 1 else ''}." if n else "No searches remembered.",
            ask("search", "Clear search history") if n else "")
        rows.append('<h4>Plugins</h4><button type="button" class="srow" data-p="scout" id="r-scout">YakScout</button>')
        panes.append(scout_pane(self.db))
        rows.append("<h4>About</h4>")
        ua = self.headers.get("User-Agent", "")
        tvapp = re.search(r"YakTubeTV/(\S+)", ua)
        tvver = (tvapp.group(1) if tvapp.group(1) != "1" else "1.6 or earlier") if tvapp else ""   # before 1.7 they all said "1"
        dev = re.search(r"Android ([\d.]+); ([^;)]+?)(?: Build/|\))", ua) if tvapp else None   # e.g. "Chromecast, Android 12"
        facts = [("Version", VERSION), ("TV app", tvver),
                 ("Device", f"{dev.group(2).strip()}, Android {dev.group(1)}" if dev else ""),
                 ("Region", REGION["gl"]), ("Language", REGION["hl"]), ("Hosting", "Self-hosted")]
        add("about", "App version", "", '<div class="sver">' + "".join(f'<span>{k}</span><b>{e(v)}</b>' for k, v in facts if v) + '</div>')
        body = ('<div class="bar"><h1>Settings</h1></div><div class="sets">'
                f'<div class="slist">{"".join(rows)}</div><div>{"".join(panes)}</div></div>' + SETTINGS_JS + SCOUT_JS)
        self.send(layout("Settings", body, self.prof, "set"))

    def settings_region(self):
        """Region and language: saved for the whole of YakTube. The next Shorts batch is asked for straight away."""
        f = self.form()
        k, v = f.get("k", ""), f.get("v", "")
        if v in dict({"gl": REGIONS, "hl": LANGS}.get(k, ())):
            self.db.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, v))
            REGION[k] = v
            OUTSIDE_AT[0] = 0
            return self.send("", 204)
        self.send("", 400)

    def settings_scout(self):
        """Settings > YakScout: the two switches, and where the YakScout plugin is (an address, or address:port)."""
        f = self.form()
        k, v = f.get("k", ""), f.get("v", "").strip()
        if k in ("text", "on") and v in ("0", "1"):
            scout_save(self.db, k, v)
        elif k == "min" and v.isdigit() and 30 <= int(v) <= 90:
            scout_save(self.db, k, str(int(v)))
            return self.send("", 204)
        elif k == "days" and v in ("30", "60", "90"):
            scout_save(self.db, k, v)
            return self.send("", 204)
        elif k == "addr":
            host = re.sub(r"^[a-z]+://", "", v, flags=re.I).split("/")[0]
            port = f.get("port", "").strip() or SCOUT_CFG["port"]
            if host.count(":") == 1:                                 # "192.168.1.31:8095" in the address box
                host, port = host.split(":")
            if host and not SCOUT_HOST_RE.fullmatch(host):
                return self.send(json.dumps({"error": "Not an address"}), 400, ctype="application/json")
            if not (port.isdigit() and 0 < int(port) < 65536):
                return self.send(json.dumps({"error": "Port is 1-65535"}), 400, ctype="application/json")
            scout_save(self.db, "addr", host)
            scout_save(self.db, "port", str(int(port)))
            if host and SCOUT_CFG["on"] != "1":
                scout_save(self.db, "on", "1")                         # giving it an address means "use it"
        else:
            return self.send("", 400)
        SCOUT_LINK.update(state="", error="")
        SCOUT_WAKE.set()
        self.send(json.dumps({"addr": SCOUT_CFG["addr"], "port": SCOUT_CFG["port"], "on": SCOUT_CFG["on"]}), ctype="application/json")

    def scout_keep(self):
        vid = self.form().get("v", "")
        self.send("", 204 if VID_RE.match(vid) and scout_keep(self.db, vid) else 400)

    def scout_channel(self):
        """Settings > YakScout: add a channel to Farm channels or Never drop, or take it off."""
        f = self.form()
        ok = scout_set_channel(self.db, f.get("kind", ""), f.get("v", ""), f.get("op") != "remove")
        self.send(json.dumps({"ok": True} if ok else {"error": "Type a channel name"}), 200 if ok else 400,
                  ctype="application/json")

    def scout_voice(self):
        """Settings > YakScout > Voices: have the YakScout plugin learn a farm Short's voice, or forget one."""
        f = self.form()
        op, v = f.get("op", ""), f.get("v", "").strip()

        def answer(obj, code=200):
            self.send(json.dumps(obj), code, ctype="application/json")
        if SCOUT_LINK["state"] != "ok":
            return answer({"error": "YakScout isn't connected"}, 503)
        try:
            if op == "learn":
                vid = video_id_from(v)
                if not vid:
                    return answer({"error": "Not a YouTube link"}, 400)
                r = scout_call("/learn", {"id": vid}, timeout=180)
                msg = ("New voice" if r.get("new") else f"Same voice as {r.get('name', '?')}") + \
                      (f" · {len(r.get('channels', []))} channels" if len(r.get("channels", [])) > 1 else "")
            elif op == "forget" and v:
                scout_call("/forget", {"name": v}, timeout=60)
                msg = "Forgotten"
            else:
                return answer({"error": "?"}, 400)
        except urllib.error.HTTPError as ex:
            try:
                why = json.loads(ex.read()).get("error", "")
            except Exception:
                why = ""
            return answer({"error": (why or "YakScout couldn't do it")[:120].capitalize()}, 502)
        except Exception:
            return answer({"error": "YakScout didn't answer"}, 502)
        SCOUT_WAKE.set()                                             # fetch the new voice list
        answer({"ok": True, "msg": msg})

    def scout_stats(self):
        self.send(json.dumps(scout_stats(self.db)), ctype="application/json", headers=[("Cache-Control", "no-store")])

    def scout_log_csv(self):
        """Settings > YakScout > Log: every verdict YakScout still remembers, one row per Short, for a spreadsheet."""
        name = time.strftime("yakscout-log-%Y-%m-%d.csv")
        self.send(self.scout_log_text(), ctype="text/csv; charset=utf-8",
                  headers=[("Content-Disposition", f'attachment; filename="{name}"'), ("Cache-Control", "no-store")])

    def scout_log_text(self):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Date", "Verdict", "Check", "Score", "Title", "Channel", "Reasons", "Link"])
        for r in self.db.execute("SELECT s.video_id, s.score, s.farm, s.why, s.at, s.deep, s.kept, v.title, v.channel_title "
                                 "FROM scout s LEFT JOIN videos v ON v.id=s.video_id ORDER BY s.at DESC"):
            w.writerow([time.strftime("%Y-%m-%d %H:%M", time.localtime(r["at"] or 0)),
                        "Dropped" if r["farm"] else "Brought back" if r["kept"] else "Kept",
                        "Scanned" if r["deep"] else "Read", r["score"], r["title"] or "", r["channel_title"] or "",
                        r["why"] or "", "https://www.youtube.com/shorts/" + r["video_id"]])
        return "\ufeff" + out.getvalue()                                    # the mark tells Excel it's UTF-8

    def scout_review_route(self):
        """Settings > YakScout > Submissions > Check: check every open submission now (takes a while: it listens)."""
        r = scout_review()
        self.send(json.dumps({"ok": not r["error"], "error": r["error"], "n": len(r["items"])}), ctype="application/json")

    def scout_share(self):
        """Settings > YakScout > Voices > Share: a learned voice as a file for the shared voice list (from the plugin)."""
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("name", [""])[0]
        if SCOUT_LINK["state"] != "ok" or not name:
            return self.send("YakScout isn't connected", 503, ctype="text/plain; charset=utf-8")
        try:
            entry = scout_call("/share?name=" + urllib.parse.quote(name), timeout=10)
        except Exception:
            return self.send("YakScout couldn't share that voice", 502, ctype="text/plain; charset=utf-8")
        fname = re.sub(r"[^a-z0-9-]", "-", str(entry.get("id", "voice")))[:40]
        self.send(json.dumps(entry, indent=1), ctype="application/json",
                  headers=[("Content-Disposition", f'attachment; filename="voice-{fname}.json"')])

    def scout_backup(self):
        """Settings > YakScout > Advanced > Backup: a .zip with backup.json (your channel lists, the Shorts you brought
        back, the Advanced settings and, while the plugin is connected, its learned voices: what Restore reads back)
        and log.csv (every verdict, for reading)."""
        voices = []
        if SCOUT_LINK["state"] == "ok":
            try:
                voices = scout_call("/voices", timeout=10).get("voices", [])
            except Exception:
                pass
        out = {"yakscout_backup": 1, "made": time.strftime("%Y-%m-%d %H:%M"),
               "settings": {"min": SCOUT_CFG["min"], "days": SCOUT_CFG["days"]},
               "channels": [{"name": n, "kind": k} for k, d in scout_lists(self.db).items() for n in d.values()],
               "brought_back": [dict(r) for r in self.db.execute(
                   "SELECT s.video_id AS id, v.title, v.channel_title AS channel FROM scout s "
                   "LEFT JOIN videos v ON v.id=s.video_id WHERE s.kept=1")],
               "voices": voices,
               "dropped": [dict(r) for r in self.db.execute(        # for reading; Restore doesn't use these
                   "SELECT s.video_id AS id, v.title, v.channel_title AS channel, s.score, s.why, s.at FROM scout s "
                   "LEFT JOIN videos v ON v.id=s.video_id WHERE s.farm=1 ORDER BY s.at DESC")]}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("backup.json", json.dumps(out, indent=1))
            z.writestr("log.csv", self.scout_log_text())
        name = time.strftime("yakscout-backup-%Y-%m-%d.zip")
        self.send(buf.getvalue(), ctype="application/zip",
                  headers=[("Content-Disposition", f'attachment; filename="{name}"'), ("Cache-Control", "no-store")])

    def scout_restore(self):
        """Settings > YakScout > Advanced > Restore: read a Backup file back in. Adds to what's there (nothing is
        removed): channels go back on their lists, Shorts you brought back stay back, voices go to the plugin."""
        def answer(obj, code=200):
            self.send(json.dumps(obj), code, ctype="application/json")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8_000_000:
            return answer({"error": "File too big"}, 400)
        raw = self.rfile.read(n)
        try:
            if raw[:2] == b"PK":                                     # the .zip Backup makes: read its backup.json
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    info = next((i for i in z.infolist() if i.filename.rsplit("/", 1)[-1] == "backup.json"), None)
                    if not info or info.file_size > 20_000_000:
                        return answer({"error": "No backup.json in that zip"}, 400)
                    raw = z.read(info)
            b = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, zipfile.BadZipFile, UnicodeDecodeError):
            return answer({"error": "Not a YakScout backup"}, 400)
        if not isinstance(b, dict) or b.get("yakscout_backup") != 1:
            return answer({"error": "Not a YakScout backup"}, 400)
        c, got = self.db, {"channels": 0, "back": 0, "voices": 0}
        for ch in b.get("channels", []) if isinstance(b.get("channels"), list) else []:
            if not (isinstance(ch, dict) and isinstance(ch.get("name"), str)):
                continue
            if ch.get("kind") in ("farm", "keep"):
                got["channels"] += scout_set_channel(c, ch["kind"], ch["name"])
            elif ch.get("kind") == "ok" and ch["name"].strip():
                name = " ".join(ch["name"].split())[:100]
                c.execute("INSERT OR REPLACE INTO scout_chan(key, name, kind, at) VALUES(?,?,'ok',?)",
                          (name.lower(), name, int(time.time())))
                got["channels"] += 1
        for r in b.get("brought_back", []) if isinstance(b.get("brought_back"), list) else []:
            vid = r.get("id") if isinstance(r, dict) else None
            if isinstance(vid, str) and VID_RE.fullmatch(vid):
                if not scout_keep(c, vid):
                    c.execute("INSERT INTO scout(video_id,score,farm,why,at,kept) VALUES(?,0,0,'brought back',?,1)",
                              (vid, int(time.time())))
                got["back"] += 1
        st = b.get("settings") if isinstance(b.get("settings"), dict) else {}
        if str(st.get("min", "")).isdigit() and 30 <= int(st["min"]) <= 90:
            scout_save(c, "min", str(int(st["min"])))
        if st.get("days") in ("30", "60", "90"):
            scout_save(c, "days", st["days"])
        voices, skipped = b.get("voices") if isinstance(b.get("voices"), list) else [], ""
        if voices and SCOUT_LINK["state"] == "ok":
            try:
                got["voices"] = int(scout_call("/voices", {"voices": voices}, timeout=60).get("added", 0))
                SCOUT_WAKE.set()
            except Exception:
                skipped = " · voices not restored (YakScout didn't answer)"
        elif voices:
            skipped = " · voices not restored (YakScout isn't connected)"
        answer({"ok": True, "msg": f'Restored {got["channels"]} channels, {got["back"]} brought back, '
                                   f'{got["voices"]} voices{skipped}'})

    def settings_clear(self):
        what, pid = self.form().get("what", ""), self.prof["id"]
        if what == "watch":
            self.db.execute("DELETE FROM history WHERE profile_id=?", (pid,))
        elif what == "search":     # YakTube's own topic searches too: they come back from what's watched next
            self.db.execute("DELETE FROM searches WHERE profile_id=?", (pid,))
        self.redirect("/settings#" + (what if what in ("watch", "search") else ""))

    def history_clear(self):
        self.db.execute("DELETE FROM history WHERE profile_id=?", (self.prof["id"],))
        self.redirect("/history")

    def history_remove(self):
        self.db.execute("DELETE FROM history WHERE profile_id=? AND video_id=?", (self.prof["id"], self.form().get("v", "")))
        self.redirect("/history")


def scout_cli(args):
    """python3 app.py scout <link or id>...: what Scout makes of each Short, and why."""
    for a in args:
        vid = video_id_from(a)
        if not vid:
            print(f"{a}: not a YouTube link or id")
            continue
        r = scout_check(vid)
        print(f"{vid}: " + (f"{r['score']:3d}  {'farm' if r['farm'] else 'ok'}  {'; '.join(r['why']) or '-'}" if r else "YouTube has nothing on it"))


def main():
    if sys.argv[1:2] == ["scout"]:
        return scout_cli(sys.argv[2:])
    init_db()
    load_outside()
    threading.Thread(target=refresher_loop, daemon=True).start()
    threading.Thread(target=scout_link_loop, daemon=True).start()
    print(f"YakTube running on http://localhost:{PORT}  (db: {DB_PATH})")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()


if __name__ == "__main__":
    main()
