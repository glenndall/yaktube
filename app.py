#!/usr/bin/env python3
"""YakTube - a tiny self-hosted YouTube portal.

One file, Python standard library only. Data lives in SQLite.
Subscriptions come from YouTube's public RSS feeds; playback uses the
privacy-enhanced embedded player. No Google account, no API key.
"""
import base64, csv, functools, html, io, json, math, os, random, re, sqlite3, struct, threading, time, urllib.parse, urllib.request, zlib
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("YAK_DB", os.path.join(HERE, "yaktube.db"))
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
    c.close()


# ---------------------------------------------------------------- youtube
def fetch(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "SOCS=CAI; CONSENT=YES+1"})  # skip the EU consent wall
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
        "User-Agent": UA, "Content-Type": "application/json", "Cookie": "SOCS=CAI; CONSENT=YES+1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


OUTSIDE = []      # Shorts YouTube shows a visitor who isn't logged in, newest batch first
OUTSIDE_AT = [0]  # when we last asked


def refresh_outside(max_age=3600, pages=5, keep=200):
    """Open youtube.com/shorts/ like a visitor with no account, then ask for the Shorts that follow
    (8 per request, as YouTube's own page does). Saved as Shorts, so they never steer Home / New to you."""
    if OUTSIDE_AT[0] > time.time() - max_age:
        return
    OUTSIDE_AT[0] = time.time()
    page, d = yt_data("https://www.youtube.com/shorts/")
    seed = next(walk(d, "reelWatchEndpoint"), {}).get("videoId", "")
    ver = (re.findall(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page) or ["2.20261105.05.01"])[0]
    if not VID_RE.match(seed):
        return
    ctx = {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en", "gl": "US"}}
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
    finally:
        c.close()


def shorts_for(c, pid, first=None, limit=300):
    """Shorts from outside your subscriptions (what YouTube shows a visitor who isn't logged in),
    newest batch first, minus ones you've watched."""
    ids = OUTSIDE[:]
    found = {r["id"]: r for r in c.execute(
        f"SELECT id, title, channel_title FROM videos WHERE id IN ({','.join('?' * len(ids))}) AND id NOT IN "
        "(SELECT video_id FROM history WHERE profile_id=?)", ids + [pid])} if ids else {}
    out = [found[v] for v in ids if v in found]
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
                           "WHERE r.profile_id=? AND r.video_id NOT IN (SELECT video_id FROM history WHERE profile_id=?)",
                           (pid, pid)):                                   # rated without watching (Shorts, mostly)
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
    c.execute("DELETE FROM searches WHERE auto=1 AND at < ?", (old,))   # YakTube's own topic searches, once stale   # passed-over picks get another chance after a month
    n = c.execute(
        "DELETE FROM videos WHERE id NOT IN (SELECT video_id FROM history) AND id NOT IN (SELECT video_id FROM later) "
        "AND id NOT IN (SELECT video_id FROM ratings) "
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
:root{--bg:#f7f6f3;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e4e2dd;--accent:#d9480f}
@media (prefers-color-scheme:dark){:root{--bg:#111;--card:#1c1c1e;--fg:#f2f2f2;--muted:#9a9aa0;--line:#2c2c2e;--accent:#ff6b2c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit;text-decoration:none}
header{position:sticky;top:0;z-index:5;display:flex;gap:14px;align-items:center;padding:10px 20px;
  background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.logo{font-weight:800;font-size:20px;letter-spacing:-.6px;white-space:nowrap}.logo b{color:var(--accent)}
header .logo{width:196px;flex:none;padding-left:8px}
form.go{flex:1;max-width:620px;margin:0 auto;display:flex}
nav svg,.rate svg{width:22px;height:22px;flex:none;fill:currentColor}
.rib,.bot,.tvside{display:none}
.shell{display:flex;align-items:flex-start}
.side{position:sticky;top:57px;width:228px;flex:none;max-height:calc(100vh - 57px);overflow-y:auto;padding:10px 10px 20px;scrollbar-width:thin}
.side a{display:flex;align-items:center;gap:20px;padding:9px 14px;border-radius:10px;white-space:nowrap}
.side a:hover{background:var(--card)}.side a.on{background:var(--card);font-weight:650;box-shadow:0 0 0 1px var(--line)}
.side a.on svg{color:var(--accent)}
.side hr{border:0;border-top:1px solid var(--line);margin:10px 6px}
@media(max-width:1100px) and (min-width:721px){header .logo{width:auto}.side{width:80px;padding:6px 4px}
  .side a{flex-direction:column;gap:5px;padding:13px 2px 10px;font-size:10.5px;text-align:center;white-space:normal;line-height:1.15}
  .side a.on{box-shadow:none}.side hr{display:none}}
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
.lt,.no{position:absolute;top:6px;width:30px;height:30px;border-radius:50%;background:rgba(0,0,0,.7);color:#fff;
  display:grid;place-items:center;font-size:15px;line-height:1;opacity:0;transition:opacity .12s}
.lt{right:6px}.no{left:6px}
.dur{position:absolute;right:6px;bottom:6px;padding:1px 5px;border-radius:4px;background:rgba(0,0,0,.8);color:#fff;
  font-size:12px;font-weight:600;line-height:1.45;pointer-events:none}.card:hover .lt,.card:hover .no,.lt.on{opacity:1}
@media (hover:none){.lt,.no{opacity:.9}}
.gone{display:grid;place-items:center;align-content:center;gap:8px;aspect-ratio:16/9;border-radius:10px;background:var(--card);
  box-shadow:0 0 0 1px var(--line);color:var(--muted);font-size:13px;text-align:center}
.gone b{color:var(--fg);cursor:pointer}.gone b:hover{text-decoration:underline}
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
.list{background:var(--card);border-radius:12px;box-shadow:0 0 0 1px var(--line)}
.list>div{display:flex;align-items:center;gap:12px;padding:12px 16px;border-top:1px solid var(--line)}.list>div:first-child{border:0}
.list .grow{flex:1;min-width:0}
.err{background:#fff0e6;color:#a33a00;padding:10px 14px;border-radius:10px;margin-bottom:16px}
@media (prefers-color-scheme:dark){.err{background:#3a1f10;color:#ffb38a}}
.profiles{display:flex;gap:28px;justify-content:center;flex-wrap:wrap;margin:40px 0}
.profiles button{background:none;border:0;color:var(--fg);font:inherit;cursor:pointer;text-align:center}
.profiles .av{width:110px;height:110px;font-size:44px;border-radius:24px;margin-bottom:10px;transition:transform .12s}
.profiles button:hover .av{transform:scale(1.05)}
.center{text-align:center}
.why{color:var(--accent);font-size:12px;margin-top:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
@media(max-width:720px){header{flex-wrap:wrap;padding:10px 16px;row-gap:10px}header>.av,.side{display:none}
  header .logo{width:auto;padding:0}
  .rib{display:flex;gap:8px;order:2;flex-basis:calc(100% + 32px);margin:0 -16px;padding:0 16px;overflow-x:auto;scrollbar-width:none}
  .rib::-webkit-scrollbar{display:none}
  .rib a{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:9px;background:var(--card);
    box-shadow:0 0 0 1px var(--line);font-size:14px;font-weight:550;white-space:nowrap}
  .rib a.on{background:var(--fg);color:var(--bg);box-shadow:none}
  .rib .av{width:20px;height:20px;font-size:11px}
  form.go{order:3;flex-basis:100%;max-width:none}
  main{padding:16px 16px calc(80px + env(safe-area-inset-bottom))}
  .bot{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:6;padding-bottom:env(safe-area-inset-bottom);
    background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(10px);border-top:1px solid var(--line)}
  .bot a{flex:1;display:flex;flex-direction:column;align-items:center;gap:3px;padding:8px 0 7px;font-size:11px;color:var(--muted)}
  .bot a.on{color:var(--fg);font-weight:600}.bot a.on svg{color:var(--accent)}
  .grid{grid-template-columns:1fr 1fr;gap:16px 10px}.row img{width:130px}
  .sgrid{grid-template-columns:1fr 1fr;gap:12px 10px}}
@media(max-width:440px){.grid{grid-template-columns:1fr}}
"""

# Extra styles when the page is shown by the YakTube TV app (its browser says "YakTubeTV").
TV_CSS = """<style>
:root{--bg:#111;--card:#1c1c1e;--fg:#f2f2f2;--muted:#9a9aa0;--line:#2c2c2e;--accent:#ff6b2c}
body{font-size:17px}main{padding:18px 28px 40px 120px;max-width:none}
header,.side,.rib,.bot{display:none!important}.shell{display:block}
/* the rail: icons down the left; it widens with names when the remote moves into it */
.tvside{display:flex;flex-direction:column;gap:2px;position:fixed;left:0;top:0;bottom:0;z-index:20;width:96px;padding:28px 16px;
  overflow:hidden;background:linear-gradient(90deg,#0a0a0a 70%,rgba(10,10,10,.92));transition:width .15s}
.tvside:focus-within{width:320px;box-shadow:30px 0 80px rgba(0,0,0,.7)}
.tvside a{display:flex;align-items:center;gap:22px;min-height:52px;padding:0 16px;border-radius:999px;white-space:nowrap;color:#aaa;flex:none}
.tvside a svg{width:30px;height:30px}.tvside a.on{color:#fff}.tvside a.on svg{color:var(--accent)}
.tvside a span,.tvside h4{opacity:0;transition:opacity .12s}.tvside:focus-within a span,.tvside:focus-within h4{opacity:1}
.tvside a:focus{background:#f2f2f2;color:#111;outline:none!important}.tvside a:focus svg{color:#111}
.tvside h4{margin:18px 16px 6px;font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:#777;white-space:nowrap}
.tvside .av{width:30px;height:30px;font-size:14px;font-style:normal;flex:none}
.tvside .me{min-height:62px;margin-bottom:10px}.tvside .me span{line-height:1.2}.tvside .me small{display:block;font-size:12px;color:#888}
.tvside a.me:focus small{color:#555}.tvside h4{border-top:1px solid #2a2a2a;padding-top:16px}
.bare .tvside{display:none}#sr{display:none}   /* Shorts: no Share button on the TV */.bare main{padding:0}
.go.big{max-width:720px;margin:12vh auto 0}.go.big input{font-size:26px;padding:16px 26px}
/* Home: rows you move along with Left / Right, the focused video's details above them */
.trow{margin:0 0 2px;scroll-margin-top:6px}.trow h2{font-size:15px;margin:0 0 0 8px;font-weight:600;color:#ddd}
/* each row has room around it, so the selected card's border and zoom aren't cut off */
.strip{position:relative;display:flex;gap:18px;overflow-x:auto;scrollbar-width:none;padding:26px 30px 30px;margin:0 -22px}
.strip::-webkit-scrollbar{display:none}
/* YouTube's TV app on a 1080p screen: video tiles 426 x 240, the top (featured) row 640 x 360; Shorts tall (9:16).
   Sizes follow the screen width, so a 4K TV gets the same layout, twice as sharp. */
.strip .card{flex:0 0 calc(100vw * 426 / 1920)}.trow:first-of-type .strip .card{flex-basis:calc(100vw * 640 / 1920)}
.strip .scard{flex:0 0 calc(100vw * 240 / 1920)}
.strip>a{min-width:0}   /* every card exactly that wide, however long its text */
/* selected: 10% bigger, white outline, drop shadow, drawn over its neighbours */
.strip>a{position:relative;transition:transform .12s,box-shadow .12s}
.strip>a:focus{transform:scale(1.1)!important;z-index:2;outline:3px solid #fff!important;outline-offset:4px;border-radius:12px;
  box-shadow:0 14px 34px rgba(0,0,0,.65)}
.strip .dur{font-size:11px;padding:0 5px}
.strip .scard img{aspect-ratio:9/16;border-radius:10px}
/* the details sit under each picture, always the same height, so nothing moves when the selection changes */
.strip .card h3,.strip .scard h3{-webkit-line-clamp:2;height:2.6em;line-height:1.3;font-size:13px;font-weight:500;margin:8px 0 2px;color:#ddd}
.strip .card .meta,.strip .scard .meta,.strip .why{display:block;font-size:11.5px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#999}
.strip .why{visibility:hidden;color:var(--accent)}.strip .card:focus .why{visibility:visible}
.strip .card:focus h3,.strip .scard:focus h3{color:var(--accent)}
.scard{transition:transform .1s}.scard:focus{transform:scale(1.06)}.scard:focus h3{color:var(--accent)}
a:focus,button:focus,input:focus,iframe:focus{outline:4px solid #fff!important;outline-offset:3px;border-radius:10px}
.card{transition:transform .1s}.card:focus{transform:scale(1.05)}.card:focus h3{color:var(--accent)}
.grid{grid-template-columns:repeat(4,1fr)}
a[target=_blank],.lt:not(.on),.no,.imp{display:none}
.watch{grid-template-columns:1fr}.player{aspect-ratio:auto;height:100vh;overflow:hidden}.tvp{position:relative}
.tvbar{position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:48px 22px 18px;
border-radius:0 0 12px 12px;background:linear-gradient(transparent,rgba(0,0,0,.75));transition:opacity .5s}.tvbar.hide{opacity:0}
.tvsk{display:flex;align-items:center;gap:14px;flex-basis:100%;margin-bottom:10px;font-size:14px;font-weight:600;color:#eee;text-shadow:0 1px 2px #000}
#sk{flex:1;height:18px;display:flex;align-items:center;border-radius:9px;outline:none!important}#sk i{position:relative;display:block;width:100%;height:4px;border-radius:2px;background:rgba(255,255,255,.3)}
#sk b{position:absolute;left:0;top:0;bottom:0;border-radius:2px;background:#f03}
#sk b:after{content:"";position:absolute;right:-8px;top:50%;width:16px;height:16px;margin-top:-8px;border-radius:50%;background:#f03;transform:scale(0);transition:transform .12s}
#sk:focus i{height:8px}#sk:focus b:after{transform:scale(1);box-shadow:0 0 0 4px rgba(255,255,255,.85)}
.tvtop{position:absolute;left:0;right:0;top:0;padding:22px 28px 60px;background:linear-gradient(rgba(0,0,0,.75),transparent);transition:opacity .5s;pointer-events:none}
.tvtop b{display:block;font-size:24px;line-height:1.25;max-width:80%;text-shadow:0 1px 3px #000}.tvtop span{display:block;margin-top:4px;font-size:15px;color:#ddd;text-shadow:0 1px 2px #000}
.tvbar.hide~.tvtop{opacity:0}
.tvgm{position:absolute;right:22px;bottom:64px;display:flex;flex-direction:column;gap:6px;min-width:230px;padding:12px;border-radius:16px;
  background:rgba(22,22,22,.96);box-shadow:0 0 0 1px var(--line),0 12px 40px rgba(0,0,0,.6)}.tvgm[hidden]{display:none}
.tvbar .tvgm button{text-align:left;font-size:15px;padding:9px 16px;border-radius:10px;background:none}
.tvbar .tvgm button:focus{background:var(--fg);color:var(--bg)}.tvgm .meta{padding:4px 16px 0;font-size:12px;color:#aaa}
#gs{position:relative}#gs.dot:after{content:"";position:absolute;top:1px;right:3px;width:7px;height:7px;border-radius:50%;background:var(--accent)}
.tvbar form{display:inline}.tvbar .rate{background:var(--card);box-shadow:none}.tvbar .rate button{border-radius:0;background:none;padding:3px 12px}
.tvbar .rate button.on{color:var(--accent)}.tvbar .rate svg{width:16px;height:16px;vertical-align:-3px}
.tvbar button,.tvnx button{font:inherit;font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;border:0;background:var(--card);color:var(--fg)}
.tvbar button:focus,.tvnx button:focus{background:var(--fg);color:var(--bg);outline-width:2px!important;outline-offset:2px}.tvbar .meta{margin-left:6px;font-size:12px;color:#eee;text-shadow:0 1px 2px #000}
.tvnx{position:absolute;right:28px;bottom:84px;max-width:440px;padding:16px 18px;border-radius:14px;background:rgba(20,20,20,.94);box-shadow:0 0 0 1px var(--line)}
.tvnx>b{display:block;margin:4px 0 12px;font-size:16px;line-height:1.3}.tvnx button{margin-right:8px;font-size:14px;padding:5px 14px}
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
<button id="pp">❚❚ Pause</button><button data-k="-10">⟲ 10s</button><button data-k="10">10s ⟳</button>__ACTS__
<button id="gs" aria-haspopup="menu" aria-expanded="false">⚙ Settings</button>
<div id="gm" class="tvgm" role="menu" hidden><button id="cc">Captions off</button><button id="sp">Speed 1×</button><button id="ql">Quality Auto</button>
<button id="ap">Autoplay off</button><span id="tm" class="meta"></span></div></div>
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
function upd(){$("pp").textContent=st.s==1||st.s==3?"❚❚ Pause":"▶ Play";$("tm").textContent=st.q?"playing "+(QL[st.q]||st.q):"";
$("t0").textContent=tf(st.t);$("t1").textContent=st.d?tf(st.d):"";st.at=Date.now()}
// Progress bar: moves smoothly between the player's reports. Select it with the remote and Left / Right jump 10s
// (hold to keep going), like the YouTube app on a TV.
(function draw(){var t=st.t+(st.s==1&&st.at?(Date.now()-st.at)/1000:0);$("sk").firstChild.firstChild.style.width=(st.d?Math.min(100,t/st.d*100):0)+"%";requestAnimationFrame(draw)})();
$("sk").addEventListener("keydown",function(k){if(k.key=="ArrowLeft"||k.key=="ArrowRight"){k.preventDefault();k.stopImmediatePropagation();wake();seek(k.key=="ArrowRight"?10:-10)}
else if(k.key=="Enter"){k.preventDefault();toggle()}});
addEventListener("message",function(e){if(!/youtube/.test(e.origin))return;var m;try{m=JSON.parse(e.data)}catch(x){return}if(m.event=="onReady"||m.event=="initialDelivery")clearInterval(hello);
var i=m.info||{},was=st.s;if("currentTime" in i)st.t=i.currentTime;if(i.duration)st.d=i.duration;if("playerState" in i)st.s=i.playerState;
if(i.playbackQuality&&i.playbackQuality!="unknown")st.q=i.playbackQuality;
if(st.s!=was){wake();if(st.s==0)ended();else if(nx)stopnx()}tr(st.t,st.d,st.s==1);
if(!cc&&(m.event=="onReady"||(st.s==1&&!st.off))){ccoff();if(st.s==1){st.off=1;setTimeout(ccoff,1500);setTimeout(ccoff,4000)}}upd()});
function ccoff(){if(!cc)cmd("setOption",["captions","track",{}])}  // off for good: unloadModule came back after every seek
function toggle(){if(st.s==1||st.s==3){cmd("pauseVideo");st.s=2}else{cmd("playVideo");st.s=1}upd()}
function seek(d){st.t=Math.min(Math.max(0,st.t+d),st.d||1e9);cmd("seekTo",[st.t,true]);upd()}
$("pp").onclick=toggle;[].forEach.call(document.querySelectorAll("[data-k]"),function(b){b.onclick=function(){seek(+b.dataset.k)}});
$("cc").onclick=function(){cc=!cc;if(cc){cmd("loadModule",["captions"]);cmd("setOption",["captions","track",{languageCode:"en"}])}else ccoff();this.textContent="Captions "+(cc?"on":"off")};
$("sp").onclick=function(){ri=(ri+1)%rates.length;cmd("setPlaybackRate",[rates[ri]]);this.textContent="Speed "+rates[ri]+"×"};
addEventListener("keydown",function(k){var c=k.key;if(c=="MediaPlayPause"||c=="MediaPlay"||c=="MediaPause"){toggle();k.preventDefault()}
else if(c=="MediaFastForward"){seek(10);k.preventDefault()}else if(c=="MediaRewind"){seek(-10);k.preventDefault()}});
var bar=document.querySelector(".tvbar");[].forEach.call(document.querySelectorAll(".tvbar button,.tvnx button,#sk"),function(b){b.scrollIntoView=function(){}});
function home(){var y=$("pl").getBoundingClientRect().top+scrollY;if(Math.abs(scrollY-y)>2)scrollTo(0,y)}
bar.addEventListener("focusin",home);$("nx").addEventListener("focusin",home);addEventListener("load",home);
// Autoplay (off unless switched on; remembered on this TV): when a video ends, count down 10s, then play the
// first "Up next" video you haven't seen. Cancel stops it.
var ap=0;try{ap=localStorage.yakap=="1"?1:0}catch(x){}
$("ap").textContent="Autoplay "+(ap?"on":"off");
$("ap").onclick=function(){ap=1-ap;try{localStorage.yakap=ap}catch(x){}this.textContent="Autoplay "+(ap?"on":"off");if(!ap)stopnx()};
var nx=0;function stopnx(){clearTimeout(nx);nx=0;$("nx").hidden=true}
function ended(){var a=document.querySelector("#upnext a.row:not(.seen)")||document.querySelector("#upnext a.row"),n=10;if(!ap||!a||nx)return;
$("nxt").textContent=a.querySelector("h3").textContent;$("nx").hidden=false;$("nxg").onclick=function(){location.href=a.href};$("nxg").focus();
(function tick(){$("nxn").textContent=n;if(n--<=0)location.href=a.href;else nx=setTimeout(tick,1000)})()}
$("nxc").onclick=function(){stopnx();$("pp").focus()};
// The buttons fade out after 5s of no remote presses while playing; the next press only brings them back
// (so OK doesn't also pause). Play/pause and fast-forward/rewind keys always act straight away.
var tmr;function wake(){bar.classList.remove("hide");clearTimeout(tmr);tmr=setTimeout(function(){if(st.s==1&&$("gm").hidden&&bar.contains(document.activeElement))bar.classList.add("hide")},5000)}
addEventListener("keydown",function(k){var hid=bar.classList.contains("hide");wake();
if(hid&&!/^Media/.test(k.key)){k.preventDefault();k.stopImmediatePropagation()}},true);
// Settings (⚙): captions, speed, quality and autoplay, in a small menu above the button. Up / Down move, OK changes
// one, Left or Back closes it. While it's open the controls don't fade. A dot on ⚙ means something isn't the usual.
var gm=$("gm"),gs=$("gs"),gl=[].slice.call(gm.querySelectorAll("button"));
function mark(){gs.classList.toggle("dot",!!(cc||ri||qi))}
function openm(){gm.hidden=false;gs.setAttribute("aria-expanded","true");gl[0].focus();wake();
  if(!window.__yakm&&window.yakBack){window.__yakm=1;var ob=window.yakBack;window.yakBack=function(){if(!gm.hidden){shut();return true}return ob()}}}
function shut(){gm.hidden=true;gs.setAttribute("aria-expanded","false");gs.focus();wake()}
gs.onclick=function(){gm.hidden?openm():shut()};
gm.addEventListener("click",function(){setTimeout(mark)});
gm.addEventListener("keydown",function(k){var d=k.key,i=gl.indexOf(document.activeElement);
  if(d=="ArrowUp"||d=="ArrowDown"){k.preventDefault();k.stopImmediatePropagation();var n=gl[i+(d=="ArrowDown"?1:-1)];if(n)n.focus();wake()}
  else if(d=="ArrowLeft"||d=="Escape"||d=="GoBack"||d=="BrowserBack"){k.preventDefault();k.stopImmediatePropagation();shut()}
  else if(d=="ArrowRight"){k.preventDefault();k.stopImmediatePropagation()}});
gm.addEventListener("focusout",function(e){if(!gm.hidden&&!gm.contains(e.relatedTarget)&&e.relatedTarget!=gs){gm.hidden=true;gs.setAttribute("aria-expanded","false")}});
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
#toast{position:absolute;left:50%;top:72px;transform:translateX(-50%);background:rgba(30,30,30,.92);padding:10px 18px;
  border-radius:999px;font-size:14px;pointer-events:none;z-index:4;white-space:nowrap}</style>"""
SHORTS_JS = """<script>(function(){var L=__LIST__,i=0,seen={},$=function(x){return document.getElementById(x)};
function close(){var r=document.referrer;if(r&&r.indexOf(location.origin+"/")==0&&history.length>1)history.back();else location.href="/"}
$("x").onclick=function(k){k.preventDefault();close()};
// Remote / keyboard. With nothing selected: Up / Down change Short, OK pauses, Right goes to the buttons on the side
// (thumbs, share), Left to ✕. On a button: OK presses it, Up / Down move between the side buttons, Left (or Down
// from ✕) goes back to the Short.
addEventListener("keydown",function(k){var d=k.key,a=document.activeElement,g;
  function stop(){k.preventDefault();k.stopImmediatePropagation()}
  if(a&&a!=document.body&&/^(A|BUTTON)$/.test(a.tagName)){
    if(d=="Enter"||d==" ")return;
    g=a.closest("#rt,#ud");
    if(g&&(d=="ArrowUp"||d=="ArrowDown")){stop();var l=[].filter.call(g.querySelectorAll("button"),function(b){return b.offsetParent}),
      n=l[l.indexOf(a)+(d=="ArrowDown"?1:-1)];if(n)n.focus();return}
    if((g&&g.id=="rt"&&d=="ArrowLeft")||(a.id=="x"&&(d=="ArrowDown"||d=="ArrowRight"))){stop();a.blur();return}
    if(d!="Escape"&&d!="MediaPlayPause")return}
  else if(d=="ArrowRight"&&!$("rt").hidden){stop();$("ru").focus();return}
  else if(d=="ArrowLeft"){stop();$("x").focus();return}
  var h={ArrowDown:next,PageDown:next,ArrowUp:prev,PageUp:prev,Enter:tap," ":tap,MediaPlayPause:tap,Escape:close}[d];
  if(h){stop();h()}},true);
if(!L.length){["rt","pb"].forEach(function(x){$(x).hidden=true});return}
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
function go(n){pt.t=pt.d=pt.at=0;i=(n+L.length)%L.length;p.st.off=0;p.st.s=-1;p.cmd("loadVideoById",[L[i].id,0]);show()}
function next(){go(i+1)}function prev(){go(i-1)}
function tap(){if(p.st.muted){p.cmd("unMute");p.cmd("playVideo");p.st.muted=false;$("snd").hidden=true}
  else if(p.st.s==2){p.cmd("playVideo");p.st.s=1;$("pz").hidden=true}else{p.cmd("pauseVideo");p.st.s=2;$("pz").hidden=false}}
var t=$("tap"),y0=null,w=0;
t.addEventListener("touchstart",function(e){y0=e.touches[0].clientY},{passive:true});
t.addEventListener("touchend",function(e){if(y0==null)return;var d=e.changedTouches[0].clientY-y0;y0=null;e.preventDefault();
  if(d<-50)next();else if(d>50)prev();else tap()});
t.addEventListener("click",tap);
addEventListener("wheel",function(e){if(Math.abs(e.deltaY)<20||Date.now()-w<700)return;w=Date.now();e.deltaY>0?next():prev()},{passive:true});
$("up").onclick=prev;$("dn").onclick=next;show()})()</script>"""


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


def card(v, seen=False, saved=False, hide=False, src=None, why=None):
    short = "short" in v.keys() and v["short"]
    meta = " · ".join(x for x in ("Short" if short else "", views(v["views"]), ago(v["published"])) if x)
    href = ("/shorts?v=" if short else "/watch?v=") + v["id"]   # Shorts open in the full-screen Shorts player
    # the channel name opens its channel page on a click or tap (see layout); it's not a link of its own,
    # so the TV remote still steps from video to video
    cid = v["channel_id"] if "channel_id" in v.keys() else None
    ch = f'<span data-ch="{cid}">{e(v["channel_title"] or "")}</span>' if cid and CID_RE.fullmatch(cid) else e(v["channel_title"] or "")
    extra = "" if short else (f'<span class="lt{" on" if saved else ""}" data-lt="{v["id"]}" title="Watch later">{"✓" if saved else "＋"}</span>'
                              + (f'<span class="no" data-no="{v["id"]}" title="Not interested">✕</span>' if hide else ""))
    tag = f' data-v="{v["id"]}" data-s="{src}"' if src else ""     # Home: reports when it's been on screen
    length = "" if short or "length" not in v.keys() else clock(v["length"])
    extra = (f'<span class="dur">{length}</span>' if length else "") + extra   # how long, bottom right
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


def side_row(r, seen=False):
    meta = " · ".join(x for x in (r["channel_title"] or "", views(r["views"]), ago(r["published"])) if x)
    return (f'<a class="row{" seen" if seen else ""}" href="/watch?v={r["id"]}"><img loading="lazy" src="{thumb(r["id"])}" alt="">'
            f'<div><h3>{e(r["title"] or r["id"])}</h3><div class="meta">{e(meta)}</div></div></a>')


# One small script on every page: "/" jumps to the search box, and the bits of a video card that aren't links of
# their own (so the TV remote still steps from video to video) react to a click or tap: the channel name opens its
# page, + saves for later, x means "not interested".
PAGE_JS = """<script>addEventListener("keydown",k=>{if(k.key=="/"&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName))
{k.preventDefault();document.querySelector(".go input").focus()}});
addEventListener("click",k=>{var t=k.target,c;if(!t.closest)return;
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b}).then(r=>r.text())}
if(c=t.closest("[data-lt]")){k.preventDefault();post("/later/toggle","v="+c.dataset.lt).then(s=>{c.classList.toggle("on",s=="1");c.textContent=s=="1"?"\u2713":"\uff0b"})}
else if(c=t.closest("[data-no]")){k.preventDefault();var a=c.closest(".card"),ch=a.querySelector("[data-ch]"),g=document.createElement("div");
g.className="gone";g.card=a;g.innerHTML="<span>Hidden from your suggestions</span>"+(ch?"<b data-hc='"+ch.dataset.ch+"'>Hide this whole channel</b>":"")+"<b data-un='"+c.dataset.no+"'>Undo</b>";
post("/hide","v="+c.dataset.no);a.style.display="none";a.after(g)}
else if(c=t.closest("[data-hc]")){var g=c.parentNode;g.hc=c.dataset.hc;post("/hide","c="+g.hc);c.replaceWith("Channel hidden too");
document.querySelectorAll('.card [data-ch="'+g.hc+'"]').forEach(x=>{x.closest(".card").style.display="none"})}
else if(c=t.closest("[data-un]")){var g=c.parentNode;post("/hide","undo=1&v="+c.dataset.un+(g.hc?"&c="+g.hc:"")).then(()=>{if(g.hc)location.reload();else{g.card.style.display="";g.remove()}})}
else if(c=t.closest("[data-rate]")){k.preventDefault();var g=c.parentNode;
post("/rate","v="+g.dataset.v+"&r="+c.dataset.rate).then(s=>{g.querySelectorAll("[data-rate]").forEach(b=>{b.classList.toggle("on",b.dataset.rate==s);b.setAttribute("aria-pressed",b.dataset.rate==s)})})}
else if(c=t.closest("[data-ch]")){k.preventDefault();location.href="/channel?id="+c.dataset.ch}});
// Subscribe / Watch later buttons (form class="js") change in place, so a playing video isn't interrupted.
addEventListener("submit",k=>{var f=k.target;if(!f.classList.contains("js"))return;k.preventDefault();
var b=new URLSearchParams(new FormData(f)),had=f.contains(document.activeElement);b.set("inline","1");f.querySelector("button").disabled=true;
fetch(f.action,{method:"POST",body:b}).then(r=>r.ok?r.text():Promise.reject()).then(h=>{var d=document.createElement("div");
d.innerHTML=h;var n=d.firstChild;f.replaceWith(n);if(had)n.querySelector("button").focus()}).catch(()=>{f.classList.remove("js");f.submit()})});
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
else{var R=rows(),n=R[R.indexOf(r)+(d=="ArrowDown"?1:-1)];if(n){var m=items(n);put(m[Math.min(+n.dataset.i||0,m.length-1)])}}}},true);
var f=document.querySelector(".trow .strip>a");if(f&&(!document.activeElement||document.activeElement==document.body))put(f)})()</script>"""

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
}.items()}
# Home, Shorts and Subscriptions are the main three (the bar along the bottom on a phone); the rest sit under them
# on a computer, or in the ribbon across the top on a phone.
MAIN_TABS = (("home", "/", "Home"), ("shorts", "/shorts", "Shorts"), ("feed", "/feed", "Subscriptions"))
MORE_TABS = (("new", "/new", "New to you"), ("later", "/later", "Later"), ("subs", "/subs", "Channels"),
             ("hist", "/history", "History"))


# The TV's rail down the left, in the order YouTube's Google TV app now uses: search, then the places you go most.
TV_TABS = (("search", "/search", "Search"), ("home", "/", "Home"), ("shorts", "/shorts", "Shorts"),
           ("feed", "/feed", "Subscriptions"), ("later", "/later", "Later"), ("hist", "/history", "History"),
           ("subs", "/subs", "Channels"))                                     # (New to you is a row on Home)


def rate_buttons(vid, val):
    """Thumbs up / down for a video; the page script sends a click to /rate."""
    return (f'<div class="rate" data-v="{vid}">' + "".join(
        f'<button type="button" class="{cls}{" on" if val == n else ""}" data-rate="{n}" aria-pressed="{str(val == n).lower()}" '
        f'title="{t}" aria-label="{t}">{ICON["up"]}</button>' for n, cls, t in ((1, "up", "I like this"), (-1, "dn", "Not for me")))
            + '</div>')


def layout(title, body, prof=None, tab="", q="", bare=False):
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<title>{e(title)} · YakTube</title><link rel="icon" type="image/png" href="/favicon.png">'
            f'<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.json" crossorigin="use-credentials">'
            f'<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
            f'<meta name="apple-mobile-web-app-title" content="YakTube"><style>{CSS}</style></head><body{" class=bare" if bare else ""}>')
    if not prof:
        return head + f'<main>{body}</main></body></html>'
    def links(tabs, icons=True):
        return "".join(f'<a href="{h}" class="{"on" if tab == k else ""}">{ICON[k] if icons else ""}<span>{n}</span></a>'
                       for k, h, n in tabs)
    av = f'<span class="av" style="background:{prof["color"]}">{e(prof["name"][:1].upper())}</span>'
    return head + (
        f'<header><a class="logo" href="/">Yak<b>Tube</b></a>'
        f'<nav class="rib">{links(MORE_TABS, False)}<a href="/profiles">{av}<span>Profiles</span></a></nav>'
        f'<form class="go" action="/go"><input type="text" name="q" placeholder="Search, or paste a YouTube link…" '
        f'autocomplete="off" value="{e(q)}"></form>'
        f'<a href="/profiles" title="Switch profile ({e(prof["name"])})" class="av" style="background:{prof["color"]}">'
        f'{e(prof["name"][:1].upper())}</a></header>'
        f'<div class="shell"><nav class="side">{links(MAIN_TABS)}<hr>{links(MORE_TABS)}</nav><main>{body}</main></div>'
        f'<nav class="bot">{links(MAIN_TABS)}</nav>'
        f'<nav class="tvside"><a class="me" href="/profiles">{av}<span>{e(prof["name"])}<small>Switch profile</small></span></a>'
        f'{links(TV_TABS)}<!--tvrecent--></nav>'
        + PAGE_JS + '</body></html>')


# ---------------------------------------------------------------- server
class App(BaseHTTPRequestHandler):
    server_version = "YakTube/1"

    # -- helpers
    def send(self, body, status=200, ctype="text/html; charset=utf-8", headers=()):
        if isinstance(body, str) and ctype.startswith("text/html") and self.tv():
            body = body.replace("</head>", TV_CSS + "</head>", 1)
            if "<!--tvrecent-->" in body:                       # the rail: channels you watched lately, and its script
                body = body.replace("<!--tvrecent-->", self.tv_recent(), 1).replace("</body>", TV_JS + "</body>", 1)
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

    def tv_recent(self):
        """The TV rail's "Recent channels": the last 5 channels this profile watched."""
        rows = self.db.execute(
            "SELECT v.channel_id, COALESCE(ch.title, MAX(v.channel_title)) AS title, MAX(h.watched_at) AS at FROM history h "
            "JOIN videos v ON v.id=h.video_id LEFT JOIN channels ch ON ch.id=v.channel_id WHERE h.profile_id=? "
            "AND v.channel_id IS NOT NULL GROUP BY v.channel_id ORDER BY at DESC LIMIT 5", (self.prof["id"],)).fetchall()
        if not rows:
            return ""
        return '<h4>Recent channels</h4>' + "".join(
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

    def card(self, v, seen=False, hide=False, src=None, why=None):
        if self._later is None:                              # this profile's Watch later list, read once per page
            self._later = {r[0] for r in self.db.execute("SELECT video_id FROM later WHERE profile_id=?", (self.prof["id"],))}
        return card(v, seen, v["id"] in self._later, hide, src, why)

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
        self._later = None
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
                 ("GET", "/later"): self.later, ("POST", "/later/toggle"): self.later_toggle, ("POST", "/hide"): self.hide,
                 ("POST", "/subs/remove"): self.sub_remove, ("POST", "/refresh"): self.refresh, ("POST", "/feed/shorts"): self.feed_shorts,
                 ("GET", "/foryou"): lambda: self.redirect("/"), ("GET", "/shorts"): self.shorts, ("POST", "/seen"): self.seen, ("GET", "/new"): self.newtoyou, ("GET", "/search"): self.search, ("GET", "/related"): self.related,
                 ("GET", "/history"): self.history, ("POST", "/history/clear"): self.history_clear,
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
    def get_profiles(self, path):
        manage = self.q.get("manage") == "1"
        tiles = "".join(
            f'<form method="post" action="/profiles/{"delete" if manage else "select"}">'
            f'<input type="hidden" name="id" value="{p["id"]}"><button>'
            f'<div class="av" style="background:{p["color"]}">{e(p["name"][:1].upper())}</div>'
            f'{e(p["name"])}{"<div class=meta>Remove</div>" if manage else ""}</button></form>'
            for p in self.db.execute("SELECT * FROM profiles ORDER BY id"))
        body = (f'<div class="center"><div class="logo" style="font-size:34px;margin-top:40px">Yak<b>Tube</b></div>'
                f'<h1 style="margin-top:28px">{"Remove a profile" if manage else "Who’s watching?"}</h1></div>'
                f'<div class="profiles">{tiles}</div>'
                f'<form class="center" method="post" action="/profiles/add" style="display:flex;gap:8px;max-width:360px;margin:0 auto">'
                f'<input type="text" name="name" placeholder="New profile name" maxlength="30" required>'
                f'<button class="btn">Add</button></form><p class="center meta" style="margin-top:22px">'
                f'<a href="/profiles{"" if manage else "?manage=1"}">{"Done" if manage else "Manage profiles"}</a></p>')
        self.send(layout("Profiles", body))

    def post_profiles(self, path):
        f = self.form()
        if path == "/profiles/add" and f.get("name", "").strip():
            n = self.db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
            self.db.execute("INSERT OR IGNORE INTO profiles(name,color) VALUES(?,?)",
                            (f["name"].strip()[:30], COLORS[n % len(COLORS)]))
        elif path == "/profiles/select" and f.get("id", "").isdigit():
            return self.redirect("/", [("Set-Cookie", f"yak_profile={int(f['id'])}; Path=/; Max-Age=31536000; SameSite=Lax; HttpOnly")])
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
        picks = home_feed(self.db, pid, 90 if self.tv() else 60)
        if self.tv() and picks:
            return self.send(layout("Home", self.tv_home(picks), self.prof, "home"), headers=[("Cache-Control", "no-store")])
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
            parts.append(self.card(r, hide=True, src=src, why=f"{reason} · {score:.2f}" if show_why else None))
        if shorts and len(picks) <= 8:
            parts.append(shelf())
        if not picks:
            body = ('<div class="empty"><h1>Nothing here yet</h1><p>Watch a few videos (search for something you like) '
                    'or subscribe to some channels, and suggestions based on what you watch will show up here. It\'s all '
                    'worked out on this computer from your own history.</p><a class="btn" href="/subs">Add subscriptions</a></div>')
            body += f'<div class="grid">{"".join(parts)}</div>' if parts else ""
        else:
            body = f'<h1 class="sr">Home</h1><div class="grid">{"".join(parts)}</div>' + HOME_JS
        self.send(layout("Home", body, self.prof, "home"), headers=[("Cache-Control", "no-store")])

    def tv_home(self, picks):
        """Home on the TV: rows like the YouTube app on Google TV. Recommended for you (the top picks), New from your
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
        return '<h1 class="sr">Home</h1>' + "".join(
            f'<section class="trow"><h2>{e(title.replace(": ", " "))}</h2><div class="strip">'
            + "".join(short_card(p) if title == "Shorts" else self.card(p[0], hide=True, src=p[1], why=p[3]) for p in row)
            + '</div></section>' for title, row in rows if row) + HOME_JS

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
                    f'new picks through the day</span></div><div class="grid">{"".join(self.card(r, hide=True) for r in rows)}</div>')
        self.send(layout("New to you", body, self.prof, "new"))

    def shorts(self):
        first = self.q.get("v", "")
        rows = shorts_for(self.db, self.prof["id"], first if VID_RE.match(first) else None)
        mine = dict(self.db.execute("SELECT video_id, val FROM ratings WHERE profile_id=?", (self.prof["id"],)).fetchall())
        for r in rows:
            r["r"] = mine.get(r["id"], 0)
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
                '<div id="toast" hidden></div><div id="ud"><button id="up" aria-label="Previous">▲</button>'
                '<button id="dn" aria-label="Next">▼</button></div></div>'
                + YT_JS + SHORTS_JS.replace("__LIST__", data).replace("__EMBED__", EMBED))
        self.send(layout("Shorts", body))

    def seen(self):
        """The players report here. n=1 (or nothing, as Shorts send) puts the video in History; t and d are how far in
        and how long, saved so the video can resume. A video watched to the end starts over and leaves Watch later."""
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
            if done:
                self.db.execute("DELETE FROM later WHERE profile_id=? AND video_id=?", (pid, vid))
        self.send("", 204)

    # -- watch later, not interested
    def later(self):
        rows = self.db.execute(
            "SELECT v.*, h.video_id AS seen FROM later l JOIN videos v ON v.id=l.video_id "
            "LEFT JOIN history h ON h.video_id=v.id AND h.profile_id=l.profile_id WHERE l.profile_id=? "
            "ORDER BY l.added_at DESC, l.rowid DESC", (self.prof["id"],)).fetchall()
        if rows:
            body = (f'<div class="bar"><h1>Watch later</h1><span class="meta">Leaves the list once you\'ve watched it to the end</span></div>'
                    f'<div class="grid">{"".join(self.card(r, r["seen"]) for r in rows)}</div>')
        else:
            body = ('<div class="empty"><h1>Nothing saved yet</h1><p>Use ＋ on a video\'s picture, or the Watch later button '
                    'under a video, to keep it here for another time.</p><a class="btn" href="/">Back to your feed</a></div>')
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
        self.back(f)

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
            acts = rate_buttons(vid, mine[0] if mine else 0) + sub + sub_later
            player = TRACK_JS + TV_PLAYER.replace("__VID__", vid).replace("__T0__", t0).replace("__TOP__", top).replace("__ACTS__", acts)
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
                'subscriptions → <b>subscriptions.csv</b>. Choose that file, or paste any list of channel links. '
                'Export saves this profile\'s list in the same format (handy for copying it to another profile).</p>'
                '<input type="file" accept=".csv,.txt,.opml,.xml,text/*" onchange="var r=new FileReader(),t=this.form.text;'
                'r.onload=function(){t.value=r.result};r.readAsText(this.files[0])">'
                '<textarea name="text" rows="5" required placeholder="Channel Id,Channel Url,Channel Title"></textarea>'
                '<button class="btn small">Import</button> <a class="btn ghost small" href="/subs/export">Export</a></form></details>')
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
        items = "".join(
            f'<div><a href="/watch?v={r["id"]}"><img src="{thumb(r["id"])}" loading="lazy" alt="" '
            f'style="width:120px;aspect-ratio:16/9;object-fit:cover;border-radius:6px;display:block"></a>'
            f'<a class="grow" href="/watch?v={r["id"]}"><strong>{e(r["title"] or r["id"])}</strong>'
            f'<div class="meta">{e(r["channel_title"] or "")} · watched {ago(r["watched_at"])}</div></a>'
            f'<form method="post" action="/history/remove"><input type="hidden" name="v" value="{r["id"]}">'
            f'<button class="btn ghost small" title="Remove from history">✕</button></form></div>' for r in rows)
        body = ('<div class="bar"><h1>Watch history</h1>'
                + ('<form method="post" action="/history/clear"><button class="btn ghost small">Clear all</button></form>' if rows else "")
                + "</div>" + (f'<div class="list">{items}</div>' if rows else '<p class="meta">Nothing watched yet.</p>'))
        self.send(layout("History", body, self.prof, "hist"))

    def history_clear(self):
        self.db.execute("DELETE FROM history WHERE profile_id=?", (self.prof["id"],))
        self.redirect("/history")

    def history_remove(self):
        self.db.execute("DELETE FROM history WHERE profile_id=? AND video_id=?", (self.prof["id"], self.form().get("v", "")))
        self.redirect("/history")


def main():
    init_db()
    threading.Thread(target=refresher_loop, daemon=True).start()
    print(f"YakTube running on http://localhost:{PORT}  (db: {DB_PATH})")
    ThreadingHTTPServer((HOST, PORT), App).serve_forever()


if __name__ == "__main__":
    main()
