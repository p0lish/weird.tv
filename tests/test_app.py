import json

import pytest

import app as app_module
from app import create_app
from channer import Channer, read_playlist


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


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, headers or {}))
        return self.routes.get(url, FakeResponse(404))


THREADS = [{"page": 1, "threads": [{"no": 1}, {"no": 2}]}]
THREAD_1 = {"posts": [
    {"no": 1, "sub": "funny", "tim": 111, "ext": ".webm", "filename": "a"},
    {"no": 3, "tim": 112, "ext": ".jpg", "filename": "b"},
    {"no": 4, "tim": 113, "ext": ".mp4", "filename": "c"},
]}
THREAD_2 = {"posts": [{"no": 2, "tim": 211, "ext": ".webm", "filename": "d", "filedeleted": 1}]}


def api_routes():
    return {
        "https://a.4cdn.org/wsg/threads.json": FakeResponse(200, THREADS, {"Last-Modified": "x"}),
        "https://a.4cdn.org/wsg/thread/1.json": FakeResponse(200, THREAD_1, {"Last-Modified": "y"}),
        "https://a.4cdn.org/wsg/thread/2.json": FakeResponse(200, THREAD_2),
    }


def test_channer_collects_videos(tmp_path):
    path = tmp_path / "videos.json"
    session = FakeSession(api_routes())
    channer = Channer(playlist_path=str(path), min_interval=0, session=session)
    assert channer.update() == 2
    videos = read_playlist(str(path))
    assert [v["id"] for v in videos] == ["wsg-111.webm", "wsg-113.mp4"]
    assert videos[0]["title"] == "funny"
    assert videos[0]["url"] == "https://i.4cdn.org/wsg/111.webm"

    # second run sends If-Modified-Since and reuses cached data on 304
    session.routes["https://a.4cdn.org/wsg/thread/1.json"] = FakeResponse(304)
    assert channer.update() == 2
    assert ("https://a.4cdn.org/wsg/thread/1.json", {"If-Modified-Since": "y"}) in session.calls


def test_channer_keeps_playlist_when_scrape_fails(tmp_path):
    path = tmp_path / "videos.json"
    path.write_text(json.dumps({"videos": ["https://i.4cdn.org/wsg/1.webm"]}))
    channer = Channer(playlist_path=str(path), min_interval=0, session=FakeSession({}))
    assert channer.update() == 0
    assert read_playlist(str(path))[0]["id"] == "wsg-1.webm"


@pytest.fixture
def tv_app(tmp_path):
    path = tmp_path / "videos.json"
    Channer(playlist_path=str(path), min_interval=0, session=FakeSession(api_routes())).update()
    app = create_app({"PLAYLIST_PATH": str(path), "REFRESH_MINUTES": 0, "UPDATE_TOKEN": "secret"})
    return app


def test_next_cycles_without_repeats(tv_app):
    c = tv_app.test_client()
    ids = {c.get("/api/next").get_json()["id"] for _ in range(2)}
    assert ids == {"wsg-111.webm", "wsg-113.mp4"}
    item = c.get("/api/next").get_json()
    assert item["src"] == "/video/" + item["id"]


def test_video_proxy_only_serves_playlist(tv_app, monkeypatch):
    http = tv_app.extensions["weirdtv"]["http"]
    monkeypatch.setattr(http, "get", lambda url, **kw: FakeResponse(
        206, headers={"Content-Type": "video/webm", "Content-Range": "bytes 0-2/3"}, body=b"abc"))
    c = tv_app.test_client()
    resp = c.get("/video/wsg-111.webm", headers={"Range": "bytes=0-"})
    assert resp.status_code == 206
    assert resp.data == b"abc"
    assert resp.headers["Content-Range"] == "bytes 0-2/3"
    assert c.get("/video/evil").status_code == 404


def test_pruned_video_is_dropped(tv_app, monkeypatch):
    http = tv_app.extensions["weirdtv"]["http"]
    monkeypatch.setattr(http, "get", lambda url, **kw: FakeResponse(404))
    c = tv_app.test_client()
    assert c.get("/video/wsg-111.webm").status_code == 404
    assert c.get("/api/status").get_json()["videos"] == 1


def test_update_requires_token(tv_app, monkeypatch):
    c = tv_app.test_client()
    assert c.get("/___update/").status_code == 405
    assert c.post("/___update/").status_code == 403
    monkeypatch.setattr(app_module.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    assert c.post("/___update/", headers={"X-Update-Token": "secret"}).status_code == 202


def test_empty_playlist(tmp_path):
    app = create_app({"PLAYLIST_PATH": str(tmp_path / "none.json"), "REFRESH_MINUTES": 0})
    assert app.test_client().get("/api/next").status_code == 503
    assert app.test_client().get("/").status_code == 200
