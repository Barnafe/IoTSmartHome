import os
import tempfile
from pathlib import Path


def test_schema_runs_once_per_database():
    import database
    old = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "s.db"
    calls = []
    orig = database._execute_schema
    database._execute_schema = lambda conn: (calls.append(1), orig(conn))[1]
    try:
        for _ in range(5):
            database.init_db()
            database.get_events(1)
        assert len(calls) == 1
    finally:
        database._execute_schema = orig
        database.DB_PATH = old


def test_pool_reuses_and_replaces_connections(monkeypatch):
    import database

    class FakeConn:
        made = 0
        def __init__(self):
            FakeConn.made += 1; self.closed = False; self.broken = False; self.commits = 0
        def execute(self, *a): return self
        def commit(self): self.commits += 1
        def rollback(self): pass
        def close(self): self.closed = True

    import types, sys
    fake = types.SimpleNamespace(connect=lambda *a, **k: FakeConn())
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    monkeypatch.setitem(sys.modules, "psycopg.rows", types.SimpleNamespace(dict_row=None))
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    database._PooledPg._idle.clear()
    for _ in range(5):
        with database._connect() as c:
            c.execute("SELECT 1")
    assert FakeConn.made == 1                      # one connection served five uses
    database._PooledPg._idle[0].broken = True      # server dropped it
    with database._connect() as c:
        pass
    assert FakeConn.made == 2                      # replaced, no crash
    database._PooledPg._idle.clear()


def test_healthz_gzip_and_static_cache():
    os.environ["EMBEDDED_WORKER"] = "false"
    from dashboard.app import app
    c = app.test_client()
    assert c.get("/healthz").data == b"ok"
    r = c.get("/dashboard", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("Content-Encoding") == "gzip" and len(r.data) < 20000
    assert "max-age" in c.get("/static/img/logo.png").headers.get("Cache-Control", "")
    assert Path("gunicorn.conf.py").read_text().count("gthread") == 1
    assert (Path("dashboard/static/img/logo.png").stat().st_size) < 60000


def test_phone_home_page_shows_whole_picture():
    html = Path("dashboard/templates/home.html").read_text(encoding="utf-8")
    assert "aspect-ratio:756/443" in html and "background-size:100% 100%" in html
