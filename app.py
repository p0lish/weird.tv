import datetime
import logging
import os
import re
import threading
import time
from zoneinfo import ZoneInfo

import requests
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request, send_file,
                   stream_with_context)

from archiver import Archiver
from channels import load_channels, public_channel
from channer import Channer, read_playlist
from db import Database

log = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# headers copied from the upstream media response to the client
PROXY_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                 "Last-Modified", "ETag")
CLIENT_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MIMETYPES = {".webm": "video/webm", ".mp4": "video/mp4"}


def env(name, default):
    return os.environ.get("WEIRDTV_" + name, default)


def parse_quiet_hours(value):
    """"01:00-06:30" -> ((1, 0), (6, 30)); empty -> None."""
    if not value:
        return None

    def hours_minutes(part):
        hours, _, minutes = part.strip().partition(":")
        return int(hours), int(minutes or 0)

    start, end = value.split("-")
    return hours_minutes(start), hours_minutes(end)


def off_air_until(quiet_hours, now):
    """End of the quiet period as "HH:MM" if `now` falls inside it, else None."""
    if not quiet_hours:
        return None
    (sh, sm), (eh, em) = quiet_hours
    minutes, start, end = now.hour * 60 + now.minute, sh * 60 + sm, eh * 60 + em
    inside = start <= minutes < end if start <= end else (minutes >= start or minutes < end)
    return "{:02d}:{:02d}".format(eh, em) if inside else None


