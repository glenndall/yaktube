# YakTube

## 1. Overview & Core Features

- **What it is:** a lightweight, self-hosted YouTube portal: one Python file plus one SQLite database. No Google
  account, no API keys, nothing to install.
- **Key features:** profiles, channel subscriptions, search, Home recommendations, watch history and a Watch later
  list, plus a full-screen Shorts player and a TV app.
- **Privacy & architecture:** no credentials are used or stored. The embedded player still loads from YouTube, so
  YouTube sees your IP address (but not any account).

## 2. Installation & Setup

YakTube can be hosted in several ways:

- **Mac:** double-click `start.command` (first time: right-click → Open), or run `python3 app.py`.
- **Synology NAS (Docker):** copy `app.py` and `docker-compose.yml` into a NAS folder, then Container Manager →
  Project → Create → pick the folder → it finds `docker-compose.yml` → Next → Done. Data is saved in the folder's
  `data/yaktube.db` (include it in Hyper Backup). **To update:** replace `app.py`, then Project → yaktube → Restart.
  No rebuild needed.
- **Ubuntu (Docker):** `docker compose up -d` (no build step).
- **Ubuntu (standalone):** `python3 app.py` (Python 3.9+, no additional packages).

Then open **http://localhost:8080**, or `http://<this-computer's-IP>:8080` from other devices on your network.

