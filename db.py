"""SQLite storage for clips, votes, views, viewers and the live channel."""
import functools
import json
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager

from channels import NSFW_BOARDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    board TEXT NOT NULL,
    thread INTEGER,
    title TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL DEFAULT '',
    md5 TEXT,
    width INTEGER,
    height INTEGER,
    size INTEGER,
    duration REAL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    gone INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 0,
    plays INTEGER NOT NULL DEFAULT 0,
    archived_path TEXT,
    archived_bytes INTEGER,
    archived_at INTEGER
);
CREATE INDEX IF NOT EXISTS videos_board ON videos (board, last_seen);
CREATE TABLE IF NOT EXISTS votes (
    video_id TEXT NOT NULL,
    client TEXT NOT NULL,
    value INTEGER NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (video_id, client)
);
CREATE TABLE IF NOT EXISTS views (
    video_id TEXT NOT NULL,
    client TEXT NOT NULL,
    created INTEGER NOT NULL,
    PRIMARY KEY (video_id, client)
);
CREATE TABLE IF NOT EXISTS viewers (
    client TEXT PRIMARY KEY,
    channel TEXT,
    last_seen INTEGER NOT NULL
);
-- ids of clips merged into another copy of the same file, so old links keep working
CREATE TABLE IF NOT EXISTS aliases (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# clips at or below this score are never shown
HIDE_SCORE = -3
# a viewer counts as watching if they sent a heartbeat this recently
VIEWER_TTL = 60
# clips whose thread died this long ago are forgotten (unless archived)
FORGET_AFTER = 7 * 24 * 3600
LIVE_RECENT = 30
LIVE_FALLBACK_DURATION = 60


def weight(score):
    """Selection weight: upvoted clips come up more often, downvoted ones less."""
    return min(6.0, max(0.2, 1.0 + 0.5 * score))


@functools.lru_cache(maxsize=64)
def _compile(pattern):
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern, text):
    return text is not None and _compile(pattern).search(text) is not None


def keyword_pattern(keywords):
    """Matches any keyword at the start of a word: "cat" finds "cats" and
    "funny_cat" but not "scatter"."""
    return r"(?<![a-z])(?:" + "|".join(re.escape(k) for k in keywords) + ")"


