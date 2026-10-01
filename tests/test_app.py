import datetime
import json
import os
import random

import pytest

import app as app_module
from app import create_app, off_air_until, parse_quiet_hours
from archiver import Archiver, archive_relpath
from cache import MediaCache
from channels import DEFAULT_CHANNELS, load_channels
from channer import Channer
from db import Database

CLIENT = {"X-Client-Id": "client-aaaaaaaa"}
MIX = {"slug": "mix"}


class FakeResponse:
    def __init__(self, status, payload=None, headers=None, body=b""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._body = body

    def json(self):
        return self._payload

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers or {}))
        return self.routes.get(url, FakeResponse(404))


THREADS = [{"page": 1, "threads": [{"no": 1, "last_modified": 10}, {"no": 2, "last_modified": 20}]}]
THREAD_1 = {"posts": [
    {"no": 1, "sub": "YLYL thread", "tim": 111, "ext": ".webm", "filename": "a", "md5": "md5-a"},
    {"no": 3, "tim": 112, "ext": ".jpg", "filename": "b", "md5": "md5-b"},
    {"no": 4, "tim": 113, "ext": ".mp4", "filename": "c", "md5": "md5-c"},
]}
THREAD_2 = {"posts": [
    {"no": 2, "sub": "Cat thread", "tim": 211, "ext": ".webm", "filename": "d", "md5": "md5-d"},
    {"no": 5, "tim": 212, "ext": ".webm", "filename": "e", "filedeleted": 1},
]}


def api_routes():
    return {
        "https://a.4cdn.org/wsg/threads.json": FakeResponse(200, THREADS, {"Last-Modified": "x"}),
        "https://a.4cdn.org/wsg/thread/1.json": FakeResponse(200, THREAD_1, {"Last-Modified": "y"}),
        "https://a.4cdn.org/wsg/thread/2.json": FakeResponse(200, THREAD_2),
    }


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    Channer(min_interval=0, session=FakeSession(api_routes())).update(database)
    return database


# --- scraper -------------------------------------------------------------

