# YakTube

A tiny self-hosted YouTube portal: profiles, subscriptions, search, Home suggestions, watch later, watch history.
No Google account, no API key, no dependencies: one Python file plus one SQLite database.

## Run it
- **Mac:** double-click `start.command` (first time: right-click → Open), or run `python3 app.py`.
- **Synology NAS:** copy `app.py` and `docker-compose.yml` to a folder on the NAS, then Container Manager →
  Project → Create → pick the folder → it finds `docker-compose.yml` → Next → Done. Open `http://<NAS-address>:8080`.
  Data is in the folder's `data/yaktube.db` (include it in Hyper Backup).
  **To update:** replace `app.py` in that folder, then Project → yaktube → Restart. No rebuild needed.
- **Ubuntu (Docker):** `docker compose up -d` (same idea: no build step)
- **Ubuntu (no Docker):** `python3 app.py` (Python 3.9+, nothing to install).
- Open **http://localhost:8080**. Other devices on your network: `http://<this-computer's-IP>:8080`.

### Settings (optional)
YakTube reads a few environment variables (there's no `.env` file). Leave them out to use the defaults.

| Variable | Default | What it does |
|---|---|---|
| `YAK_REFRESH_MIN` | `30` | Minutes between checks for new videos from your subscriptions |
| `YAK_DISCOVER_HOURS` | `6` | Hours between look-arounds for new Home / New to you suggestions (`0` = off) |
| `YAK_SCOUT` | `1` | `0` starts with YakScout's title & caption check off (Settings → YakScout switches it any time) |
| `YAK_SCOUT_MIN` | `50` | How sure YakScout must be (0-100) before it drops a Short; lower drops more, higher drops fewer |
| `YAK_DB` | `./yaktube.db` | Where the database lives (Docker: `/data/yaktube.db`, already set) |
| `YAK_PORT` | `8080` | Port to listen on (not for Docker, see below) |
| `YAK_HOST` | `0.0.0.0` | Address to listen on; `127.0.0.1` keeps it to this computer only |

- **Docker / NAS:** add them under `environment:` in `docker-compose.yml`, e.g. `- YAK_REFRESH_MIN=60`.
  To use another port, change the left number in `ports:` (e.g. `"8484:8080"`), not `YAK_PORT`.
  A plain Restart keeps the old settings: on a Synology, stop the project, edit the file, then Build;
  elsewhere run `docker compose up -d`. Your data in `data/` stays.
- **Without Docker:** put them in front of the command, e.g. `YAK_PORT=9000 python3 app.py`.

## TV app (Google TV, Android TV, Fire TV)
`android/` is a tiny TV app that opens YakTube full-screen and makes the remote work:
arrows move between videos, OK opens, Back leaves the player / goes back.
GitHub builds it automatically (Actions → "TV app"); download `yaktube-tv.apk` from the latest Release.
No address is built in, so the same app works with anyone's YakTube.

**Install it on the TV:**
1. Put `yaktube-tv.apk` next to the database (on a NAS: the `data/` folder). YakTube then offers it at
   `http://<NAS-address>:8080/yaktube-tv.apk`.
2. On the TV, install the free **Downloader** app and allow it to install unknown apps
   (Settings → Apps → Security / Device preferences → Install unknown apps → Downloader).
3. In Downloader, enter `http://<NAS-address>:8080/yaktube-tv.apk` and choose **Install**.
   Use the NAS's number address (e.g. `192.168.1.20`); TVs often don't understand `NAS.local`.
4. Open YakTube on the TV. It asks for the address once: type it in full, e.g. `http://192.168.1.20:8080/`.
   To change it later, use **Change address** on the "Can't reach YakTube" screen.

**To update:** replace the `.apk` in the same place and install it again from Downloader. It installs over the old
one and keeps the address and profile.

**The TV layout** is laid out like the YouTube app on Google TV:
- A rail of icons down the left: your profile (Switch profile) at the top, then Search, Home, Shorts, Subscriptions,
  Later, History, Channels, and your recently watched channels at the bottom. It widens with names when
  the remote moves into it.
- Shorts with the remote: Up / Down change Short, OK pauses, Right goes to the thumbs (Up / Down between
  them, OK presses one, Left goes back), Left goes to ✕ (OK closes).
- Home is rows you scroll sideways: *Recommended for you*, *New from your subscriptions*, *New to you*, *Shorts* (tall
  cards like YouTube's TV app; pick one to start there), then a row for each reason (*Because you watched …*,
  *From your search …*). Sizes follow YouTube's TV app (on a 1080p screen): video tiles 426 × 240, the top row
  640 × 360, Shorts tall; the selected one grows 10% with a white outline. Each video's title, channel and views sit
  under its picture (the reason it was picked shows when it's selected).
- Every video picture (TV, phone and computer) shows its length bottom right, once YakTube knows it: YouTube's lists
  include it, and the player reports it for anything you play.
- Remote: Left / Right along a row, Up / Down between rows (each row remembers where you were), Left at the start of a
  row opens the rail, Right leaves it. The rail is hidden while a video plays.
- The video player shows the title on top and, along the bottom, a progress bar (select it, then Left / Right jump
  10 seconds; hold to keep going), then Play/Pause, ⟲10s / 10s⟳, thumbs up / down, Subscribe, Watch later and
  ⚙ Settings. Settings opens a small menu with captions, speed, quality and autoplay (Up / Down to move, OK to change,
  Left or Back to close); a dot on ⚙ means one of them isn't the usual. The controls fade after 5 seconds (not while
  the menu is open); any button brings them back.

It all lives in `app.py` (`TV_CSS`, `TV_JS`), so it updates with the server: no need to reinstall the TV app.
To preview it on a computer, add `?tv=1` to any YakTube address (`?tv=0` switches back).

## Using it
- **Profiles:** "Who's watching?" Each profile has its own subscriptions, history and suggestions.
- **Search bar:** type words to search, or paste any YouTube link to watch it. Press `/` to jump there.
  Channels that match show above the videos, each with a Subscribe button.
- **Subscribe:** the button on any watch page, channel page or search result, or paste a channel link / `@handle`
  on the Subscriptions page.
- **Bring your YouTube subscriptions:** Subscriptions → *Import or export subscriptions*. Choose the
  `subscriptions.csv` from Google Takeout (YouTube and YouTube Music → subscriptions), or paste any list of channel
  links. Export saves a profile's list in the same format, e.g. to copy it to another profile.
- **Watch later:** Watch later button under a video, saves it to the **Later** tab.
  It leaves the list once you've watched it to the end.
- **Picks up where you left off:** a video you stopped part-way resumes from there, for each profile.
- **Channel pages:** click a channel's name (under any video, on a watch page, or in Subscriptions) to see its
  newest ~30 videos and subscribe or unsubscribe there. On the TV, open a video and pick the channel name below it.
- **Layout:** like YouTube's. On a computer, Home, Shorts and Subscriptions sit in a bar down the left, with New to you,
  Later, Channels and History under them (it shrinks to icons on a narrow window). On a phone, Home, Shorts and
  Subscriptions sit along the bottom, and New to you, Later, Channels, History and Profiles across the top.
- **Home:** suggestions worked out on this computer from what you watch, like, search for and subscribe to (see
  *How Home picks videos* below), with a group of 4 Shorts every few rows (unwatched ones from your subscriptions taking
  turns with YouTube's). It changes as you use it: after every video you watch, Back to Home shows picks that follow it.
  Add `?why=1` to the address (`/?why=1`) to see why each video is there and its score.
- **Subscriptions:** newest uploads from your subscriptions (`/feed`). The **Shorts** switch (next to Refresh) hides or
  shows their Shorts. **Channels** is where you add, remove, import and export subscriptions.
- **Thumbs up / down:** under a video and beside a Short. A thumbs up brings in more like it (and that channel); a
  thumbs down drops the video's "up next" from your suggestions and sinks its channel. Thumbed-down Shorts leave Home.
- **New to you:** channels you've never watched or subscribed to, found through the ones you like. One video per channel.
- **Shorts:** full screen, one at a time, from all over YouTube (not your subscriptions), skipping ones you've
  watched. Swipe up for the next one, down to go back, tap to pause; each one replays until you move on. On a computer use the mouse wheel,
  arrow keys or the ▲▼ buttons; on the TV, the remote's Down / Up. ✕ (top left) or Back closes it.
  Pasting a youtube.com/shorts/ link opens it here.
- **Videos start with captions off.** Where the browser blocks autoplay (iPhone, Safari), press play. In Shorts,
  a blocked Short starts muted with a "Tap for sound" button.
- **Sound but no picture on the TV?** Some TVs can't draw 4K or 60-frames-a-second video. In the TV player, ⚙ Settings →
  **Quality** switches Auto → 720p → 480p (remembered on that TV); the line under it shows what's playing.
- **Home-screen icon:** Safari → Share → Add to Home Screen (iPhone), or Chrome → ⋮ → Add to Home screen (Android).
- **Not interested:** use *Hide from suggestions* on a channel's page). Hidden channels are listed at the bottom of Subscriptions,
  where you can unhide them. Your feed and search are not affected.
- **Autoplay on the TV:** off by default. Switch on **Autoplay** in the TV player's ⚙ Settings (remembered on that TV)
  and when a video ends, the next "Up next" video you haven't seen starts after 10 seconds (Cancel stops it).
- **History:** a video counts as watched once it has played for 10 seconds, not when you open it. Remove single
  videos or clear it all. Removing a video also removes its influence on suggestions.

## How it works
- **Subscriptions:** each channel's public RSS feed. If YouTube's RSS is down (it is as of Nov 2026),
  YakTube reads the channel's normal videos page instead. Checked every 30 minutes.
- **Search and "Up next":** read from YouTube's public pages (the same data a logged-out browser sees).
- **Playback:** YouTube's privacy-mode embedded player (youtube-nocookie.com). Nothing to download or extract.
- **How Home picks videos:** all worked out on this computer; YouTube only supplies lists of videos, the same
  ones a visitor who isn't logged in sees. Never anything you've watched or rated.
  - *Where picks come from:* the "up next" lists of your recent watches (the last few hours count most, so Home follows
    your mood) and of videos you thumbed up; new uploads and older unwatched videos from your subscriptions; results of
    searches you've made (recent and repeated ones count more); searches YakTube makes by itself from the words in
    titles you watch and like; and New to you.
  - *How each is scored:* how strongly those lists point at it (a video several lists agree on counts more) × how much
    you like its channel (from how much of its videos you watch, how often, how lately, and your thumbs) × how well its
    title matches your topics × how new it is to you. Each time a video was on your screen and you passed it over, it
    sinks; it recovers over a few days. Home's page tells YakTube which videos were really on screen.
  - *Learning:* for each of those sources YakTube tracks how many of the videos it showed you went on to watch (a thumbs
    up counts double, a thumbs down as a miss). Sources that work for you get more of Home; a little randomness keeps
    trying the others.
  - *Variety:* at most 2 per channel in the first 24 and 3 overall (more only if nothing else is left), one list or
    search can't fill the page, and every 6th place goes to something new. Home reshuffles after every video you watch
    and every 15 minutes.
  - *Keeping it fresh:* the moment a video counts as watched, YakTube fetches its "up next" (and looks one step past it).
    On the 6-hourly look-around it also re-reads thumbed-up videos' lists, re-runs your recent searches and its own
    topic searches, and reads your two favourite subscriptions' channel pages in depth (each at most weekly).
- **New to you:** the "up next" videos of your watches, and of those suggestions, from channels you haven't tried.
  Every 6 hours YakTube re-reads the "up next" lists of your last 25 watches and looks one step further out from
  the current picks (up to 18 YouTube pages per profile). A channel you've been shown and passed over sinks
  (half as strong every 3 days) so others take its place; after a month it gets another chance.
- **Shorts:** once an hour YakTube opens youtube.com/shorts like a visitor who isn't logged in and keeps the ~40
  Shorts YouTube offers (ones that can't play outside YouTube are skipped), up to the newest 200. It uses YouTube's
  own internal request, so YouTube could break it. Watching Shorts doesn't change your Home / New to you suggestions.
- **YakScout:** every Short from the hourly batch is looked at before it reaches the Shorts list, and ones from content
  farms are dropped: the faceless channels that churn out Shorts from one formula to farm views and ad money. Being made
  with AI isn't what it looks for (people make clever, funny things with AI); the formula is. YakScout adds up clues from
  the title, description and auto-captions: copy-pasted channel descriptions, scripts that open "In this town,
  everyone …", melodramatic wording, minutes of non-stop fast narration, and (only a little) saying it was made with AI.
  No one clue is enough on its own. A channel YakScout has mostly dropped is dropped
  without looking at its other Shorts. It never judges Shorts from your subscriptions, and if YouTube won't answer, the
  Short is let through. To see what it makes of one Short: `python3 app.py scout <link or id>`. Dropped ones are
  listed in the log (`[scout] dropped …`). Spots by wording, not by looking at the video, so some get through.
  **Settings → YakScout** switches it on or off and shows what it has done (see [YakScout settings](#yakscout-settings)).
- **YakScout plugin (optional):** for more, run the YakScout plugin on the NAS with Docker, or on another computer on your
  network (an old PC, a Mac). It listens to the first 15 seconds of each Short and recognises the synthetic voices farms
  reuse across channels (about 2 seconds a Short). Put its address in **Settings → YakScout** (port `8095` unless you
  changed it); YakTube connects to it, hands it Shorts one at a time and adds what it finds to its own score. The page
  shows whether it's connected. When the plugin is off, YakTube carries on without it, and works the same without it.
- **Tidy-up:** once a day YakTube forgets old "Up next" lists and videos nobody needs (unwatched, not from
  your subscriptions, older than 60 days; subscription videos after a year), so the database stays a few MB.
  Profiles, subscriptions and watch history are never touched.
- **Data:** everything is in `yaktube.db`. Back it up by copying that file
  (Docker: `docker cp yaktube:/data/yaktube.db .`).

## YakScout settings
**Settings → YakScout** (under Plugins). The numbers at the top refresh by themselves while the page is open.

**How a Short is judged.** Each new Short gets a score from 0 to 100. The built-in check (**Titles & captions**) adds
points for farm clues in what the Short says about itself; the YakScout plugin (**Scan voices**) adds points if it
hears a farm voice. Once the total reaches **Drop at** (Advanced, 50 to start) the Short leaves the Shorts list. Two
lists overrule the score: **Never drop** always wins, then **AI-Farm channels**.

**Checks**
- **Titles & captions:** YakTube's own check, no plugin needed.
- **Scan voices:** sends Shorts to the YakScout plugin to be listened to. Needs the plugin running (Connection).

**Connection:** where the YakScout plugin is (the NAS's address, port `8095`). Once connected, the line under it shows
the plugin's version, how long a Short takes, its last scan and how long it has been running.

**Lists** (each folds away; the longer ones can be searched)
- **Dropped:** the Shorts taken out of the list, newest first, with what caught them (*Story*, *Voice*, *Channel*,
  *Pasted*, *Text*). **Bring back** puts one back at the front of the Shorts list, and it's never dropped again.
- **AI-Farm channels:** every Short from these is dropped without being checked. YakScout adds a channel by itself once
  it has judged 3 or more of its Shorts and dropped over half (*Found by YakScout*); you can add one by name (*Added by
  you*), which drops its Shorts already in the list straight away. **×** takes a channel off; one YakScout found stays
  off.
- **Never drop:** channels whose Shorts are always kept, whatever they score. Adding one brings its dropped Shorts back.
  For a channel that keeps getting caught by mistake.
- **Voices:** the farm voices the plugin knows, and the channels each was heard on. A voice heard on 2 or more channels
  drops a Short by itself (50 points); a voice from one channel needs one more clue (35). To teach it a voice, paste a
  link to a farm Short and press **Learn**: if it's a voice it already knows, that channel is added to it. **×**
  forgets a voice. Shown while the plugin is connected; the plugin keeps them (in its `data/voices.json`).
- **Log:** the last 100 verdicts: *Read* (titles & captions only) or *Scanned* (the plugin listened too), the score and
  the reasons. Handy for seeing why something was, or wasn't, dropped. **Download CSV** saves every verdict YakScout
  still remembers (not just 100) for Numbers or Excel: date, verdict, check, score, title, channel, reasons, link.

**Advanced**
- **Drop at:** the score that drops a Short (30-90). Lower catches more and risks more mistakes; higher lets more through.
- **Remember for:** how long verdicts are kept (30, 60 or 90 days). After that a Short is judged again if it turns up.
  Ones you brought back are always remembered.
- **Backup:** downloads `yakscout-backup-<date>.zip`, holding `backup.json` (your channel lists, the Shorts you
  brought back, these Advanced settings and, while the plugin is connected, its learned voices) and `log.csv`.
- **Restore:** pick a backup (the `.zip`, or the `backup.json` from inside it). It adds to what's there and removes
  nothing. Voices go back only while the plugin is connected; everything else restores either way.

## Using it away from home
YakTube has no login, so **never open port 8080 to the internet**. Two safe ways to reach it from your phone:
- **Tailscale** (simplest): install it on the NAS and your phone; your phone then reaches YakTube at the NAS's
  Tailscale address, as if you were home. Nothing is exposed to the internet.
- **Cloudflare Tunnel + Cloudflare Access:** gives YakTube a web address on your own domain without opening ports.
  Add an Access policy that only lets in your email address; Cloudflare then emails a one-time code before
  anyone reaches YakTube. Both are on Cloudflare's free plan. The TV app works through it too: sign in once
  with the code and it stays signed in for as long as your Access session lasts.

## Honest limits
- Search and suggestions read YouTube's web pages. If YouTube changes their layout, those two features
  show a friendly error until the parser in `app.py` (`parse_items`) is updated. Feed, history and playback keep working.
- The embedded player still loads from YouTube, so YouTube sees your IP address (not any account).
- Profiles have no PINs; anyone on your network can pick any profile.
- A few videos block embedding; use "Open on YouTube" for those.

## Tests
`python3 test_app.py` runs everything against a fake YouTube (no network needed).

## License
MIT. See [LICENSE](LICENSE).
