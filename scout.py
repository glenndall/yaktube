"""Scout: keeps content-farm Shorts out of YakTube's Shorts list (Settings > Scout).

Optional: app.py imports this file when it sits next to it, and runs without it when it doesn't. Scout shares
YakTube's database, Shorts list and helpers through `yak`, which app.py sets to itself when it imports this file.
"""
import csv, io, json, os, re, threading, time, urllib.parse, urllib.request, zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

yak = None      # app.py (set by it on import)

# Scout keeps content-farm Shorts out of the Shorts list: the faceless channels that mass-produce Shorts from one
# formula (an "In this world, everyone..." script, the same synthetic voice, a pasted description) to farm views and
# ad money. Being made with AI isn't the problem, and on its own it never gets a Short dropped: plenty of people make
# clever, funny things with AI. Scout reads each Short's title, description and auto-captions and adds up the signs of
# a farm; nothing is downloaded and nothing needs installing. The weights are a starting guess: `python3 app.py scout <link or id>...` shows what
# Scout makes of any Short and why, and the log says what was dropped. It only ever judges Shorts from outside your
# subscriptions, and when YouTube won't answer it lets the Short through.
SCOUT = os.environ.get("YAK_SCOUT", "1") != "0"                 # the title/caption check's default (Settings can change it)
SCOUT_CFG = {"text": "1" if SCOUT else "0",                     # Settings > Scout, saved as scout_<k> in settings:
             "on": "0", "addr": "", "port": "8095",             #   the Scout plugin: on/off, where it is
             "seen": "0",                                       #   and when it last answered
             "min": "", "days": "60",                           # Advanced: drop at (blank: SCOUT_MIN), remember for,
             "review": "0",                                     #   the maintainer's Submissions section (no switch in Settings)
             "secs": "0", "last": "0"}                          # seconds a Short, last scan (kept over restarts)
MAINTAINER = os.environ.get("YAK_SCOUT_MAINTAINER") == "1"      # whoever merges the shared voice list sees Submissions
SCOUT_MIN = int(os.environ.get("YAK_SCOUT_MIN", "50"))          # score (0-100) at which a Short counts as a farm's
SCOUT_PER_RUN = 80                                              # Shorts judged per batch (the rest wait for the next)


def maintainer():
    return MAINTAINER or SCOUT_CFG["review"] == "1"             # (the setting is for a portal that was set up before the variable)


