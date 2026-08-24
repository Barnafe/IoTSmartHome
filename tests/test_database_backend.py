import os
import tempfile
from pathlib import Path


def test_sqlite_fallback_and_crud(monkeypatch):
    import database

    monkeypatch.delenv("DATABASE_URL", raising=False)
    old_db = database.DB_PATH
    database.DB_PATH = Path(tempfile.mkdtemp()) / "backend.db"
    try:
        assert database.get_backend() == "sqlite"
        database.init_db()
        event_id = database.log_event(
            "security_breach", "critical", "gate", "Main Gate", "Test breach",
            snapshot_path="snapshots/test.jpg",
            metadata={"test": True},
        )
        assert event_id == 1
        assert database.get_event(event_id)["snapshot_path"] == "snapshots/test.jpg"
        assert database.get_stats()["incidents"] == 1
    finally:
        database.DB_PATH = old_db


def test_postgres_configuration_is_selected_without_connecting(monkeypatch):
    import database

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://user:password@example.neon.tech/db?sslmode=require",
    )
    assert database.get_backend() == "postgresql"
    assert database._database_url().startswith("postgresql://")


def test_postgres_sql_path_with_driver_stub(monkeypatch):
    """Exercise PostgreSQL SQL/placeholder paths without requiring network access."""
    import sys
    import types
    import database

    class Result:
        def __init__(self, rows=None):
            self.rows = rows or []

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.statements = []
            self.next_id = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            self.statements.append((sql, params))
            normalized = " ".join(sql.split()).lower()
            if "returning id" in normalized:
                row = {"id": self.next_id}
                self.next_id += 1
                return Result([row])
            if "count(*)" in normalized:
                return Result([(1,)])
            if normalized.startswith("select * from events"):
                return Result([])
            if normalized.startswith("select id,created_at"):
                return Result([])
            return Result([])

        def commit(self):
            pass

        def close(self):
            pass

    fake_connection = FakeConnection()

    class FakePsycopg(types.ModuleType):
        def connect(self, *args, **kwargs):
            return fake_connection

    fake_psycopg = FakePsycopg("psycopg")
    fake_rows = types.ModuleType("psycopg.rows")
    fake_rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@example/db")

    database.init_db()
    event_id = database.log_event(
        "security_breach", "critical", "gate", "Main Gate", "Postgres path"
    )
    assert event_id == 1
    assert any("INSERT INTO events" in sql for sql, _ in fake_connection.statements)
    assert any("%s" in sql for sql, _ in fake_connection.statements)


def test_retention_policy_is_configurable(monkeypatch):
    import database

    monkeypatch.setenv("RETENTION_INFO_DAYS", "14")
    monkeypatch.setenv("RETENTION_WARNING_DAYS", "60")
    monkeypatch.setenv("RETENTION_STATE_DAYS", "21")
    assert database.get_retention_policy() == {
        "info_days": 14,
        "warning_days": 60,
        "state_days": 21,
    }


def test_retention_deletes_low_value_and_preserves_critical(monkeypatch, tmp_path):
    import database
    from datetime import datetime, timedelta, timezone

    monkeypatch.delenv("DATABASE_URL", raising=False)
    old_db = database.DB_PATH
    database.DB_PATH = tmp_path / "retention.db"
    try:
        database.init_db()
        now = datetime(2026, 8, 22, 8, 0, 0, tzinfo=timezone.utc)
        old_info = (now - timedelta(days=31)).isoformat(timespec="seconds")
        old_warning = (now - timedelta(days=91)).isoformat(timespec="seconds")
        old_critical = (now - timedelta(days=365)).isoformat(timespec="seconds")
        recent_info = (now - timedelta(days=2)).isoformat(timespec="seconds")
        old_state = (now - timedelta(days=31)).isoformat(timespec="seconds")

        database.log_event("normal_scan", "info", "gas", "Kitchen", "old info")
        database.log_event("warning_event", "warning", "door", "Back Door", "old warning")
        database.log_event("security_breach", "critical", "gate", "Main Gate", "old critical")
        database.log_event("normal_scan", "info", "energy", "House", "recent info")
        database.save_state({"test": True})

        # Make timestamps deterministic/old for the test.
        with database._connect() as conn:
            conn.execute("UPDATE events SET created_at=? WHERE id=1", (old_info,))
            conn.execute("UPDATE events SET created_at=? WHERE id=2", (old_warning,))
            conn.execute("UPDATE events SET created_at=? WHERE id=3", (old_critical,))
            conn.execute("UPDATE events SET created_at=? WHERE id=4", (recent_info,))
            conn.execute("UPDATE state_history SET created_at=? WHERE id=1", (old_state,))
            conn.commit()

        result = database.cleanup_expired_records(now=now, info_days=30, warning_days=90, state_days=30)
        assert result == {"events_deleted": 2, "states_deleted": 1}

        events = database.get_events(limit=20)
        messages = {event["message"] for event in events}
        assert messages == {"old critical", "recent info"}
    finally:
        database.DB_PATH = old_db
