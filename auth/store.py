"""SQLite storage for tenants, API keys and usage.

**This is a separate database from the scraped snapshot, on purpose.**
`foodpanda-scraper/foodpanda.db` is republished by the Milestone 2 daily
scrape, which copies the file, scrapes for ~25 minutes, then atomically
replaces the original. Any tenant or usage row written during that window
would be silently discarded by the swap, because the copy predates it. Billing
data that disappears for 25 minutes a night is worse than no billing data, so
tenant state lives here instead.

Path: `data/tenants.db`, overridable with `TENANT_DB_PATH`.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "tenants.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    tier        TEXT    NOT NULL DEFAULT 'free',
    created_at  TEXT    NOT NULL,
    disabled_at TEXT
);

-- key_hash is SHA-256 of the presented key. The plaintext key is shown once at
-- creation and never stored. key_prefix is the first few characters, kept in
-- clear purely so a key can be identified in a list or a log line.
CREATE TABLE IF NOT EXISTS api_keys (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    INTEGER NOT NULL REFERENCES tenants(id),
    key_hash     TEXT    NOT NULL UNIQUE,
    key_prefix   TEXT    NOT NULL,
    label        TEXT,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

-- One row per authenticated request, including rejected ones (429s carry
-- status_code 429), so the log shows demand and not just served traffic.
CREATE TABLE IF NOT EXISTS usage_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id            INTEGER NOT NULL,
    tenant_id         INTEGER NOT NULL,
    created_at        TEXT    NOT NULL,
    route             TEXT    NOT NULL,
    method            TEXT    NOT NULL,
    status_code       INTEGER NOT NULL,
    latency_ms        REAL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    llm_rounds        INTEGER,
    provider          TEXT,
    model             TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_events_key ON usage_events(key_id, created_at);

-- The quota ledger: counts requests *admitted* against the daily limit, so it
-- stays meaningful for billing. Durable on purpose — a daily quota held only in
-- memory resets on every restart, which makes it not a quota.
CREATE TABLE IF NOT EXISTS usage_daily (
    key_id            INTEGER NOT NULL,
    tenant_id         INTEGER NOT NULL,
    day               TEXT    NOT NULL,
    requests          INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, day)
);
"""


def db_path() -> Path:
    override = os.getenv("TENANT_DB_PATH")
    return Path(override) if override else DEFAULT_DB


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day() -> str:
    """Quota day. UTC so the reset point does not move with the host clock."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL suits a request-path database: readers never block the writer. Safe
    # here precisely because nothing republishes this file behind our back.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Short-lived connection. Cheap for SQLite and avoids sharing one
    connection across FastAPI's worker threads."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