def scout_min():
    """The score that drops a Short: Settings > Scout > Advanced, or YAK_SCOUT_MIN."""
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
    r = yak.post_json("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", {"videoId": vid, "context": {"client": {
        "clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 34, "hl": yak.REGION["hl"], "gl": yak.REGION["gl"]}}}, 6)
    d = r.get("videoDetails")
    if not d:
        return None
    text, secs_ = "", int(d.get("lengthSeconds") or 0)
    tracks = r.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    track = next((t for t in tracks if t.get("languageCode", "")[:2] == yak.REGION["hl"] and t.get("kind") == "asr"), None)
    if track:
        try:
            text, _ = scout_text(yak.fetch(track["baseUrl"] + "&fmt=json3", timeout=8))
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
    """The channels you've set in Settings > Scout, by kind: farm (always drop), keep (never drop) and ok (one Scout
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
    """Judge the Shorts in yak.OUTSIDE that Scout hasn't seen yet and take the content-farm ones out of the list."""
    if not yak.OUTSIDE:
        return
    known = {r[0]: r[1] for r in yak.by_ids(c, "SELECT video_id, farm FROM scout WHERE video_id IN ({})", yak.OUTSIDE)}
    todo = [v for v in yak.OUTSIDE if v not in known][:SCOUT_PER_RUN] if SCOUT_CFG["text"] == "1" else []
    if todo:
        names = {r[0]: r[1] for r in yak.by_ids(c, "SELECT id, channel_title FROM videos WHERE id IN ({})", todo)}
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
            if old:     # the Scout plugin got to it while this check ran: add to its verdict, don't replace it
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
    yak.OUTSIDE[:] = [v for v in yak.OUTSIDE if v not in drop]


def scout_queue(c, n=10):
    """Shorts in the list that the Scout plugin hasn't looked at yet, newest first: [{id, title, channel}]."""
    ids = yak.OUTSIDE[:]
    if not ids:
        return []
    done = {r[0] for r in yak.by_ids(c, "SELECT video_id FROM scout WHERE (deep=1 OR kept=1) AND video_id IN ({})", ids)}
    todo = [v for v in ids if v not in done][:n]
    info = {r["id"]: r for r in yak.by_ids(c, "SELECT id, title, channel_title FROM videos WHERE id IN ({})", todo)}
    return [{"id": v, "title": info[v]["title"] if v in info else "", "channel": info[v]["channel_title"] if v in info else ""}
            for v in todo]


def scout_verdict(c, vid, score, why):
    """The Scout plugin's verdict on the video itself. Adds to what the title/caption check already found; if the
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
        yak.OUTSIDE[:] = [v for v in yak.OUTSIDE if v != vid]
        c.execute("DELETE FROM outside WHERE video_id=?", (vid,))
        print(f"[scout] dropped {vid} ({total}) after scanning it - {'; '.join(reasons)}")
    return bool(farm)


def scout_set_channel(c, kind, name, add=True):
    """Settings > Scout: add a channel to (or take it off) the Farm channels or Never drop list. Adding a farm channel
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
            if vid in yak.OUTSIDE:
                c.execute("INSERT INTO scout(video_id,score,farm,why,at) VALUES(?,100,1,'a farm channel',?) ON CONFLICT(video_id) "
                          "DO UPDATE SET farm=1, kept=0, why='a farm channel'", (vid, now))
        drop = set(ids)
        if drop & set(yak.OUTSIDE):
            yak.OUTSIDE[:] = [v for v in yak.OUTSIDE if v not in drop]
            c.execute("DELETE FROM outside")
            c.executemany("INSERT INTO outside(pos, video_id) VALUES(?,?)", enumerate(yak.OUTSIDE))
    return True


def scout_keep(c, vid):
    """Bring back: a dropped Short isn't a farm's after all. It goes back at the front of the Shorts list and is never
    dropped again."""
    if not c.execute("UPDATE scout SET farm=0, kept=1 WHERE video_id=?", (vid,)).rowcount:
        return False
    if vid not in yak.OUTSIDE:
        yak.OUTSIDE.insert(0, vid)
        c.execute("DELETE FROM outside")
        c.executemany("INSERT INTO outside(pos, video_id) VALUES(?,?)", enumerate(yak.OUTSIDE))
    return True


# The Scout plugin (Settings > Scout): the separate glenndall/scout program, running on this NAS (Docker) or another computer on the network; it listens to the start of each Short
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
    """One step of talking to the Scout plugin. Returns how many seconds to wait before the next."""
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
    c = yak.connect()
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
        scout_save(c, "secs", f'{SCOUT_LINK["secs"]:.2f}')
        scout_save(c, "last", str(SCOUT_LINK["last"]))
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


# Settings > Scout > Submissions: the open pull requests on the shared voice list, each checked twice at home:
# the Scout plugin listens to its example Shorts (same voice?), and the titles & captions check reads them (farm
# clues?). GitHub's servers can't do the listening (YouTube blocks them), so it happens here. Merging stays on GitHub.
SCOUT_LIST_REPO = os.environ.get("YAK_SCOUT_LIST_REPO", "glenndall/scout-voices")
SCOUT_REVIEW = {"at": 0, "error": "", "items": [], "synced": 0, "sync_ok": False}
SCOUT_FARM_CLUE = 30                                            # a titles & captions score this high is a farm clue


def gh_get(url, timeout=15):
    """Public GitHub data as JSON (no sign-in, and nothing of YouTube's sent along)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"YakTube/{yak.RELEASE}", "Accept": "application/vnd.github+json"})
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
    """When Settings > Scout opens (at most once a minute): ask GitHub which submissions are still open, so merged
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
    examples = [x for x in entry.get("examples", []) if isinstance(x, str) and yak.VID_RE.fullmatch(x)][:10]
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
    """Check every open submission (Settings > Scout > Submissions > Check)."""
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
    """The live part of Settings > Scout (the page asks for it every few seconds): the connection badge, today's
    counts and the plugin's facts line."""
    on = SCOUT_CFG["on"] == "1" and SCOUT_CFG["addr"]
    seen = int(SCOUT_CFG["seen"] or 0)
    st = SCOUT_LINK["state"] if on else "off"
    chip = {"ok": ("ok", "Connected"), "down": ("down", "Can't reach" + (f" · seen {yak.ago(seen)}" if seen else "")),
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
        f'Plugin <b>v{yak.e(SCOUT_LINK["version"])}</b>' if SCOUT_LINK["version"] else "",
        f'<b>{SCOUT_LINK["secs"]:.1f} s</b> a Short' if SCOUT_LINK["secs"] else "",
        f'Last scan <b>{yak.ago(SCOUT_LINK["last"])}</b>' if SCOUT_LINK["last"] else "",
        f'Up <b>{dur(SCOUT_LINK["up"])}</b>' if SCOUT_LINK["up"] else "") if x) if st == "ok" else ""
    return {"state": st, "chip": chip[0], "text": chip[1], "error": SCOUT_LINK["error"] if st == "down" else "",
            "scanned": scanned, "dropped": dropped, "waiting": len(scout_queue(c, 1000)) if on else "–", "facts": facts}


def scout_pane(c):
    """Settings > Scout: everything Scout does, in one place. The lists fold away; each can be searched."""
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
        return f'<div class="item" data-q="{yak.e(q.lower())}">{pic}<div><span class="t">{main}</span><small>{sub}</small></div>{btn}</div>'
    none = '<div class="empty">None yet.</div>'
    # dropped Shorts
    rows = c.execute("SELECT s.video_id, s.why, s.at, v.title, v.channel_title FROM scout s LEFT JOIN videos v ON v.id=s.video_id "
                     "WHERE s.farm=1 ORDER BY s.at DESC LIMIT 200").fetchall()
    n_drop = c.execute("SELECT COUNT(*) FROM scout WHERE farm=1").fetchone()[0]
    drops = "".join(item(f'{r["title"] or ""} {r["channel_title"] or ""} {r["why"] or ""}', yak.e(r["title"] or "?"),
                         f'<span class="tag" title="{yak.e(r["why"] or "")}">{scout_badge(r["why"] or "")}</span>{yak.e(r["channel_title"] or "")}',
                         f'<button type="button" class="btn ghost small" data-keep="{r["video_id"]}">Bring back</button>',
                         f'<img src="https://i.ytimg.com/vi/{r["video_id"]}/oar2.jpg" alt="" loading="lazy">') for r in rows)
    # channels
    ls, found = scout_lists(c), scout_found(c)
    farm = [(n, f"Found by Scout · {k} dropped") for n, k in sorted(found.items(), key=lambda x: -x[1])
            if n.lower() not in ls["ok"] and n.lower() not in ls["keep"] and n.lower() not in ls["farm"]]
    farm = [(n, "Added by you") for n in ls["farm"].values()] + farm
    x = lambda kind, n: f'<button type="button" class="x" data-rm="{kind}" data-name="{yak.e(n)}" aria-label="Remove {yak.e(n)}">×</button>'
    farms = "".join(item(n, yak.e(n), sub, x("farm", n)) for n, sub in farm)
    keeps = "".join(item(n, yak.e(n), "Added by you", x("keep", n)) for n in ls["keep"].values())
    # voices (Scout keeps them; shown while it's connected)
    voices = SCOUT_LINK["voices"] if st == "ok" else []

    def vsub(v):
        n = v.get("channels") if isinstance(v.get("channels"), int) else 0
        names = [str(x) for x in v.get("channel_names", [])][:6] if isinstance(v.get("channel_names"), list) else []
        bits = [f'{n} channel{"s" if n != 1 else ""}' + (": " + ", ".join(names) if names else "")] if n else []
        return " · ".join(bits + ([f'learned {v["learned"]}'] if v.get("learned") else []))
    listed = {str(c).lower() for v in voices if v.get("source") == "shared" for c in v.get("channel_names", [])}
    def vbtn(v):
        q = urllib.parse.quote(str(v.get("name", "")))
        share = ""
        if v.get("source") != "shared":                   # the shared list wants 2+ channels and 2+ Shorts: shown always
            # (an older Scout that doesn't count Shorts is judged on channels alone)
            chans = v["channels"] if isinstance(v.get("channels"), int) else 0
            more = [f'{k} more {w}{"s" if k > 1 else ""}' for k, w in ((2 - chans, "channel"), (2 - v.get("examples", 2), "Short"))
                    if k > 0]                               # red: the first thing missing (a channel brings a Short); green: ready
            if v.get("channel_names") and all(str(c).lower() in listed for c in v["channel_names"]):
                share = '<span class="tag">Submitted</span>'      # its channels are all on the shared list already
            elif more:
                share = f'<span class="tag need" title="The shared list needs 2 channels and 2 Shorts">Needs {more[0]}</span>'
            else:
                share = ('<span class="tag ready">Ready to submit</span>'
                         f'<a class="btn ghost small" href="/scout/share?name={q}" download data-submit="/scout/submit?name={q}" '
                         'title="Send it to the shared voice list">Share</a>')
        return share
    vlist = "".join(item(f'{v.get("name", "")} {vsub(v)} {v.get("source", "")}', yak.e(str(v.get("name", "?"))),
                         f'<span class="tag">{"Shared" if v.get("source") == "shared" else "Learned"}</span>{yak.e(vsub(v))}',
                         vbtn(v)) for v in voices)
    lists = SCOUT_LINK["lists"] if st == "ok" else []
    vinfo = "".join(f'<div class="empty">Shared list: {l.get("count", 0)} voices · '
                    f'{"updated " + yak.ago(l["fetched"]) if l.get("fetched") else "not downloaded yet"}'
                    f'{" · " + yak.e(str(l["error"])[:60]) if l.get("error") else ""}</div>' for l in lists)
    # submissions to the shared voice list, checked at home (only for the list's maintainer)
    if maintainer():
        scout_sync_open()

    def sline(f):
        if f.get("error"):
            return f'{yak.e(f["id"])}: {yak.e(f["error"])}'
        bits = [f'voice {f["same"]}/{f["examples"]} same' + (f', {f["diff"]} different' if f["diff"] else ""),
                f'farm clues {f["clues"]}/{f["examples"]}' + (f' ({", ".join(f["why"])})' if f["why"] else "")]
        return f'{yak.e(f["id"])} · ' + " · ".join(yak.e(b) for b in bits)
    subs = "".join(
        f'<div class="item sub" data-q="{yak.e((it["title"] + " " + it["user"]).lower())}"><div><span class="t">'
        f'{SCOUT_VERDICT.get(it["verdict"], "")}</span><small>#{it["number"]} {yak.e(it["title"])}'
        f'{" · by " + yak.e(it["user"]) if it["user"] else ""}</small>'
        + "".join(f'<small>{sline(f)}</small>' for f in it["files"]) +
        f'</div><a class="btn ghost small" href="{yak.e(it["url"])}" target="_blank" rel="noopener">Open</a></div>'
        for it in SCOUT_REVIEW["items"])
    stext = (f'Checked {yak.ago(SCOUT_REVIEW["at"])}' if SCOUT_REVIEW["at"] and SCOUT_REVIEW["items"] else
             "" if SCOUT_REVIEW["sync_ok"] and not SCOUT_REVIEW["items"] else "Not checked yet") + \
        (f' · {yak.e(SCOUT_REVIEW["error"])}' if SCOUT_REVIEW["error"] else "")
    sinfo = f'<div class="empty">{stext}</div>' if stext else ""
    # the log: what was judged lately
    log = c.execute("SELECT s.video_id, s.score, s.farm, s.why, s.at, s.deep, v.title, v.channel_title FROM scout s "
                    "LEFT JOIN videos v ON v.id=s.video_id ORDER BY s.at DESC LIMIT 100").fetchall()
    logs = "".join(item(f'{r["title"] or ""} {r["channel_title"] or ""} {r["why"] or ""}',
                        yak.e(r["title"] or r["video_id"]),
                        f'{"Dropped" if r["farm"] else "Kept"} · {r["score"]} · {yak.e(r["why"] or "-")} · {yak.ago(r["at"])}',
                        f'<span class="tag">{"Scanned" if r["deep"] else "Read"}</span>') for r in log)
    lo = scout_min()
    days = SCOUT_CFG["days"] if SCOUT_CFG["days"] in ("30", "60", "90") else "60"
    return (f'<section class="spane scp" id="p-scout" hidden><div class="sch"><h2>Scout</h2><span class="chip {live["chip"]}" '
            f'id="sc-chip" title="{yak.e(live["error"])}"><i></i><span>{live["text"]}</span></span></div>'
            '<p>Keeps content-farm Shorts out. AI-made is fine.</p>'
            f'<div class="stats"><div class="stat"><b id="sc-scanned">{live["scanned"]}</b><span>Scanned today</span></div>'
            f'<div class="stat"><b id="sc-dropped">{live["dropped"]}</b><span>Dropped today</span></div>'
            f'<div class="stat"><b id="sc-waiting">{live["waiting"]}</b><span>Waiting</span></div></div>'
            '<h3>Checks</h3><div class="card">' + switch("text", "Titles &amp; captions", "Built in") +
            switch("on", "Scan voices", "Needs the Scout plugin") + '</div>'
            '<h3>Connection</h3><div class="card"><form class="chk addr" id="scaddr"><label class="fld wide"><span>Address</span>'
            f'<input name="addr" value="{yak.e(SCOUT_CFG["addr"])}" placeholder="192.168.1.31" inputmode="url" autocapitalize="off" '
            'autocorrect="off" spellcheck="false"></label><label class="fld port"><span>Port</span>'
            f'<input name="port" value="{yak.e(SCOUT_CFG["port"])}" placeholder="8095" inputmode="numeric"></label>'
            f'<button class="btn small save">Save</button><span class="msg" role="status"></span></form>'
            f'<div class="facts" id="sc-facts"{"" if live["facts"] else " hidden"}>{live["facts"]}</div></div>'
            '<h3>Lists</h3>' +
            fold("drop", "Dropped", n_drop, drops or none, search("drop")) +
            fold("farm", "AI-Farm channels", len(farm), farms or none, add("farm", "Channel name") + search("farm")) +
            fold("keep", "Never drop", len(ls["keep"]), keeps or none, add("keep", "Channel name")) +
            fold("voice", "Voices", len(voices), vlist or ('<div class="empty">None learned yet.</div>' if st == "ok" else
                 '<div class="empty">Shows when Scout is connected.</div>'),
                 (add("voice", "Link to a farm Short", "Learn") + vinfo) if st == "ok" else "") +
            (fold("subs", "Submissions", len(SCOUT_REVIEW["items"]),
                  subs or ('<div class="empty">None open.</div>' if SCOUT_REVIEW["sync_ok"] else ""),
                  '<div class="tools"><button type="button" class="btn small" data-review>Check</button>'
                  '<span class="msg" role="status"></span></div>' + sinfo) if maintainer() else "") +
            fold("log", "Log", len(log), logs or none,
                 '<div class="tools"><input type="search" placeholder="Search" data-filter="log" aria-label="Search">'
                 '<a class="btn ghost small" href="/scout/log.csv" download>Download CSV</a></div>') +
            '<details class="card fold" id="sf-adv"><summary>Advanced</summary>'
            f'<div class="chk"><div>Drop at<small>Lower drops more</small></div><div class="slider"><input type="range" min="30" '
            f'max="90" step="5" value="{lo}" data-set="min" aria-label="Drop at"><b>{lo}</b></div></div>'
            '<div class="chk"><div>Remember for<small>Then Scout looks again</small></div><select class="btn ghost small" '
            'data-set="days" aria-label="Remember for">' + "".join(
                f'<option value="{d}"{" selected" if d == days else ""}>{d} days</option>' for d in ("30", "60", "90")) +
            '</select></div>' +
            '<div class="chk"><div>Backup<small>Lists, voices, settings &amp; log (.zip)</small></div>'
            '<a class="btn ghost small" href="/scout/backup" download>Download</a></div>'
            '<div class="chk"><div>Restore<small>From a backup; adds to what\'s here</small><span class="msg" id="sc-rmsg" '
            'role="status"></span></div><label class="btn ghost small" tabindex="0">Choose file<input type="file" '
            'accept=".zip,.json,application/zip,application/json" id="sc-restore" hidden></label></div></details></section>')


# Settings > Scout: the switches save straight away; Save checks the address (YakTube then tries to connect, and the
# page comes back a few seconds later to show whether it could); Bring back returns a Short to the Shorts list. The
# lists fold away (the page remembers which were open) and can be searched; channels are added and removed in place.
SCOUT_JS = """<script>(function(){var p=document.getElementById("p-scout");if(!p)return;
function post(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:b})}
function again(){setTimeout(function(){location.replace("/settings#scout");location.reload()},2500)}
function now(){try{sessionStorage.setItem("yak-scout-y",String(window.scrollY))}catch(e){}location.replace("/settings#scout");location.reload()}
try{var sy=sessionStorage.getItem("yak-scout-y");if(sy!==null){sessionStorage.removeItem("yak-scout-y");
  window.addEventListener("load",function(){window.scrollTo(0,+sy)})}}catch(e){}
var KEY="yak-scout-open";try{(sessionStorage.getItem(KEY)||"").split(",").forEach(function(id){var d=id&&document.getElementById(id);if(d)d.open=true})}catch(e){}
p.querySelectorAll("details.fold").forEach(function(d){d.addEventListener("toggle",function(){try{sessionStorage.setItem(KEY,
  [].map.call(p.querySelectorAll("details.fold[open]"),function(x){return x.id}).join(","))}catch(e){}})});
function subs(){fetch("/settings").then(function(r){return r.text()}).then(function(h){   // Submissions on/off, in place
  var d=new DOMParser().parseFromString(h,"text/html"),nv=d.querySelector("#sf-voice .sclist"),ov=p.querySelector("#sf-voice .sclist");
  if(nv&&ov)ov.innerHTML=nv.innerHTML;var ns=d.getElementById("sf-subs"),os=document.getElementById("sf-subs");
  if(os){if(ns&&os.open)ns.open=true;os.remove()}if(ns)document.getElementById("sf-voice").after(ns)}).catch(function(){})}
function count(d){var n=d.querySelector(".n");if(n)n.textContent="· "+d.querySelectorAll(".item").length}
p.addEventListener("input",function(ev){var k=ev.target.dataset.filter;if(k){var q=ev.target.value.toLowerCase();
  document.querySelectorAll("#sf-"+k+" .item").forEach(function(r){r.hidden=q&&r.dataset.q.indexOf(q)<0})}
  if(ev.target.dataset.set=="min")ev.target.nextElementSibling.textContent=ev.target.value});
p.addEventListener("change",function(ev){var k=ev.target.dataset.set;if(k)post("/settings/scout","k="+k+"&v="+ev.target.value)});
p.addEventListener("click",function(ev){var a=ev.target.closest("a[data-submit]");if(a){window.open(a.dataset.submit,"_blank","noopener")}
  var b=ev.target.closest("button");if(!b)return;
  if(b.dataset.k){var on=!b.classList.contains("on");b.classList.toggle("on",on);b.setAttribute("aria-checked",on);
    b.firstChild.nodeValue=on?"On":"Off";post("/settings/scout","k="+b.dataset.k+"&v="+(on?1:0));if(b.dataset.k=="on")again()}
  else if(b.dataset.keep){post("/scout/keep","v="+b.dataset.keep);var s=document.createElement("span");s.className="sdone";
    s.textContent="Back";b.closest(".item").classList.add("back");b.replaceWith(s)}
  else if(b.dataset.rm){var d=b.closest("details");post("/scout/channel","op=remove&kind="+b.dataset.rm+"&v="+encodeURIComponent(b.dataset.name));
    b.closest(".item").remove();count(d)}
  else if(b.hasAttribute("data-review")){var rm2=b.nextElementSibling;b.disabled=true;rm2.className="msg";
    rm2.textContent="Checking… (about 10 seconds a Short)";post("/scout/review","").then(function(r){return r.json()})
    .then(function(j){if(j.error){b.disabled=false;rm2.className="msg bad";rm2.textContent=j.error;return}
      subs()})
    .catch(function(){b.disabled=false;rm2.className="msg bad";rm2.textContent="Couldn't check"})}});
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


# Scout's pages and buttons: "METHOD /path" -> the App method that answers it (added to app.py's ROUTES)
ROUTES = {"POST /settings/scout": "settings_scout", "POST /scout/keep": "scout_keep",
          "POST /scout/channel": "scout_channel", "POST /scout/voice": "scout_voice",
          "GET /scout/backup": "scout_backup", "POST /scout/restore": "scout_restore",
          "GET /scout/log.csv": "scout_log_csv", "GET /scout/share": "scout_share",
          "GET /scout/submit": "scout_submit", "POST /scout/review": "scout_review_route",
          "GET /scout/stats": "scout_stats"}


class Routes:
    """Scout's handlers, mixed into app.py's App (self.db, self.form, self.send... are its)."""

    def settings_scout(self):
        """Settings > Scout: the two switches, and where the Scout plugin is (an address, or address:port)."""
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
        self.send("", 204 if yak.VID_RE.match(vid) and scout_keep(self.db, vid) else 400)

    def scout_channel(self):
        """Settings > Scout: add a channel to Farm channels or Never drop, or take it off."""
        f = self.form()
        ok = scout_set_channel(self.db, f.get("kind", ""), f.get("v", ""), f.get("op") != "remove")
        self.send(json.dumps({"ok": True} if ok else {"error": "Type a channel name"}), 200 if ok else 400,
                  ctype="application/json")

    def scout_voice(self):
        """Settings > Scout > Voices: have the Scout plugin learn a farm Short's voice. Voices stay: there is no way to forget or hide one."""
        f = self.form()
        op, v = f.get("op", ""), f.get("v", "").strip()

        def answer(obj, code=200):
            self.send(json.dumps(obj), code, ctype="application/json")
        if SCOUT_LINK["state"] != "ok":
            return answer({"error": "Scout isn't connected"}, 503)
        try:
            if op == "learn":
                vid = yak.video_id_from(v)
                if not vid:
                    return answer({"error": "Not a YouTube link"}, 400)
                r = scout_call("/learn", {"id": vid}, timeout=180)
                msg = ("New voice" if r.get("new") else f"Same voice as {r.get('name', '?')}") + \
                      (f" · {len(r.get('channels', []))} channels" if len(r.get("channels", [])) > 1 else "")
            else:
                return answer({"error": "?"}, 400)
        except urllib.error.HTTPError as ex:
            try:
                why = json.loads(ex.read()).get("error", "")
            except Exception:
                why = ""
            return answer({"error": (why or "Scout couldn't do it")[:120].capitalize()}, 502)
        except Exception:
            return answer({"error": "Scout didn't answer"}, 502)
        SCOUT_WAKE.set()                                             # fetch the new voice list
        answer({"ok": True, "msg": msg})

    def scout_stats(self):
        self.send(json.dumps(scout_stats(self.db)), ctype="application/json", headers=[("Cache-Control", "no-store")])

    def scout_log_csv(self):
        """Settings > Scout > Log: every verdict Scout still remembers, one row per Short, for a spreadsheet."""
        name = time.strftime("scout-log-%Y-%m-%d.csv")
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
        """Settings > Scout > Submissions > Check: check every open submission now (takes a while: it listens)."""
        r = scout_review()
        self.send(json.dumps({"ok": not r["error"], "error": r["error"], "n": len(r["items"])}), ctype="application/json")

    def scout_submit(self):
        """Share's second half: opens the shared list's "Submit a farm voice" form on GitHub with the voice's example
        Shorts and channels filled in (the voice file itself is dragged in: Share downloads it)."""
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("name", [""])[0]
        try:
            entry = scout_call("/share?name=" + urllib.parse.quote(name), timeout=10) if SCOUT_LINK["state"] == "ok" else {}
        except Exception:
            entry = {}
        q = {"template": "submit-voice.yml", "title": "Voice: " + ", ".join(entry.get("channels", [])[:3]),
             "examples": "\n".join("https://youtube.com/shorts/" + x for x in entry.get("examples", [])[:10]),
             "channels": "\n".join(entry.get("channels", [])[:20])}
        url = f"https://github.com/{SCOUT_LIST_REPO}/issues/new?"
        if entry:                                         # the voice file's contents too, so nothing is dragged in
            full = url + urllib.parse.urlencode({**q, "voice": json.dumps(entry, separators=(",", ":"))})
            if len(full) < 7500:                          # GitHub refuses much longer links: then the file is dragged in
                return self.redirect(full)
        self.redirect(url + urllib.parse.urlencode(q))

    def scout_share(self):
        """Settings > Scout > Voices > Share: a learned voice as a file for the shared voice list (from the plugin)."""
        name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("name", [""])[0]
        if SCOUT_LINK["state"] != "ok" or not name:
            return self.send("Scout isn't connected", 503, ctype="text/plain; charset=utf-8")
        try:
            entry = scout_call("/share?name=" + urllib.parse.quote(name), timeout=10)
        except Exception:
            return self.send("Scout couldn't share that voice", 502, ctype="text/plain; charset=utf-8")
        fname = re.sub(r"[^a-z0-9-]", "-", str(entry.get("id", "voice")))[:40]
        self.send(json.dumps(entry, indent=1), ctype="application/json",
                  headers=[("Content-Disposition", f'attachment; filename="voice-{fname}.json"')])

    def scout_backup(self):
        """Settings > Scout > Advanced > Backup: a .zip with backup.json (your channel lists, the Shorts you brought
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
        name = time.strftime("scout-backup-%Y-%m-%d.zip")
        self.send(buf.getvalue(), ctype="application/zip",
                  headers=[("Content-Disposition", f'attachment; filename="{name}"'), ("Cache-Control", "no-store")])

    def scout_restore(self):
        """Settings > Scout > Advanced > Restore: read a Backup file back in. Adds to what's there (nothing is
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
            return answer({"error": "Not a Scout backup"}, 400)
        if not isinstance(b, dict) or b.get("yakscout_backup") != 1:
            return answer({"error": "Not a Scout backup"}, 400)
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
            if isinstance(vid, str) and yak.VID_RE.fullmatch(vid):
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
                skipped = " · voices not restored (Scout didn't answer)"
        elif voices:
            skipped = " · voices not restored (Scout isn't connected)"
        answer({"ok": True, "msg": f'Restored {got["channels"]} channels, {got["back"]} brought back, '
                                   f'{got["voices"]} voices{skipped}'})


def scout_cli(args):
    """python3 app.py scout <link or id>...: what Scout makes of each Short, and why."""
    for a in args:
        vid = yak.video_id_from(a)
        if not vid:
            print(f"{a}: not a YouTube link or id")
            continue
        r = scout_check(vid)
        print(f"{vid}: " + (f"{r['score']:3d}  {'farm' if r['farm'] else 'ok'}  {'; '.join(r['why']) or '-'}" if r else "YouTube has nothing on it"))
