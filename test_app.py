"""Phase 1 tests: run with `python3 test_app.py`. YouTube is mocked; no network needed."""
import json, os, re, sys, tempfile, threading, time, unittest, urllib.error, urllib.request, urllib.parse, http.cookiejar
os.environ["YAK_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

CID = "UC" + "a" * 22
FEED = f"""<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
 xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom"><title>Woz &amp; Friends</title>
{''.join(f'''<entry><yt:videoId>vid{i:08d}</yt:videoId><yt:channelId>{CID}</yt:channelId><title>Video {i} &lt;b&gt;</title>
<link rel="alternate" href="https://www.youtube.com/{'shorts/' if i == 3 else 'watch?v='}vid{i:08d}"/>
<author><name>Woz &amp; Friends</name></author><published>2026-11-0{i+1}T10:00:00+00:00</published>
<media:group><media:community><media:statistics views="{1234567 * i}"/></media:community></media:group></entry>''' for i in range(1, 5))}
</feed>"""

CID2 = "UC" + "b" * 22   # a channel whose RSS feed is "down" (404), like YouTube in Nov 2026
import json as _j
def lockup(vid, title, chan="", cid="", when="3d ago", v="1.2M"):
    rows = ([{"metadataParts": [{"text": {"content": chan}}]}] if chan else []) + \
           [{"metadataParts": [{"text": {"content": v}}, {"text": {"content": when}}]}]
    nav = {"browseEndpoint": {"browseId": cid}} if cid else {}
    return {"lockupViewModel": {"contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO", "nav": nav,
            "metadata": {"lockupMetadataViewModel": {"title": {"content": title},
                         "metadata": {"contentMetadataViewModel": {"metadataRows": rows}}}}}}
def page(data, extra=""): return extra + "<script>var ytInitialData = " + _j.dumps(data) + ";</script>"
SEARCH = {"contents": [{"videoRenderer": {"videoId": "srch0000001", "title": {"runs": [{"text": "Apple II <story>"}]},
          "longBylineText": {"runs": [{"text": "Retro", "navigationEndpoint": {"browseEndpoint": {"browseId": CID2}}}]},
          "publishedTimeText": {"simpleText": "13y ago"}, "viewCountText": {"simpleText": "17,872 views"}}},
          {"channelRenderer": {"channelId": CID2, "title": {"simpleText": "Retro & Co"},   # YouTube's odd field names
           "subscriberCountText": {"simpleText": "@retro"}, "videoCountText": {"simpleText": "12K subscribers"}}},
          {"shortsLockupViewModel": {"x": 1}}, {"lockupViewModel": {"contentId": "PLx", "contentType": "LOCKUP_CONTENT_TYPE_PLAYLIST"}}]}
RELATED = [lockup(f"rel{i:08d}", f"Related {i}", "Retro", CID2) for i in range(3)] + [lockup("vid00000001", "Video 1", "Woz", CID)]

def fake_fetch(url, timeout=10):
    if "feeds/videos.xml" in url and CID2 in url: raise IOError("HTTP Error 404")
    if "feeds/videos.xml" in url: return FEED
    if f"/channel/{CID2}/videos" in url:
        return page({"items": [lockup("chb00000001", "Newest retro", when="1d ago"), lockup("chb00000002", "Older retro", when="1w ago")]},
                    '<meta property="og:title" content="Retro &amp; Co">')
    if f"/channel/{CID}/videos" in url:   # the channel page looks past the feed's newest few
        return page({"items": [lockup("vid00000004", "Video 4 <b>"), lockup("old00000001", "Back catalogue", when="2y ago")]},
                    '<meta property="og:title" content="Woz &amp; Friends">')
    if "results?" in url: return page(SEARCH)
    if "watch?v=srch0000001" in url: return page({"r": RELATED}, f'"channelId":"{CID2}","viewCount":"17872","publishDate":"2013-11-09T12:00:00-08:00"')
    if "oembed" in url: return '{"title":"Outside vid","author_name":"Other","author_url":"https://www.youtube.com/@other"}'
    if "woz" in url or "watch?v=" in url: return f'<link rel="canonical" href="https://www.youtube.com/channel/{CID}">'
    raise IOError("no network in tests")
app.fetch = fake_fetch
app.gh_get = lambda *a, **k: (_ for _ in ()).throw(IOError("no network in tests"))   # GitHub too

class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        cls.srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    def client(self):
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, op, path, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        r = op.open(self.base + path, body)
        return r.status, r.geturl(), r.read().decode()

    def play(self, op, vid, t=30, d=600):
        """Open a video and report what the player reports once it has really played."""
        page = self.get(op, "/watch?v=" + vid)[2]
        op.open(self.base + "/seen", f"v={vid}&n=1&t={t}&d={d}".encode())
        return page

    def login(self, name):
        op = self.client()
        self.get(op, "/profiles/add", {"name": name})
        _, _, page = self.get(op, "/profiles")
        pid = re.findall(r'name="id" value="(\d+)"><button><div class="av"[^>]*>.</div>' + name, page)[0]
        self.get(op, "/profiles/select", {"id": pid})
        return op

    def test_1_flow(self):
        op = self.client()
        s, url, _ = self.get(op, "/")
        self.assertTrue(url.endswith("/profiles"))                    # no profile -> picker
        a = self.login("Alice")
        s, url, page = self.get(a, "/feed")
        self.assertIn("Your feed is empty", page)
        s, url, page = self.get(a, "/subs/add", {"url": "@woz"})      # subscribe by handle
        self.assertIn("Woz &amp; Friends", page)
        s, url, page = self.get(a, "/feed")
        self.assertIn("Video 4 &lt;b&gt;", page)                      # escaped, newest first
        self.assertLess(page.index("Video 4"), page.index("Video 1"))
        self.assertIn('href="/shorts?v=vid00000003"', page)           # subs' Shorts are on by default
        self.assertIn('aria-checked="true"', page)
        s, url, page = self.get(a, "/feed/shorts", {})                 # toggle them off
        self.assertNotIn("Video 3", page)
        self.assertIn('aria-checked="false"', page)
        self.get(a, "/feed/shorts", {})                                # and back on
        self.assertIn("4.9M views", page)
        s, url, page = self.get(a, "/watch?v=vid00000002")
        self.assertIn("youtube-nocookie.com/embed/vid00000002", page)
        self.assertIn("Subscribed ✓", page)
        self.assertIn("More from this channel", page)
        _, _, page = self.get(a, "/feed")
        self.assertNotIn('card seen"', page)                          # opening a video isn't watching it
        self.assertIn("Nothing watched yet", self.get(a, "/history")[2])
        self.play(a, "vid00000002"); self.play(a, "vid00000002")
        _, _, page = self.get(a, "/feed")
        self.assertIn('card seen" href="/watch?v=vid00000002', page)  # watched videos dimmed
        _, _, page = self.get(a, "/history")
        self.assertEqual(page.count("vid00000002/mqdefault"), 1)     # deduped
        # outside link via paste box, then subscribe from watch page
        s, url, page = self.get(a, "/go?q=" + urllib.parse.quote("https://youtu.be/zzzzzzzzzzz"))
        self.assertIn("/watch?v=zzzzzzzzzzz", url)
        self.assertIn("Outside vid", page)
        self.assertIn("Subscribe</button>", page)
        # profiles are isolated
        b = self.login("Kid")
        self.assertIn("Your feed is empty", self.get(b, "/feed")[2])
        self.assertIn("Nothing watched yet", self.get(b, "/history")[2])
        # unsubscribe + history clear
        self.get(a, "/subs/remove", {"id": app.CID_RE.search(CID).group(0)})
        self.assertIn("No subscriptions yet", self.get(a, "/subs")[2])
        self.get(a, "/history/remove", {"v": "zzzzzzzzzzz"})
        self.assertNotIn("Outside vid", self.get(a, "/history")[2])
        self.get(a, "/history/clear", {})
        self.assertIn("Nothing watched yet", self.get(a, "/history")[2])

    def test_16_home_layout(self):
        a = self.login("Homey")
        self.get(a, "/subs/add", {"url": "@woz"})
        c = app.connect()
        now = int(time.time())
        app.store_videos(c, [{"id": f"hsh{i:08d}", "title": f"Home short {i}", "channel_id": CID, "published": now - i, "short": 1}
                             for i in range(6)])
        app.store_videos(c, [{"id": f"hvd{i:08d}", "title": f"Home vid {i}", "channel_id": CID, "published": now - 100 - i}
                             for i in range(20)])
        app.OUTSIDE[:] = []
        _, url, page = self.get(a, "/")
        self.assertIn('href="/" class="on"', page)                    # Home is the start page
        grid = page[page.index('class="grid hgrid"'):page.index('class="hrows"')]   # the phone's grid
        self.assertEqual(grid.count('class="scard"'), 4 * grid.count('class="shelf"'))   # Shorts come in groups of 4...
        self.assertEqual(grid.count('class="shelf"'), 2)               # (24 videos: after the 8th and the 20th)
        self.assertEqual(grid[:grid.index('class="shelf"')].count('<a class="card'), 8)   # ...after the first 8 videos
        self.assertIn('/oar2.jpg', page)
        for nav in ('class="side"', 'class="rib"', 'class="bot"'):    # sidebar (computer), ribbon + bottom bar (phone)
            self.assertIn(nav, page)
        self.assertIn('href="/feed" class=""><svg', page)
        self.assertIn('<span>Profiles</span>', page)
        side = page[page.index('<nav class="side">'):page.index('<main>')]
        self.assertNotIn('class="theme"', side)                       # light or dark: in Settings, not the side panel...
        self.assertIn('class="theme"', page[:page.index('<nav class="side">')])   # ...a phone keeps its header button
        self.assertIn('data-p="theme"', self.get(a, "/settings")[2])
        self.assertTrue(self.get(a, "/foryou")[1].endswith("/"))       # old For you link lands on Home
        self.assertIn('href="/feed" class="on"', self.get(a, "/feed")[2])
        # thumbs up / down: a click sets it, the same again clears it, the other one swaps it
        post = lambda body: a.open(self.base + "/rate", body.encode()).read().decode()
        self.assertIn('data-rate="1" aria-pressed="false"', self.get(a, "/watch?v=hvd00000001")[2])
        self.assertEqual(post("v=hvd00000001&r=1"), "1")
        self.assertIn('class="up on" data-rate="1"', self.get(a, "/watch?v=hvd00000001")[2])
        self.assertEqual(post("v=hvd00000001&r=-1"), "-1")
        self.assertEqual(post("v=hvd00000001&r=-1"), "0")
        self.assertEqual(post("v=hsh00000000&r=-1"), "-1")             # a thumbed-down Short leaves the shelves
        self.assertNotIn("hsh00000000", self.get(a, "/")[2])
        with self.assertRaises(urllib.error.HTTPError):
            post("v=bad&r=1")
        pid = c.execute("SELECT id FROM profiles WHERE name='Homey'").fetchone()[0]
        self.assertEqual(c.execute("SELECT val FROM ratings WHERE profile_id=?", (pid,)).fetchall()[0][0], -1)
        app.OUTSIDE[:] = ["hsh00000001"]
        page = self.get(a, "/shorts")[2]
        self.assertIn('"r": 0', page)                                  # the Shorts player knows each one's rating
        self.assertIn('id="pb" role="slider"', page)                   # progress bar you can drag
        self.assertIn('id="sr" aria-label="Share"', page)
        self.assertIn('"https://youtube.com/shorts/"+it.id', page)     # shares the YouTube link
        app.OUTSIDE[:] = []
        c.close()

    def test_17_engine(self):
        a = self.login("Eng")
        db = app.connect()
        pid = db.execute("SELECT id FROM profiles WHERE name='Eng'").fetchone()[0]
        now = int(time.time())
        post = lambda path, body: a.open(self.base + path, body.encode()).read().decode()
        vids = lambda pre, n, ch: [(f"{pre}{i:08d}", "UC" + ch * 22, f"{pre} topic{i} title") for i in range(n)]
        # two watched videos, each with its own "up next" list from its own channels
        for src, pre in (("engwatchA01", "ea0"), ("engwatchB01", "eb0")):
            db.execute("INSERT OR REPLACE INTO videos(id,channel_id,title) VALUES(?,?,?)", (src, "UCw" + "0" * 21, src))
            rows = [(f"{pre}{i:08d}", "UC" + pre[1] * 4 + str(i) * 18, f"{pre} clip {i}") for i in range(6)]
            db.executemany("INSERT OR REPLACE INTO videos(id,channel_id,title) VALUES(?,?,?)", rows)
            db.executemany("INSERT OR REPLACE INTO related(src,video_id,pos) VALUES(?,?,?)", [(src, r[0], i) for i, r in enumerate(rows)])
            db.execute("INSERT OR REPLACE INTO related_src VALUES(?,?)", (src, now))
        db.execute("INSERT INTO history(profile_id,video_id,watched_at,best,dur) VALUES(?,?,?,?,?)", (pid, "engwatchA01", now - 3 * 86400, 500, 600))
        top = lambda: [r["id"] for r, *_ in app.home_feed(db, pid)][:3]
        self.assertTrue(all(v.startswith("ea0") for v in top()))
        # Home follows what you just watched: after B, B's "up next" leads
        db.execute("INSERT INTO history(profile_id,video_id,watched_at,best,dur) VALUES(?,?,?,?,?)", (pid, "engwatchB01", now - 60, 590, 600))
        self.assertTrue(all(v.startswith("eb0") for v in top()))
        page = self.get(a, "/?why=1")[2]
        self.assertIn("Because you watched: engwatchB01", page)
        self.assertIn('data-s="watch"', page)
        self.assertIn("sendBeacon", page)                              # reports what was on screen
        self.assertEqual(a.open(self.base + "/").headers["Cache-Control"], "no-store")   # Back shows a fresh Home
        # passed over on several visits -> sinks
        first = top()[0]
        post("/impr", f"v={first}&s=watch")
        for _ in range(3):
            db.execute("UPDATE impressions SET last_at=last_at-3600 WHERE profile_id=?", (pid,))
            post("/impr", f"v={first}&s=watch")
        self.assertEqual(db.execute("SELECT n FROM impressions WHERE profile_id=? AND video_id=?", (pid, first)).fetchone()[0], 4)
        self.assertNotIn(first, top())
        post("/impr", "v=bad,eb000000001&s=watch,nonsense")            # junk is ignored
        # thumbs down on B: its "up next" stops counting, and B's channel sinks
        post("/rate", "v=engwatchB01&r=-1")
        self.assertTrue(all(v.startswith("ea0") for v in top()))
        post("/rate", "v=engwatchB01&r=-1")                            # (take it back)
        # a search you made becomes a source; YakTube re-runs it and its own topic searches in the background
        self.get(a, "/search?q=Apple%20%20II")
        self.assertEqual(db.execute("SELECT q, n FROM searches WHERE profile_id=?", (pid,)).fetchall()[0][:], ("apple ii", 1))
        self.assertIn("From your search: “apple ii”", self.get(a, "/?why=1")[2])
        db.execute("UPDATE related_src SET fetched_at=0 WHERE src IN ('q:apple ii','engwatchA01')")
        ran, real = [], (app.search, app.fetch_related, app.refresh_channel)
        app.search = lambda c, q: ran.append(q) or []
        app.fetch_related = lambda c, vid, max_age=0: ran.append(vid)
        app.refresh_channel = lambda c, cid, deep=False: ran.append(cid)
        try:
            db.execute("INSERT INTO ratings VALUES(?, 'engwatchA01', 1, ?)", (pid, now))
            db.execute("INSERT OR IGNORE INTO channels(id,title) VALUES(?, 'W')", ("UCw" + "0" * 21,))
            db.execute("INSERT INTO subs(profile_id,channel_id) VALUES(?,?)", (pid, "UCw" + "0" * 21))
            app.discover_more(db, pid)
        finally:
            app.search, app.fetch_related, app.refresh_channel = real
        self.assertIn("engwatchA01", ran)                              # thumbed up: its list refreshed
        self.assertIn("apple ii", ran)                                 # your search, re-run
        self.assertIn("UCw" + "0" * 21, ran)                           # a favourite subscription, read in depth
        auto = [q for (q,) in db.execute("SELECT q FROM searches WHERE profile_id=? AND auto=1", (pid,))]
        self.assertTrue(auto and all(q in ran for q in auto))          # and topic searches of its own
        # what you watch teaches it which sources work: watched picks raise their source's weight
        db.executemany("INSERT OR REPLACE INTO impressions(profile_id,video_id,src,first_at,last_at,n) VALUES(?,?,'search',?,?,1)",
                       [(pid, f"wgt{i:08d}", now - 100, now - 100) for i in range(30)])
        db.executemany("INSERT OR REPLACE INTO history(profile_id,video_id,watched_at) VALUES(?,?,?)",
                       [(pid, f"wgt{i:08d}", now) for i in range(15)])
        import random
        w = app.source_weights(db, pid, now, random.Random(1))
        self.assertGreater(w["search"], 1.4 * w["topic"])
        # how much of a video was watched is kept
        self.play(a, "engwatchA01", t=300, d=600)
        self.assertEqual(tuple(db.execute("SELECT best, dur FROM history WHERE profile_id=? AND video_id='engwatchA01'", (pid,)).fetchone()), (500, 600))
        db.close()

    def test_18_inline_buttons(self):
        a = self.login("Inline")
        post = lambda path, body: a.open(self.base + path, body.encode()).read().decode()
        page = self.get(a, "/watch?v=vid00000002")[2]
        self.assertIn('<form class="js" method="post" action="/subs/add">', page)   # sent by the page script, no reload
        self.assertIn('<form class="js" method="post" action="/later/toggle">', page)
        self.assertIn('addEventListener("submit"', page)
        out = post("/subs/add", f"url={CID}&v=vid00000002&next=/watch?v=vid00000002&inline=1")
        self.assertTrue(out.startswith('<form class="js" method="post" action="/subs/remove">') and "Subscribed ✓" in out)
        self.assertIn("Subscribed ✓", self.get(a, "/watch?v=vid00000002")[2])
        self.assertIn(">Subscribe</button>", post("/subs/remove", f"id={CID}&next=/x&inline=1"))
        self.assertIn("Saved ✓", post("/later/toggle", "v=vid00000002&next=/watch?v=vid00000002&inline=1"))
        self.assertIn(">Watch later</button>", post("/later/toggle", "v=vid00000002&next=/watch?v=vid00000002&inline=1"))
        with self.assertRaises(urllib.error.HTTPError):               # a failure falls back to the normal form
            post("/subs/add", "url=nonsense-channel&inline=1")
        _, url, _ = self.get(a, "/later/toggle", {"v": "vid00000002", "next": "/watch?v=vid00000002"})   # without the script: as before
        self.assertTrue(url.endswith("/watch?v=vid00000002"))

    def test_19_tv_layout(self):
        a = self.login("TVrows")
        db = app.connect()
        pid = db.execute("SELECT id FROM profiles WHERE name='TVrows'").fetchone()[0]
        now = int(time.time())
        db.execute("INSERT OR REPLACE INTO videos(id,channel_id,title) VALUES('tvwatched01',?, 'Watched on TV')", (CID2,))
        rows = ([(f"tvr{i:08d}", CID2, f"TV pick {i}") for i in range(16)]           # a channel you know...
                + [(f"tvn{i:08d}", "UC" + "%02d" % i * 11, f"TV new {i}") for i in range(6)])   # ...and 6 you don't
        db.executemany("INSERT OR REPLACE INTO videos(id,channel_id,title) VALUES(?,?,?)", rows)
        db.executemany("INSERT OR REPLACE INTO related(src,video_id,pos) VALUES('tvwatched01',?,?)", [(r[0], i) for i, r in enumerate(rows)])
        db.execute("INSERT OR REPLACE INTO related_src VALUES('tvwatched01', ?)", (now,))
        db.execute("INSERT INTO history(profile_id,video_id,watched_at) VALUES(?, 'tvwatched01', ?)", (pid, now))
        web = self.get(a, "/")[2]
        self.assertIn('class="grid hgrid"', web)                       # phones: the grid; computers: the TV's rows...
        self.assertIn('<div class="hrows"><section class="trow" data-row="rec"><h2>Recommended for you</h2>', web)
        self.assertIn('<h2>Because you watched Watched on TV</h2>', web)
        self.assertIn('x.className="rarr "', web)                            # ...moved along with arrows
        for tv_only in ('class="tvhead"', "window.__yak", ".tvside:focus-within", 'var rail=document.querySelector(".tvside")'):
            self.assertNotIn(tv_only, web)                             # no TV search pill, remote, recent channels or TV styles
        a.addheaders = [("User-Agent", "Mozilla/5.0 YakTubeTV/1")]
        page = self.get(a, "/")[2]
        self.assertNotIn('tvinfo', page)                                # details sit under each picture...
        self.assertIn(".strip>a{min-width:0}", page)                     # (cards never stretch to fit long text)
        self.assertIn(".strip .card h3,.strip .scard h3{-webkit-line-clamp:2;height:2.6em", page)   # ...always the same height
        self.assertIn('<h2>Recommended for you</h2>', page)            # rows like the Google TV app
        self.assertIn('<h2>Because you watched Watched on TV</h2>', page)
        self.assertIn('<h2>New to you</h2>', page)                     # its own row, not in the rail
        self.assertEqual(page.count('href="/watch?v=tvn'), 6)          # each new channel once, nowhere else
        self.assertTrue(all(page.index('<h2>New to you</h2>') < page.index(f'href="/watch?v=tvn{i:08d}"') for i in range(6)))
        self.assertIn('class="tvside"', page)
        rail = page[page.index('class="tvside"'):]
        for label in ("Search", "Home", "Shorts", "Subscriptions", "Watch Later", "History", "Channels", "Settings"):
            self.assertIn(f"<span>{label}</span>", rail)
        order = [rail.index(f"<span>{x}</span>") for x in ("Search", "Home", "Subscriptions", "Shorts", "Watch Later", "Channels", "History")]
        self.assertEqual(order, sorted(order))                                   # in the TV app's order...
        self.assertLess(rail.index("Recent channels"), rail.index("<span>Settings</span>"))   # ...and Settings last
        self.assertNotIn("<span>New to you</span>", rail)
        self.assertLess(rail.index("<span>Search"), rail.index("<span>Home"))
        self.assertLess(rail.index("Switch profile"), rail.index("<span>Search"))   # profile at the top...
        self.assertLess(rail.index("<span>Channels"), rail.index("Recent channels"))   # ...recent channels at the bottom
        self.assertIn("Recent channels", rail)                          # channels you watched lately
        self.assertIn(f'href="/channel?id={CID2}"', rail)
        self.assertIn('stopImmediatePropagation', page)                 # the remote: rows and rail
        self.assertIn('menu.className="tvlm"', page)                     # hold OK on a video: a menu
        self.assertIn('fetch("/vstate?v="', page)
        self.assertIn("window.yakHold=function", page)                     # the TV app calls this when OK is held
        self.assertNotIn("TV_MENU", web)
        self.assertNotIn('"tvlm"', web)                                  # (only on the TV)
        post = lambda path, body: a.open(self.base + path, body.encode())
        state = lambda: json.loads(self.get(a, "/vstate?v=tvr00000001")[2])
        self.assertEqual(state(), {"r": 0, "later": False})
        post("/rate", "v=tvr00000001&r=-1")
        post("/later/toggle", "v=tvr00000001")
        self.assertEqual(state(), {"r": -1, "later": True})              # the menu marks what's set
        self.assertEqual(post("/hide", "v=tvr00000002&js=1").status, 204)   # Not interested from the menu: no page back
        self.assertNotIn('href="/watch?v=tvr00000002"', self.get(a, "/")[2])
        with self.assertRaises(urllib.error.HTTPError):
            self.get(a, "/vstate?v=bad")
        # rows seem endless: near a row's end the page asks for up to 12 more it doesn't show yet
        page = self.get(a, "/")[2]
        self.assertIn('data-row="rec"', page)
        self.assertIn('fetch("/tv/more"', page)
        vids = lambda h: re.findall(r'href="/(?:watch|shorts)\?v=([\w-]{11})"', h)
        more = lambda row, have: post("/tv/more", "row=" + urllib.parse.quote(row) + "&have=" + ",".join(have)).read().decode()
        shown = vids(page)
        have = shown[:6]                                                 # pretend only these are on the page
        got = vids(more("rec", have))
        self.assertEqual(len(got), 12)
        self.assertFalse(set(got) & set(have))                           # nothing that's already on the page
        self.assertNotIn("tvr00000002", got)                             # nor anything marked Not interested
        self.assertEqual(len(set(got)), 12)
        got = more("Because you watched: Watched on TV", have)
        self.assertTrue(vids(got))                                       # a reason's row: its own picks first
        self.assertTrue(vids(more("subs", have)))                        # no subscriptions yet: borrows from the others
        app.OUTSIDE_AT[0], app.OUTSIDE[:] = time.time(), ["shr00000001"]  # (no real YouTube in tests)
        self.assertIn('class="scard" href="/shorts?v=shr00000001"', more("shorts", []))   # Shorts: more Shorts
        self.assertEqual(more("shorts", ["shr00000001"]), "")            # (none left: the row rests a minute)
        app.OUTSIDE[:] = []
        self.get(a, "/subs/add", {"url": "@woz"})
        page = self.get(a, "/")[2]
        h2 = re.findall(r"<h2>([^<]*)</h2>", page)
        self.assertEqual(h2[:4], ["Recommended for you", "New from your subscriptions", "New to you", "Shorts"])
        self.assertIn('class="scard" href="/shorts?v=', page)          # pick a Short on its own
        self.assertIn(".strip .scard img{aspect-ratio:9/16", page)      # tall, like YouTube's TV app
        self.assertIn('class=bare', self.get(a, "/watch?v=tvwatched01")[2])   # no rail while a video plays
        self.assertIn('class="go big"', self.get(a, "/search")[2])     # Search: a big box
        self.assertIn("#rt #sr{display:none!important}", self.get(a, "/shorts")[2])  # Shorts: no Share button on the TV
        a.addheaders = []
        self.assertNotIn("#rt #sr{display:none", self.get(a, "/shorts")[2])   # (phones and computers keep it)
        a.addheaders = [("User-Agent", "Mozilla/5.0 YakTubeTV/1")]
        # a normal browser can preview the TV layout
        b = self.login("TVpreview")
        _, url, page = self.get(b, "/feed?tv=1")
        self.assertTrue(url.endswith("/feed"))
        self.assertIn('class="tvside"', page)
        self.assertIn(".tvside{display:flex", page)
        self.assertNotIn(".tvside{display:flex", self.get(b, "/feed?tv=0")[2])
        page = self.get(b, "/feed?tv=1")[2]                                   # the TV app's arrow keys, in the page too,
        self.assertIn("window.__yak=1", page)                                 # after the page's own key handling
        self.assertLess(page.index("stopImmediatePropagation"), page.index("window.__yak=1"))
        self.assertNotIn("window.__yak=1", self.get(b, "/feed?tv=0")[2])
        db.close()

    def test_20_lengths(self):
        self.assertEqual([app.secs(x) for x in ("4:05", "1:02:33", "12:00", "LIVE", "", "3 views")], [245, 3753, 720, 0, 0, 0])
        self.assertEqual([app.clock(x) for x in (245, 3753, 0, None)], ["4:05", "1:02:33", "", ""])
        lk = lockup("len00000001", "Has a length")
        lk["lockupViewModel"]["contentImage"] = {"thumbnailViewModel": {"overlays": [{"thumbnailBottomOverlayViewModel": {
            "badges": [{"thumbnailBadgeViewModel": {"text": "12:34"}}]}}]}}
        sr = {"videoRenderer": {"videoId": "len00000002", "title": {"runs": [{"text": "Searched"}]}, "lengthText": {"simpleText": "1:02:33"}}}
        got = {v["id"]: v["length"] for v in app.parse_items({"a": [lk, sr]})}
        self.assertEqual(got, {"len00000001": 754, "len00000002": 3753})   # from up-next / channel lists and search
        c = app.connect()
        app.store_videos(c, [{"id": "len00000001", "title": "x", "length": 754}])
        app.store_videos(c, [{"id": "len00000001", "title": "x"}])        # a list without the length keeps it
        self.assertEqual(c.execute("SELECT length FROM videos WHERE id='len00000001'").fetchone()[0], 754)
        row = c.execute("SELECT * FROM videos WHERE id='len00000001'").fetchone()
        self.assertIn('<span class="dur">12:34</span>', app.card(row))   # bottom right of the picture
        a = self.login("Lengths")
        a.open(self.base + "/seen", b"v=len00000002&n=1&t=5&d=0")
        a.open(self.base + "/seen", b"v=len00000003&n=1&t=30&d=615")      # the player says how long it is
        self.assertEqual(c.execute("SELECT length FROM videos WHERE id='len00000003'").fetchone()[0], 615)
        c.close()

    def test_2_bad_input(self):
        a = self.login("Tester")
        _, _, page = self.get(a, "/subs/add", {"url": "<script>x</script>"})
        self.assertIn("Couldn&#x27;t subscribe", page)
        self.assertNotIn("<script>x", page)
        self.assertTrue(self.get(a, "/watch?v=bad")[1].endswith("/"))
        _, _, page = self.get(a, "/subs/add", {"url": "@woz", "next": "//evil.com"})
        self.assertNotIn("evil", page)
        try: self.get(a, "/nope"); self.fail()
        except urllib.error.HTTPError as ex: self.assertEqual(ex.code, 404)

    def test_3_parsers(self):
        for s in ("dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3", "https://youtu.be/dQw4w9WgXcQ",
                  "https://youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ"):
            self.assertEqual(app.video_id_from(s), "dQw4w9WgXcQ")
        self.assertIsNone(app.video_id_from("https://www.youtube.com/@woz"))
        self.assertEqual(app.resolve_channel("https://www.youtube.com/channel/" + CID), CID)
        self.assertEqual(app.ago(int(time.time()) - 7200), "2 hours ago")

    def test_5_phase2(self):
        a = self.login("P2")
        # subscribing still works when the channel's RSS feed is down (falls back to the /videos page)
        _, _, page = self.get(a, "/subs/add", {"url": CID2})
        self.assertIn("Retro &amp; Co", page)
        _, _, page = self.get(a, "/feed")
        self.assertLess(page.index("Newest retro"), page.index("Older retro"))
        self.assertIn("1 day ago", page)
        # search box: words -> search, links -> watch, @handle -> subscribe
        s, url, page = self.get(a, "/go?q=apple+ii")
        self.assertIn("/search?q=apple", url)
        self.assertIn("Apple II &lt;story&gt;", page)
        self.assertIn("17.9K views", page)
        self.assertIn("13 years ago", page)
        self.assertNotIn("PLx", page)                                  # playlists/shorts skipped
        self.assertIn("/search?q=programming", self.get(a, "/go?q=programming")[1])  # 11 letters != video id
        self.assertIn("/subs?add=", self.get(a, "/go?q=%40woz")[1])
        # watch page loads "up next" separately; it also learns the video's channel
        _, _, page = self.get(a, "/watch?v=srch0000001")
        self.assertIn('fetch("/related?v=srch0000001")', page)
        _, _, frag = self.get(a, "/related?v=srch0000001")
        self.assertEqual(frag.count('class="row'), 4)
        self.assertNotIn("srch0000001", frag)                         # never suggests the video you're on
        db = app.connect()
        self.assertEqual(db.execute("SELECT channel_id FROM videos WHERE id='srch0000001'").fetchone()[0], CID2)
        app.fetch = lambda *a, **k: (_ for _ in ()).throw(IOError("offline"))   # cached: no network needed
        try: self.assertEqual(self.get(a, "/related?v=srch0000001")[2], frag)
        finally: app.fetch = fake_fetch
        # search failure shows a friendly message, not a crash
        app.fetch = lambda *a, **k: (_ for _ in ()).throw(IOError("offline"))
        try: self.assertIn("Search isn", self.get(a, "/search?q=zzz")[2])
        finally: app.fetch = fake_fetch

    def test_14_channel(self):
        a = self.login("Chan")
        # a channel you're not subscribed to: its videos, a Subscribe button, and more than the feed's newest few
        app.CHANNEL_AT.clear()
        s, url, page = self.get(a, "/channel?id=" + CID)
        self.assertIn("<h1>Woz &amp; Friends</h1>", page)
        self.assertIn("Subscribe</button>", page)
        self.assertLess(page.index("Video 4"), page.index("Video 1"))  # newest first
        self.assertIn("Back catalogue", page)                          # from the channel's videos page
        self.assertIn("4 days ago", page)                              # the feed's exact date wins over "3d ago"
        self.assertNotIn("Your feed is empty", page)
        self.assertIn("Your feed is empty", self.get(a, "/feed")[2])        # looking doesn't subscribe
        # subscribe from the channel page: comes back to it, now subscribed
        s, url, page = self.get(a, "/subs/add", {"url": CID, "next": "/channel?id=" + CID})
        self.assertIn("/channel?id=" + CID, url)
        self.assertIn("Subscribed ✓", page)
        # cards, the watch page and the subscriptions list all lead here
        self.assertIn(f'data-ch="{CID}"', self.get(a, "/feed")[2])
        self.assertIn(f'href="/channel?id={CID}"', self.get(a, "/watch?v=vid00000002")[2])
        self.assertIn(f'href="/channel?id={CID}"', self.get(a, "/subs")[2])
        self.play(a, "vid00000002")
        self.assertIn('card seen" href="/watch?v=vid00000002', self.get(a, "/channel?id=" + CID)[2])
        # a handle is looked up; works with the feed down; cached pages need no network; junk is a tidy 404
        self.assertTrue(self.get(a, "/channel?id=%40woz")[1].endswith("/channel?id=" + CID))
        self.assertIn("Newest retro", self.get(a, "/channel?id=" + CID2)[2])
        app.fetch = lambda *a, **k: (_ for _ in ()).throw(IOError("offline"))
        try:
            self.assertIn("Back catalogue", self.get(a, "/channel?id=" + CID)[2])
            app.CHANNEL_AT.clear()
            page = self.get(a, "/channel?id=" + CID)[2]
            self.assertIn("Couldn&#x27;t check this channel", page)
            self.assertIn("Back catalogue", page)                      # still shows what it has
        finally: app.fetch = fake_fetch
        try: self.get(a, "/channel?id=" + urllib.parse.quote("<script>")); self.fail()
        except urllib.error.HTTPError as ex: self.assertEqual(ex.code, 404)

    def test_15_upgrades(self):
        a = self.login("Up")
        post = lambda path, body: a.open(self.base + path, body.encode()).read().decode()
        # resume: a video picks up where this profile left off; watched to the end, it starts over
        self.assertIn("&start=0&", self.get(a, "/watch?v=vid00000002")[2])
        self.play(a, "vid00000002", t=10)
        post("/seen", "v=vid00000002&n=0&t=125&d=600")
        self.assertIn("&start=125&", self.get(a, "/watch?v=vid00000002")[2])
        db = app.connect()
        times = lambda: db.execute("SELECT times FROM history WHERE video_id='vid00000002' AND profile_id="
                                   "(SELECT id FROM profiles WHERE name='Up')").fetchone()[0]
        self.assertEqual(times(), 1)                                   # position reports aren't extra watches
        a.addheaders = [("User-Agent", "Mozilla/5.0 YakTubeTV/1")]
        page = self.get(a, "/watch?v=vid00000002")[2]
        self.assertIn("t0=125,", page)                                 # the TV player resumes too
        self.assertIn('id="nxg">Play now', page)                       # ...and can play the next video when one ends,
        self.assertIn("localStorage.yakap", page)                     # but only if switched on in Settings (off by default)
        self.assertIn('if(!ap||!a||nx)return', page)
        self.assertIn("#upnext a.row:not(.seen)", page)
        a.addheaders = []
        # watch later: the button under a video, the Later page; cards have no + or x on their pictures
        self.assertIn("Nothing saved yet", self.get(a, "/later")[2])
        self.get(a, "/subs/add", {"url": CID})
        self.assertNotIn("data-lt=", self.get(a, "/feed")[2])
        self.assertEqual(post("/later/toggle", "v=vid00000004"), "1")
        _, url, page = self.get(a, "/later/toggle", {"v": "vid00000002", "next": "/watch?v=vid00000002"})
        self.assertIn("/watch?v=vid00000002", url)
        self.assertIn("Saved ✓", page)
        page = self.get(a, "/later")[2]
        self.assertLess(page.index("Video 2"), page.index("Video 4"))  # newest save first
        self.assertEqual(post("/later/toggle", "v=vid00000004"), "0")  # again takes it off
        post("/seen", "v=vid00000002&n=0&t=595&d=600")                 # watched to the end: stays on the list...
        self.assertIn("Video 2", self.get(a, "/later")[2])
        self.assertIn("&start=0&", self.get(a, "/watch?v=vid00000002")[2])   # ...and starts over next time
        self.assertEqual(post("/later/toggle", "v=bad"), "0")
        # search shows matching channels, with a Subscribe button that comes back to the results
        page = self.get(a, "/search?q=retro")[2]
        self.assertIn(f'href="/channel?id={CID2}"><strong>Retro &amp; Co</strong>', page)
        self.assertIn("@retro · 12K subscribers", page)
        self.assertIn('name="next" value="/search?q=retro"', page)
        self.assertEqual(app.parse_channels({"x": [{"channelRenderer": {"channelId": "nope"}}]}), [])
        # not interested: a video, then its whole channel, leave For you; undo brings them back
        self.play(a, "srch0000001")
        self.get(a, "/related?v=srch0000001")
        page = self.get(a, "/foryou")[2]
        self.assertIn("Related 0", page)
        self.assertNotIn("data-no=", page)
        post("/hide", "v=rel00000000")
        page = self.get(a, "/foryou")[2]
        self.assertNotIn("Related 0", page)
        self.assertIn("Related 1", page)
        post("/hide", "c=" + CID2)
        self.assertNotIn("Related 1", self.get(a, "/foryou")[2])
        self.assertIn("Hidden from suggestions ✓", self.get(a, "/channel?id=" + CID2)[2])
        self.assertIn("Unhide", self.get(a, "/subs")[2])               # listed where you can take it back
        post("/hide", "undo=1&v=rel00000000&c=" + CID2)
        self.assertIn("Related 0", self.get(a, "/foryou")[2])
        self.assertIn("Hide from suggestions", self.get(a, "/channel?id=" + CID2)[2])
        # import (YouTube Takeout's file, or any list of links) and export
        b = self.login("Imp")
        real, app.refresh_stale = app.refresh_stale, lambda *a, **k: 0   # (the background refresh would rename them)
        self.addCleanup(setattr, app, "refresh_stale", real)
        C8, C9 = "UC" + "8" * 22, "UC" + "9" * 22
        text = (f"Channel Id,Channel Url,Channel Title\n{C8},http://www.youtube.com/channel/{C8},\"Eight, the <b>\"\n"
                f"https://www.youtube.com/channel/{C9}\n{C8}\nnot a channel UCshort\n")
        _, url, page = self.get(b, "/subs/import", {"text": text})
        self.assertIn("imported=2&of=2", url)
        self.assertIn("Added 2 new subscriptions", page)
        self.assertIn("Eight, the &lt;b&gt;", page)
        self.assertIn("imported=0&of=2", self.get(b, "/subs/import", {"text": text})[1])   # again: nothing new
        r = b.open(self.base + "/subs/export")
        out = r.read().decode()
        self.assertIn("text/csv", r.headers["Content-Type"])
        self.assertIn(f'{C8},http://www.youtube.com/channel/{C8},"Eight, the <b>"', out)
        c = self.login("Imp2")                                         # the export goes straight into another profile
        self.assertIn("imported=2&of=2", self.get(c, "/subs/import", {"text": out})[1])
        # saved videos survive the daily tidy-up
        post("/later/toggle", "v=tdykeepme01")
        db.execute("UPDATE videos SET published=1 WHERE id='tdykeepme01'")
        app.tidy(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM videos WHERE id='tdykeepme01'").fetchone()[0], 1)

    def test_7_for_you(self):
        a = self.login("FY")
        self.assertIn("Nothing here yet", self.get(a, "/foryou")[2])
        self.play(a, "srch0000001")
        self.get(a, "/related?v=srch0000001")                       # what the watch page's script does
        _, _, page = self.get(a, "/foryou")
        for i in range(3): self.assertIn(f"Related {i}", page)
        self.assertNotIn("watch?v=srch0000001", page)                 # never suggests what you've seen
        self.assertLess(page.index("Related 0"), page.index("Related 2"))   # higher in "up next" ranks higher
        self.play(a, "rel00000000")
        self.assertNotIn("Related 0", self.get(a, "/foryou")[2])
        # channel cap: 5 related from one channel -> only 3 shown
        db = app.connect()
        db.execute("INSERT OR IGNORE INTO videos(id,channel_id,title) VALUES('cap00000001',?,'Cap')", (CID2,))
        db.executemany("INSERT OR IGNORE INTO videos(id,channel_id,title) VALUES(?,?,?)",
                       [(f"cap0000000{i}", CID2, f"Cap {i}") for i in range(2, 7)])
        db.executemany("INSERT OR REPLACE INTO related(src,video_id,pos) VALUES('srch0000001',?,?)",
                       [(f"cap0000000{i}", 10 + i) for i in range(2, 7)])
        db.executemany("INSERT OR IGNORE INTO videos(id,channel_id,title) VALUES(?,?,?)",     # + 3 from other channels
                       [(f"oth0000000{i}", "UC" + str(i) * 22, f"Other {i}") for i in range(3)])
        db.executemany("INSERT OR REPLACE INTO related(src,video_id,pos) VALUES('srch0000001',?,?)",
                       [(f"oth0000000{i}", 30 + i) for i in range(3)])
        pid = db.execute("SELECT id FROM profiles WHERE name='FY'").fetchone()[0]
        chans = [r["channel_id"] for r, *_ in app.home_feed(db, pid)]
        self.assertEqual(chans[:2], [CID2, CID2])                     # the strongest channel: 2 near the top...
        self.assertTrue(all(ch != CID2 for ch in chans[2:5]))         # ...then the others before any more of it
        self.assertIn("Nothing here yet", self.get(self.login("Fresh"), "/foryou")[2])   # per profile

    def test_8_new_to_you(self):
        a = self.login("NTY")
        self.assertIn("Nothing new yet", self.get(a, "/new")[2])
        db = app.connect()
        C1, C2, C3 = "UC" + "c" * 22, "UC" + "d" * 22, "UC" + "e" * 22
        db.executemany("INSERT OR REPLACE INTO videos(id,channel_id,channel_title,title) VALUES(?,?,?,?)", [
            ("ntyseen0001", C1, "Known Chan", "Seen video"),
            ("ntyknown001", C1, "Known Chan", "More from known"),       # channel already watched -> excluded
            ("ntynew00001", C2, "Fresh Chan", "Fresh A"),
            ("ntynew00002", C2, "Fresh Chan", "Fresh B"),               # same new channel -> only one shown
            ("ntyhop20001", C3, "Far Chan", "Two hops away"),
            ("ntyshort001", "UC" + "f" * 22, "Shorty", "A short")])
        db.execute("UPDATE videos SET short=1 WHERE id='ntyshort001'")
        db.executemany("INSERT OR REPLACE INTO related(src,video_id,pos) VALUES(?,?,?)", [
            ("ntyseen0001", "ntyknown001", 0), ("ntyseen0001", "ntynew00001", 1),
            ("ntyseen0001", "ntynew00002", 2), ("ntyseen0001", "ntyshort001", 3),
            ("ntynew00001", "ntyhop20001", 0)])
        db.executemany("INSERT OR REPLACE INTO related_src(src,fetched_at) VALUES(?,?)",
                       [("ntyseen0001", int(time.time())), ("ntynew00001", int(time.time()))])
        self.play(a, "ntyseen0001")
        _, _, page = self.get(a, "/new")
        self.assertIn("Fresh A", page)
        self.assertNotIn("Fresh B", page)                             # one per channel
        self.assertIn("Two hops away", page)                          # reached through a suggestion
        self.assertNotIn("More from known", page)                     # not new: you've watched that channel
        self.assertNotIn("A short", page)
        self.assertLess(page.index("Fresh A"), page.index("Two hops away"))
        # shown and passed over for 9 days -> that channel sinks below fresher picks
        pid = db.execute("SELECT id FROM profiles WHERE name='NTY'").fetchone()[0]
        self.assertEqual(db.execute("SELECT COUNT(*) FROM shown WHERE profile_id=?", (pid,)).fetchone()[0], 2)
        db.execute("UPDATE shown SET first_at=? WHERE video_id='ntynew00001'", (int(time.time()) - 9 * 86400,))
        _, _, page = self.get(a, "/new")
        self.assertLess(page.index("Two hops away"), page.index("Fresh "))
        # the scheduled look-around fetches lists nobody opened: the watch's own, then the picks' (one step out)
        db.execute("DELETE FROM related_src WHERE src IN ('ntyseen0001','ntyhop20001')")
        calls = []
        real = app.fetch_related
        app.fetch_related = lambda c, vid, max_age=86400: calls.append(vid)
        try:
            self.assertEqual(app.discover(db, pid), 2)
        finally:
            app.fetch_related = real
        self.assertEqual(calls, ["ntyseen0001", "ntyhop20001"])
        db.execute("INSERT INTO shown(profile_id,video_id,first_at) VALUES(?, 'ntyold00001', ?)", (pid, int(time.time()) - 40 * 86400))
        app.tidy(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM shown WHERE video_id='ntyold00001'").fetchone()[0], 0)
        self.assertIn('href="/new" class="on"', page)
        self.get(a, "/subs/add", {"url": C2})                         # subscribing makes it no longer new
        self.assertNotIn("Fresh A", self.get(a, "/new")[2])

    def test_6_parsers2(self):
        now = 1_000_000_000
        for t, d in (("2d ago", 2 * 86400), ("3 weeks ago", 3 * 604800), ("Streamed 1 month ago", 2592000),
                     ("8mo ago", 8 * 2592000), ("13y ago", 13 * 31536000), ("5 minutes ago", 300)):
            self.assertEqual(app.rel_ts(t, now), now - d, t)
        self.assertEqual(app.rel_ts("Premieres soon"), 0)
        for t, n in (("7.4M", 7_400_000), ("3,582,218 views", 3_582_218), ("182K", 182_000), ("", 0)):
            self.assertEqual(app.num(t), n)

    def test_4_speed(self):
        a = self.login("Speed")
        self.get(a, "/subs/add", {"url": "@woz"})
        t = time.perf_counter()
        for _ in range(50): self.get(a, "/feed")
        ms = (time.perf_counter() - t) / 50 * 1000
        print(f"\n  feed page avg: {ms:.1f} ms")
        self.assertLess(ms, 50)

    def test_9_tv_player(self):
        a = self.login("TVtest")
        _, _, page = self.get(a, "/watch?v=vid00000002")
        self.assertIn('<iframe id="yt" class="player"', page)          # normal browsers: YouTube's controls
        self.assertIn('setOption",["captions","track",{}]', page)     # captions start off (and stay off after seeking)
        self.assertIn('},1)})()', page)                                 # Feed: no muted autoplay fallback
        self.assertIn('yakTrack("vid00000002")', page)                  # reports when it has really played
        self.assertNotIn('Tap for sound', page)
        self.assertNotIn('id="pp"', page)
        a.addheaders = [("User-Agent", "Mozilla/5.0 YakTubeTV/1")]
        _, _, page = self.get(a, "/watch?v=vid00000002")
        self.assertIn('id="pp"', page)                                  # TV app: our own buttons
        self.assertIn('<div id="sk" tabindex="0" role="slider"', page)  # a progress bar you can scrub with the remote
        tvbar = page[page.index('<div class="tvbar">'):page.index('<div class="tvtop">')]
        for b in ('data-rate="1"', 'data-rate="-1"', 'Watch later', 'action="/subs/'):   # thumbs, subscribe, later
            self.assertIn(b, tvbar)
        self.assertIn('<div class="tvtop"><b>Video 2 &lt;b&gt;</b>', page)   # the title over the picture
        self.assertNotIn("__ACTS__", page)
        self.assertNotIn("__CHAN__", page)
        panel = tvbar[tvbar.index('<div id="cp" class="tvcp"'):]                 # the channel's circle opens a panel
        self.assertIn('<button id="chb" aria-haspopup="dialog"', tvbar)
        for b in ('action="/subs/', '>Go to channel</a>', "Don't recommend channel"):
            self.assertIn(b, panel)
        menu = page[page.index('<div id="gm" class="tvgm" role="menu" hidden>'):page.index('<div class="tvtop">')]   # ⚙ Settings
        for b in ('id="cc">Captions off', 'id="sp">Speed 1×', 'id="ql">Quality Auto'):
            self.assertIn(b, menu)
        self.assertNotIn('id="cc"', tvbar[:tvbar.index('id="gs"')])     # not on the bar itself any more
        self.assertNotIn('id="ap"', page)                                # Autoplay moved to Settings
        self.assertIn('embed/"+V', page)
        self.assertIn("vid00000002", page)
        self.assertIn(".tvbar", page)                                   # TV styles included
        self.assertIn('classList.add("hide")', page)                   # buttons fade out while playing
        self.assertIn('id="ql">Quality Auto', page)                    # quality switch + what YouTube is playing
        self.assertIn("localStorage.yakq", page)
        self.assertIn("playbackQuality", page)
        self.assertNotIn('<iframe id="yt"', page)

    def test_10_tidy(self):
        c = app.connect()
        now = int(time.time()); day = 86400
        c.execute("INSERT INTO profiles(name,color) VALUES('Tidy','#000')")
        pid = c.execute("SELECT id FROM profiles WHERE name='Tidy'").fetchone()[0]
        c.execute("INSERT OR IGNORE INTO channels(id,title) VALUES('UCtidysub00000000000000a','Sub')")
        c.execute("INSERT INTO subs(profile_id,channel_id) VALUES(?, 'UCtidysub00000000000000a')", (pid,))
        rows = [("tdyoldfree1", "UCother", now - 90 * day),   # old, unwatched, not subscribed -> goes
                ("tdynewfree1", "UCother", now - 5 * day),    # recent -> stays
                ("tdywatched1", "UCother", now - 900 * day),  # watched -> stays forever
                ("tdysubold01", "UCtidysub00000000000000a", now - 90 * day),   # subscription, < 1 year -> stays
                ("tdysubanc01", "UCtidysub00000000000000a", now - 400 * day),  # subscription, > 1 year -> goes
                ("tdyinlist01", "UCother", now - 90 * day),   # in a fresh Up next list -> stays
                ("tdyinstale1", "UCother", now - 90 * day)]   # only in a stale list -> goes
        for vid, ch, pub in rows:
            c.execute("INSERT OR REPLACE INTO videos(id,channel_id,title,published) VALUES(?,?,?,?)", (vid, ch, vid, pub))
        c.execute("INSERT INTO history(profile_id,video_id,watched_at) VALUES(?, 'tdywatched1', ?)", (pid, now - 900 * day))
        c.execute("INSERT OR REPLACE INTO related_src VALUES('tdywatched1', ?)", (now - day,))
        c.execute("INSERT OR REPLACE INTO related VALUES('tdywatched1','tdyinlist01',0)")
        c.execute("INSERT OR REPLACE INTO related_src VALUES('tdystalesrc', ?)", (now - 40 * day,))
        c.execute("INSERT OR REPLACE INTO related VALUES('tdystalesrc','tdyinstale1',0)")
        app.tidy(c, now)
        left = {r[0] for r in c.execute("SELECT id FROM videos WHERE id LIKE 'tdy%'")}
        self.assertEqual(left, {"tdynewfree1", "tdywatched1", "tdysubold01", "tdyinlist01"})
        self.assertIsNone(c.execute("SELECT 1 FROM related_src WHERE src='tdystalesrc'").fetchone())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM subs WHERE profile_id=?", (pid,)).fetchone()[0], 1)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM history WHERE profile_id=?", (pid,)).fetchone()[0], 1)
        c.close()

    def test_11_shorts(self):
        a = self.login("Shorty")
        self.get(a, "/subs/add", {"url": "@woz"})
        c = app.connect()                                                               # Shorts from a subscribed channel
        app.store_videos(c, [{"id": v, "title": t, "channel_id": CID, "published": int(time.time()), "short": 1}
                             for v, t in (("shr00000001", "Newest short"), ("shr00000002", "Old </script> short"))])
        c.close()
        self.assertEqual(re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2]), [])  # the tab is outside Shorts only
        self.assertIn("No Shorts right now", self.get(a, "/shorts")[2])
        app.OUTSIDE[:] = ["shr00000001", "vid00000003", "shr00000002"]
        s, url, page = self.get(a, "/shorts")
        ids = re.findall(r'"id": "(\w{11})"', page)
        self.assertEqual(ids, ["shr00000001", "vid00000003", "shr00000002"])            # in the order YouTube gave
        self.assertNotIn("Old </script>", page)                                         # can't break out of the script
        self.assertIn('id="x"', page)                                                   # close button
        self.assertIn('if(d=="Enter"||d==" ")return;', page)            # remote: OK on a button presses it (not pause)
        self.assertIn('g=a.closest("#rt,#ud")', page)                   # Up/Down move between side buttons, not Shorts
        r = a.open(self.base + "/seen", b"v=shr00000001")
        self.assertEqual(r.status, 204)
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2])
        self.assertEqual(ids, ["vid00000003", "shr00000002"])                           # watched ones are gone
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts?v=shr00000002")[2])
        self.assertEqual(ids[0], "shr00000002")                                         # a chosen one goes first
        # thumbs steer the list by channel name: liked channels move up, thumbed-down ones leave
        c = app.connect()
        out = [f"thb{i:08d}" for i in range(40)]
        app.store_videos(c, [{"id": v, "title": f"Thumb {i}", "channel_title": "Cats" if i in (5, 35) else
                              "Dogs" if i in (6, 36) else f"Chan {i}", "published": int(time.time()), "short": 1}
                             for i, v in enumerate(out)])
        c.close()
        app.OUTSIDE[:] = out
        post = lambda b: a.open(self.base + "/rate", b.encode()).read().decode()
        self.assertEqual(post("v=thb00000005&r=1"), "1")                                # like a Cats short...
        self.assertEqual(post("v=thb00000006&r=-1"), "-1")                              # ...thumb down a Dogs one
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2])
        self.assertNotIn("thb00000006", ids)
        self.assertNotIn("thb00000036", ids)                                            # all of Dogs is gone
        self.assertLess(ids.index("thb00000035"), ids.index("thb00000010"))             # Cats' other short moved up
        self.assertLess(ids.index("thb00000000"), ids.index("thb00000035"))             # ...but not to the very top
        post("v=thb00000006&r=-1")                                                      # taking the thumb back...
        self.assertIn("thb00000036", re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2]))   # ...brings Dogs back
        c = app.connect()
        pid = c.execute("SELECT id FROM profiles WHERE name='Shorty'").fetchone()[0]
        c.execute("UPDATE videos SET channel_id=? WHERE id='thb00000005'", (CID,))
        app.mark_watched(c, pid, "thb00000005")                                         # watched AND rated: still counts
        self.assertGreater(app.Taste(c, pid, time.time()).chan.get(CID, 0), 0)
        c.execute("DELETE FROM videos WHERE id LIKE 'thb%'")                            # tidy up for later tests
        c.execute("DELETE FROM ratings WHERE video_id LIKE 'thb%'")
        c.execute("DELETE FROM history WHERE video_id LIKE 'thb%'")
        c.commit()
        c.close()
        s, url, _ = self.get(a, "/go?q=" + urllib.parse.quote("https://youtube.com/shorts/shr00000002"))
        self.assertTrue(url.endswith("/shorts?v=shr00000002"))
        app.OUTSIDE[:] = []
        self.assertIn('href="/shorts?v=shr00000002"', self.get(a, "/feed")[2])             # subs' Shorts: in the feed (toggle on)
        self.get(a, "/feed/shorts", {})
        self.assertNotIn("shr0000000", self.get(a, "/feed")[2])                            # toggle off: not in the feed
        self.assertIn('href="/shorts"', self.get(a, "/feed")[2])                           # the tab

    def test_12_icons(self):
        op = self.client()                                                              # no profile needed
        for path, n in (("/favicon.png", 64), ("/apple-touch-icon.png", 180), ("/icon-192.png", 192)):
            r = op.open(self.base + path)
            data = r.read()
            self.assertEqual(r.headers["Content-Type"], "image/png")
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(app.struct.unpack(">II", data[16:24]), (n, n))
        import json
        m = json.loads(op.open(self.base + "/manifest.json").read())
        self.assertEqual(m["display"], "standalone")
        self.assertIn('rel="apple-touch-icon"', self.get(op, "/profiles")[2])

    def test_13_outside_shorts(self):
        a = self.login("Mixer")
        self.get(a, "/subs/add", {"url": "@woz"})
        sent = []
        def fetch(url, timeout=10):
            if url == "https://www.youtube.com/shorts/":
                return page({"e": {"reelWatchEndpoint": {"videoId": "out00000001"}}}, '"INNERTUBE_CLIENT_VERSION":"2.1"')
            if "oembed" in url and "out00000003" in url: raise IOError("HTTP Error 401")   # can't be embedded
            return fake_fetch(url, timeout)
        def post(url, body, timeout=10):
            sent.append(body)
            return {"entries": [{"command": {"reelWatchEndpoint": {"videoId": v}}}
                                for v in ("out00000002", "out00000001", "out00000003", "shr00000001")]}
        real_post, app.fetch, app.post_json = app.post_json, fetch, post
        try:
            app.OUTSIDE_AT[0] = 0
            app.refresh_outside()
        finally:
            app.fetch, app.post_json = fake_fetch, real_post
        self.assertEqual(sent[0]["sequenceParams"], app.base64.urlsafe_b64encode(b"\n\x0bout00000001").decode())
        self.assertEqual(sent[0]["context"]["client"]["clientVersion"], "2.1")
        self.assertEqual(app.OUTSIDE, ["out00000001", "out00000002", "shr00000001"])      # unembeddable one dropped
        c = app.connect()
        self.assertEqual(c.execute("SELECT short FROM videos WHERE id='out00000001'").fetchone()[0], 1)  # never steers For you
        c.close()
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2])
        self.assertEqual(ids, ["out00000001", "out00000002", "shr00000001"])             # only outside Shorts
        a.open(self.base + "/seen", b"v=out00000001")
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2])
        self.assertNotIn("out00000001", ids)                                             # watched outside ones don't come back
        # the list survives a restart
        c = app.connect()
        self.assertEqual([r[0] for r in c.execute("SELECT video_id FROM outside ORDER BY pos")], app.OUTSIDE)
        c.close()
        saved, app.OUTSIDE[:] = app.OUTSIDE[:], []
        app.load_outside()
        self.assertEqual(app.OUTSIDE, saved)
        # near the end of the list the player asks for more: a new batch, minus what it already has
        sent.clear()
        def post2(url, body, timeout=10):
            sent.append(body)
            return {"entries": [{"command": {"reelWatchEndpoint": {"videoId": v}}} for v in ("out00000004", "out00000002")]}
        app.fetch, app.post_json = fetch, post2
        try:
            app.OUTSIDE_AT[0] = 0
            more = lambda have: json.loads(a.open(self.base + "/shorts/more", ("have=" + have).encode()).read())
            got = more("out00000002")
            self.assertEqual([r["id"] for r in got], ["out00000004", "shr00000001"])
            self.assertEqual(got[0]["r"], 0)                                             # with the thumb buttons' state
            n = len(sent)
            self.assertEqual(more("out00000004,shr00000001,out00000002"), [])           # nothing new...
            self.assertEqual(len(sent), n)                                               # ...and YouTube isn't asked again so soon
        finally:
            app.fetch, app.post_json = fake_fetch, real_post
        player = self.get(a, "/shorts")[2]
        self.assertIn('fetch("/shorts/more"', player)
        self.assertIn("if(L.length-i<=8)more()", player)
        app.OUTSIDE[:] = []

    def test_21_profile_edit(self):
        op = self.login("Edy")
        self.login("Edy2")
        db = app.connect()
        pid = db.execute("SELECT id FROM profiles WHERE name='Edy'").fetchone()[0]
        manage = self.get(op, "/profiles?manage=1")[2]
        self.assertIn('<a class="pback" href="/profiles">← Back</a>', manage)        # Back: to the profiles...
        op.addheaders = [("Referer", self.base + "/later")]                          # ...or to where you came from,
        self.assertIn('<a class="pback" href="/later">← Back</a>', self.get(op, "/profiles")[2])
        op.addheaders = [("Referer", self.base + "/profiles?manage=1")]               # remembered past Manage / Done
        self.assertIn('<a class="pback" href="/later">← Back</a>', self.get(op, "/profiles")[2])
        op.addheaders = []
        self.assertIn(f'href="/profiles/edit?id={pid}"', manage)                   # tiles open the editor...
        self.assertNotIn('action="/profiles/delete"', manage)                       # ...and never delete straight away
        page = self.get(op, f"/profiles/edit?id={pid}")[2]
        self.assertIn('data-a="🦊"', page)
        self.assertIn('data-c="#495057"', page)
        # save: new name, colour and picture; a bad colour or picture is ignored
        self.get(op, "/profiles/save", {"id": pid, "name": " Eddie ", "color": "#495057", "avatar": "🦊"})
        r = db.execute("SELECT name, color, avatar FROM profiles WHERE id=?", (pid,)).fetchone()
        self.assertEqual(tuple(r), ("Eddie", "#495057", "🦊"))
        self.get(op, "/profiles/save", {"id": pid, "name": "Eddie", "color": "red;x", "avatar": "<b>"})
        self.assertEqual(tuple(db.execute("SELECT color, avatar FROM profiles WHERE id=?", (pid,)).fetchone()), ("#495057", ""))
        self.get(op, "/profiles/save", {"id": pid, "name": "Eddie", "color": "#495057", "avatar": "🦊"})
        self.assertIn('<i class="emo">🦊</i>', self.get(op, "/later")[2])          # the picture shows in the header
        # a name another profile has is refused
        _, url, page = self.get(op, "/profiles/save", {"id": pid, "name": "Edy2", "color": "#495057"})
        self.assertIn("err=taken", url)
        self.assertIn("already has that name", page)
        self.assertEqual(db.execute("SELECT name FROM profiles WHERE id=?", (pid,)).fetchone()[0], "Eddie")
        # moving: swap with its neighbour, and keep the focus on the arrow pressed
        order = lambda: [r[0] for r in db.execute("SELECT id FROM profiles ORDER BY pos, id")]
        before = order()
        i = before.index(pid)
        _, url, page = self.get(op, "/profiles/move", {"id": pid, "d": "-1"})
        self.assertEqual(order().index(pid), i - 1)
        self.assertIn(f"moved={pid}&d=-1", url)
        self.assertIn('title="Move left" autofocus', page)
        self.get(op, "/profiles/move", {"id": pid, "d": "1"})
        self.assertEqual(order(), before)
        self.get(op, "/profiles/move", {"id": order()[0], "d": "-1"})              # the first can't go further left
        self.assertEqual(order(), before)
        # removing asks first, with "Keep it" as the safe choice
        page = self.get(op, f"/profiles/remove?id={pid}")[2]
        self.assertIn("Remove Eddie?", page)
        self.assertIn("autofocus>Keep it", page)
        self.get(op, "/profiles/delete", {"id": pid})
        self.assertIsNone(db.execute("SELECT id FROM profiles WHERE id=?", (pid,)).fetchone())
        db.close()
        subs = self.get(self.login("Zippy"), "/subs")[2]                          # a Takeout .zip can be chosen as is
        self.assertIn('accept=".zip,', subs)
        self.assertIn('DecompressionStream("deflate-raw")', subs)

    def test_22_settings(self):
        op = self.login("Seth")
        db = app.connect()
        pid = db.execute("SELECT id FROM profiles WHERE name='Seth'").fetchone()[0]
        post = lambda path, body: op.open(self.base + path, body.encode())
        page = self.get(op, "/settings")[2]
        self.assertIn('href="/settings"', page)                                        # in the side panel
        for k in ("lang", "loc", "hidden", "watch", "search"):
            self.assertIn(f'data-p="{k}"', page)
        self.assertNotIn('data-p="addr"', page)                                        # this-TV settings only on a TV
        for h in ("Language &amp; Location", "History &amp; Data", "About"):   # grouped like YouTube's
            self.assertIn(f"<h4>{h}</h4>", page)
        self.assertIn('data-p="about"', page)                                         # App version, last
        self.assertIn(app.VERSION, page)
        self.assertRegex(app.VERSION, r"^\d{4}\.\d\d\.\d\d · [0-9a-f]{7}$")
        self.assertNotIn("YakTube TV app", page)                                       # (no TV app in a browser)
        # region and language: for the whole of YakTube, sent with every request to YouTube
        try:
            self.assertEqual(post("/settings/region", "k=gl&v=MX").status, 204)
            self.assertEqual(post("/settings/region", "k=hl&v=es").status, 204)
            with self.assertRaises(urllib.error.HTTPError):
                post("/settings/region", "k=gl&v=XX")
            self.assertEqual(app.REGION, {"gl": "MX", "hl": "es"})
            self.assertIn("PREF=hl=es&gl=MX", app.yt_cookie())
            page = self.get(op, "/settings")[2]
            self.assertIn("Location: <span>Mexico</span>", page)
            self.assertIn("Language: <span>Español</span>", page)
            op.addheaders = [("User-Agent", "Mozilla/5.0 YakTubeTV/1")]
            self.assertIn('languageCode:"es"', self.get(op, "/watch?v=vid00000002")[2])   # captions in Spanish
            app.REGION.update(gl="US", hl="en")
            app.init_db()                                                             # saved: back after a restart
            self.assertEqual(app.REGION, {"gl": "MX", "hl": "es"})
            tv = self.get(op, "/settings")[2]
            self.assertIn('data-p="addr"', tv)                                         # on the TV: server address,
            self.assertIn('href="yaktube:setup"', tv)
            self.assertIn('data-p="ap"', tv)                                           # and autoplay
            op.addheaders = []
            self.assertIn("1.6 or earlier", tv)                                        # app version (old apps say "1")
            op.addheaders = [("User-Agent", "Mozilla/5.0 (Linux; Android 12; Chromecast Build/STTE.230; wv) AppleWebKit/537.36 YakTubeTV/1.9")]
            about = self.get(op, "/settings")[2]
            self.assertIn("<span>TV app</span><b>1.9</b>", about)                     # like YouTube's: facts, no sentences
            self.assertIn("<span>Device</span><b>Chromecast, Android 12</b>", about)
            self.assertIn("<span>Region</span><b>MX</b>", about)
            self.assertIn("<span>Hosting</span><b>Self-hosted</b>", about)
            op.addheaders = []
            br = self.get(op, "/settings?tv=1")[2]                                    # the TV layout in a browser:
            self.assertIn('data-p="ap"', br)                                           # autoplay, but no server address
            self.assertNotIn('data-p="addr"', br)                                      # (only the TV app can change it)
            self.get(op, "/settings?tv=0")
        finally:
            db.execute("DELETE FROM settings")
            app.REGION.update(gl="US", hl="en")
        # hidden channels, listed with Recommend again
        db.execute("INSERT INTO hidden(profile_id,kind,ref,added_at) VALUES(?,?,?,1)", (pid, "c", CID2))
        page = self.get(op, "/settings")[2]
        self.assertIn(f'data-c="{CID2}">Recommend again', page)
        self.assertIn("Retro &amp; Co", page)
        # clearing: watch history and search history, for this profile only
        self.play(op, "vid00000001")
        db.execute("INSERT INTO searches(profile_id,q,at) VALUES(?,?,1)", (pid, "retro games"))
        page = self.get(op, "/settings")[2]
        self.assertIn("Removes Seth's 1 watched video.", page)                     # one short line each
        self.assertIn("Removes Seth's 1 search.", page)
        self.assertIn("This can't be undone", page)                                # Clear asks first
        _, url, _ = self.get(op, "/settings/clear", {"what": "watch"})
        self.assertTrue(url.endswith("/settings#watch"))                              # back to that setting
        self.assertEqual(db.execute("SELECT COUNT(*) FROM history WHERE profile_id=?", (pid,)).fetchone()[0], 0)
        self.get(op, "/settings/clear", {"what": "search"})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM searches WHERE profile_id=?", (pid,)).fetchone()[0], 0)
        page = self.get(op, "/settings")[2]
        self.assertIn("Seth's History is empty.", page)
        # the History page asks first too: Clear all, and ✕ on a video needs a second press
        self.play(op, "vid00000001")
        hist = self.get(op, "/history")[2]
        self.assertIn('data-ask>Clear all</button>', hist)
        self.assertIn("Clear all 1 video from History? This can't be undone.", hist)
        self.assertIn('action="/history/remove" data-sure="Remove"', hist)
        self.assertIn('f.dataset.sure', hist)
        self.assertIn("No searches remembered.", page)
        db.close()