class Database:
    def __init__(self, path):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
            if "md5" not in columns:
                conn.execute("ALTER TABLE videos ADD COLUMN md5 TEXT")
            # reposts of the same file on a board are one clip
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS videos_md5 ON videos (board, md5)")
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def connect(self, immediate=False):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.create_function("REGEXP", 2, _regexp, deterministic=True)
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # --- scraping -----------------------------------------------------------

    def save_videos(self, videos, now=None):
        """Upsert clips seen on 4chan and return the id each one is stored under.

        A repost of a file already on the board (same md5) updates the existing
        clip instead of adding a copy, merging the two if both were stored."""
        now = int(now or time.time())
        ids = []
        with self.connect(immediate=True) as conn:
            for v in videos:
                v = dict({"width": None, "height": None, "size": None, "thread": None, "md5": None}, **v, now=now)
                stored = self._resolve(conn, v["id"])
                same_file = None
                if v["md5"]:
                    row = conn.execute("SELECT id FROM videos WHERE board = ? AND md5 = ?",
                                       (v["board"], v["md5"])).fetchone()
                    same_file = row and row[0]
                if same_file and stored and stored != same_file:
                    self._merge(conn, same_file, stored)
                target = same_file or stored
                if target:
                    conn.execute(
                        """UPDATE videos SET url = :url, thread = :thread, title = :title, filename = :filename,
                                             width = :width, height = :height, size = :size,
                                             md5 = COALESCE(:md5, md5), last_seen = :now, gone = 0
                           WHERE id = :target""", dict(v, target=target))
                else:
                    target = v["id"]
                    conn.execute(
                        """INSERT INTO videos (id, url, board, thread, title, filename, md5, width, height, size,
                                               first_seen, last_seen, gone)
                           VALUES (:id, :url, :board, :thread, :title, :filename, :md5, :width, :height, :size,
                                   :now, :now, 0)""", v)
                ids.append(target)
        return ids

    def mark_missing(self, board, seen):
        """Mark every clip of `board` that isn't in `seen` (the whole board) as gone."""
        if not seen:
            return
        with self.connect(immediate=True) as conn:
            seen = {self._resolve(conn, video_id) for video_id in seen}
            known = [r[0] for r in conn.execute("SELECT id FROM videos WHERE board = ? AND gone = 0", (board,))]
            conn.executemany("UPDATE videos SET gone = 1 WHERE id = ?",
                             [(video_id,) for video_id in known if video_id not in seen])

    def set_md5(self, video_id, md5):
        """Record a clip's md5, merging it into another clip of the board with the
        same file. Returns the id the clip now lives under."""
        with self.connect(immediate=True) as conn:
            row = conn.execute("SELECT board FROM videos WHERE id = ?", (video_id,)).fetchone()
            if row is None:
                return None
            other = conn.execute("SELECT id FROM videos WHERE board = ? AND md5 = ? AND id != ?",
                                 (row[0], md5, video_id)).fetchone()
            if other:
                self._merge(conn, other[0], video_id)
                return other[0]
            conn.execute("UPDATE videos SET md5 = ? WHERE id = ?", (md5, video_id))
            return video_id

    @staticmethod
    def _resolve(conn, video_id):
        """The id a clip is stored under (following merges), or None."""
        if conn.execute("SELECT 1 FROM videos WHERE id = ?", (video_id,)).fetchone():
            return video_id
        row = conn.execute("SELECT video_id FROM aliases WHERE id = ?", (video_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _merge(conn, keep, dup):
        """Fold clip `dup` into `keep`: votes, views, the vault copy and its links.
        A vault file left without a clip is cleaned up by the archiver."""
        k = conn.execute("SELECT * FROM videos WHERE id = ?", (keep,)).fetchone()
        d = conn.execute("SELECT * FROM videos WHERE id = ?", (dup,)).fetchone()
        conn.execute("""INSERT OR IGNORE INTO votes (video_id, client, value, created)
                        SELECT ?, client, value, created FROM votes WHERE video_id = ?""", (keep, dup))
        moved_views = conn.execute("""INSERT OR IGNORE INTO views (video_id, client, created)
                                      SELECT ?, client, created FROM views WHERE video_id = ?""", (keep, dup)).rowcount
        conn.execute("DELETE FROM votes WHERE video_id = ?", (dup,))
        conn.execute("DELETE FROM views WHERE video_id = ?", (dup,))
        fields = {
            "score": conn.execute("SELECT COALESCE(SUM(value), 0) FROM votes WHERE video_id = ?", (keep,)).fetchone()[0],
            "plays": k["plays"] + moved_views,
            "duration": k["duration"] or d["duration"],
            "first_seen": min(k["first_seen"], d["first_seen"]),
            "last_seen": max(k["last_seen"], d["last_seen"]),
            "gone": k["gone"] and d["gone"],
        }
        if k["gone"] and not d["gone"]:
            # the duplicate is the copy that is still up
            fields.update({f: d[f] for f in ("url", "thread", "title", "filename")})
        if k["archived_path"] is None and d["archived_path"] is not None:
            fields.update({f: d[f] for f in ("archived_path", "archived_bytes", "archived_at")})
        conn.execute("UPDATE videos SET " + ", ".join(f + " = :" + f for f in fields) + " WHERE id = :id",
                     dict(fields, id=keep))
        conn.execute("DELETE FROM videos WHERE id = ?", (dup,))
        conn.execute("UPDATE aliases SET video_id = ? WHERE video_id = ?", (keep, dup))
        conn.execute("INSERT OR REPLACE INTO aliases (id, video_id) VALUES (?, ?)", (dup, keep))

    def import_legacy(self, videos):
        """One-off import of the old videos.json playlist into an empty database."""
        with self.connect(immediate=True) as conn:
            if conn.execute("SELECT 1 FROM videos LIMIT 1").fetchone():
                return 0
        self.save_videos(videos)
        return len(videos)

    def mark_gone(self, video_id):
        with self.connect() as conn:
            conn.execute("UPDATE videos SET gone = 1 WHERE id = ?", (video_id,))

    def prune(self, now=None):
        now = int(now or time.time())
        with self.connect(immediate=True) as conn:
            stale = """SELECT id FROM videos WHERE gone = 1 AND archived_path IS NULL
                       AND last_seen < ?"""
            conn.execute("DELETE FROM votes WHERE video_id IN (%s)" % stale, (now - FORGET_AFTER,))
            conn.execute("DELETE FROM views WHERE video_id IN (%s)" % stale, (now - FORGET_AFTER,))
            conn.execute("DELETE FROM videos WHERE id IN (%s)" % stale, (now - FORGET_AFTER,))
            conn.execute("DELETE FROM aliases WHERE video_id NOT IN (SELECT id FROM videos)")
            conn.execute("DELETE FROM viewers WHERE last_seen < ?", (now - 24 * 3600,))

    # --- reading ------------------------------------------------------------

    def get(self, video_id):
        """A clip by id, following merges into another copy of the same file."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM videos WHERE id = ?", (self._resolve(conn, video_id),)).fetchone()
        return dict(row) if row else None

    def _channel_where(self, channel, now):
        where = ["(gone = 0 OR archived_path IS NOT NULL)", "score > ?"]
        params = [HIDE_SCORE]
        boards = channel.get("boards") or ()
        if not boards and not channel.get("nsfw"):
            where.append("board NOT IN (%s)" % ",".join("?" * len(NSFW_BOARDS)))
            params += NSFW_BOARDS
        if boards:
            where.append("board IN (%s)" % ",".join("?" * len(boards)))
            params += boards
        if channel.get("keywords"):
            pattern = keyword_pattern(channel["keywords"])
            where.append("(title REGEXP ? OR filename REGEXP ?)")
            params += [pattern, pattern]
        kind = channel.get("filter")
        if kind == "top":
            where.append("score >= 1")
        elif kind == "fresh":
            where.append("first_seen >= ?")
            params.append(int(now) - 24 * 3600)
        elif kind == "vault":
            where.append("archived_path IS NOT NULL")
        return " AND ".join(where), params

    def pick(self, channel, exclude=(), now=None, rng=random):
        """Weighted random clip for `channel`, avoiding `exclude` when possible."""
        where, params = self._channel_where(channel, now or time.time())
        with self.connect() as conn:
            rows = conn.execute("SELECT id, score FROM videos WHERE " + where, params).fetchall()
            if not rows:
                return None
            exclude = set(exclude)
            fresh = [r for r in rows if r["id"] not in exclude] or rows
            chosen = rng.choices(fresh, weights=[weight(r["score"]) for r in fresh])[0]
            return dict(conn.execute("SELECT * FROM videos WHERE id = ?", (chosen["id"],)).fetchone())

    def count(self, channel=None):
        """Clips airing on `channel`, or in the whole library (NSFW boards included)."""
        where, params = self._channel_where(channel if channel is not None else {"nsfw": True}, time.time())
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM videos WHERE " + where, params).fetchone()[0]

    def stats(self):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(gone = 0) AS live,
                          SUM(archived_path IS NOT NULL) AS archived,
                          COALESCE(SUM(archived_bytes), 0) AS archived_bytes
                   FROM videos""").fetchone()
        return {k: row[k] or 0 for k in row.keys()}

    # --- viewers' input -----------------------------------------------------

    def vote(self, video_id, client, value, now=None):
        """Set a client's vote (-1, 0 or 1) and return the clip's new score."""
        now = int(now or time.time())
        with self.connect(immediate=True) as conn:
            if value:
                conn.execute(
                    """INSERT INTO votes (video_id, client, value, created) VALUES (?, ?, ?, ?)
                       ON CONFLICT (video_id, client) DO UPDATE SET value = excluded.value""",
                    (video_id, client, value, now))
            else:
                conn.execute("DELETE FROM votes WHERE video_id = ? AND client = ?", (video_id, client))
            score = conn.execute("SELECT COALESCE(SUM(value), 0) FROM votes WHERE video_id = ?",
                                 (video_id,)).fetchone()[0]
            conn.execute("UPDATE videos SET score = ? WHERE id = ?", (score, video_id))
        return score

    def my_vote(self, video_id, client):
        if not client:
            return 0
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM votes WHERE video_id = ? AND client = ?",
                               (video_id, client)).fetchone()
        return row[0] if row else 0

    def record_view(self, video_id, client, now=None):
        """Count a view once per client; returns the clip's view count."""
        now = int(now or time.time())
        with self.connect(immediate=True) as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO views (video_id, client, created) VALUES (?, ?, ?)",
                                    (video_id, client, now)).rowcount
            if inserted:
                conn.execute("UPDATE videos SET plays = plays + 1 WHERE id = ?", (video_id,))
            row = conn.execute("SELECT plays FROM videos WHERE id = ?", (video_id,)).fetchone()
        return row[0] if row else 0

    def set_duration(self, video_id, duration):
        with self.connect() as conn:
            conn.execute("UPDATE videos SET duration = ? WHERE id = ? AND duration IS NULL",
                         (duration, video_id))

    def heartbeat(self, client, channel, now=None):
        """Register a viewer; returns (watching in total, watching this channel)."""
        now = int(now or time.time())
        with self.connect(immediate=True) as conn:
            conn.execute(
                """INSERT INTO viewers (client, channel, last_seen) VALUES (?, ?, ?)
                   ON CONFLICT (client) DO UPDATE SET channel = excluded.channel, last_seen = excluded.last_seen""",
                (client, channel, now))
            total = conn.execute("SELECT COUNT(*) FROM viewers WHERE last_seen >= ?",
                                 (now - VIEWER_TTL,)).fetchone()[0]
            same = conn.execute("SELECT COUNT(*) FROM viewers WHERE last_seen >= ? AND channel = ?",
                                (now - VIEWER_TTL, channel)).fetchone()[0]
        return total, same

    # --- live channel -------------------------------------------------------

    def live(self, channel, now=None, rng=random):
        """The clip everyone on the live channel is watching and how far into it they are.

        Advances to a new clip once the current one has finished. Returns
        (video, offset_seconds) or (None, 0) when there is nothing to show.
        """
        now = now or time.time()
        with self.connect(immediate=True) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'live'").fetchone()
            state = json.loads(row[0]) if row else {"id": None, "started": 0, "recent": []}
            current = None
            if state["id"]:
                current = conn.execute("SELECT * FROM videos WHERE id = ? AND (gone = 0 OR archived_path IS NOT NULL)",
                                       (state["id"],)).fetchone()
            if current is not None:
                duration = current["duration"] or LIVE_FALLBACK_DURATION
                if now - state["started"] < duration - 0.5:
                    return dict(current), now - state["started"]
        # the current clip is over (or gone): pick the next one outside the lock
        # to keep the transaction short, then claim it unless another worker won
        video = self.pick(channel, exclude=state["recent"], now=now, rng=rng)
        if video is None:
            return None, 0
        with self.connect(immediate=True) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'live'").fetchone()
            latest = json.loads(row[0]) if row else {"id": None, "started": 0, "recent": []}
            if latest["id"] != state["id"] or latest["started"] != state["started"]:
                claimed = conn.execute("SELECT * FROM videos WHERE id = ?", (latest["id"],)).fetchone()
                if claimed is not None:
                    return dict(claimed), now - latest["started"]
            recent = ([state["id"]] if state["id"] else []) + state["recent"]
            new_state = {"id": video["id"], "started": now, "recent": recent[:LIVE_RECENT]}
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('live', ?)", (json.dumps(new_state),))
        return video, 0.0

    # --- archive ------------------------------------------------------------

    def archive_candidates(self, min_score, min_plays, limit=20):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM videos
                   WHERE archived_path IS NULL AND gone = 0 AND (score >= ? OR plays >= ?)
                   ORDER BY score DESC, plays DESC LIMIT ?""",
                (min_score, min_plays, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_archived(self, video_id, path, size, now=None):
        with self.connect() as conn:
            conn.execute("UPDATE videos SET archived_path = ?, archived_bytes = ?, archived_at = ? WHERE id = ?",
                         (path, size, int(now or time.time()), video_id))

    def unarchive(self, video_id):
        with self.connect() as conn:
            conn.execute("UPDATE videos SET archived_path = NULL, archived_bytes = NULL, archived_at = NULL "
                         "WHERE id = ?", (video_id,))

    def archived_without_md5(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT id, archived_path FROM videos WHERE archived_path IS NOT NULL AND md5 IS NULL")
            return [dict(r) for r in rows]

    def archived_paths(self):
        with self.connect() as conn:
            return {r[0] for r in conn.execute("SELECT archived_path FROM videos WHERE archived_path IS NOT NULL")}

    def archived_by_rank(self):
        """Archived clips, least popular first (eviction order)."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT id, archived_path, archived_bytes FROM videos WHERE archived_path IS NOT NULL
                   ORDER BY score ASC, plays ASC, archived_at ASC""").fetchall()
        return [dict(r) for r in rows]
