#!/usr/bin/env python3
"""YakTube - a tiny self-hosted YouTube portal.

One file, Python standard library only. Data lives in SQLite.
Subscriptions come from YouTube's public RSS feeds; playback uses the
privacy-enhanced embedded player. No Google account, no API key.
"""
import base64, functools, html, json, os, re, sqlite3, struct, threading, time, urllib.parse, urllib.request, zlib
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
CREATE TABLE IF NOT EXISTS related(src TEXT NOT NULL, video_id TEXT NOT NULL, pos INTEGER, PRIMARY KEY(src, video_id));
CREATE TABLE IF NOT EXISTS related_src(src TEXT PRIMARY KEY, fetched_at INTEGER);
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


def parse_items(d):
    """Pull plain video dicts out of ytInitialData (both old and new layouts). Shorts are skipped."""
    out, seen = [], set()

    def add(vid, title, cid, ctitle, when, views_):
        if vid and VID_RE.match(vid) and vid not in seen:
            seen.add(vid)
            out.append({"id": vid, "title": title, "channel_id": cid if cid and CID_RE.fullmatch(cid) else None,
                        "channel_title": ctitle, "published": rel_ts(when), "views": num(views_), "short": 0})
    for r in walk(d, "videoRenderer"):               # search results
        by = ((r.get("longBylineText") or r.get("ownerText") or {}).get("runs") or [{}])[0]
        add(r.get("videoId"), "".join(x.get("text", "") for x in r.get("title", {}).get("runs", [])),
            by.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId"), by.get("text", ""),
            r.get("publishedTimeText", {}).get("simpleText", ""), r.get("viewCountText", {}).get("simpleText", ""))
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
            ctitle, when, vws)
    for i, v in enumerate(out):                      # keep page order for equal "2d ago" dates
        if v["published"]:
            v["published"] -= i
    return out


