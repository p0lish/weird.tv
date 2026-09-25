# WEIRD.TV
Creepy funny weird tv channel. Enjoy.

Plays random videos from 4chan boards (by default the work-safe `/wsg/`) as if you
were flipping through channels on an old CRT.

## Running

```sh
pip install -r requirements.txt
python app.py            # http://127.0.0.1:8088
```

The playlist is scraped in the background on startup and every 30 minutes after that.
Scraping follows the 4chan API rules (max 1 request/second, `If-Modified-Since`), so the
first full scrape of a board takes a few minutes.

## Controls

| Key | Action |
| --- | --- |
| click, `→`, `↑`, space, `n` | next channel |
| `←`, `↓`, `p` | previous channel |
| `m` | mute / unmute |
| `f` | fullscreen |
| `i` | show channel info |
| `s` | scanlines on/off |
| `g` | glitchy static between channels on/off (plain test card when off) |
| `z` | static sound on/off |
| `e` | all effects off / on |

Effect settings are remembered per browser. They can also be forced with a URL parameter,
e.g. `/?fx=none` or `/?fx=scanlines,noise` (`all` enables everything). The glitch effect
starts off for visitors whose system asks for reduced motion.

## Configuration (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `WEIRDTV_BOARDS` | `wsg` | comma-separated boards to scrape |
| `WEIRDTV_REFRESH_MINUTES` | `30` | minutes between scrapes, `0` disables the background scraper |
| `WEIRDTV_UPDATE_TOKEN` | unset | enables `POST /___update/` with an `X-Update-Token` header |
| `WEIRDTV_PLAYLIST` | `static/videos.json` | where the playlist is stored |
| `HOST` / `PORT` | `127.0.0.1` / `8088` | |

When running several workers (e.g. gunicorn), set `WEIRDTV_REFRESH_MINUTES=0` on all but
one of them, or run the scraper from cron with `python -c "from channer import Channer; Channer().update()"`.

## API

- `GET /api/next`: the next video in the shuffled playlist (`id`, `src`, `title`, `board`, `source`)
- `GET /api/status`: playlist size and boards
- `GET /video/<id>`: streams a playlist video (supports `Range` requests)

## Tests

```sh
pip install pytest
python -m pytest tests
```