def test_channer_collects_videos(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    session = FakeSession(api_routes())
    channer = Channer(min_interval=0, session=session)
    assert channer.update(database) == 3
    assert database.count() == 3
    video = database.get("wsg-111.webm")
    assert video["title"] == "YLYL thread"
    assert video["url"] == "https://i.4cdn.org/wsg/111.webm"
    assert video["thread"] == 1

    # second run: the index is revalidated, unchanged threads aren't fetched at all
    session.calls.clear()
    session.routes["https://a.4cdn.org/wsg/threads.json"] = FakeResponse(304)
    assert channer.update(database) == 3
    assert session.calls == [("https://a.4cdn.org/wsg/threads.json", {"If-Modified-Since": "x"})]
    assert database.get("wsg-111.webm")["gone"] == 0


def test_only_changed_threads_are_refetched(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    session = FakeSession(api_routes())
    channer = Channer(min_interval=0, session=session)
    channer.update(database)
    session.calls.clear()
    session.routes["https://a.4cdn.org/wsg/threads.json"] = FakeResponse(
        200, [{"threads": [{"no": 1, "last_modified": 11}, {"no": 2, "last_modified": 20}]}])
    session.routes["https://a.4cdn.org/wsg/thread/1.json"] = FakeResponse(200, {"posts": THREAD_1["posts"][:1]})
    channer.update(database)
    assert [url for url, _ in session.calls] == ["https://a.4cdn.org/wsg/threads.json",
                                                 "https://a.4cdn.org/wsg/thread/1.json"]
    # 113 left thread 1; thread 2 wasn't refetched but its clip is still up
    assert database.get("wsg-113.mp4")["gone"] == 1
    assert database.get("wsg-211.webm")["gone"] == 0


def test_reposts_are_one_clip(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    routes = api_routes()
    routes["https://a.4cdn.org/wsg/thread/2.json"] = FakeResponse(200, {"posts": [
        {"no": 2, "sub": "Cat thread", "tim": 211, "ext": ".webm", "filename": "d", "md5": "md5-d"},
        {"no": 6, "tim": 311, "ext": ".webm", "filename": "a again", "md5": "md5-a"},
    ]})
    assert Channer(min_interval=0, session=FakeSession(routes)).update(database) == 3
    assert database.count() == 3
    assert database.get("wsg-311.webm") is None

    # the first copy's thread dies: the clip lives on through the repost
    routes["https://a.4cdn.org/wsg/threads.json"] = FakeResponse(200, [{"threads": [{"no": 2}]}])
    Channer(min_interval=0, session=FakeSession(routes)).update(database)
    clip = database.get("wsg-111.webm")
    assert clip["gone"] == 0 and clip["thread"] == 2 and clip["url"].endswith("/311.webm")


def test_old_duplicates_are_merged(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    # stored before md5s were recorded
    database.save_videos([{"id": "wsg-1.webm", "url": "u1", "board": "wsg", "title": "", "filename": "x"},
                          {"id": "wsg-2.webm", "url": "u2", "board": "wsg", "title": "", "filename": "x"}])
    database.vote("wsg-1.webm", "a", 1)
    database.vote("wsg-2.webm", "a", 1)
    database.vote("wsg-2.webm", "b", 1)
    database.record_view("wsg-2.webm", "a")
    database.mark_archived("wsg-2.webm", "wsg/2.webm", 10)
    database.save_videos([{"id": "wsg-1.webm", "url": "u1", "board": "wsg", "title": "", "filename": "x",
                           "md5": "same"}])
    assert database.save_videos([{"id": "wsg-2.webm", "url": "u2", "board": "wsg", "title": "",
                                  "filename": "x", "md5": "same"}]) == ["wsg-1.webm"]
    clip = database.get("wsg-2.webm")
    assert clip["id"] == "wsg-1.webm"
    assert clip["score"] == 2 and clip["plays"] == 1 and clip["archived_path"] == "wsg/2.webm"
    assert database.count() == 1


def test_dead_threads_are_marked_gone(db):
    routes = api_routes()
    routes["https://a.4cdn.org/wsg/threads.json"] = FakeResponse(200, [{"threads": [{"no": 2}]}])
    Channer(min_interval=0, session=FakeSession(routes)).update(db)
    assert db.get("wsg-111.webm")["gone"] == 1
    assert db.get("wsg-211.webm")["gone"] == 0
    assert db.count() == 1


def test_clips_are_saved_while_scanning(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    counts = []
    routes = api_routes()
    session = FakeSession(routes)
    original = session.get

    def get(url, **kwargs):
        if url.endswith("/thread/2.json"):
            counts.append(database.count())
        return original(url, **kwargs)

    session.get = get
    channer = Channer(min_interval=0, session=session)
    channer.update(database)
    # thread 1's clips were already playable while thread 2 was being fetched
    assert counts == [2]
    assert channer.status["scanning"] is False and channer.status["done"] == 2


def test_board_outage_keeps_clips(db):
    Channer(min_interval=0, session=FakeSession({})).update(db)
    assert db.count() == 3


# --- database ------------------------------------------------------------

def test_keywords_match_word_starts(db):
    music = {"slug": "music", "keywords": ["ear"]}
    db.save_videos([{"id": "wsg-7.webm", "url": "u", "board": "wsg", "title": "best of the year", "filename": "y"}])
    assert db.pick(music) is None
    db.save_videos([{"id": "wsg-8.webm", "url": "u", "board": "wsg", "title": "", "filename": "EARRAPE_remix"}])
    assert db.pick(music)["id"] == "wsg-8.webm"


def test_nsfw_boards_stay_on_their_channel(db):
    adult = next(c for c in DEFAULT_CHANNELS if c["slug"] == "adult")
    db.save_videos([{"id": "hc-9.webm", "url": "u", "board": "hc", "title": "cat", "filename": "z"}])
    assert "hc-9.webm" not in {db.pick(MIX)["id"] for _ in range(50)}
    assert db.pick({"slug": "animals", "keywords": ["cat"]})["id"] == "wsg-211.webm"
    assert db.pick(adult)["id"] == "hc-9.webm"
    assert db.count() == 4


def test_channels_for_unscraped_boards_are_hidden():
    assert "adult" not in [c["slug"] for c in load_channels(boards=["wsg"])]
    assert "adult" in [c["slug"] for c in load_channels(boards=["wsg", "gif"])]


def test_pick_respects_keywords_and_exclude(db):
    ylyl = next(c for c in DEFAULT_CHANNELS if c["slug"] == "ylyl")
    animals = next(c for c in DEFAULT_CHANNELS if c["slug"] == "animals")
    assert {db.pick(ylyl)["id"] for _ in range(20)} == {"wsg-111.webm", "wsg-113.mp4"}
    assert db.pick(animals)["id"] == "wsg-211.webm"
    assert db.pick(MIX, exclude=["wsg-111.webm", "wsg-113.mp4"])["id"] == "wsg-211.webm"
    # everything excluded: repeats are better than dead air
    assert db.pick(animals, exclude=["wsg-211.webm"])["id"] == "wsg-211.webm"
    assert db.pick({"slug": "x", "keywords": ["nothing-matches"]}) is None


def test_votes_change_score_and_hide_bad_clips(db):
    assert db.vote("wsg-111.webm", "a", 1) == 1
    assert db.vote("wsg-111.webm", "a", 1) == 1  # one vote per client
    assert db.vote("wsg-111.webm", "b", -1) == 0
    assert db.vote("wsg-111.webm", "b", 0) == 1
    assert db.my_vote("wsg-111.webm", "a") == 1
    assert db.pick({"slug": "top", "filter": "top"})["id"] == "wsg-111.webm"
    for voter in "cdef":
        db.vote("wsg-211.webm", voter, -1)
    ids = {db.pick(MIX)["id"] for _ in range(50)}
    assert "wsg-211.webm" not in ids


def test_views_are_unique_per_client(db):
    assert db.record_view("wsg-111.webm", "a") == 1
    assert db.record_view("wsg-111.webm", "a") == 1
    assert db.record_view("wsg-111.webm", "b") == 2


def test_heartbeat_counts_recent_viewers(db):
    db.heartbeat("a", "mix", now=1000)
    db.heartbeat("b", "live", now=1000)
    db.heartbeat("c", "live", now=500)  # stale
    assert db.heartbeat("d", "live", now=1010) == (3, 2)


def test_live_is_shared_and_advances(db):
    rng = random.Random(1)
    first, offset = db.live(MIX, now=1000, rng=rng)
    assert offset == 0
    db.set_duration(first["id"], 10)
    same, offset = db.live(MIX, now=1004, rng=rng)
    assert same["id"] == first["id"] and offset == 4
    nxt, offset = db.live(MIX, now=1011, rng=rng)
    assert nxt["id"] != first["id"] and offset == 0


def test_prune_forgets_old_gone_clips(db):
    db.mark_gone("wsg-111.webm")
    db.mark_gone("wsg-113.mp4")
    db.mark_archived("wsg-113.mp4", "wsg/113.mp4", 10)
    db.prune(now=db.get("wsg-111.webm")["last_seen"] + 8 * 24 * 3600)
    assert db.get("wsg-111.webm") is None
    assert db.get("wsg-113.mp4") is not None


def test_legacy_import(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    videos = [{"id": "wsg-1.webm", "url": "https://i.4cdn.org/wsg/1.webm", "board": "wsg",
               "thread": None, "title": "", "filename": "1.webm"}]
    assert database.import_legacy(videos) == 1
    assert database.import_legacy(videos) == 0


# --- archive -------------------------------------------------------------

def test_archiver_keeps_popular_clips(db, tmp_path):
    routes = {"https://i.4cdn.org/wsg/111.webm": FakeResponse(200, body=b"x" * 100),
              "https://i.4cdn.org/wsg/113.mp4": FakeResponse(200, body=b"y" * 100)}
    archiver = Archiver(db, str(tmp_path / "archive"), min_score=1, min_plays=100, max_bytes=150,
                        min_interval=0, session=FakeSession(routes))
    db.vote("wsg-111.webm", "a", 1)
    db.vote("wsg-111.webm", "b", 1)
    db.vote("wsg-113.mp4", "a", 1)
    db.vote("wsg-211.webm", "a", 1)  # upstream 404 -> gone
    archiver.run()
    # over the 150 byte cap: the less popular clip is evicted
    assert db.get("wsg-111.webm")["archived_path"] == "wsg/111.webm"
    assert (tmp_path / "archive" / "wsg" / "111.webm").read_bytes() == b"x" * 100
    assert db.get("wsg-113.mp4")["archived_path"] is None
    assert not (tmp_path / "archive" / "wsg" / "113.mp4").exists()
    assert db.get("wsg-211.webm")["gone"] == 1


def test_archiver_moves_cached_clips_and_merges_copies(db, tmp_path):
    cache = MediaCache(str(tmp_path / "cache"))
    (tmp_path / "cache" / "wsg").mkdir(parents=True)
    (tmp_path / "cache" / "wsg" / "111.webm").write_bytes(b"x" * 10)
    archiver = Archiver(db, str(tmp_path / "archive"), min_score=1, min_interval=0,
                        session=FakeSession({}), cache=cache)
    db.vote("wsg-111.webm", "a", 1)
    archiver.run()
    assert db.get("wsg-111.webm")["archived_path"] == "wsg/111.webm"
    assert not (tmp_path / "cache" / "wsg" / "111.webm").exists()

    # two vault copies of one file from before md5s were stored: one is merged away
    vault = tmp_path / "archive" / "wsg"
    db.save_videos([{"id": "wsg-5.webm", "url": "u", "board": "wsg", "title": "", "filename": "f"},
                    {"id": "wsg-6.webm", "url": "u", "board": "wsg", "title": "", "filename": "f"}])
    for name in ("5", "6"):
        (vault / (name + ".webm")).write_bytes(b"same")
        db.mark_archived("wsg-" + name + ".webm", "wsg/" + name + ".webm", 4)
    archiver.run()
    assert db.get("wsg-6.webm")["id"] == "wsg-5.webm"
    assert sorted(p.name for p in vault.iterdir()) == ["111.webm", "5.webm"]


def test_cache_evicts_least_recently_watched(tmp_path):
    cache = MediaCache(str(tmp_path / "cache"), max_bytes=15)
    for name in ("1", "2"):
        path = tmp_path / "cache" / "wsg" / (name + ".webm")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
    cache.get("wsg-1.webm")
    os.utime(cache.path("wsg-2.webm"), (0, 0))
    cache.enforce_limit()
    assert cache.get("wsg-1.webm") and not cache.get("wsg-2.webm")


def test_archive_paths_are_sanitized():
    assert archive_relpath("wsg-123.webm") == "wsg/123.webm"
    with pytest.raises(ValueError):
        archive_relpath("../etc-1.webm")


# --- web app -------------------------------------------------------------

def make_app(tmp_path, **config):
    base = {"DATABASE": str(tmp_path / "test.db"), "ARCHIVE_DIR": str(tmp_path / "archive"),
            "CACHE_DIR": str(tmp_path / "cache"),
            "REFRESH_MINUTES": 0, "ARCHIVE_MINUTES": 0, "UPDATE_TOKEN": "secret",
            "LEGACY_PLAYLIST": str(tmp_path / "none.json")}
    base.update(config)
    return create_app(base)


@pytest.fixture
def tv_app(tmp_path, db):
    return make_app(tmp_path)


def test_next_and_channels(tv_app):
    c = tv_app.test_client()
    slugs = [ch["slug"] for ch in c.get("/api/channels").get_json()]
    assert slugs[0] == "live" and "mix" in slugs
    item = c.get("/api/next?channel=animals").get_json()
    assert item["id"] == "wsg-211.webm" and item["channel"] == "animals"
    assert item["src"] == "/video/wsg-211.webm" and item["share"] == "/v/wsg-211.webm"
    assert c.get("/api/next?channel=vault").get_json()["offair"] is True


def test_quiet_hours(tmp_path, db):
    assert parse_quiet_hours("23:30-6") == ((23, 30), (6, 0))
    quiet = parse_quiet_hours("23:30-06:00")
    assert off_air_until(quiet, datetime.datetime(2026, 1, 1, 2, 0)) == "06:00"
    assert off_air_until(quiet, datetime.datetime(2026, 1, 1, 12, 0)) is None
    tv = make_app(tmp_path, QUIET_HOURS="00:00-23:59")
    data = tv.test_client().get("/api/next").get_json()
    assert data["offair"] is True and data["reason"] == "quiet"


def test_vote_view_and_heartbeat_endpoints(tv_app):
    c = tv_app.test_client()
    assert c.post("/api/videos/wsg-111.webm/vote", json={"value": 1}).status_code == 400
    assert c.post("/api/videos/wsg-111.webm/vote", json={"value": 5}, headers=CLIENT).status_code == 400
    assert c.post("/api/videos/wsg-111.webm/vote", json={"value": 1}, headers=CLIENT).get_json()["score"] == 1
    assert c.get("/api/videos/wsg-111.webm", headers=CLIENT).get_json()["vote"] == 1
    assert c.post("/api/videos/wsg-111.webm/view", json={}, headers=CLIENT).get_json()["plays"] == 1
    assert c.post("/api/videos/nope/view", json={}, headers=CLIENT).status_code == 404
    assert c.post("/api/heartbeat", json={"channel": "live"}, headers=CLIENT).get_json() == \
        {"watching": 1, "channel_watching": 1}
    assert c.post("/api/videos/wsg-111.webm/duration", json={"duration": 12.5}).status_code == 204
    assert c.get("/api/videos/wsg-111.webm").get_json()["duration"] == 12.5


def test_live_endpoint(tv_app):
    data = tv_app.test_client().get("/api/live").get_json()
    assert data["channel"] == "live" and data["offset"] == 0


def test_video_proxy_only_serves_library(tv_app, monkeypatch):
    http = tv_app.extensions["weirdtv"]["http"]
    monkeypatch.setattr(http, "get", lambda url, **kw: FakeResponse(
        206, headers={"Content-Type": "video/webm", "Content-Range": "bytes 0-2/3"}, body=b"abc"))
    c = tv_app.test_client()
    resp = c.get("/video/wsg-111.webm", headers={"Range": "bytes=0-"})
    assert resp.status_code == 206
    assert resp.data == b"abc"
    assert resp.headers["Content-Range"] == "bytes 0-2/3"
    assert c.get("/video/evil").status_code == 404


def test_streamed_clip_is_cached(tv_app, monkeypatch):
    http = tv_app.extensions["weirdtv"]["http"]
    calls = []

    def get(url, **kw):
        calls.append(url)
        if url.endswith("113.mp4"):
            return FakeResponse(206, headers={"Content-Range": "bytes 0-1/3"}, body=b"ab")
        return FakeResponse(206, headers={"Content-Type": "video/webm", "Content-Range": "bytes 0-2/3"},
                            body=b"abc")

    monkeypatch.setattr(http, "get", get)
    c = tv_app.test_client()
    assert c.get("/video/wsg-111.webm", headers={"Range": "bytes=0-"}).data == b"abc"
    resp = c.get("/video/wsg-111.webm", headers={"Range": "bytes=1-"})
    assert resp.status_code == 206 and resp.data == b"bc"
    assert len(calls) == 1
    # a partial response isn't cached
    assert c.get("/video/wsg-113.mp4", headers={"Range": "bytes=0-1"}).status_code == 206
    assert tv_app.extensions["weirdtv"]["cache"].get("wsg-113.mp4") is None


def test_video_download_sets_attachment(tv_app, monkeypatch):
    http = tv_app.extensions["weirdtv"]["http"]
    monkeypatch.setattr(http, "get", lambda url, **kw: FakeResponse(
        200, headers={"Content-Type": "video/webm"}, body=b"abc"))
    c = tv_app.test_client()
    assert "Content-Disposition" not in c.get("/video/wsg-111.webm").headers
    disposition = c.get("/video/wsg-111.webm?download=1").headers["Content-Disposition"]
    assert disposition.startswith("attachment") and ".webm" in disposition


def test_pruned_video_is_marked_gone(tv_app, monkeypatch):
    http = tv_app.extensions["weirdtv"]["http"]
    monkeypatch.setattr(http, "get", lambda url, **kw: FakeResponse(404))
    c = tv_app.test_client()
    assert c.get("/video/wsg-111.webm").status_code == 404
    assert tv_app.extensions["weirdtv"]["db"].get("wsg-111.webm")["gone"] == 1


def test_archived_clip_served_from_disk(tv_app, tmp_path, monkeypatch):
    store = tv_app.extensions["weirdtv"]["db"]
    (tmp_path / "archive" / "wsg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archive" / "wsg" / "111.webm").write_bytes(b"0123456789")
    store.mark_archived("wsg-111.webm", "wsg/111.webm", 10)
    store.mark_gone("wsg-111.webm")
    monkeypatch.setattr(tv_app.extensions["weirdtv"]["http"], "get", lambda *a, **k: pytest.fail("proxied"))
    resp = tv_app.test_client().get("/video/wsg-111.webm", headers={"Range": "bytes=2-4"})
    assert resp.status_code == 206 and resp.data == b"234"
    assert resp.headers["Content-Type"] == "video/webm"


def test_share_page(tv_app):
    c = tv_app.test_client()
    page = c.get("/v/wsg-111.webm").get_data(as_text=True)
    assert '"id": "wsg-111.webm"' in page and "YLYL thread - Weird TV" in page
    assert c.get("/v/missing.webm").status_code == 302


def test_update_requires_token(tv_app, monkeypatch):
    c = tv_app.test_client()
    assert c.get("/___update/").status_code == 405
    assert c.post("/___update/").status_code == 403
    monkeypatch.setattr(app_module.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    assert c.post("/___update/", headers={"X-Update-Token": "secret"}).status_code == 202


def test_empty_library(tmp_path):
    tv = make_app(tmp_path)
    c = tv.test_client()
    channer = tv.extensions["weirdtv"]["channer"]
    data = c.get("/api/next").get_json()
    assert data["offair"] is True and data["reason"] == "scanning" and data["retry"] == 5
    channer.status.update(scanning=True, board="wsg", done=12, total=150)
    assert c.get("/api/next").get_json()["message"] == "TUNING IN... SCANNING /wsg/ 12/150"
    channer.status.update(scanning=False, finished=1, error="HTTP 403 (https://a.4cdn.org/wsg/threads.json)")
    data = c.get("/api/next").get_json()
    assert data["reason"] == "error" and "HTTP 403" in data["message"]
    assert c.get("/").status_code == 200


def test_legacy_playlist_is_imported(tmp_path):
    legacy = tmp_path / "videos.json"
    legacy.write_text(json.dumps({"videos": ["https://i.4cdn.org/wsg/5.webm"]}))
    tv = make_app(tmp_path, LEGACY_PLAYLIST=str(legacy))
    assert tv.test_client().get("/api/next").get_json()["id"] == "wsg-5.webm"


def test_channel_file_validation(tmp_path):
    path = tmp_path / "channels.json"
    path.write_text(json.dumps([{"number": 1, "slug": "a", "name": "A"}, {"number": 1, "slug": "b", "name": "B"}]))
    with pytest.raises(ValueError):
        load_channels(str(path))
