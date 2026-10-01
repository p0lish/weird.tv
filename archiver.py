"""Keeps copies of popular clips so they outlive their 4chan threads."""
import base64
import hashlib
import logging
import os
import re
import tempfile
import threading
import time

import requests

from channer import USER_AGENT

log = logging.getLogger(__name__)

VIDEO_ID = re.compile(r"^([a-z0-9]+)-(\d+)\.(webm|mp4)$")


def archive_relpath(video_id):
    """`wsg-123.webm` -> `wsg/123.webm`; raises ValueError for anything else."""
    match = VIDEO_ID.match(video_id)
    if not match:
        raise ValueError("bad video id: %r" % video_id)
    board, tim, ext = match.groups()
    return os.path.join(board, "{}.{}".format(tim, ext))


class Archiver:
    def __init__(self, db, directory, min_score=2, min_plays=50, max_bytes=2 * 1024 ** 3,
                 max_file_bytes=64 * 1024 ** 2, min_interval=1.0, session=None, cache=None):
        self.db = db
        # a MediaCache whose copies are moved into the vault instead of downloading again
        self.cache = cache
        self.directory = directory
        self.min_score = min_score
        self.min_plays = min_plays
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes
        self.min_interval = min_interval
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._lock = threading.Lock()
        os.makedirs(directory, exist_ok=True)

    def path(self, relpath):
        return os.path.join(self.directory, relpath)

    def _download(self, video):
        """Copy a clip into the vault; returns False if 4chan wasn't asked for it."""
        relpath = archive_relpath(video["id"])
        target = self.path(relpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        size = self.cache.take(video["id"], target) if self.cache else None
        if size is not None:
            self.db.mark_archived(video["id"], relpath, size)
            log.info("archived %s from the cache (%d bytes)", video["id"], size)
            return False
        try:
            resp = self.session.get(video["url"], stream=True, timeout=30)
        except requests.RequestException as exc:
            log.warning("archiving %s failed: %s", video["id"], exc)
            return True
        with resp:
            if resp.status_code == 404:
                self.db.mark_gone(video["id"])
                return True
            if resp.status_code != 200:
                log.warning("archiving %s failed: HTTP %s", video["id"], resp.status_code)
                return True
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".part")
            size = 0
            try:
                with os.fdopen(fd, "wb") as f:
                    for chunk in resp.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > self.max_file_bytes:
                            raise ValueError("clip larger than the per-file limit")
                        f.write(chunk)
                os.replace(tmp, target)
            except (OSError, ValueError, requests.RequestException) as exc:
                log.warning("archiving %s failed: %s", video["id"], exc)
                if os.path.exists(tmp):
                    os.remove(tmp)
                return True
        self.db.mark_archived(video["id"], relpath, size)
        log.info("archived %s (%d bytes)", video["id"], size)
        return True

    def _backfill_md5(self):
        """Hash vault files of clips stored before md5s were recorded; copies of
        the same file are merged and the spare file is removed by _sweep."""
        for entry in self.db.archived_without_md5():
            digest = hashlib.md5()
            try:
                with open(self.path(entry["archived_path"]), "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
            kept = self.db.set_md5(entry["id"], base64.b64encode(digest.digest()).decode())
            if kept != entry["id"]:
                log.info("merged %s into %s (same file)", entry["id"], kept)

    def _sweep(self):
        """Delete vault files no clip refers to (left over from merges)."""
        referenced = self.db.archived_paths()
        for root, _, names in os.walk(self.directory):
            for name in names:
                relpath = os.path.relpath(os.path.join(root, name), self.directory)
                board = os.path.dirname(relpath)
                if relpath in referenced or not VIDEO_ID.match("{}-{}".format(board, name)):
                    continue
                os.remove(os.path.join(root, name))
                log.info("removed %s from the vault (no longer used)", relpath)

    def _enforce_limit(self):
        archived = self.db.archived_by_rank()
        total = sum(a["archived_bytes"] or 0 for a in archived)
        for entry in archived:
            if total <= self.max_bytes:
                break
            try:
                os.remove(self.path(entry["archived_path"]))
            except FileNotFoundError:
                pass
            self.db.unarchive(entry["id"])
            total -= entry["archived_bytes"] or 0
            log.info("evicted %s from the archive", entry["id"])

    def run(self):
        """Archive every clip that became popular since the last run."""
        with self._lock:
            self._backfill_md5()
            for video in self.db.archive_candidates(self.min_score, self.min_plays):
                if self._download(video):
                    time.sleep(self.min_interval)
            self._enforce_limit()
            self._sweep()
