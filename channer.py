"""Scrapes video posts from 4chan boards via the read-only JSON API.

API rules (https://github.com/4chan/4chan-API): max one request per second,
use If-Modified-Since, and don't refetch a thread more than once per 10s.
Threads whose `last_modified` in threads.json hasn't changed since the last
scrape aren't fetched at all.
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
        # board index url -> (Last-Modified header, parsed json)
        self._cache = {}
        # board -> {thread number: (last_modified, ids of the thread's clips)}
        self._threads = {}
        self._lock = threading.Lock()
        # progress of the current scrape, shown to viewers while the library is empty
        self.status = {"scanning": False, "board": None, "done": 0, "total": 0,
                       "error": None, "finished": None}

    def _get_json(self, url, cache=True):
        """GET a JSON document, honouring rate limits and If-Modified-Since
        (for documents fetched with `cache`).

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
            self.status["error"] = "{} ({})".format(type(exc).__name__, url)
            return None
        finally:
            self._last_request = time.monotonic()

        if resp.status_code == 304 and cached:
            return cached[1]
        if resp.status_code != 200:
            log.warning("request to %s returned HTTP %s", url, resp.status_code)
            self.status["error"] = "HTTP {} ({})".format(resp.status_code, url)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("invalid JSON from %s", url)
            return None
        if cache and "Last-Modified" in resp.headers:
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
            "md5": post.get("md5"),
            "width": post.get("w"),
            "height": post.get("h"),
            "size": post.get("fsize"),
        }

    def scrape_board(self, board, save):
        """Return the ids of every clip currently posted on `board`, or None if
        the board index couldn't be fetched (so callers don't mistake an outage
        for an empty board). Each changed thread's clips are passed to
        `save(videos)` as it is read, which returns the ids they're stored under."""
        index = self._get_json("{}/{}/threads.json".format(API_URL, board))
        if index is None:
            return None
        threads = [(t["no"], t.get("last_modified")) for page in index for t in page.get("threads", [])]
        self.status.update(board=board, done=0, total=len(threads))
        known = self._threads.get(board, {})
        current = {}
        for number, modified in threads:
            previous = known.get(number)
            if previous and modified is not None and previous[0] == modified:
                current[number] = previous
                self.status["done"] += 1
                continue
            data = self._get_json("{}/{}/thread/{}.json".format(API_URL, board, number), cache=False)
            self.status["done"] += 1
            if not data or not data.get("posts"):
                if previous:
                    # couldn't read it this time: keep its clips, retry next scrape
                    current[number] = (None, previous[1])
                continue
            op = data["posts"][0]
            found = [v for v in (self._video_from_post(board, op, post) for post in data["posts"]) if v]
            current[number] = (modified, save(found) if found else [])
        self._threads[board] = current
        return [video_id for _, ids in current.values() for video_id in ids]

    def update(self, db):
        """Scrape all boards into the database. Concurrent calls are serialized.

        Returns the number of videos found.
        """
        with self._lock:
            self.status.update(scanning=True, error=None)
            found = 0
            try:
                for board in self.boards:
                    # clips are saved thread by thread so a fresh install has something
                    # to show within seconds instead of after the whole board is read
                    ids = self.scrape_board(board, db.save_videos)
                    if ids is None:
                        log.warning("couldn't fetch /%s/, keeping its clips as they are", board)
                        continue
                    db.mark_missing(board, ids)
                    found += len(set(ids))
                    log.info("/%s/: %d videos", board, len(set(ids)))
            finally:
                self.status.update(scanning=False, finished=time.time())
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
