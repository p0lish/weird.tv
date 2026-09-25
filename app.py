import logging
import os
import random
import threading
import time

import requests
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, stream_with_context

from channer import Channer, read_playlist

log = logging.getLogger(__name__)

PLAYLIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "videos.json")
# headers copied from the upstream media response to the client
PROXY_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                 "Last-Modified", "ETag")


class Playlist:
    """The current set of videos, reloaded whenever the playlist file changes.

    Videos are served from a shuffled "bag" so nothing repeats until every
    video has been shown once.
    """

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self._videos = {}
        self._bag = []
        self._lock = threading.Lock()

    def _reload(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        self._videos = {v["id"]: v for v in read_playlist(self.path)}
        self._bag = []

    def next(self):
        with self._lock:
            self._reload()
            if not self._bag:
                self._bag = list(self._videos)
                random.shuffle(self._bag)
            while self._bag:
                video = self._videos.get(self._bag.pop())
                if video:
                    return video
            return None

    def get(self, video_id):
        with self._lock:
            self._reload()
            return self._videos.get(video_id)

    def discard(self, video_id):
        """Forget a video whose upstream file is gone (thread pruned)."""
        with self._lock:
            self._videos.pop(video_id, None)

    def __len__(self):
        with self._lock:
            self._reload()
            return len(self._videos)


def public_video(video):
    """The video metadata exposed to the browser."""
    thread = video.get("thread")
    return {
        "id": video["id"],
        "src": "/video/" + video["id"],
        "board": video.get("board"),
        "title": video.get("title") or "",
        "filename": video.get("filename") or "",
        "source": "https://boards.4chan.org/{}/thread/{}".format(video["board"], thread) if thread else None,
    }


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(
        BOARDS=os.environ.get("WEIRDTV_BOARDS", "wsg").split(","),
        PLAYLIST_PATH=os.environ.get("WEIRDTV_PLAYLIST", PLAYLIST_PATH),
        # minutes between automatic scrapes, 0 disables the background refresher
        REFRESH_MINUTES=float(os.environ.get("WEIRDTV_REFRESH_MINUTES", "30")),
        # secret required by /___update/, the endpoint is disabled when unset
        UPDATE_TOKEN=os.environ.get("WEIRDTV_UPDATE_TOKEN"),
    )
    app.config.update(config or {})

    playlist = Playlist(app.config["PLAYLIST_PATH"])
    channer = Channer(boards=app.config["BOARDS"], playlist_path=app.config["PLAYLIST_PATH"])
    http = requests.Session()
    app.extensions["weirdtv"] = {"playlist": playlist, "channer": channer, "http": http}

    def run_update():
        try:
            channer.update()
        except Exception:
            log.exception("playlist update failed")

    def refresher(interval):
        while True:
            run_update()
            time.sleep(interval)

    if app.config["REFRESH_MINUTES"] > 0:
        threading.Thread(target=refresher, args=(app.config["REFRESH_MINUTES"] * 60,),
                         daemon=True, name="playlist-refresher").start()

    @app.route("/")
    def tv():
        return render_template("index.html")

    @app.route("/api/next")
    def next_video():
        video = playlist.next()
        if video is None:
            return jsonify(error="the playlist is empty, try again in a minute"), 503
        return jsonify(public_video(video))

    @app.route("/api/status")
    def status():
        return jsonify(videos=len(playlist), boards=app.config["BOARDS"])

    @app.route("/video/<video_id>")
    def video(video_id):
        # Only proxy URLs from our own playlist so this can't be used as an open proxy.
        entry = playlist.get(video_id)
        if entry is None:
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
            playlist.discard(video_id)
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
        return response

    @app.route("/video.webm")
    def legacy_video():
        video = playlist.next()
        if video is None:
            abort(503)
        return redirect("/video/" + video["id"])

    @app.route("/___update/", methods=["POST"])
    def update():
        token = app.config["UPDATE_TOKEN"]
        if not token or request.headers.get("X-Update-Token") != token:
            abort(403)
        threading.Thread(target=run_update, daemon=True).start()
        return jsonify(status="update started", videos=len(playlist)), 202

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8088")))
