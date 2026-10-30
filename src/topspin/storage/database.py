"""SQLite index over the journal for inspection and ad-hoc analysis.

The append-only JSONL journal is the source of truth; this database is a derived,
rebuildable index (idempotent inserts keyed by journal file + sequence). SQLite keeps the
local setup dependency-free; the schema is plain SQL and portable to PostgreSQL.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_DDL = [
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS records (
        source TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
        local_ts_ns INTEGER NOT NULL, source_ts_ns INTEGER, match_key TEXT, market_slug TEXT,
        payload TEXT NOT NULL, PRIMARY KEY (source, seq))""",
    "CREATE INDEX IF NOT EXISTS ix_records_kind ON records(kind, local_ts_ns)",
    "CREATE INDEX IF NOT EXISTS ix_records_match ON records(match_key, local_ts_ns)",
    """CREATE TABLE IF NOT EXISTS mappings (
        market_slug TEXT PRIMARY KEY, match_key TEXT NOT NULL, provider TEXT NOT NULL,
        provider_match_id TEXT, long_competitor_id TEXT, short_competitor_id TEXT,
        method TEXT NOT NULL, first_seen_ns INTEGER NOT NULL)""",
]


class Database:
    def __init__(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        for stmt in _DDL:
            self.conn.execute(stmt)
        cur = self.conn.execute("SELECT version FROM schema_version").fetchone()
        if cur is None:
            self.conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        elif cur[0] != SCHEMA_VERSION:
            raise RuntimeError(f"database schema {cur[0]} != {SCHEMA_VERSION}; rebuild the index")
        self.conn.commit()

    def index_records(self, source: str, records: Iterable[dict[str, Any]]) -> int:
        n = 0
        for r in records:
            p = r.get("payload") or {}
            match_key = p.get("match_key") or p.get("event_slug")
            slug = p.get("market_slug") or p.get("slug")
            if r["kind"] == "fill":
                match_key, slug = p["fill"]["match_key"], p["fill"]["market_slug"]
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO records VALUES (?,?,?,?,?,?,?,?)",
                (
                    source,
                    r["seq"],
                    r["kind"],
                    r["local_ts_ns"],
                    r.get("source_ts_ns"),
                    match_key,
                    slug,
                    json.dumps(p),
                ),
            )
            n += cur.rowcount
            if r["kind"] == "mapping":
                link = p["link"]
                self.conn.execute(
                    "INSERT OR IGNORE INTO mappings VALUES (?,?,?,?,?,?,?,?)",
                    (
                        link["market_slug"],
                        link["match_key"],
                        link["provider"],
                        link["provider_match_id"],
                        link["long_competitor_id"],
                        link["short_competitor_id"],
                        link["method"],
                        r["local_ts_ns"],
                    ),
                )
        self.conn.commit()
        return n

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        return list(self.conn.execute(sql, params))

    def close(self) -> None:
        self.conn.close()