class ScoutTests(unittest.TestCase):
    """Scout: spotting content-farm Shorts from what they say about themselves (YouTube mocked)."""
    STORY = ("In this world, everyone lives in tiny cabins on one giant wall underground, and nobody knows what's on the "
             "surface. You see, the wall is miles long and miles tall, inside a cave so big you can't see the other side. ")

    def setUp(self):
        app.init_db()
        c = app.connect()
        c.execute("DELETE FROM scout"); c.execute("DELETE FROM videos"); c.execute("DELETE FROM outside")
        c.execute("DELETE FROM settings WHERE k LIKE 'scout_%'"); c.execute("DELETE FROM scout_chan")
        c.close()
        self.keep = (dict(app.SCOUT_CFG), app.post_json, app.fetch, app.OUTSIDE[:])
        app.SCOUT_CFG.update(text="1", on="0", addr="", port="8095", seen="0", min="", days="60")
        app.SCOUT_LINK.update(state="", error="", voices=[], version="", up=0, secs=0.0, last=0)

    def tearDown(self):
        app.post_json, app.fetch = self.keep[1:3]
        app.SCOUT_CFG.clear(); app.SCOUT_CFG.update(self.keep[0])
        app.OUTSIDE[:] = self.keep[3]

    def test_score(self):
        long_story = self.STORY * 8                                       # ~330 words
        score, why = app.scout_score("Everyone Lives In Tiny Cabins", "", long_story, 75)
        self.assertGreaterEqual(score, app.SCOUT_MIN)                     # world-rule opening + non-stop fast narration
        self.assertIn("world-rule opening", why)
        self.assertEqual(app.scout_score("Fixing my sink", "", "okay so first you turn the water off. " * 8, 60)[0], 0)
        self.assertLess(app.scout_score("Cats", "cute #aicat #cat", "", 20)[0], app.SCOUT_MIN)      # made with AI isn't a farm
        self.assertEqual(app.scout_score("My cat as a knight", "Made with Sora #aiart", "", 20), (10, ["made with AI"]))
        self.assertGreaterEqual(app.scout_score("Funny", "Welcome to our Cats channel. Educational & Entertainment Notice", "", 20)[0], app.SCOUT_MIN)
        self.assertLess(app.scout_score("GPT-5 reviewed #ai #aitools", "", "", 30)[0], app.SCOUT_MIN)   # talking about AI
        self.assertLess(app.scout_score("POV: you what if part 2 #pov #storytime", "", "", 30)[0], app.SCOUT_MIN)  # clickbait alone isn't enough
        self.assertLess(app.scout_score("x", "", "In this world where " + "word " * 40, 30)[0], app.SCOUT_MIN)    # one signal isn't enough
        self.assertEqual(app.scout_score("", "", "In a world where " + "hello " * 4, 5)[0], 0)             # too short to read
        melo = "the forbidden outcast whispered about destiny. " * 6
        self.assertIn("melodrama", " ".join(app.scout_score("", "", melo, 40)[1]))

    def test_text(self):
        xml = ('<timedtext format="3"><body><p t="0" d="1500"><s>Hello</s><s t="400"> there</s></p>'
               '<p t="1500" d="1000" a="1">\n</p><p t="2500" d="2000">[music] and bye</p></body></timedtext>')
        self.assertEqual(app.scout_text(xml), ("Hello there and bye", 4))

    def _wire(self, details, calls):
        cap = lambda url: '<timedtext><body><p t="0" d="60000">' + (self.STORY * 8 if "st0000" in url else "okay first turn the water off. " * 8) + '</p></body></timedtext>'
        def post(url, body, timeout=10):
            vid = body["videoId"]; calls.append(vid)
            if vid == "err00000000": raise IOError("offline")
            if vid not in details: return {}
            d = details[vid]
            return {"videoDetails": {"title": d[0], "shortDescription": d[1], "lengthSeconds": "75"},
                    "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
                        {"languageCode": "en", "kind": "asr", "baseUrl": "https://www.youtube.com/api/timedtext?v=" + vid}]}}}
        app.post_json = post
        app.fetch = lambda url, timeout=10: cap(url) if "timedtext" in url else fake_fetch(url, timeout)

    def test_outside_drops_ai(self):
        details = {"ok000000000": ("Fixing a sink", ""), "ai000000000": ("Cats", "so cute #aicat"),
                   "st000000000": ("Cabins", "")}
        calls = []
        self._wire(details, calls)
        app.SCOUT_CFG["text"] = "1"
        c = app.connect()
        c.executemany("INSERT INTO videos(id,title,channel_title,short) VALUES(?,?,?,1)", [
            ("ok000000000", "Fixing a sink", "Handy"), ("ai000000000", "Cats", "Meow"), ("st000000000", "Cabins", "Tales"),
            ("err00000000", "?", "Net"), ("nodata000000", "?", "Gone")])
        app.OUTSIDE[:] = ["ok000000000", "ai000000000", "st000000000", "err00000000", "nodata000000"]
        app.scout_outside(c)
        # the farm story is gone; the AI-tagged cat isn't a farm's, so it stays; so do the one that can't be judged
        # (offline) and the one YouTube knows nothing of
        self.assertEqual(app.OUTSIDE, ["ok000000000", "ai000000000", "err00000000", "nodata000000"])
        got = {r[0]: r[1] for r in c.execute("SELECT video_id, farm FROM scout")}
        self.assertEqual(got, {"ok000000000": 0, "ai000000000": 0, "st000000000": 1, "nodata000000": 0})   # err: not remembered
        # next batch: only what hasn't been judged is asked about again
        calls.clear()
        app.OUTSIDE[:] = ["ok000000000", "ai000000000", "st000000000", "err00000000"]
        app.scout_outside(c)
        self.assertEqual(calls, ["err00000000"])
        self.assertEqual(app.OUTSIDE, ["ok000000000", "ai000000000", "err00000000"])
        c.close()

    def test_scan_during_text_check(self):
        """The Scout plugin answers while the text check is still running: the two add up, neither is lost."""
        keep = app.scout_check
        app.SCOUT_CFG["text"] = "1"
        c = app.connect()
        c.execute("INSERT INTO videos(id,title,channel_title,short) VALUES('race0000000','Tale','Voxly',1)")

        def text_check(vid):
            c2 = app.connect()
            app.scout_verdict(c2, vid, 35, ["same voice as x"])      # Scout gets there first
            c2.commit(); c2.close()
            return {"score": 20, "farm": 0, "why": ["non-stop fast narration"]}
        try:
            app.scout_check = text_check
            app.OUTSIDE[:] = ["race0000000"]
            app.scout_outside(c)
        finally:
            app.scout_check = keep
        row = c.execute("SELECT score, farm, deep, why FROM scout WHERE video_id='race0000000'").fetchone()
        self.assertEqual(tuple(row), (55, 1, 1, "non-stop fast narration; same voice as x"))
        self.assertEqual(app.OUTSIDE, [])
        self.assertEqual(app.scout_queue(c), [])                       # not handed to Scout a second time
        c.close()

    def _fake_scout(self, looks):
        """A Scout plugin on this machine: answers /status, and /look from `looks` (id -> (code, body))."""
        seen = []
        class H(app.BaseHTTPRequestHandler):
            def answer(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(b)
            def do_GET(self):
                if self.path == "/voices":
                    return self.answer(200, {"voices": [{"name": "v1", "channels": ["A", "B"], "print": [0.0] * 512}]})
                if self.path.startswith("/share"):
                    return self.answer(200, {"id": "tbqta6u4lem", "model": "m", "channels": ["Drip Dyl"], "print": [0.0] * 512})
                self.answer(200, {"scout": "0.2", "busy": False, "voices": [{"name": "tbQTA6u4lEM", "channels": 4, "learned": "Nov 13"}]})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/learn":
                    seen.append("learn " + body["id"])
                    return self.answer(200, {"name": "tbQTA6u4lEM", "channels": ["Drip Dyl", "Volze"], "new": False})
                if self.path == "/voices":
                    seen.append(f"restore {len(body['voices'])}")
                    return self.answer(200, {"added": len(body["voices"]), "voices": 2})
                if self.path == "/forget":
                    seen.append("forget " + body["name"])
                    return self.answer(200, {"forgot": True})
                vid = body["id"]
                seen.append(vid)
                self.answer(*looks.get(vid, (200, {"score": 0, "why": []})))
            def log_message(self, *a): pass
        srv = app.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_port, seen

    def test_scout_computer(self):
        """YakTube connects to the Scout plugin, hands it Shorts one at a time and adds up what it says."""
        c = app.connect()
        c.executemany("INSERT INTO videos(id,title,channel_title,short) VALUES(?,?,?,1)",
                      [("qa000000000", "Cabins", "Tales"), ("qb000000000", "Sink", "Handy"), ("qc000000000", "Gone", "X")])
        c.execute("INSERT INTO scout(video_id,score,farm,why,at) VALUES('qa000000000',30,0,'world-rule opening',0)")
        app.OUTSIDE[:] = ["qa000000000", "qb000000000", "qc000000000"]
        c.executemany("INSERT INTO outside VALUES(?,?)", enumerate(app.OUTSIDE))
        port, seen = self._fake_scout({"qa000000000": (200, {"score": 35, "why": ["same voice as x"]}),
                                       "qb000000000": (200, {"score": 15, "why": ["word-by-word captions"]}),
                                       "qc000000000": (502, {"error": "Video unavailable"})})
        self.assertEqual(app.scout_link_once(), 30)                                     # off until it's set up
        self.assertEqual(app.SCOUT_LINK["state"], "off")
        app.SCOUT_CFG.update(on="1", addr="127.0.0.1", port=str(port))
        for _ in range(5):
            app.scout_link_once()
        self.assertEqual(app.SCOUT_LINK["state"], "ok")
        self.assertEqual(app.SCOUT_LINK["voices"][0]["name"], "tbQTA6u4lEM")
        self.assertEqual(seen, ["qa000000000", "qb000000000", "qc000000000", "qc000000000"])   # one at a time; 2 tries
        self.assertEqual(app.OUTSIDE, ["qb000000000", "qc000000000"])                   # 30 + 35: the farm is gone
        self.assertNotIn("qa000000000", [r[0] for r in c.execute("SELECT video_id FROM outside")])
        self.assertEqual(c.execute("SELECT score, farm, why, deep FROM scout WHERE video_id='qa000000000'").fetchone()[:],
                         (65, 1, "world-rule opening; same voice as x", 1))
        self.assertEqual(c.execute("SELECT why FROM scout WHERE video_id='qc000000000'").fetchone()[0], "couldn't scan")
        self.assertEqual(app.scout_queue(c), [])                                         # all watched
        self.assertGreater(int(app.SCOUT_CFG["seen"]), 0)
        app.SCOUT_CFG["port"] = "1"                                                      # nothing there
        self.assertEqual(app.scout_link_once(), 60)
        self.assertEqual(app.SCOUT_LINK["state"], "down")
        c.close()

    def test_settings_and_bring_back(self):
        c = app.connect()
        pid = c.execute("INSERT INTO profiles(name,color) VALUES('Scouter','#123456')").lastrowid
        c.execute("INSERT INTO videos(id,title,channel_title,short) VALUES('qd000000000','Farm story','Tales',1)")
        c.execute("INSERT INTO scout(video_id,score,farm,why,at,deep) VALUES('qd000000000',65,1,'world-rule opening',1,1)")
        srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"
        def post(path, **form):
            req = urllib.request.Request(base + path, urllib.parse.urlencode(form).encode(), {"Cookie": f"yak_profile={pid}"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as ex:
                return ex.code, ex.read().decode()
        page = urllib.request.urlopen(urllib.request.Request(base + "/settings", headers={"Cookie": f"yak_profile={pid}"})).read().decode()
        self.assertIn('data-p="scout"', page)                                            # Plugins > Scout
        self.assertIn("Not set up", page)
        self.assertIn('data-keep="qd000000000"', page)                                   # in Dropped, with Bring back
        code, body = post("/settings/scout", k="addr", v="http://192.168.1.31:9000/", port="")
        self.assertEqual((code, json.loads(body)), (200, {"addr": "192.168.1.31", "port": "9000", "on": "1"}))
        self.assertEqual(dict(c.execute("SELECT k, v FROM settings WHERE k IN ('scout_addr','scout_port','scout_on')").fetchall()),
                         {"scout_addr": "192.168.1.31", "scout_port": "9000", "scout_on": "1"})   # kept over a restart
        self.assertEqual(post("/settings/scout", k="addr", v="scout.local", port="8095")[0], 200)
        self.assertEqual(app.SCOUT_CFG["addr"], "scout.local")
        self.assertEqual(post("/settings/scout", k="addr", v="not an address", port="8095")[0], 400)
        self.assertEqual(post("/settings/scout", k="addr", v="scout.local", port="70000")[0], 400)
        self.assertEqual(app.SCOUT_CFG["addr"], "scout.local")                           # bad ones don't change it
        self.assertEqual(post("/settings/scout", k="text", v="0")[0], 200)
        self.assertEqual(app.SCOUT_CFG["text"], "0")
        self.assertEqual(post("/settings/scout", k="nope", v="1")[0], 400)
        # Bring back: back at the front of Shorts, and never dropped again
        app.OUTSIDE[:] = ["zz000000000"]
        self.assertEqual(post("/scout/keep", v="qd000000000")[0], 204)
        self.assertEqual(app.OUTSIDE, ["qd000000000", "zz000000000"])
        self.assertEqual(app.scout_verdict(c, "qd000000000", 35, ["same voice as x"]), False)
        self.assertEqual(app.OUTSIDE, ["qd000000000", "zz000000000"])
        self.assertEqual(post("/scout/keep", v="unknown0000")[0], 400)
        c.close()

    def _app(self, pid):
        srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        def call(path, **form):
            req = urllib.request.Request(base + path, urllib.parse.urlencode(form).encode() if form else None,
                                         {"Cookie": f"yak_profile={pid}"})
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as ex:
                return ex.code, ex.read().decode()
        call.port = srv.server_port
        return call

    def test_channel_lists(self):
        """Farm channels and Never drop: added by hand, and what that does to the Shorts list straight away."""
        c = app.connect()
        c.executemany("INSERT INTO videos(id,title,channel_title,short) VALUES(?,?,?,1)", [
            ("f1000000000", "t", "Ch Farm"), ("k1000000000", "t", "Good Guy"), ("x1000000000", "t", "Other"),
            ("k2000000000", "t", "Good Guy")] + [(f"au00000000{i}", "t", "Auto") for i in range(3)])
        c.execute("INSERT INTO scout(video_id,score,farm,why,at) VALUES('k1000000000',60,1,'world-rule opening',0)")
        c.executemany("INSERT INTO scout(video_id,score,farm,why,at) VALUES(?,90,1,'x',0)", [(f"au00000000{i}",) for i in range(3)])
        app.OUTSIDE[:] = ["f1000000000", "x1000000000"]
        self.assertTrue(app.scout_set_channel(c, "farm", "  ch   FARM "))           # any case, extra spaces
        self.assertEqual(app.OUTSIDE, ["x1000000000"])                           # its Shorts leave the list now
        self.assertEqual(c.execute("SELECT farm FROM scout WHERE video_id='f1000000000'").fetchone()[0], 1)
        self.assertEqual(app.scout_channels(c), {"ch farm", "auto"})
        self.assertTrue(app.scout_set_channel(c, "keep", "good guy"))            # Never drop: its dropped Short comes back
        self.assertEqual(app.OUTSIDE, ["k1000000000", "x1000000000"])
        self.assertFalse(app.scout_verdict(c, "k2000000000", 95, ["farm voice (4 channels)"]))   # and none get dropped
        self.assertTrue(app.scout_set_channel(c, "farm", "Auto", add=False))     # taking off one Scout found
        self.assertEqual(app.scout_channels(c), {"ch farm"})
        self.assertEqual(list(app.scout_lists(c)["ok"]), ["auto"])
        self.assertTrue(app.scout_set_channel(c, "farm", "ch farm", add=False))
        self.assertEqual(app.scout_channels(c), set())
        self.assertFalse(app.scout_set_channel(c, "farm", "   "))
        self.assertFalse(app.scout_set_channel(c, "nope", "x"))
        c.close()

    def test_drop_score_and_memory(self):
        c = app.connect()
        app.SCOUT_CFG["min"] = "70"
        self.assertFalse(app.scout_verdict(c, "m1000000000", 60, ["x"]))          # under the drop score you chose
        app.SCOUT_CFG["min"] = ""
        self.assertTrue(app.scout_verdict(c, "m2000000000", 60, ["x"]))           # the default, 50
        now = int(time.time())
        c.execute("INSERT INTO scout(video_id,score,farm,why,at,kept) VALUES('old00000000',0,0,'',?,0)", (now - 40 * 86400,))
        c.execute("INSERT INTO scout(video_id,score,farm,why,at,kept) VALUES('kept0000000',0,0,'',?,1)", (now - 400 * 86400,))
        app.tidy(c, now)
        self.assertIsNotNone(c.execute("SELECT 1 FROM scout WHERE video_id='old00000000'").fetchone())   # 60 days
        app.SCOUT_CFG["days"] = "30"
        app.tidy(c, now)
        self.assertIsNone(c.execute("SELECT 1 FROM scout WHERE video_id='old00000000'").fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM scout WHERE video_id='kept0000000'").fetchone())   # brought back: kept
        c.close()

    def test_page_routes(self):
        """The Settings > Scout page and what it posts to."""
        c = app.connect()
        pid = c.execute("INSERT INTO profiles(name,color) VALUES('Scouter2','#123456')").lastrowid
        c.execute("INSERT INTO videos(id,title,channel_title,short) VALUES('qd000000000','Farm story','Tales',1)")
        c.execute("INSERT INTO scout(video_id,score,farm,why,at,deep) VALUES('qd000000000',65,1,'world-rule opening',?,1)",
                  (int(time.time()),))
        call = self._app(pid)
        page = call("/settings")[1]
        for bit in ('id="sf-drop"', 'id="sf-farm"', 'id="sf-keep"', 'id="sf-voice"', 'id="sf-log"', 'id="sf-adv"',
                    "Scanned today", 'data-set="min"', 'data-filter="drop"', "Shows when YakScout is connected."):
            self.assertIn(bit, page)
        self.assertIn('<b id="sc-dropped">1</b><span>Dropped today</span>', page)
        self.assertEqual(call("/settings/scout", k="min", v="65")[0], 204)
        self.assertEqual(app.SCOUT_CFG["min"], "65")
        self.assertEqual(call("/settings/scout", k="min", v="99")[0], 400)
        self.assertEqual(call("/settings/scout", k="days", v="90")[0], 204)
        self.assertEqual(call("/settings/scout", k="days", v="45")[0], 400)
        self.assertEqual(call("/scout/channel", op="add", kind="farm", v="Bad Farm")[0], 200)
        self.assertEqual(call("/scout/channel", op="add", kind="keep", v="")[0], 400)
        self.assertIn("Bad Farm", call("/settings")[1])
        self.assertEqual(call("/scout/channel", op="remove", kind="farm", v="Bad Farm")[0], 200)
        self.assertEqual(call("/scout/voice", op="learn", v="https://youtube.com/shorts/AeyLzuVhr1s")[0], 503)  # not connected
        port, seen = self._fake_scout({})
        app.SCOUT_CFG.update(on="1", addr="127.0.0.1", port=str(port))
        app.SCOUT_LINK.update(state="ok", voices=[{"name": "tbQTA6u4lEM", "channels": 2, "channel_names": ["Drip Dyl", "Volze"],
                                                    "learned": "Nov 13"}], version="0.4", up=7200, secs=2.4, last=int(time.time()))
        code, body = call("/scout/voice", op="learn", v="https://youtube.com/shorts/AeyLzuVhr1s?si=x")
        self.assertEqual((code, json.loads(body)), (200, {"ok": True, "msg": "Same voice as tbQTA6u4lEM · 2 channels"}))
        self.assertEqual(call("/scout/voice", op="learn", v="hello")[0], 400)
        self.assertEqual(call("/scout/voice", op="forget", v="tbQTA6u4lEM")[0], 200)
        self.assertEqual(seen, ["learn AeyLzuVhr1s", "forget tbQTA6u4lEM"])
        app.SCOUT_LINK["voices"].append({"name": "farm-0002", "channels": 2, "channel_names": ["A", "B"], "source": "shared"})
        app.SCOUT_LINK["lists"] = [{"url": "https://x/voices.json", "count": 2, "fetched": int(time.time()) - 7200, "error": ""}]
        page = call("/settings")[1]
        self.assertIn("2 channels: Drip Dyl, Volze", page)
        self.assertIn('<span class="tag">Learned</span>', page)
        self.assertIn('<span class="tag">Shared</span>', page)
        self.assertIn('href="/scout/share?name=tbQTA6u4lEM"', page)                       # Share: learned voices only
        self.assertNotIn('href="/scout/share?name=farm-0002"', page)
        self.assertIn("Shared list: 2 voices · updated 2 hours ago", page)
        code, body = call("/scout/share?name=tbQTA6u4lEM")
        self.assertEqual((code, json.loads(body)["id"]), (200, "tbqta6u4lem"))
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", call.port)
        conn.request("GET", "/scout/submit?name=tbQTA6u4lEM", headers={"Cookie": f"yak_profile={pid}"})
        r = conn.getresponse()
        to = urllib.parse.urlsplit(r.getheader("Location"))
        q = urllib.parse.parse_qs(to.query)
        self.assertEqual((r.status, to.netloc, to.path), (303, "github.com", "/glenndall/yakscout-voices/issues/new"))
        self.assertEqual((q["template"], q["channels"], q["title"]), (["submit-voice.yml"], ["Drip Dyl"], ["Voice: Drip Dyl"]))
        self.assertIn('data-submit="/scout/submit?name=tbQTA6u4lEM"', page)
        self.assertIn("<b>2.4 s</b> a Short", page)
        self.assertIn("Up <b>2 hours</b>", page)
        self.assertIn("Plugin <b>v0.4</b>", page)
        self.assertIn("AI-Farm channels", page)
        live = json.loads(call("/scout/stats")[1])                                    # what the page refreshes from
        self.assertEqual((live["state"], live["text"], live["dropped"]), ("ok", "Connected", 1))
        self.assertIn("Plugin <b>v0.4</b>", live["facts"])
        c.close()

    def test_backup_restore_and_log(self):
        """Advanced > Backup (a zip: backup.json + log.csv), Restore (the zip or its json), and Log > Download CSV."""
        import zipfile, io as _io
        c = app.connect()
        pid = c.execute("INSERT INTO profiles(name,color) VALUES('Scouter3','#123456')").lastrowid
        c.executemany("INSERT INTO videos(id,title,channel_title,short) VALUES(?,?,?,1)",
                      [("qd000000000", "Farm, story 😳", "Tales"), ("kb000000000", "Kept one", "Nice")])
        c.execute("INSERT INTO scout(video_id,score,farm,why,at,deep) VALUES('qd000000000',65,1,'world-rule opening',?,1)",
                  (int(time.time()),))
        c.execute("INSERT INTO scout(video_id,score,farm,why,at,kept) VALUES('kb000000000',50,0,'x',?,1)", (int(time.time()),))
        app.scout_set_channel(c, "farm", "Bad Farm"); app.scout_set_channel(c, "keep", "Good Guy")
        app.SCOUT_CFG["min"] = "65"
        port, seen = self._fake_scout({})
        app.SCOUT_CFG.update(on="1", addr="127.0.0.1", port=str(port)); app.SCOUT_LINK.update(state="ok")
        srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.App)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_port}"

        def req(path, data=None):
            r = urllib.request.Request(base + path, data, {"Cookie": f"yak_profile={pid}"})
            try:
                with urllib.request.urlopen(r) as x:
                    return x.status, x.headers, x.read()
            except urllib.error.HTTPError as ex:
                return ex.code, ex.headers, ex.read()
        code, hd, body = req("/scout/log.csv")                                     # the log, for a spreadsheet
        self.assertIn("yakscout-log-", hd["Content-Disposition"])
        rows = body.decode("utf-8-sig").splitlines()
        self.assertEqual(rows[0], "Date,Verdict,Check,Score,Title,Channel,Reasons,Link")
        self.assertIn('Dropped,Scanned,65,"Farm, story 😳",Tales,world-rule opening,https://www.youtube.com/shorts/qd000000000',
                      rows[1] + rows[2])
        self.assertIn("Brought back,Read,50,Kept one", body.decode("utf-8-sig"))
        code, hd, zipped = req("/scout/backup")                                    # the backup
        self.assertEqual((code, hd["Content-Type"]), (200, "application/zip"))
        z = zipfile.ZipFile(_io.BytesIO(zipped))
        self.assertEqual(sorted(z.namelist()), ["backup.json", "log.csv"])
        b = json.loads(z.read("backup.json"))
        self.assertEqual(b["settings"]["min"], "65")
        self.assertEqual(sorted((x["kind"], x["name"]) for x in b["channels"]), [("farm", "Bad Farm"), ("keep", "Good Guy")])
        self.assertEqual([x["id"] for x in b["brought_back"]], ["kb000000000"])
        self.assertEqual(len(b["voices"][0]["print"]), 512)                       # voiceprints from the plugin
        # start again from nothing, then restore the zip
        c.execute("DELETE FROM scout_chan"); c.execute("DELETE FROM scout"); app.SCOUT_CFG["min"] = ""
        code, _, out = req("/scout/restore", zipped)
        self.assertEqual((code, json.loads(out)["msg"]), (200, "Restored 2 channels, 1 brought back, 1 voices"))
        self.assertEqual(app.scout_channels(c), {"bad farm"})
        self.assertEqual(app.SCOUT_CFG["min"], "65")
        self.assertEqual(c.execute("SELECT kept FROM scout WHERE video_id='kb000000000'").fetchone()[0], 1)
        self.assertEqual(seen, ["restore 1"])
        self.assertEqual(req("/scout/restore", z.read("backup.json"))[0], 200)     # the plain json works too
        app.SCOUT_LINK.update(state="down")                                        # plugin away: the rest still restores
        self.assertIn("voices not restored", json.loads(req("/scout/restore", zipped)[2])["msg"])
        self.assertEqual(req("/scout/restore", b'{"hello": 1}')[0], 400)
        self.assertEqual(req("/scout/restore", b"PK not a zip")[0], 400)
        self.assertEqual(req("/scout/restore", b"nonsense")[0], 400)
        c.close()

    def test_submissions(self):
        """Settings > YakScout > Submissions: open pull requests on the voice list, checked twice, one plain verdict."""
        api = "https://api.github.com/repos/glenndall/yakscout-voices"
        e1 = {"id": "farm-0003", "channels": ["A", "B"], "examples": ["aaaaaaaaaaa", "bbbbbbbbbbb"], "print": [0.0] * 512}
        e2 = dict(e1, id="farm-0004", examples=["ccccccccccc", "ddddddddddd"])
        pages = {f"{api}/pulls?state=open&per_page=10": [{"number": 7, "title": "Add farm-0003", "html_url": "https://gh/7",
                                                        "user": {"login": "someone"}},
                                                       {"number": 8, "title": "Add farm-0004", "html_url": "https://gh/8",
                                                        "user": {"login": "other"}}],
                 f"{api}/pulls/7/files?per_page=30": [{"filename": "voices/farm-0003.json", "status": "added", "raw_url": "raw7"},
                                                     {"filename": "README.md", "status": "modified", "raw_url": "x"}],
                 f"{api}/pulls/8/files?per_page=30": [{"filename": "voices/farm-0004.json", "status": "added", "raw_url": "raw8"}],
                 "raw7": e1, "raw8": e2}
        gaps = {"aaaaaaaaaaa": 0.1, "bbbbbbbbbbb": 0.12, "ccccccccccc": 0.1, "ddddddddddd": 0.9}
        verify = lambda en: {"examples": [{"id": v, "gap": gaps[v]} for v in en["examples"]]}
        farm = {"aaaaaaaaaaa": {"score": 50, "why": ["world-rule opening"]}, "bbbbbbbbbbb": {"score": 30, "why": ["farm description"]}}
        text = lambda vid: farm.get(vid, {"score": 0, "why": []})
        r = app.scout_review(lambda u: pages[u], verify, text)
        self.assertEqual(r["error"], "")
        self.assertEqual([(i["number"], i["verdict"]) for i in r["items"]], [(7, "ok"), (8, "no")])
        f = r["items"][0]["files"][0]
        self.assertEqual((f["same"], f["diff"], f["clues"], f["why"]), (2, 0, 2, ["world-rule opening", "farm description"]))
        # same voice but no farm clues: look at the channels; nothing heard: wait
        self.assertEqual(app.scout_judge_entry(e1, verify, lambda v: {"score": 0})["verdict"], "look")
        self.assertEqual(app.scout_judge_entry(e1, lambda en: {"examples": []}, text)["verdict"], "wait")
        def broken(en):
            raise OSError("plugin off")
        self.assertEqual(app.scout_judge_entry(e1, broken, text)["verdict"], "wait")
        page = app.scout_pane(app.connect())
        self.assertIn('id="sf-subs"', page)
        self.assertIn("✅ Looks good to merge", page)
        self.assertIn("❌ Don&#x27;t merge" if "&#x27;" in page else "❌ Don't merge", page)
        self.assertIn('href="https://gh/7"', page)
        self.assertIn("voice 2/2 same · farm clues 2/2 (world-rule opening, farm description)", page)
        def down(u):
            raise OSError("rate limited")
        self.assertIn("rate limited", app.scout_review(down)["error"])
        # opening the page later: GitHub says which are still open. #7 was merged (gone), #8 changed (not checked
        # yet), #9 is new (not checked yet)
        app.scout_review(lambda u: pages[u], verify, text)
        app.SCOUT_REVIEW["synced"] = 0
        open_now = [{"number": 8, "title": "Add farm-0004", "html_url": "https://gh/8", "head": {"sha": "new"}},
                    {"number": 9, "title": "Add farm-0005", "html_url": "https://gh/9", "head": {"sha": "s9"}}]
        app.scout_sync_open(lambda u: open_now)
        self.assertEqual([(i["number"], i["verdict"]) for i in app.SCOUT_REVIEW["items"]], [(8, "unchecked"), (9, "unchecked")])
        app.SCOUT_REVIEW["synced"] = int(time.time())
        self.assertIn("Not checked yet: press Check", app.scout_pane(app.connect()))
        app.scout_sync_open(lambda u: [])                             # within a minute: GitHub isn't asked again
        self.assertEqual(len(app.SCOUT_REVIEW["items"]), 2)
        app.SCOUT_REVIEW["synced"] = 0
        app.scout_sync_open(lambda u: [])                             # all merged or closed
        self.assertEqual(app.SCOUT_REVIEW["items"], [])
        self.assertIn("None open.", app.scout_pane(app.connect()))
        app.SCOUT_REVIEW.update(synced=0, items=[{"number": 8, "title": "t", "url": "u", "user": "", "files": [],
                                                  "verdict": "ok", "sha": "x"}])
        def offline(u):
            raise OSError("offline")
        app.scout_sync_open(offline)                                  # GitHub away: keep the list, never "None open"
        self.assertEqual(len(app.SCOUT_REVIEW["items"]), 1)
        self.assertNotIn("None open.", app.scout_pane(app.connect()))
        app.SCOUT_REVIEW.update(at=0, error="", items=[], synced=0, sync_ok=False)

    def test_channel_memory_and_off_switch(self):
        c = app.connect()
        for i in range(3):                                                # a channel with 3 Shorts, all judged a farm's
            c.execute("INSERT INTO videos(id,title,channel_title,short) VALUES(?,?,?,1)", (f"bad0000000{i}", "t", "Farm"))
            c.execute("INSERT INTO scout(video_id,score,farm,why,at) VALUES(?,90,1,'x',0)", (f"bad0000000{i}",))
        c.execute("INSERT INTO videos(id,title,channel_title,short) VALUES('bad00000009','t','Farm',1)")
        self.assertEqual(app.scout_channels(c), {"farm"})
        calls = []
        self._wire({}, calls)
        app.SCOUT_CFG["text"] = "1"
        app.OUTSIDE[:] = ["bad00000009"]
        app.scout_outside(c)
        self.assertEqual((app.OUTSIDE, calls), ([], []))                  # dropped without asking YouTube
        app.SCOUT_CFG["text"] = "0"
        app.OUTSIDE[:] = ["bad00000000", "zzz00000000"]
        app.scout_outside(c)
        self.assertEqual((app.OUTSIDE, calls), (["zzz00000000"], []))    # off: nothing new is read; what was already
        self.assertIsNone(c.execute("SELECT 1 FROM scout WHERE video_id='zzz00000000'").fetchone())   # decided stands
        c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