**Optional configuration.** YakTube reads a few environment variables (there's no `.env` file); leave them out for
the defaults.

| Variable | Default | What it does |
|---|---|---|
| `YAK_REFRESH_MIN` | `30` | Minutes between checks for new videos from your subscriptions |
| `YAK_DISCOVER_HOURS` | `6` | Hours between look-arounds for new Home / New to you suggestions (`0` = off) |
| `YAK_SCOUT` | `1` | `0` starts with YakScout's title & caption check off (Settings → YakScout switches it any time) |
| `YAK_SCOUT_MIN` | `50` | How sure YakScout must be (0-100) before it drops a Short; lower drops more, higher drops fewer |
| `YAK_DB` | `./yaktube.db` | Where the database lives (Docker: `/data/yaktube.db`, already set) |
| `YAK_PORT` | `8080` | Port to listen on (not for Docker, see below) |
| `YAK_HOST` | `0.0.0.0` | Address to listen on; `127.0.0.1` keeps it to this computer only |

- **Docker / NAS:** add them under `environment:` in `docker-compose.yml`, e.g. `- YAK_REFRESH_MIN=60`. To use
  another port, change the left number in `ports:` (e.g. `"8484:8080"`), not `YAK_PORT`. A plain Restart keeps the
  old settings: on a Synology, stop the project, edit the file, then Build; elsewhere run `docker compose up -d`.
  Your data in `data/` stays.
- **Command line:** put them in front of the command, e.g. `YAK_PORT=9000 python3 app.py`.

## 3. TV App Integration

- **Compatibility:** `android/` is a compact TV app for Android TV, Google TV and Fire TV. It opens YakTube full-screen
  and makes the remote work. GitHub builds it automatically (Actions → "TV app"); download `yaktube-tv.apk` from the
  latest Release. No address is built in, so the same app works with anyone's YakTube.
- **Installation:**
  1. Put `yaktube-tv.apk` next to the database (on a NAS: the `data/` folder). YakTube then offers it at
     `http://<NAS-address>:8080/yaktube-tv.apk`.
  2. On the TV, install the free **Downloader** app and allow it to install unknown apps (Settings → Apps → Security /
     Device preferences → Install unknown apps → Downloader).
  3. In Downloader, enter `http://<NAS-address>:8080/yaktube-tv.apk` and choose **Install**. Use the NAS's number
     address (e.g. `192.168.1.20`); TVs often don't understand `NAS.local`.
  4. Open YakTube on the TV. It asks for the address once: type it in full, e.g. `http://192.168.1.20:8080/`. To change
     it later, use **Change address** on the "Can't reach YakTube" screen.

  **To update:** replace the `.apk` in the same place and install it again from Downloader; it keeps the address and
  profile.
- **Interface & features** (laid out like the YouTube app on Google TV):
  - A rail down the left: your profile (Switch profile), Search, Home, Shorts, Subscriptions, Later, History, Channels
    and your recently watched channels. It widens with names when the remote moves into it, and hides during a video.
  - Home is rows you scroll sideways: *Recommended for you*, *New from your subscriptions*, *New to you*, *Shorts*,
    then a row for each reason (*Because you watched …*, *From your search …*). Sizes follow YouTube's TV app; the
    selected tile grows 10% with a white outline and shows why it was picked.
  - Remote: Left / Right along a row, Up / Down between rows (each row remembers where you were), Left at the start of
    a row opens the rail. In Shorts: Up / Down change Short, OK pauses, Right goes to the thumbs, Left to ✕.
  - The player shows the title on top and, along the bottom, a progress bar (Left / Right jump 10 seconds; hold to keep
    going), Play/Pause, ⟲10s / 10s⟳, thumbs, Subscribe, Watch later and ⚙ Settings (captions, speed, quality,
    autoplay; a dot on ⚙ means one isn't the usual). The controls fade after 5 seconds; any button brings them back.
  - **Sound but no picture?** Some TVs can't draw 4K or 60-frames-a-second video: ⚙ Settings → **Quality** switches
    Auto → 720p → 480p (remembered on that TV).
  - **Autoplay** is off by default; switch it on in ⚙ Settings, and the next unseen "Up next" video starts 10 seconds
    after one ends (Cancel stops it).

  It all lives in `app.py` (`TV_CSS`, `TV_JS`), so it updates with the server: no need to reinstall the TV app. To
  preview it on a computer, add `?tv=1` to any YakTube address (`?tv=0` switches back).

## 4. Usage & Navigation Guide

**Profiles & subscriptions**
- **Profiles:** "Who's watching?" Each profile has its own subscriptions, history and recommendations.
- **Subscribe:** the button on any watch page, channel page or search result, or paste a channel link / `@handle` on
  the Channels page.
- **Import / export:** Channels → *Import or export subscriptions*. Choose the Google Takeout `.zip` as it downloaded,
  or the `subscriptions.csv` inside it (YouTube and YouTube Music → subscriptions), or paste any list of channel links.
  Export saves a profile's list in the same CSV format, e.g. to copy it to another profile.
- **Channel pages:** click a channel's name to see its newest ~30 videos and subscribe or unsubscribe there.
- **Not interested:** *Hide from suggestions* on a channel's page. Hidden channels are listed in Settings → Hidden
  channels, where you can recommend them again. Your feed and search aren't affected.

**Layout & navigation**
- **Computer:** a bar down the left (Home, Shorts, Subscriptions, then New to you, Later, Channels, History, then
  Settings); it shrinks to icons on a narrow window. Home is rows you scroll sideways, like the TV: hover a card for
  its outline, use the arrows at the row's ends, and the reason a video was picked shows under it.
- **Phone:** Home, Shorts and Subscriptions along the bottom; New to you, Later, Channels, History and Profiles across
  the top; a single column of videos.
- **Search bar:** type words to search, or paste any YouTube link to watch it. Press `/` to jump there. Channels that
  match show above the videos, each with a Subscribe button.
- **Theme:** Settings → Theme (System, Light or Dark). On a phone, the moon button at the top switches it too.
- **Home-screen icon:** Safari → Share → Add to Home Screen (iPhone), or Chrome → ⋮ → Add to Home screen (Android).

**Playback & history**
- **Picks up where you left off:** a video you stopped part-way resumes from there, for each profile.
- **History:** a video counts as watched once it has played for 10 seconds, not when you open it. Remove single videos
  or clear it all; removing a video also removes its influence on suggestions.
- **Watch later:** the Watch later button under a video saves it to **Later**; it leaves once you've watched it to the
  end.
- **Thumbs up / down:** under a video and beside a Short. A thumbs up brings in more like it (and that channel); a
  thumbs down drops its "up next" from your suggestions and sinks its channel. Thumbed-down Shorts leave Home.
- **Videos start with captions off.** Where the browser blocks autoplay (iPhone, Safari), press play. In Shorts, a
  blocked Short starts muted with a "Tap for sound" button. A few videos block embedding; use "Open on YouTube".

**Home feed & suggestions**
- **Home:** suggestions worked out on this computer from what you watch, like, search for and subscribe to, with
  Shorts mixed in (your subscriptions' and YouTube's). After every video you watch, Back to Home shows picks that
  follow it. Add `?why=1` to the address (`/?why=1`) to see why each video is there and its score.
- **Subscriptions:** newest uploads from your subscriptions; the **Shorts** switch (next to Refresh) hides or shows
  their Shorts.
- **New to you:** channels you've never watched or subscribed to, found through the ones you like. One video per channel.
- **How Home picks videos** (all on this computer; YouTube only supplies the lists a logged-out visitor sees, never
  anything you've watched or rated):
  - *Where picks come from:* the "up next" lists of your recent watches (the last few hours count most, so Home follows
    your mood) and of videos you thumbed up; new and older unwatched videos from your subscriptions; your searches
    (recent and repeated ones count more); searches YakTube makes itself from words in titles you watch and like; and
    New to you.
  - *Scoring:* how strongly those lists point at a video × how much you like its channel (how much of its videos you
    watch, how often, how lately, your thumbs) × how well its title matches your topics × how new it is to you. A video
    you pass over on screen sinks, and recovers over a few days.
  - *Learning:* YakTube tracks how many of each source's picks you went on to watch (a thumbs up counts double, a thumbs
    down as a miss); sources that work for you get more of Home, and a little randomness keeps trying the others.
  - *Variety:* at most 2 per channel in the first 24 and 3 overall, one list or search can't fill the page, and every
    6th place goes to something new. Home reshuffles after every video you watch and every 15 minutes.
  - *Keeping it fresh:* the moment a video counts as watched, YakTube fetches its "up next". Every 6 hours it re-reads
    thumbed-up videos' lists, re-runs your searches and its own topic searches, and reads your two favourite
    subscriptions' channel pages in depth. New to you looks one step further out each time (up to 18 YouTube pages per
    profile); a channel you pass over sinks so others take its place, and gets another chance after a month.

**Shorts & content filtering**
- **Shorts:** full screen, one at a time, from all over YouTube (not your subscriptions), skipping ones you've watched.
  Swipe up for the next one, down to go back, tap to pause; each replays until you move on. On a computer: the mouse
  wheel, arrow keys or ▲▼; on the TV, the remote. ✕ or Back closes it. Pasting a youtube.com/shorts/ link opens it here.
  Once an hour YakTube asks youtube.com/shorts for a new batch of ~40, as a visitor who isn't logged in, and keeps the
  newest 600. Watching Shorts doesn't change your Home suggestions.
- **YakScout:** keeps content-farm Shorts out of that list: the faceless channels that churn out Shorts from one formula
  to farm views and ad money. Being made with AI isn't what it looks for (people make clever, funny things with AI);
  the formula is. Two parts:
  - **Titles & captions** (built in): adds up clues from the title, description and auto-captions (copy-pasted channel
    descriptions, scripts that open "In this town, everyone …", melodramatic wording, minutes of non-stop fast
    narration, and only a little for saying it was made with AI). No one clue is enough on its own. To see what it
    makes of one Short: `python3 app.py scout <link or id>`.
  - **Scan voices** (the optional [YakScout plugin](https://github.com/glenndall/yakscout), on the NAS with
    Docker or on another computer): listens to the first 15 seconds of each Short and recognises the synthetic voices farms reuse
    across channels (2-4 seconds a Short). YakTube works the same without it.

  It never judges Shorts from your subscriptions, and lets a Short through if YouTube won't answer.

**YakScout settings** (Settings → YakScout, under Plugins; the numbers at the top refresh by themselves)
- **How a Short is judged:** each new Short gets a score from 0 to 100 from the two checks above. Once the total
  reaches **Drop at** (50 to start) it leaves the Shorts list. Two lists overrule the score: **Never drop** always wins,
  then **AI-Farm channels**.
- **Connection:** the plugin's address (the NAS's address, port `8095`). Once connected, the line under it shows the
  plugin's version, seconds per Short, its last scan and uptime.
- **Dropped:** what was taken out, newest first, and what caught it (*Story*, *Voice*, *Channel*, *Pasted*, *Text*).
  **Bring back** returns one for good.
- **AI-Farm channels:** every Short from these is dropped unchecked. YakScout adds a channel itself once it has judged
  3 or more of its Shorts and dropped over half; you can add one by name, which drops its Shorts already in the list.
  **×** takes one off (one YakScout found stays off).
- **Never drop:** channels whose Shorts are always kept, whatever they score. Adding one brings its dropped Shorts back.
- **Voices:** the plugin's learned farm voices and the channels each was heard on. A voice on 2 or more channels drops
  a Short by itself (50 points); one from a single channel needs one more clue (35). Paste a farm Short's link and
  **Learn**; **×** forgets one. Shown while the plugin is connected. **Share** on one you learned downloads it and
  opens the shared list's *Submit a farm voice* form, filled in: drag the file in and press Create.
- **Submissions:** the open pull requests on the shared voice list. **Check** listens to each one's example Shorts
  with the plugin (same voice?) and reads them with the titles & captions check (farm clues?), here at home, since
  YouTube blocks GitHub's servers. Each gets one verdict: ✅ looks good to merge, ⚠️ same voice but few farm clues
  (look at the channels), ❌ don't merge, or ⏸ couldn't check yet. **Open** takes you to the pull request to merge it.
- **Log:** the last 100 verdicts, *Read* (titles & captions) or *Scanned* (the plugin listened too), with score and
  reasons. **Download CSV** saves every verdict still remembered, for Numbers or Excel.
- **Advanced:** **Drop at** (30-90; lower catches more and risks more mistakes), **Remember for** (30, 60 or 90 days,
  after which a Short is judged again; ones you brought back are always remembered), **Backup** (a `.zip` with
  `backup.json`: channel lists, Shorts you brought back, these settings and the plugin's voices; plus `log.csv`) and
  **Restore** (the `.zip` or its `backup.json`; adds to what's there, removes nothing).

**Behind the scenes**
- **Subscriptions** come from each channel's public RSS feed, checked every 30 minutes; if YouTube's RSS is down (it
  is as of Nov 2026), YakTube reads the channel's videos page instead. **Search** and **"Up next"** are read from
  YouTube's public pages. **Playback** is YouTube's privacy-mode embedded player (youtube-nocookie.com).
- **Tidy-up:** once a day YakTube forgets old "Up next" lists and videos nobody needs (unwatched, not from your
  subscriptions, older than 60 days; subscription videos after a year), so the database stays a few MB. Profiles,
  subscriptions and watch history are never touched.
- **Data:** everything is in `yaktube.db`. Back it up by copying that file (Docker: `docker cp yaktube:/data/yaktube.db .`).

## 5. Security & Remote Access

- **Local network use:** YakTube has no login, so **never open port 8080 directly to the internet**. Profiles have no
  PINs either: anyone on your network can pick any profile.
- **Secure remote options:**
  - **Tailscale** (simplest): install it on the NAS and your phone; your phone then reaches YakTube at the NAS's
    Tailscale address, as if you were home. Nothing is exposed to the internet.
  - **Cloudflare Tunnel + Cloudflare Access:** gives YakTube a web address on your own domain without opening ports.
    Add an Access policy that only lets in your email address; Cloudflare then emails a one-time code before anyone
    reaches YakTube. Both are on Cloudflare's free plan, and the TV app works through it too (sign in once with the
    code; it stays signed in for as long as your Access session lasts).

## Honest limits
- Search and suggestions read YouTube's web pages. If YouTube changes their layout, those two features show a friendly
  error until the parser in `app.py` (`parse_items`) is updated. Feed, history and playback keep working.
- Shorts use YouTube's own internal request, so YouTube could break them too.
- The embedded player loads from YouTube, so YouTube sees your IP address (not any account).

## Tests
`python3 test_app.py` runs everything against a fake YouTube (no network needed).

## License
MIT. See [LICENSE](LICENSE).
