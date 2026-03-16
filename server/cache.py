"""
Append-only SQLite cache for Revit UI observations.

Every live response from /tree, /inspect, /search is recorded here
with the document name (from Revit window title) and timestamp.
"""

import json
import sqlite3
import re
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "revit_ui_cache.db"

_CREATE = """
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document    TEXT,
    endpoint    TEXT NOT NULL,
    path        TEXT,
    query       TEXT,
    response    JSON NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_document ON observations(document);
CREATE INDEX IF NOT EXISTS idx_obs_endpoint ON observations(endpoint);
CREATE INDEX IF NOT EXISTS idx_obs_created  ON observations(created_at);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _extract_document(window_title):
    """Extract .rvt filename from Revit window title."""
    if not window_title:
        return None
    m = re.search(r'[\w\s\-\.]+\.rvt', window_title, re.IGNORECASE)
    return m.group(0).strip() if m else window_title


class Cache:
    def __init__(self, db_path=None):
        self._path = db_path or DB_PATH
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_CREATE)

    def close(self):
        self._conn.close()

    # ── Write ────────────────────────────────────────────────────────

    def record(self, document_title, endpoint, response, path=None, query=None):
        """Append an observation."""
        doc = _extract_document(document_title)
        self._conn.execute(
            "INSERT INTO observations (document, endpoint, path, query, response, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (doc, endpoint, path, query, json.dumps(response, ensure_ascii=False), _now()),
        )
        self._conn.commit()

    # ── Read ─────────────────────────────────────────────────────────

    def documents(self):
        """List distinct documents with observation counts."""
        rows = self._conn.execute(
            "SELECT document, COUNT(*) as count, MAX(created_at) as last_seen "
            "FROM observations GROUP BY document ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def history(self, document=None, endpoint=None, limit=50):
        """Get observation history, optionally filtered."""
        sql = "SELECT id, document, endpoint, path, query, created_at FROM observations WHERE 1=1"
        params = []
        if document:
            sql += " AND document = ?"
            params.append(document)
        if endpoint:
            sql += " AND endpoint = ?"
            params.append(endpoint)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_observation(self, obs_id):
        """Get a single observation with full response."""
        row = self._conn.execute(
            "SELECT * FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["response"] = json.loads(d["response"])
        return d

    def search(self, term, document=None, limit=50):
        """Full-text search across cached responses."""
        sql = "SELECT id, document, endpoint, path, query, created_at FROM observations WHERE response LIKE ?"
        params = [f"%{term}%"]
        if document:
            sql += " AND document = ?"
            params.append(document)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
