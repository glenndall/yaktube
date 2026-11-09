# YakTube

A tiny self-hosted YouTube portal: profiles, subscriptions, search, "For you" suggestions, watch history.
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

The TV layout (bigger, dark, highlight on the selected item) lives in `app.py` (`TV_CSS`), so it updates with the server.

## Using it
- **Profiles:** "Who's watching?" Each profile has its own subscriptions, history and suggestions.
- **Search bar:** type words to search, or paste any YouTube link to watch it. Press `/` to jump there.
- **Subscribe:** the button on any watch page, or paste a channel link / `@handle` on the Subscriptions page.
- **Channel pages:** click a channel's name (under any video, on a watch page, or in Subscriptions) to see its
  newest ~30 videos and subscribe or unsubscribe there. On the TV, open a video and pick the channel name below it.
- **Feed:** newest uploads from your subscriptions. The **Shorts** switch (next to Refresh) hides or shows their Shorts.
- **For you:** suggestions worked out on this computer from your own history (see below).
- **New to you:** channels you've never watched or subscribed to, found through the ones you like. One video per channel.
- **Shorts:** full screen, one at a time, from all over YouTube (not your subscriptions), skipping ones you've
  watched. Swipe up for the next one, down to go back, tap to pause; each one replays until you move on. On a computer use the mouse wheel,
  arrow keys or the ▲▼ buttons; on the TV, the remote's Down / Up. ✕ (top left) or Back closes it.
  Pasting a youtube.com/shorts/ link opens it here.
- **Videos start with captions off.** Where the browser blocks autoplay (iPhone, Safari), press play. In Shorts,
  a blocked Short starts muted with a "Tap for sound" button.
- **Home-screen icon:** Safari → Share → Add to Home Screen (iPhone), or Chrome → ⋮ → Add to Home screen (Android).
- **History:** remove single videos or clear it all. Removing a video also removes its influence on suggestions.

## How it works
- **Subscriptions:** each channel's public RSS feed. If YouTube's RSS is down (it is as of Nov 2026),
  YakTube reads the channel's normal videos page instead. Checked every 30 minutes.
- **Search and "Up next":** read from YouTube's public pages (the same data a logged-out browser sees).
- **Playback:** YouTube's privacy-mode embedded player (youtube-nocookie.com). Nothing to download or extract.
- **For you:** ranks the "up next" videos of your last 25 watches (recent ones count more) plus fresh unwatched
  uploads from your subscriptions, boosted for channels you watch often. At most 3 per channel; never anything you've seen.
- **Shorts:** once an hour YakTube opens youtube.com/shorts like a visitor who isn't logged in and keeps the ~40
  Shorts YouTube offers (ones that can't play outside YouTube are skipped), up to the newest 200. It uses YouTube's
  own internal request, so YouTube could break it. Watching Shorts doesn't change your For you / New to you suggestions.
- **Tidy-up:** once a day YakTube forgets old "Up next" lists and videos nobody needs (unwatched, not from
  your subscriptions, older than 60 days; subscription videos after a year), so the database stays a few MB.
  Profiles, subscriptions and watch history are never touched.
- **Data:** everything is in `yaktube.db`. Back it up by copying that file
  (Docker: `docker cp yaktube:/data/yaktube.db .`).

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
