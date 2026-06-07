from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import utc_now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    report_id   TEXT PRIMARY KEY,
    report_type TEXT,
    date        TEXT,
    created_at  TEXT NOT NULL,
    markdown    TEXT NOT NULL
);
"""


class Storage:
    """SQLite persistence for the MCP server.

    Holds singleton settings (profile, selected accounts, last sync) as a JSON
    key/value table, plus history collections: portfolio snapshots,
    recommendations, and generated reports. Use ``":memory:"`` for tests.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- settings (JSON KV) --------------------------------------------------
    def get_setting(self, key: str) -> Any | None:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self._conn.commit()

    # -- snapshots -----------------------------------------------------------
    def save_snapshot(self, snapshot_id: str, data: dict[str, Any], created_at: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO snapshots(snapshot_id, created_at, data) VALUES(?, ?, ?) "
            "ON CONFLICT(snapshot_id) DO UPDATE SET created_at = excluded.created_at, data = excluded.data",
            (snapshot_id, created_at or utc_now_iso(), json.dumps(data, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT snapshot_id, created_at, data FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        return {"snapshot_id": row["snapshot_id"], "created_at": row["created_at"], **json.loads(row["data"])}

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT snapshot_id, created_at FROM snapshots ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{"snapshot_id": r["snapshot_id"], "created_at": r["created_at"]} for r in rows]

    # -- recommendations -----------------------------------------------------
    def save_recommendation(self, recommendation_id: str, data: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT INTO recommendations(id, created_at, data) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET created_at = excluded.created_at, data = excluded.data",
            (recommendation_id, utc_now_iso(), json.dumps(data, ensure_ascii=False)),
        )
        self._conn.commit()

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT data FROM recommendations WHERE id = ?", (recommendation_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    # -- reports -------------------------------------------------------------
    def save_report(self, report_id: str, report_type: str, date: str, markdown: str) -> None:
        self._conn.execute(
            "INSERT INTO reports(report_id, report_type, date, created_at, markdown) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(report_id) DO UPDATE SET report_type = excluded.report_type, "
            "date = excluded.date, created_at = excluded.created_at, markdown = excluded.markdown",
            (report_id, report_type, date, utc_now_iso(), markdown),
        )
        self._conn.commit()

    def get_report(self, report_type: str, date: str) -> str | None:
        row = self._conn.execute(
            "SELECT markdown FROM reports WHERE report_type = ? AND date = ?", (report_type, date)
        ).fetchone()
        return row["markdown"] if row else None

    def close(self) -> None:
        self._conn.close()