def store_videos(c, vids, exact=False):
    """Upsert videos. Approximate dates ("2d ago") never overwrite a date we already have."""
    rows = [dict({"channel_id": None, "channel_title": "", "title": "", "published": 0, "views": 0, "short": 0},
                 **v, exact=int(exact)) for v in vids if VID_RE.match(v.get("id", ""))]
    c.execute("BEGIN")
    try:
        c.executemany(
            "INSERT INTO videos(id,channel_id,channel_title,title,published,views,short) "
            "VALUES(:id,:channel_id,:channel_title,:title,:published,:views,:short) ON CONFLICT(id) DO UPDATE SET "
            "channel_id=COALESCE(excluded.channel_id, videos.channel_id), "
            "channel_title=COALESCE(NULLIF(excluded.channel_title,''), videos.channel_title), "
            "title=COALESCE(NULLIF(excluded.title,''), videos.title), "
            "published=CASE WHEN :exact OR videos.published=0 THEN excluded.published ELSE videos.published END, "
            "views=MAX(videos.views, excluded.views), short=MAX(videos.short, excluded.short)", rows)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def refresh_channel(c, cid):
    """RSS first (exact dates); if YouTube's RSS is down, read the channel's /videos page instead."""
    try:
        title, vids = parse_feed(fetch(FEED + cid))
        exact = True
    except Exception:
        page, d = yt_data(f"https://www.youtube.com/channel/{cid}/videos")
        m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        title = html.unescape(m.group(1)) if m else ""
        if not d:
            raise ValueError("Couldn't load that channel's videos.")
        vids = [dict(v, channel_id=cid, channel_title=title) for v in parse_items(d)]
        exact = False
    c.execute("INSERT INTO channels(id,title,fetched_at) VALUES(?,?,?) "
              "ON CONFLICT(id) DO UPDATE SET title=excluded.title, fetched_at=excluded.fetched_at",
              (cid, title or cid, int(time.time())))
    store_videos(c, vids, exact)
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
    (8 per request, as YouTube's own page does). Saved as Shorts, so they never steer For you / New to you."""
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


def mark_watched(c, pid, vid):
    c.execute("INSERT INTO history(profile_id,video_id,watched_at) VALUES(?,?,?) ON CONFLICT(profile_id,video_id) DO UPDATE "
              "SET watched_at=excluded.watched_at, times=times+1", (pid, vid, int(time.time())))


def search(c, q):
    # sp=EgIQAQ== limits results to videos (no channels/playlists/shorts shelves)
    _, d = yt_data("https://www.youtube.com/results?sp=EgIQAQ%253D%253D&search_query=" + urllib.parse.quote_plus(q))
    vids = parse_items(d)
    store_videos(c, vids)
    return vids


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


def for_you(c, pid, limit=48):
    """Local recommendations from this profile's own history. Nothing leaves the machine."""
    now, day = time.time(), 86400
    hist = c.execute("SELECT h.video_id, h.watched_at, h.times, v.channel_id FROM history h LEFT JOIN videos v "
                     "ON v.id=h.video_id WHERE h.profile_id=? AND COALESCE(v.short,0)=0 "   # Shorts don't steer suggestions
                     "ORDER BY h.watched_at DESC LIMIT 300", (pid,)).fetchall()
    watched = {r[0] for r in c.execute("SELECT video_id FROM history WHERE profile_id=?", (pid,))}
    subs = {r[0] for r in c.execute("SELECT channel_id FROM subs WHERE profile_id=?", (pid,))}
    aff = {}                                  # how much you like each channel, fading with a 30-day half-life
    for h in hist:
        if h["channel_id"]:
            aff[h["channel_id"]] = aff.get(h["channel_id"], 0) + h["times"] * 0.5 ** ((now - h["watched_at"]) / (30 * day))
    top = max(aff.values(), default=1)
    score = {}
    for i, h in enumerate(hist[:25]):         # "up next" of recent watches, recent ones count more
        for vid, pos in c.execute("SELECT video_id, pos FROM related WHERE src=?", (h["video_id"],)):
            score[vid] = score.get(vid, 0) + 1 / (1 + 0.15 * i) / (1 + 0.1 * pos)
    for vid, pub in c.execute("SELECT v.id, v.published FROM videos v JOIN subs s ON s.channel_id=v.channel_id "
                              "AND s.profile_id=? WHERE v.short=0 AND v.published>?", (pid, now - 21 * day)):
        score[vid] = score.get(vid, 0) + 0.8 * (1 - (now - pub) / (21 * day))   # fresh, unwatched uploads
    ids = [v for v in sorted(score, key=score.get, reverse=True) if v not in watched][:500]
    if not ids:
        return []
    rows = c.execute(f"SELECT * FROM videos WHERE short=0 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()

    def final(r):
        ch = r["channel_id"]
        return score[r["id"]] * (1 + aff.get(ch, 0) / top) * (1.25 if ch in subs else 1)
    out, per = [], {}
    for r in sorted(rows, key=final, reverse=True):
        k = r["channel_id"] or r["channel_title"]
        if per.get(k, 0) < 3:                 # variety: at most 3 per channel
            per[k] = per.get(k, 0) + 1
            out.append(r)
            if len(out) >= limit:
                break
    return out


def new_to_you(c, pid, limit=48):
    """Channels this profile has never watched or subscribed to, reached through what it does watch.
    Hop 1: 'up next' of your watches. Hop 2: 'up next' of those suggestions (cached in the background)."""
    hist = c.execute("SELECT h.video_id FROM history h LEFT JOIN videos v ON v.id=h.video_id WHERE h.profile_id=? "
                     "AND COALESCE(v.short,0)=0 ORDER BY h.watched_at DESC LIMIT 100", (pid,)).fetchall()
    watched = {r[0] for r in c.execute("SELECT video_id FROM history WHERE profile_id=?", (pid,))}
    known = {r[0] for r in c.execute("SELECT channel_id FROM subs WHERE profile_id=?", (pid,))}
    known_titles = set()
    for cid, title in c.execute("SELECT v.channel_id, v.channel_title FROM history h JOIN videos v ON v.id=h.video_id "
                                "WHERE h.profile_id=?", (pid,)):
        known.add(cid)
        known_titles.add(title)
    known.discard(None)
    known_titles.discard("")
    known_titles.discard(None)

    def spread(sources, into):
        for src, w in sources.items():
            for vid, pos in c.execute("SELECT video_id, pos FROM related WHERE src=?", (src,)):
                into[vid] = into.get(vid, 0) + w / (1 + 0.1 * pos)
    hop1 = {}
    spread({h[0]: 1 / (1 + 0.05 * i) for i, h in enumerate(hist)}, hop1)
    cached = {r[0] for r in c.execute("SELECT src FROM related_src")}
    score = dict(hop1)
    spread({v: 0.5 * min(s, 1) for v, s in hop1.items() if v in cached}, score)
    ids = [v for v in sorted(score, key=score.get, reverse=True) if v not in watched][:800]
    if not ids:
        return []
    out, seen = [], set()
    for r in sorted(c.execute(f"SELECT * FROM videos WHERE short=0 AND id IN ({','.join('?' * len(ids))})", ids),
                    key=lambda r: score[r["id"]], reverse=True):
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
    # stale lists go, except those of each profile's last 25 watches (For you / New to you are built from them)
    stale = ("SELECT src FROM related_src WHERE fetched_at < ? AND src NOT IN (SELECT video_id FROM history h WHERE "
             "(SELECT COUNT(*) FROM history h2 WHERE h2.profile_id=h.profile_id AND h2.watched_at>h.watched_at) < 25)")
    old = now - 30 * 86400
    c.execute(f"DELETE FROM related WHERE src IN ({stale})", (old,))
    c.execute(f"DELETE FROM related_src WHERE src IN ({stale})", (old,))
    n = c.execute(
        "DELETE FROM videos WHERE id NOT IN (SELECT video_id FROM history) "
        "AND id NOT IN (SELECT video_id FROM related) AND id NOT IN (SELECT src FROM related_src) "
        "AND (CASE WHEN channel_id IN (SELECT channel_id FROM subs) THEN published < ? ELSE published < ? END)",
        (now - 365 * 86400, now - 60 * 86400)).rowcount
    c.execute("DELETE FROM channels WHERE id NOT IN (SELECT channel_id FROM subs)")
    c.execute("VACUUM")  # hand the freed space back to the drive
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return n


def refresher_loop():
    last_tidy = 0
    while True:
        try:
            refresh_stale()
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
nav{display:flex;gap:2px}nav a{padding:6px 12px;border-radius:999px;color:var(--muted);white-space:nowrap}
nav a:hover{color:var(--fg)}nav a.on{background:var(--card);color:var(--fg);box-shadow:0 0 0 1px var(--line)}
form.go{flex:1;max-width:520px;margin-left:auto;display:flex}
input[type=text]{width:100%;padding:8px 14px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--fg);font:inherit}
input[type=text]:focus{outline:2px solid var(--accent);outline-offset:-1px}
.btn{display:inline-block;padding:7px 16px;border-radius:999px;border:0;background:var(--accent);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.btn.ghost{background:var(--card);color:var(--fg);box-shadow:0 0 0 1px var(--line)}
.btn.small{padding:4px 12px;font-size:13px}
.av{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;color:#fff;font-weight:700;flex:none}
main{max-width:1440px;margin:0 auto;padding:22px 20px 60px}
h1{font-size:22px;margin:0 0 18px;letter-spacing:-.3px}
.bar{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}.bar h1{margin:0;margin-right:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:24px 16px}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px;background:var(--line);display:block}
.card h3{font-size:14.5px;font-weight:600;margin:8px 0 2px;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card:hover h3{color:var(--accent)}
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
@media(max-width:720px){header{flex-wrap:wrap;padding:10px 16px;row-gap:8px}header>.av{order:1;margin-left:auto}
  nav{order:2;flex-basis:100%;overflow-x:auto;scrollbar-width:none}nav::-webkit-scrollbar{display:none}
  nav a{padding:6px 9px;font-size:14px}form.go{order:3;flex-basis:100%;max-width:none}main{padding:16px}
  .grid{grid-template-columns:1fr 1fr;gap:16px 10px}.row img{width:130px}}
@media(max-width:440px){.grid{grid-template-columns:1fr}}
"""

# Extra styles when the page is shown by the YakTube TV app (its browser says "YakTubeTV").
TV_CSS = """<style>
:root{--bg:#111;--card:#1c1c1e;--fg:#f2f2f2;--muted:#9a9aa0;--line:#2c2c2e;--accent:#ff6b2c}
body{font-size:17px}header{position:static}main{padding:18px 28px 40px}
a:focus,button:focus,input:focus,iframe:focus{outline:4px solid #fff!important;outline-offset:3px;border-radius:10px}
.card{transition:transform .1s}.card:focus{transform:scale(1.05)}.card:focus h3{color:var(--accent)}
.grid{grid-template-columns:repeat(4,1fr)}
a[target=_blank]{display:none}
.watch{grid-template-columns:1fr}.player{aspect-ratio:auto;height:calc(100vh - 58px);overflow:hidden}
.tvbar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:8px}
.tvbar button{font:inherit;font-size:12px;font-weight:600;padding:3px 10px;border-radius:999px;border:0;background:var(--card);color:var(--fg)}
.tvbar button:focus{background:var(--fg);color:var(--bg);outline-width:2px!important;outline-offset:2px}.tvbar .meta{margin-left:6px;font-size:12px}
</style>"""
# TV player: the YouTube player never takes the remote's focus (it lives in a shadow root the remote
# navigation can't see). Our own buttons drive it through YouTube's embed commands (enablejsapi).
TV_PLAYER = """<div id="pl" class="player"></div>
<div class="tvbar"><button id="pp">❚❚ Pause</button><button data-k="-10">⟲ 10s</button><button data-k="10">10s ⟳</button>
<button id="cc">CC off</button><button id="sp">Speed 1×</button><span id="tm" class="meta">0:00</span></div>
<script>(function(){var V="__VID__",st={t:0,d:0,s:-1},cc=false,rates=[1,1.25,1.5,2,0.75],ri=0,$=function(i){return document.getElementById(i)};
var f=document.createElement("iframe");f.allow="autoplay; encrypted-media; fullscreen";f.referrerPolicy="strict-origin-when-cross-origin";
f.style.cssText="width:100%;height:100%;border:0;display:block;background:#000";
f.src="https://www.youtube-nocookie.com/embed/"+V+"?autoplay=1&rel=0&modestbranding=1&controls=0&enablejsapi=1&origin="+encodeURIComponent(location.origin);
$("pl").attachShadow({mode:"open"}).appendChild(f);
function cmd(fn,a){f.contentWindow.postMessage(JSON.stringify({event:"command",func:fn,args:a||[],id:1,channel:"widget"}),"*")}
var hello=setInterval(function(){f.contentWindow&&f.contentWindow.postMessage(JSON.stringify({event:"listening",id:1,channel:"widget"}),"*")},400);
function tf(x){x=Math.max(0,Math.floor(x));var h=Math.floor(x/3600),m=Math.floor(x/60)%60,s=("0"+x%60).slice(-2);return(h?h+":"+("0"+m).slice(-2):m)+":"+s}
function upd(){$("pp").textContent=st.s==1||st.s==3?"❚❚ Pause":"▶ Play";$("tm").textContent=tf(st.t)+(st.d?" / "+tf(st.d):"")}
addEventListener("message",function(e){if(!/youtube/.test(e.origin))return;var m;try{m=JSON.parse(e.data)}catch(x){return}if(m.event=="onReady"||m.event=="initialDelivery")clearInterval(hello);
var i=m.info||{};if("currentTime" in i)st.t=i.currentTime;if(i.duration)st.d=i.duration;if("playerState" in i)st.s=i.playerState;
if(!cc&&(m.event=="onReady"||(st.s==1&&!st.off))){cmd("unloadModule",["captions"]);if(st.s==1)st.off=1}upd()});
function toggle(){if(st.s==1||st.s==3){cmd("pauseVideo");st.s=2}else{cmd("playVideo");st.s=1}upd()}
function seek(d){st.t=Math.min(Math.max(0,st.t+d),st.d||1e9);cmd("seekTo",[st.t,true]);upd()}
$("pp").onclick=toggle;[].forEach.call(document.querySelectorAll("[data-k]"),function(b){b.onclick=function(){seek(+b.dataset.k)}});
$("cc").onclick=function(){cc=!cc;cmd(cc?"loadModule":"unloadModule",["captions"]);if(cc)cmd("setOption",["captions","track",{languageCode:"en"}]);this.textContent="CC "+(cc?"on":"off")};
$("sp").onclick=function(){ri=(ri+1)%rates.length;cmd("setPlaybackRate",[rates[ri]]);this.textContent="Speed "+rates[ri]+"×"};
addEventListener("keydown",function(k){var c=k.key;if(c=="MediaPlayPause"||c=="MediaPlay"||c=="MediaPause"){toggle();k.preventDefault()}
else if(c=="MediaFastForward"){seek(10);k.preventDefault()}else if(c=="MediaRewind"){seek(-10);k.preventDefault()}});
var bar=document.querySelector(".tvbar");[].forEach.call(bar.querySelectorAll("button"),function(b){b.scrollIntoView=function(){}});
function home(){var y=$("pl").getBoundingClientRect().top+scrollY-8;if(Math.abs(scrollY-y)>2)scrollTo(0,y)}
bar.addEventListener("focusin",home);addEventListener("load",home);
$("pp").focus()})()</script>"""
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
if(m.event=="onReady"&&!go){go=1;cmd("unloadModule",["captions"]);if(!plain){cmd("addEventListener",["onAutoplayBlocked"]);cmd("playVideo");setTimeout(quiet,2500)}}
if(m.event=="onAutoplayBlocked"&&!plain)quiet();
if(st.s==1&&!st.off){st.off=1;cc();setTimeout(cc,1500);setTimeout(cc,4000)}
on&&on(st,m)});
function cc(){cmd("unloadModule",["captions"])}  // YouTube loads captions a moment after starting: switch them off again
return{st:st,cmd:cmd}}</script>"""
EMBED = "https://www.youtube-nocookie.com/embed/"
WEB_PLAYER = """<div class="pw"><iframe id="yt" class="player" allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe></div>
""" + YT_JS + """<script>(function(){var f=document.getElementById("yt");
f.src="https://www.youtube-nocookie.com/embed/__VID__?autoplay=1&rel=0&modestbranding=1&playsinline=1&enablejsapi=1&origin="+encodeURIComponent(location.origin);
yakPlayer(f,null,1)})()</script>"""

# Shorts: full screen, one at a time, loops. Swipe up (or wheel / Down arrow / the TV remote's Down) for the next one,
# tap (or OK) to pause. A see-through layer over the player catches the swipes; the player itself never gets touched.
SHORTS_CSS = """<style>html,body{overflow:hidden;overscroll-behavior:none;background:#000}
#sh{position:fixed;inset:0;background:#000;color:#fff;z-index:10;-webkit-user-select:none;user-select:none}
#pl,#info{position:absolute;left:50%;transform:translateX(-50%);width:min(100vw,56.25vh);width:min(100vw,56.25dvh)}
#pl{top:0;bottom:0}#tap{position:absolute;inset:0;touch-action:none;cursor:pointer}
#x{position:absolute;top:12px;left:12px;width:44px;height:44px;border-radius:50%;background:rgba(0,0,0,.5);
  display:grid;place-items:center;font-size:24px;line-height:1;z-index:2}
#info{bottom:0;padding:48px 16px 22px;background:linear-gradient(transparent,rgba(0,0,0,.7));pointer-events:none}
#info b{display:block;font-size:15px;line-height:1.3}#info span{font-size:13px;opacity:.8}
#snd{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.7);padding:16px 28px;
  border-radius:999px;font-size:20px;font-weight:600;pointer-events:none;white-space:nowrap}
#pz{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;opacity:.85;pointer-events:none}
#ud{position:absolute;right:24px;top:50%;transform:translateY(-50%);display:flex;flex-direction:column;gap:14px;z-index:2}
#ud button{width:52px;height:52px;border-radius:50%;border:0;background:rgba(255,255,255,.16);color:#fff;font-size:20px;cursor:pointer}
@media (hover:none){#ud{display:none}}
#sh .empty{position:absolute;top:30%;left:0;right:0;color:#ccc}</style>"""
SHORTS_JS = """<script>(function(){var L=__LIST__,i=0,seen={},$=function(x){return document.getElementById(x)};
function close(){var r=document.referrer;if(r&&r.indexOf(location.origin+"/")==0&&history.length>1)history.back();else location.href="/"}
$("x").onclick=function(k){k.preventDefault();close()};
addEventListener("keydown",function(k){var h={ArrowDown:next,PageDown:next,ArrowUp:prev,PageUp:prev,Enter:tap," ":tap,
  MediaPlayPause:tap,Escape:close}[k.key];if(h){k.preventDefault();k.stopImmediatePropagation();h()}},true);
if(!L.length)return;
var f=document.createElement("iframe");f.allow="autoplay; encrypted-media";f.referrerPolicy="strict-origin-when-cross-origin";
f.style.cssText="width:100%;height:100%;border:0;display:block;pointer-events:none";
f.src="__EMBED__"+L[0].id+"?autoplay=1&controls=0&rel=0&playsinline=1&modestbranding=1&iv_load_policy=3&enablejsapi=1&origin="+encodeURIComponent(location.origin);
$("pl").attachShadow({mode:"open"}).appendChild(f);
var p=yakPlayer(f,function(st){if(st.s==0){p.cmd("seekTo",[0,true]);p.cmd("playVideo")}
  if(st.s==1&&!seen[L[i].id]){seen[L[i].id]=1;fetch("/seen",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"v="+L[i].id})}
  $("snd").hidden=!st.muted;$("pz").hidden=st.s!=2});
function show(){$("ti").textContent=L[i].title;$("ch").textContent=L[i].channel;history.replaceState(null,"","/shorts?v="+L[i].id)}
function go(n){i=(n+L.length)%L.length;p.st.off=0;p.st.s=-1;p.cmd("loadVideoById",[L[i].id,0]);show()}
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


def thumb(vid):
    return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"


def card(v, seen=False):
    meta = " · ".join(x for x in (views(v["views"]), ago(v["published"])) if x)
    return (f'<a class="card{" seen" if seen else ""}" href="/watch?v={v["id"]}">'
            f'<img loading="lazy" src="{thumb(v["id"])}" alt=""><h3>{e(v["title"] or v["id"])}</h3>'
            f'<div class="meta">{e(v["channel_title"] or "")}</div><div class="meta">{meta}</div></a>')


def side_row(r, seen=False):
    meta = " · ".join(x for x in (r["channel_title"] or "", views(r["views"]), ago(r["published"])) if x)
    return (f'<a class="row{" seen" if seen else ""}" href="/watch?v={r["id"]}"><img loading="lazy" src="{thumb(r["id"])}" alt="">'
            f'<div><h3>{e(r["title"] or r["id"])}</h3><div class="meta">{e(meta)}</div></div></a>')


def layout(title, body, prof=None, tab="", q=""):
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(title)} · YakTube</title><link rel="icon" type="image/png" href="/favicon.png">'
            f'<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.json" crossorigin="use-credentials">'
            f'<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">'
            f'<meta name="apple-mobile-web-app-title" content="YakTube"><style>{CSS}</style></head><body>')
    if not prof:
        return head + f'<main>{body}</main></body></html>'
    tabs = "".join(f'<a href="{h}" class="{"on" if tab == k else ""}">{n}</a>'
                   for k, h, n in (("feed", "/", "Feed"), ("fy", "/foryou", "For you"), ("new", "/new", "New to you"), ("shorts", "/shorts", "Shorts"), ("subs", "/subs", "Subscriptions"), ("hist", "/history", "History")))
    return head + (
        f'<header><a class="logo" href="/">Yak<b>Tube</b></a><nav>{tabs}</nav>'
        f'<form class="go" action="/go"><input type="text" name="q" placeholder="Search, or paste a YouTube link…" '
        f'autocomplete="off" value="{e(q)}"></form>'
        f'<a href="/profiles" title="Switch profile ({e(prof["name"])})" class="av" style="background:{prof["color"]}">'
        f'{e(prof["name"][:1].upper())}</a></header><main>{body}</main>'
        # press "/" anywhere to jump to the search box
        '<script>addEventListener("keydown",k=>{if(k.key=="/"&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName))'
        '{k.preventDefault();document.querySelector(".go input").focus()}});'
        'var t=document.querySelector("nav .on");if(t)t.parentNode.scrollLeft=t.offsetLeft-40</script></body></html>')


# ---------------------------------------------------------------- server
class App(BaseHTTPRequestHandler):
    server_version = "YakTube/1"

    # -- helpers
    def send(self, body, status=200, ctype="text/html; charset=utf-8", headers=()):
        if isinstance(body, str) and ctype.startswith("text/html") and "YakTubeTV" in self.headers.get("User-Agent", ""):
            body = body.replace("</head>", TV_CSS + "</head>", 1)
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")  # embeds need a referrer
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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
            if u.path.startswith("/profiles"):
                return getattr(self, method.lower() + "_profiles")(u.path)
            self.prof = self.profile()
            if not self.prof:
                return self.redirect("/profiles")
            h = {("GET", "/"): self.feed, ("GET", "/watch"): self.watch, ("GET", "/go"): self.go,
                 ("GET", "/subs"): self.subs, ("POST", "/subs/add"): self.sub_add,
                 ("POST", "/subs/remove"): self.sub_remove, ("POST", "/refresh"): self.refresh,
                 ("GET", "/foryou"): self.foryou, ("GET", "/shorts"): self.shorts, ("POST", "/seen"): self.seen, ("GET", "/new"): self.newtoyou, ("GET", "/search"): self.search, ("GET", "/related"): self.related,
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
        nsubs = self.db.execute("SELECT COUNT(*) FROM subs WHERE profile_id=?", (pid,)).fetchone()[0]
        if not nsubs:
            return self.send(layout("Feed", '<div class="empty"><h1>Your feed is empty</h1><p>Subscribe to a few channels '
                                    'and their new videos will show up here, newest first.</p>'
                                    '<a class="btn" href="/subs">Add subscriptions</a></div>', self.prof, "feed"))
        rows = self.db.execute(
            "SELECT v.*, h.video_id AS seen FROM videos v JOIN subs s ON s.channel_id=v.channel_id AND s.profile_id=? "
            "LEFT JOIN history h ON h.video_id=v.id AND h.profile_id=? WHERE v.short=0 "   # Shorts have their own tab
            "ORDER BY v.published DESC LIMIT ? OFFSET ?",
            (pid, pid, PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)).fetchall()
        more = len(rows) > PAGE_SIZE
        grid = "".join(card(v, v["seen"]) for v in rows[:PAGE_SIZE])
        body = (f'<div class="bar"><h1>Latest from your subscriptions</h1>'
                f'<form method="post" action="/refresh"><button class="btn ghost small">Refresh</button></form></div>'
                f'<div class="grid">{grid or "<p class=meta>Fetching videos… refresh in a moment.</p>"}</div>'
                + (f'<p class="center" style="margin-top:30px"><a class="btn ghost" href="/?page={page + 1}">Load more</a></p>' if more else ""))
        self.send(layout("Feed", body, self.prof, "feed"))

    def foryou(self):
        threading.Thread(target=warm_related, args=(self.prof["id"],), daemon=True).start()
        rows = for_you(self.db, self.prof["id"])
        if not rows:
            body = ('<div class="empty"><h1>Nothing here yet</h1><p>Watch a few videos (search for something you like) '
                    'and suggestions based on what you watch will show up here. It\'s all worked out on this '
                    'computer from your own history.</p><a class="btn" href="/">Back to your feed</a></div>')
        else:
            body = (f'<div class="bar"><h1>For you</h1><span class="meta">Based on what you\'ve watched</span></div>'
                    f'<div class="grid">{"".join(card(r) for r in rows)}</div>')
        self.send(layout("For you", body, self.prof, "fy"))

    def newtoyou(self):
        threading.Thread(target=warm_related, args=(self.prof["id"],), daemon=True).start()
        threading.Thread(target=warm_explore, args=(self.prof["id"],), daemon=True).start()
        rows = new_to_you(self.db, self.prof["id"])
        if not rows:
            body = ('<div class="empty"><h1>Nothing new yet</h1><p>Watch a few videos and this page will find '
                    'channels you haven\'t tried, reached through the ones you like.</p>'
                    '<a class="btn" href="/">Back to your feed</a></div>')
        else:
            body = (f'<div class="bar"><h1>New to you</h1><span class="meta">Channels you haven\'t watched yet · '
                    f'grows as you explore</span></div><div class="grid">{"".join(card(r) for r in rows)}</div>')
        self.send(layout("New to you", body, self.prof, "new"))

    def shorts(self):
        first = self.q.get("v", "")
        rows = shorts_for(self.db, self.prof["id"], first if VID_RE.match(first) else None)
        data = json.dumps(rows).replace("</", "<\\/")
        empty = ('' if rows else '<div class="empty"><h1>No Shorts right now</h1><p>Shorts from all over YouTube show up here. '
                 'A new batch arrives every hour (and a few minutes after a restart).</p></div>')
        body = (SHORTS_CSS + '<div id="sh"><div id="pl"></div><div id="tap"></div>'
                '<a id="x" href="/" aria-label="Close Shorts">✕</a>' + empty +
                '<div id="info"><b id="ti"></b><span id="ch"></span></div><div id="snd" hidden>🔇 Tap for sound</div>'
                '<div id="pz" hidden>▶</div><div id="ud"><button id="up" aria-label="Previous">▲</button>'
                '<button id="dn" aria-label="Next">▼</button></div></div>'
                + YT_JS + SHORTS_JS.replace("__LIST__", data).replace("__EMBED__", EMBED))
        self.send(layout("Shorts", body))

    def seen(self):
        vid = self.form().get("v", "")
        if VID_RE.match(vid):
            ensure_video(self.db, vid)
            mark_watched(self.db, self.prof["id"], vid)
        self.send("", 204)

    def refresh(self):
        refresh_stale(force=True)
        self.redirect("/")

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
            return self.redirect("/")
        try:
            vids = search(self.db, q)
            err = ""
        except Exception as ex:
            print("[search]", repr(ex))
            vids, err = [], "Search isn't working right now (YouTube may have changed something). Pasting a link still works."
        seen = {r[0] for r in self.db.execute("SELECT video_id FROM history WHERE profile_id=?", (self.prof["id"],))}
        grid = "".join(card(v, v["id"] in seen) for v in vids)
        body = (f'<h1>Results for “{e(q)}”</h1>' + (f'<div class="err">{e(err)}</div>' if err else "")
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
        mark_watched(self.db, pid, vid)
        subbed = v["channel_id"] and self.db.execute(
            "SELECT 1 FROM subs WHERE profile_id=? AND channel_id=?", (pid, v["channel_id"])).fetchone()
        nxt = e("/watch?v=" + vid)
        if subbed:
            sub = (f'<form method="post" action="/subs/remove"><input type="hidden" name="id" value="{v["channel_id"]}">'
                   f'<input type="hidden" name="next" value="{nxt}"><button class="btn ghost small">Subscribed ✓</button></form>')
        elif v["channel_id"] or v["channel_url"]:
            sub = (f'<form method="post" action="/subs/add"><input type="hidden" name="url" value="{e(v["channel_id"] or v["channel_url"])}">'
                   f'<input type="hidden" name="v" value="{vid}"><input type="hidden" name="next" value="{nxt}">'
                   f'<button class="btn small">Subscribe</button></form>')
        else:
            sub = ""
        side = ""
        if v["channel_id"]:
            rows = self.db.execute("SELECT * FROM videos WHERE channel_id=? AND id<>? ORDER BY published DESC LIMIT 6",
                                   (v["channel_id"], vid)).fetchall()
            side = "".join(side_row(r) for r in rows)
        meta = " · ".join(x for x in (views(v["views"]), ago(v["published"])) if x)
        if "YakTubeTV" in self.headers.get("User-Agent", ""):
            player = TV_PLAYER.replace("__VID__", vid)
        else:
            player = WEB_PLAYER.replace("__VID__", vid)
        body = (f'<div class="watch"><div>{player}'
                f'<h1>{e(v["title"] or "Video")}</h1>'
                f'<div class="bar"><strong>{e(v["channel_title"] or "")}</strong>{sub}'
                f'<span class="meta" style="margin-left:auto">{meta}</span>'
                f'<a class="meta" href="https://www.youtube.com/watch?v={vid}" target="_blank" rel="noopener">Open on YouTube ↗</a></div></div>'
                f'<aside><h3 style="margin-top:0">Up next</h3><div id="upnext"><p class="meta">Loading…</p></div>'
                f'{"<h3>More from this channel</h3>" + side if side else ""}</aside></div>'
                # one tiny script: load related videos after the player has started
                f'<script>fetch("/related?v={vid}").then(r=>r.text()).then(h=>{{document.getElementById("upnext")'
                f'.innerHTML=h||"<p class=meta>Nothing to suggest.</p>"}})</script>')
        self.send(layout(v["title"] or "Watch", body, self.prof))

    # -- subscriptions
    def subs(self, err=""):
        pid = self.prof["id"]
        rows = self.db.execute(
            "SELECT ch.*, (SELECT MAX(published) FROM videos WHERE channel_id=ch.id) AS last "
            "FROM subs s JOIN channels ch ON ch.id=s.channel_id WHERE s.profile_id=? ORDER BY ch.title COLLATE NOCASE",
            (pid,)).fetchall()
        items = "".join(
            f'<div><div class="av" style="background:{COLORS[sum(map(ord, r["id"])) % len(COLORS)]}">{e(r["title"][:1].upper())}</div>'
            f'<div class="grow"><strong>{e(r["title"])}</strong><div class="meta">'
            f'{"Last upload " + ago(r["last"]) if r["last"] else "No videos fetched yet"}</div></div>'
            f'<a class="meta" href="https://www.youtube.com/channel/{r["id"]}" target="_blank" rel="noopener">YouTube ↗</a>'
            f'<form method="post" action="/subs/remove"><input type="hidden" name="id" value="{r["id"]}">'
            f'<button class="btn ghost small">Unsubscribe</button></form></div>' for r in rows)
        pre = e(self.q.get("add", ""))
        body = (f'<h1>Subscriptions <span class="meta">({len(rows)})</span></h1>'
                + (f'<div class="err">{e(err)}</div>' if err else "")
                + f'<form method="post" action="/subs/add" class="bar"><input type="text" name="url" value="{pre}" required '
                  f'placeholder="Channel link, @handle, or any video link from the channel" style="flex:1;max-width:560px">'
                  f'<button class="btn">Subscribe</button></form>'
                + (f'<div class="list">{items}</div>' if rows else '<p class="meta">No subscriptions yet.</p>'))
        self.send(layout("Subscriptions", body, self.prof, "subs"))

    def sub_add(self):
        f = self.form()
        try:
            cid = resolve_channel(f.get("url", ""))
            refresh_channel(self.db, cid)
        except Exception as ex:
            return self.subs(err=f"Couldn't subscribe: {ex}")
        self.db.execute("INSERT OR IGNORE INTO subs(profile_id,channel_id,added_at) VALUES(?,?,?)",
                        (self.prof["id"], cid, int(time.time())))
        if VID_RE.match(f.get("v", "")):
            self.db.execute("UPDATE videos SET channel_id=? WHERE id=? AND channel_id IS NULL", (cid, f["v"]))
        nxt = f.get("next", "")
        self.redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/subs")

    def sub_remove(self):
        f = self.form()
        self.db.execute("DELETE FROM subs WHERE profile_id=? AND channel_id=?", (self.prof["id"], f.get("id", "")))
        nxt = f.get("next", "")
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