def create_app(config=None):
    app = Flask(__name__)
    data_dir = env("DATA_DIR", os.path.join(BASE_DIR, "data"))
    app.config.update(
        BOARDS=env("BOARDS", "wsg").split(","),
        DATABASE=env("DATABASE", os.path.join(data_dir, "weirdtv.db")),
        ARCHIVE_DIR=env("ARCHIVE_DIR", os.path.join(data_dir, "archive")),
        # a clip is archived once it reaches this score or this many unique views
        ARCHIVE_MIN_SCORE=int(env("ARCHIVE_MIN_SCORE", "2")),
        ARCHIVE_MIN_PLAYS=int(env("ARCHIVE_MIN_PLAYS", "50")),
        ARCHIVE_MAX_MB=int(env("ARCHIVE_MAX_MB", "2048")),
        CHANNELS_FILE=env("CHANNELS", None),
        # minutes between automatic scrapes/archive runs, 0 disables the background jobs
        REFRESH_MINUTES=float(env("REFRESH_MINUTES", "30")),
        ARCHIVE_MINUTES=float(env("ARCHIVE_MINUTES", "5")),
        # secret required by /___update/, the endpoint is disabled when unset
        UPDATE_TOKEN=env("UPDATE_TOKEN", None),
        # e.g. "02:00-06:00": the channel goes off air in this window
        QUIET_HOURS=env("QUIET_HOURS", ""),
        TIMEZONE=env("TIMEZONE", "UTC"),
        LEGACY_PLAYLIST=os.path.join(BASE_DIR, "static", "videos.json"),
    )
    app.config.update(config or {})

    db = Database(app.config["DATABASE"])
    channels = load_channels(app.config["CHANNELS_FILE"])
    by_slug = {c["slug"]: c for c in channels}
    mix = next(c for c in channels if not c.get("live"))
    channer = Channer(boards=app.config["BOARDS"])
    archiver = Archiver(db, app.config["ARCHIVE_DIR"],
                        min_score=app.config["ARCHIVE_MIN_SCORE"],
                        min_plays=app.config["ARCHIVE_MIN_PLAYS"],
                        max_bytes=app.config["ARCHIVE_MAX_MB"] * 1024 ** 2)
    quiet_hours = parse_quiet_hours(app.config["QUIET_HOURS"])
    timezone = ZoneInfo(app.config["TIMEZONE"])
    http = requests.Session()
    app.extensions["weirdtv"] = {"db": db, "channer": channer, "archiver": archiver, "http": http}

    if os.path.exists(app.config["LEGACY_PLAYLIST"]):
        imported = db.import_legacy(read_playlist(app.config["LEGACY_PLAYLIST"]))
        if imported:
            log.info("imported %d clips from the old videos.json", imported)

    def scrape():
        try:
            channer.update(db)
            db.prune()
        except Exception:
            log.exception("scrape failed")

    def archive():
        try:
            archiver.run()
        except Exception:
            log.exception("archive run failed")

    def every(minutes, job, name, empty_retry_minutes=None):
        def loop():
            while True:
                job()
                # retry sooner while there is nothing to show at all
                wait = empty_retry_minutes if empty_retry_minutes and db.count() == 0 else minutes
                time.sleep(min(minutes, wait) * 60)
        threading.Thread(target=loop, daemon=True, name=name).start()

    if app.config["REFRESH_MINUTES"] > 0:
        every(app.config["REFRESH_MINUTES"], scrape, "scraper", empty_retry_minutes=1)
    if app.config["ARCHIVE_MINUTES"] > 0:
        every(app.config["ARCHIVE_MINUTES"], archive, "archiver")

    def client_id():
        value = request.headers.get("X-Client-Id", "")
        return value if CLIENT_ID.match(value) else None

    def require_client():
        client = client_id()
        if not client:
            abort(400, "missing X-Client-Id header")
        return client

    def off_air():
        until = off_air_until(quiet_hours, datetime.datetime.now(timezone))
        if until:
            return {"offair": True, "reason": "quiet", "message": "OFF AIR - BACK AT " + until}
        return None

    def no_signal(channel):
        """Why `channel` has nothing to show, and how soon the player should retry."""
        if db.count() == 0:
            scraper = channer.status
            if scraper["scanning"] and scraper["total"]:
                return {"offair": True, "reason": "scanning", "retry": 5,
                        "message": "TUNING IN... SCANNING /{}/ {}/{}".format(
                            scraper["board"], scraper["done"], scraper["total"])}
            if scraper["scanning"] or scraper["finished"] is None:
                return {"offair": True, "reason": "scanning", "retry": 5, "message": "TUNING IN..."}
            if scraper["error"]:
                return {"offair": True, "reason": "error", "retry": 30,
                        "message": "NO SIGNAL - CAN'T REACH 4CHAN: " + scraper["error"]}
            return {"offair": True, "reason": "empty", "retry": 30,
                    "message": "NO SIGNAL - NO VIDEOS ON /" + "/, /".join(app.config["BOARDS"]) + "/"}
        return {"offair": True, "reason": "empty", "retry": 30, "message": "NO SIGNAL ON " + channel["name"]}

    def public_video(video, client=None):
        """The video metadata exposed to the browser."""
        thread = video.get("thread")
        return {
            "id": video["id"],
            "src": "/video/" + video["id"],
            "share": "/v/" + video["id"],
            "board": video["board"],
            "title": video.get("title") or "",
            "filename": video.get("filename") or "",
            "source": "https://boards.4chan.org/{}/thread/{}".format(video["board"], thread) if thread else None,
            "score": video.get("score", 0),
            "plays": video.get("plays", 0),
            "duration": video.get("duration"),
            "archived": bool(video.get("archived_path")),
            "vote": db.my_vote(video["id"], client),
        }

    def download_name(video):
        """A filename for saving the clip: its original name with the right extension."""
        ext = os.path.splitext(video["id"])[1]
        name = os.path.splitext(os.path.basename(video.get("filename") or ""))[0].strip()
        return (name or os.path.splitext(video["id"])[0]) + ext

    def get_video_or_404(video_id):
        video = db.get(video_id)
        if video is None:
            abort(404)
        return video

    @app.route("/")
    def tv():
        return render_template("index.html", initial=None)

    @app.route("/v/<video_id>")
    def shared(video_id):
        video = db.get(video_id)
        if video is None or (video["gone"] and not video["archived_path"]):
            return redirect("/")
        return render_template("index.html", initial=public_video(video))

    @app.route("/api/channels")
    def list_channels():
        return jsonify([public_channel(c) for c in channels])

    @app.route("/api/next")
    def next_video():
        status = off_air()
        if status:
            return jsonify(status)
        channel = by_slug.get(request.args.get("channel", mix["slug"]), mix)
        if channel.get("live"):
            channel = mix
        exclude = [x for x in request.args.get("exclude", "").split(",") if x][:200]
        video = db.pick(channel, exclude=exclude)
        if video is None:
            return jsonify(no_signal(channel))
        return jsonify(public_video(video, client_id()) | {"channel": channel["slug"]})

    @app.route("/api/live")
    def live():
        status = off_air()
        if status:
            return jsonify(status)
        video, offset = db.live(mix)
        if video is None:
            return jsonify(no_signal(by_slug["live"]))
        return jsonify(public_video(video, client_id()) | {"channel": "live", "offset": offset,
                                                           "server_time": time.time()})

    @app.route("/api/videos/<video_id>")
    def video_info(video_id):
        return jsonify(public_video(get_video_or_404(video_id), client_id()))

    @app.route("/api/videos/<video_id>/vote", methods=["POST"])
    def vote(video_id):
        client = require_client()
        get_video_or_404(video_id)
        value = (request.get_json(silent=True) or {}).get("value")
        if value not in (-1, 0, 1):
            abort(400, "value must be -1, 0 or 1")
        return jsonify(score=db.vote(video_id, client, value), vote=value)

    @app.route("/api/videos/<video_id>/view", methods=["POST"])
    def view(video_id):
        client = require_client()
        get_video_or_404(video_id)
        return jsonify(plays=db.record_view(video_id, client))

    @app.route("/api/videos/<video_id>/duration", methods=["POST"])
    def duration(video_id):
        get_video_or_404(video_id)
        value = (request.get_json(silent=True) or {}).get("duration")
        if not isinstance(value, (int, float)) or not 0.5 <= value <= 3600:
            abort(400, "invalid duration")
        db.set_duration(video_id, float(value))
        return "", 204

    @app.route("/api/heartbeat", methods=["POST"])
    def heartbeat():
        client = require_client()
        channel = (request.get_json(silent=True) or {}).get("channel")
        if channel not in by_slug:
            channel = mix["slug"]
        total, same = db.heartbeat(client, channel)
        return jsonify(watching=total, channel_watching=same)

    @app.route("/api/status")
    def status():
        return jsonify(db.stats() | {"boards": app.config["BOARDS"],
                                     "offair": bool(off_air()),
                                     "scraper": channer.status})

    @app.route("/video/<video_id>")
    def video(video_id):
        # Only serve clips from our own library so this can't be used as an open proxy.
        entry = get_video_or_404(video_id)
        # ?download=1 asks the browser to save the clip instead of playing it
        download = download_name(entry) if request.args.get("download") else None
        if entry["archived_path"]:
            path = archiver.path(entry["archived_path"])
            if os.path.exists(path):
                return send_file(path, mimetype=MIMETYPES.get(os.path.splitext(path)[1]),
                                 conditional=True, max_age=86400,
                                 as_attachment=bool(download), download_name=download)
            db.unarchive(video_id)
        if entry["gone"]:
            abort(404)
        headers = {}
        if "Range" in request.headers:
            headers["Range"] = request.headers["Range"]
        try:
            upstream = http.get(entry["url"], headers=headers, stream=True, timeout=20)
        except requests.RequestException:
            abort(502)
        if upstream.status_code == 404:
            upstream.close()
            db.mark_gone(video_id)
            abort(404)
        if upstream.status_code not in (200, 206):
            upstream.close()
            abort(502)

        def body():
            try:
                yield from upstream.iter_content(chunk_size=64 * 1024)
            finally:
                upstream.close()

        response = Response(stream_with_context(body()), status=upstream.status_code,
                            direct_passthrough=True)
        for name in PROXY_HEADERS:
            if name in upstream.headers:
                response.headers[name] = upstream.headers[name]
        response.headers["Cache-Control"] = "public, max-age=86400"
        if download:
            response.headers.set("Content-Disposition", "attachment", filename=download)
        return response

    @app.route("/video.webm")
    def legacy_video():
        video = db.pick(mix)
        if video is None:
            abort(503)
        return redirect("/video/" + video["id"])

    @app.route("/___update/", methods=["POST"])
    def update():
        token = app.config["UPDATE_TOKEN"]
        if not token or request.headers.get("X-Update-Token") != token:
            abort(403)
        threading.Thread(target=lambda: (scrape(), archive()), daemon=True).start()
        return jsonify(status="update started", videos=db.count()), 202

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8088")),
                     threaded=True)
