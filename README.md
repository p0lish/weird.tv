# WEIRD.TV
Creepy funny weird tv channel. Enjoy.

Plays random videos from 4chan boards (by default the work-safe `/wsg/`) as if you
were flipping through channels on an old CRT: themed channels, a shared live broadcast,
votes, share links and a vault that keeps popular clips after their threads die.

## Running

```sh
pip install -r requirements.txt
python app.py                     # development server on http://127.0.0.1:8088
gunicorn -c gunicorn.conf.py wsgi:app   # production
```

or with Docker:

```sh
docker compose up -d              # http://localhost:8088, data in the weirdtv-data volume
```

The library is scraped in the background on startup and every 30 minutes after that.
Scraping follows the 4chan API rules (max 1 request/second, `If-Modified-Since`), so the
first full scrape of a board takes a few minutes. An old `static/videos.json` playlist is
imported automatically on first start.

## Deploying on a Raspberry Pi

Requirements: 64-bit Raspberry Pi OS, Python 3.11 or newer. This runs it on the home
network only; there is no tunnel and nothing to forward on the router.

```bash
git clone https://github.com/p0lish/weird.tv.git /opt/weird-tv
cd /opt/weird-tv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo mkdir -p /var/lib/weird-tv && sudo chown "$USER" /var/lib/weird-tv
cp .env.example .env   # then edit: WEIRDTV_DATA_DIR on the NVMe drive, time zone
```

**systemd** (`/etc/systemd/system/weird-tv.service`):

```ini
[Unit]
Description=Weird TV
After=network-online.target

[Service]
WorkingDirectory=/opt/weird-tv
EnvironmentFile=/opt/weird-tv/.env
ExecStart=/opt/weird-tv/.venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=on-failure
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now weird-tv
journalctl -u weird-tv -f
```

Open `http://<pi-hostname>.local:8088` from any device on the network. To update:

```bash
cd /opt/weird-tv && git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart weird-tv
```

## Storage

Everything lives in the data directory (`./data`, or `/data` in Docker):

- `weirdtv.db`: SQLite database with clips, votes, unique views, viewers and the live channel state
- `archive/<board>/<file>`: **the vault**, local copies of popular clips. A clip is archived once
  it reaches `WEIRDTV_ARCHIVE_MIN_SCORE` votes or `WEIRDTV_ARCHIVE_MIN_PLAYS` unique views, and keeps
  playing after 4chan deletes its thread. When the vault is over `WEIRDTV_ARCHIVE_MAX_MB`, the least
  popular clips are evicted first.

Clips whose thread died and that weren't archived are forgotten after a week.

## Channels

| # | Channel | |
| --- | --- | --- |
| 0 | LIVE | everyone watches the same clip at the same moment, no skipping |
| 1 | WEIRD MIX | everything |
| 2 | YLYL | "you laugh you lose" threads |
| 3 | MUSIC | YGYL, music, dance |
| 4 | ANIMALS | cats, dogs & critters |
| 5 | WCGW | what could go wrong / fails |
| 6 | TOP RATED | upvoted clips |
| 7 | FRESH | clips found in the last 24 hours |
| 8 | THE VAULT | archived clips |

Channels match keywords in the thread title and file name. Define your own with a JSON file
(same shape as `DEFAULT_CHANNELS` in `channels.py`, numbers 1-9) and point
`WEIRDTV_CHANNELS` at it.

Upvoted clips come up more often, downvoted ones less, and clips at -3 or below stop airing.
Downvoting a clip also skips it.

## Controls

| Key | Action |
| --- | --- |
| click, `→`, space, `n` | next clip |
| `←`, `p` | previous clip |
| `↑` / `↓` | channel up / down |
| `0`-`9` | tune to a channel |
| `+` / `-` | like / dislike (press again to take the vote back) |
| `c` | copy a share link (`/v/<clip>`) |
| `m` | mute / unmute |
| `f` | fullscreen |
| `i` | show channel info |
| `?`, `h` | menu (also the ⚙ button, which is the way in on phones) |
| `s` | scanlines |
| `t` | CRT tube shader (curvature, colour fringing, glow, flicker) |
| `g` | glitchy static between clips (plain test card when off) |
| `v` | VHS overlay (PLAY/LIVE and a camcorder clock) |
| `z` | static sound |
| `b` | channel change blip |
| `e` | all effects off / on |

On touch screens: tap for the next clip, swipe left/right for next/previous, swipe up/down
to change channel.

Effect settings are remembered per browser. They can also be forced with a URL parameter,
e.g. `/?fx=none` or `/?fx=scanlines,crt` (`all` enables everything). Motion effects start
off for visitors whose system asks for reduced motion.

## Configuration (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `WEIRDTV_BOARDS` | `wsg` | comma-separated boards to scrape |
| `WEIRDTV_DATA_DIR` | `./data` | where the database and vault live |
| `WEIRDTV_DATABASE` | `<data>/weirdtv.db` | |
| `WEIRDTV_ARCHIVE_DIR` | `<data>/archive` | the vault folder |
| `WEIRDTV_ARCHIVE_MIN_SCORE` | `2` | votes needed to archive a clip |
| `WEIRDTV_ARCHIVE_MIN_PLAYS` | `50` | unique views needed to archive a clip |
| `WEIRDTV_ARCHIVE_MAX_MB` | `2048` | vault size limit |
| `WEIRDTV_CHANNELS` | unset | JSON file with custom channels |
| `WEIRDTV_REFRESH_MINUTES` | `30` | minutes between scrapes, `0` disables the scraper |
| `WEIRDTV_ARCHIVE_MINUTES` | `5` | minutes between vault runs, `0` disables the archiver |
| `WEIRDTV_QUIET_HOURS` | unset | e.g. `02:00-06:00`: the station goes off air (test card + tone) |
| `WEIRDTV_TIMEZONE` | `UTC` | time zone for the quiet hours |
| `WEIRDTV_UPDATE_TOKEN` | unset | enables `POST /___update/` with an `X-Update-Token` header |
| `HOST` / `PORT` | `127.0.0.1` / `8088` | (`gunicorn.conf.py` defaults to `0.0.0.0`) |

The background jobs run inside the web process, so run a single gunicorn worker (the default
config uses one worker with 32 threads). SQLite runs in WAL mode, so extra processes pointed
at the same data directory are safe, but set `WEIRDTV_REFRESH_MINUTES=0` and
`WEIRDTV_ARCHIVE_MINUTES=0` on them.

## API

- `GET /api/channels`: channel list
- `GET /api/next?channel=<slug>&exclude=<id,id>`: a clip for the channel, or `{"offair": true, "message": ...}`
- `GET /api/live`: the clip the live channel is showing and the `offset` into it
- `GET /api/videos/<id>`: clip details, including the caller's vote
- `POST /api/videos/<id>/vote` `{"value": -1|0|1}`, `POST /api/videos/<id>/view`,
  `POST /api/heartbeat` `{"channel": ...}`: need an `X-Client-Id` header (the player generates one)
- `GET /api/status`: library and vault stats
- `GET /video/<id>`: streams a clip from the vault or 4chan (supports `Range` requests)
- `GET /v/<id>`: the player starting with that clip

Client ids are anonymous and self-assigned, so votes and view counts are for fun, not
tamper-proof.

## Tests

```sh
pip install pytest
python -m pytest tests
```
