"""Disk cache of clips streamed from 4chan, so a clip many people watch (the
live channel above all) is fetched upstream once instead of once per viewer."""
import logging
import os
import re
import shutil
import tempfile
import time

from archiver import archive_relpath

log = logging.getLogger(__name__)

# Content-Range of a response that holds the whole file
FULL_RANGE = re.compile(r"^bytes 0-(\d+)/(\d+)$")
# leftovers of downloads that never finished
STALE_PART = 3600


def full_size(status, headers):
    """The file size if an upstream response carries the whole file, else None."""
    if status == 200:
        length = headers.get("Content-Length")
        return int(length) if length and length.isdigit() else None
    match = FULL_RANGE.match(headers.get("Content-Range", ""))
    if status == 206 and match and int(match.group(1)) + 1 == int(match.group(2)):
        return int(match.group(2))
    return None


class Writer:
    """Collects a streamed clip into the cache; only a complete copy is kept."""

    def __init__(self, target, size):
        self.target = target
        self.size = size
        self.written = 0
        os.makedirs(os.path.dirname(target), exist_ok=True)
        fd, self.tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".part")
        self.file = os.fdopen(fd, "wb")

    def write(self, chunk):
        if self.file is None:
            return
        self.written += len(chunk)
        try:
            self.file.write(chunk)
        except OSError as exc:
            log.warning("caching %s failed: %s", self.target, exc)
            self.abort()

    def commit(self):
        if self.file is None:
            return
        self.file.close()
        self.file = None
        if self.written == self.size:
            os.replace(self.tmp, self.target)
        else:
            os.remove(self.tmp)

    def abort(self):
        if self.file is None:
            return
        self.file.close()
        self.file = None
        try:
            os.remove(self.tmp)
        except FileNotFoundError:
            pass


class MediaCache:
    def __init__(self, directory, max_bytes=512 * 1024 ** 2, max_file_bytes=64 * 1024 ** 2):
        self.directory = directory
        self.max_bytes = max_bytes
        self.max_file_bytes = max_file_bytes
        os.makedirs(directory, exist_ok=True)

    def path(self, video_id):
        return os.path.join(self.directory, archive_relpath(video_id))

    def get(self, video_id):
        """Path of the cached clip, or None. Marks it as recently used."""
        path = self.path(video_id)
        try:
            os.utime(path)
        except FileNotFoundError:
            return None
        return path

    def writer(self, video_id, size):
        """A Writer for a clip of `size` bytes, or None if it shouldn't be cached."""
        if self.max_bytes <= 0 or size is None or size > self.max_file_bytes:
            return None
        try:
            return Writer(self.path(video_id), size)
        except OSError as exc:
            log.warning("can't cache %s: %s", video_id, exc)
            return None

    def take(self, video_id, target):
        """Move a cached clip to `target` (the vault); returns its size or None."""
        path = self.path(video_id)
        if not os.path.exists(path):
            return None
        try:
            shutil.move(path, target)
        except OSError as exc:
            log.warning("moving %s out of the cache failed: %s", video_id, exc)
            return None
        return os.path.getsize(target)

    def enforce_limit(self, now=None):
        """Delete the least recently watched clips until the cache fits."""
        now = now or time.time()
        files = []
        for root, _, names in os.walk(self.directory):
            for name in names:
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue
                if name.endswith(".part"):
                    if now - stat.st_mtime > STALE_PART:
                        os.remove(path)
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            total -= size
