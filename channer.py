"""Scrapes video posts from 4chan boards via the read-only JSON API.

API rules (https://github.com/4chan/4chan-API): max one request per second,
use If-Modified-Since, and don't refetch a thread more than once per 10s.
"""
import json
import logging
import threading
import time

import requests

log = logging.getLogger(__name__)

API_URL = "https://a.4cdn.org"
MEDIA_URL = "https://i.4cdn.org"
USER_AGENT = "weird.tv/2.0 (+https://github.com/p0lish/weird.tv)"
VIDEO_EXTENSIONS = (".webm", ".mp4")


class Channer:
    def __init__(self, boards=("wsg",), min_interval=1.0, timeout=20, session=None):
        self.boards = list(boards)
        self.min_interval = min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last_request = 0.0
        # url -> (last_modified header, parsed json), survives between updates
        self._cache = {}
        self._lock = threading.Lock()

    def _get_json(self, url):
        """GET a JSON document, honouring rate limits and If-Modified-Since.

        Returns the parsed JSON, or None if the request failed.
        """
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        headers = {}
        cached = self._cache.get(url)
        if cached:
            headers["If-Modified-Since"] = cached[0]
        try:
            resp = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("request to %s failed: %s", url, exc)
            return None
        finally:
            self._last_request = time.monotonic()

        if resp.status_code == 304 and cached:
            return cached[1]
        if resp.status_code != 200:
            log.warning("request to %s returned HTTP %s", url, resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("invalid JSON from %s", url)
            return None
        if "Last-Modified" in resp.headers:
            self._cache[url] = (resp.headers["Last-Modified"], data)
        return data

    @staticmethod
    def _video_from_post(board, thread, post):
        ext = post.get("ext")
        if "tim" not in post or ext not in VIDEO_EXTENSIONS or post.get("filedeleted"):
            return None
        return {
            "id": "{}-{}{}".format(board, post["tim"], ext),
            "url": "{}/{}/{}{}".format(MEDIA_URL, board, post["tim"], ext),
            "board": board,
            "thread": thread["no"],
            "title": thread.get("sub") or post.get("filename", ""),
            "filename": post.get("filename", "") + ext,
            "width": post.get("w"),
            "height": post.get("h"),
            "size": post.get("fsize"),
        }

    def scrape_board(self, board):
        """Return every video currently posted on `board`, or None if the board
        index couldn't be fetched (so callers don't mistake an outage for an
        empty board)."""
        threads = self._get_json("{}/{}/threads.json".format(API_URL, board))
        if threads is None:
            return None
        videos = []
        for page in threads:
            for thread in page.get("threads", []):
                data = self._get_json("{}/{}/thread/{}.json".format(API_URL, board, thread["no"]))
                if not data or not data.get("posts"):
                    continue
                op = data["posts"][0]
                for post in data["posts"]:
                    video = self._video_from_post(board, op, post)
                    if video:
                        videos.append(video)
        # drop cache entries of threads that have since been archived/pruned
        live = {"{}/{}/thread/{}.json".format(API_URL, board, t["no"])
                for page in threads for t in page.get("threads", [])}
        prefix = "{}/{}/thread/".format(API_URL, board)
        for url in [u for u in self._cache if u.startswith(prefix) and u not in live]:
            del self._cache[url]
        return videos

    def update(self, db):
        """Scrape all boards into the database. Concurrent calls are serialized.

        Returns the number of videos found.
        """
        with self._lock:
            found = 0
            for board in self.boards:
                videos = self.scrape_board(board)
                if videos is None:
                    log.warning("couldn't fetch /%s/, keeping its clips as they are", board)
                    continue
                unique = list({v["id"]: v for v in videos}.values())
                db.sync_board(board, unique)
                found += len(unique)
                log.info("/%s/: %d videos", board, len(unique))
            return found


def read_playlist(path):
    """Load a legacy videos.json playlist (a list of URLs or of video dicts)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    videos = []
    for item in data.get("videos", []):
        if isinstance(item, str):
            parts = item.rstrip("/").split("/")
            item = {"id": "{}-{}".format(parts[-2], parts[-1]), "url": item,
                    "board": parts[-2], "thread": None, "title": "", "filename": parts[-1]}
        videos.append(item)
    return videos
