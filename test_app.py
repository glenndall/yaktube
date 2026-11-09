"""Phase 1 tests: run with `python3 test_app.py`. YouTube is mocked; no network needed."""
import os, re, sys, tempfile, threading, time, unittest, urllib.request, urllib.parse, http.cookiejar
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
        s, url, page = self.get(a, "/")
        self.assertIn("Your feed is empty", page)
        s, url, page = self.get(a, "/subs/add", {"url": "@woz"})      # subscribe by handle
        self.assertIn("Woz &amp; Friends", page)
        s, url, page = self.get(a, "/")
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
        self.get(a, "/watch?v=vid00000002")
        _, _, page = self.get(a, "/")
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
        self.assertIn("Your feed is empty", self.get(b, "/")[2])
        self.assertIn("Nothing watched yet", self.get(b, "/history")[2])
        # unsubscribe + history clear
        self.get(a, "/subs/remove", {"id": app.CID_RE.search(CID).group(0)})
        self.assertIn("No subscriptions yet", self.get(a, "/subs")[2])
        self.get(a, "/history/remove", {"v": "zzzzzzzzzzz"})
        self.assertNotIn("Outside vid", self.get(a, "/history")[2])
        self.get(a, "/history/clear", {})
        self.assertIn("Nothing watched yet", self.get(a, "/history")[2])

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
        _, _, page = self.get(a, "/")
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
        self.assertIn("Your feed is empty", self.get(a, "/")[2])        # looking doesn't subscribe
        # subscribe from the channel page: comes back to it, now subscribed
        s, url, page = self.get(a, "/subs/add", {"url": CID, "next": "/channel?id=" + CID})
        self.assertIn("/channel?id=" + CID, url)
        self.assertIn("Subscribed ✓", page)
        # cards, the watch page and the subscriptions list all lead here
        self.assertIn(f'data-ch="{CID}"', self.get(a, "/")[2])
        self.assertIn(f'href="/channel?id={CID}"', self.get(a, "/watch?v=vid00000002")[2])
        self.assertIn(f'href="/channel?id={CID}"', self.get(a, "/subs")[2])
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

    def test_7_for_you(self):
        a = self.login("FY")
        self.assertIn("Nothing here yet", self.get(a, "/foryou")[2])
        self.get(a, "/watch?v=srch0000001")
        self.get(a, "/related?v=srch0000001")                       # what the watch page's script does
        _, _, page = self.get(a, "/foryou")
        for i in range(3): self.assertIn(f"Related {i}", page)
        self.assertNotIn("watch?v=srch0000001", page)                 # never suggests what you've seen
        self.assertLess(page.index("Related 0"), page.index("Related 2"))   # higher in "up next" ranks higher
        self.get(a, "/watch?v=rel00000000")
        self.assertNotIn("Related 0", self.get(a, "/foryou")[2])
        # channel cap: 5 related from one channel -> only 3 shown
        db = app.connect()
        db.execute("INSERT OR IGNORE INTO videos(id,channel_id,title) VALUES('cap00000001',?,'Cap')", (CID2,))
        db.executemany("INSERT OR IGNORE INTO videos(id,channel_id,title) VALUES(?,?,?)",
                       [(f"cap0000000{i}", CID2, f"Cap {i}") for i in range(2, 7)])
        db.executemany("INSERT OR REPLACE INTO related(src,video_id,pos) VALUES('srch0000001',?,?)",
                       [(f"cap0000000{i}", 10 + i) for i in range(2, 7)])
        self.assertLessEqual(self.get(a, "/foryou")[2].count("Retro") + self.get(a, "/foryou")[2].count("Cap "), 3)
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
        self.get(a, "/watch?v=ntyseen0001")
        _, _, page = self.get(a, "/new")
        self.assertIn("Fresh A", page)
        self.assertNotIn("Fresh B", page)                             # one per channel
        self.assertIn("Two hops away", page)                          # reached through a suggestion
        self.assertNotIn("More from known", page)                     # not new: you've watched that channel
        self.assertNotIn("A short", page)
        self.assertLess(page.index("Fresh A"), page.index("Two hops away"))
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
        for _ in range(50): self.get(a, "/")
        ms = (time.perf_counter() - t) / 50 * 1000
        print(f"\n  feed page avg: {ms:.1f} ms")
        self.assertLess(ms, 50)

    def test_9_tv_player(self):
        a = self.login("TVtest")
        _, _, page = self.get(a, "/watch?v=vid00000002")
        self.assertIn('<iframe id="yt" class="player"', page)          # normal browsers: YouTube's controls
        self.assertIn('setOption",["captions","track",{}]', page)     # captions start off (and stay off after seeking)
        self.assertIn('yakPlayer(f,null,1)', page)                      # Feed: no muted autoplay fallback
        self.assertNotIn('Tap for sound', page)
        self.assertNotIn('id="pp"', page)
        a.addheaders = [("User-Agent", "Mozilla/5.0 YakTubeTV/1")]
        _, _, page = self.get(a, "/watch?v=vid00000002")
        self.assertIn('id="pp"', page)                                  # TV app: our own buttons
        self.assertIn('embed/"+V', page)
        self.assertIn("vid00000002", page)
        self.assertIn(".tvbar", page)                                   # TV styles included
        self.assertIn('classList.add("hide")', page)                   # buttons fade out while playing
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
        r = a.open(self.base + "/seen", b"v=shr00000001")
        self.assertEqual(r.status, 204)
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts")[2])
        self.assertEqual(ids, ["vid00000003", "shr00000002"])                           # watched ones are gone
        ids = re.findall(r'"id": "(\w{11})"', self.get(a, "/shorts?v=shr00000002")[2])
        self.assertEqual(ids[0], "shr00000002")                                         # a chosen one goes first
        s, url, _ = self.get(a, "/go?q=" + urllib.parse.quote("https://youtube.com/shorts/shr00000002"))
        self.assertTrue(url.endswith("/shorts?v=shr00000002"))
        app.OUTSIDE[:] = []
        self.assertIn('href="/shorts?v=shr00000002"', self.get(a, "/")[2])             # subs' Shorts: in the feed (toggle on)
        self.get(a, "/feed/shorts", {})
        self.assertNotIn("shr0000000", self.get(a, "/")[2])                            # toggle off: not in the feed
        self.assertIn('href="/shorts"', self.get(a, "/")[2])                           # the tab

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
        app.OUTSIDE[:] = []

if __name__ == "__main__":
    unittest.main(verbosity=2)
