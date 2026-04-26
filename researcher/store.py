"""SQLite-Layer für Topics und Quellen."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "researcher.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  question TEXT NOT NULL,
  tldr TEXT,
  body_md TEXT,
  tags TEXT,
  created_at TEXT NOT NULL,
  last_refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY,
  topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  title TEXT,
  etag TEXT,
  last_modified TEXT,
  content_sha256 TEXT,
  fetched_at TEXT NOT NULL,
  is_stale INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sources_topic ON sources(topic_id);
CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(url);
"""


@dataclass
class Topic:
    id: int
    slug: str
    question: str
    tldr: str | None
    body_md: str | None
    tags: str | None
    created_at: str
    last_refreshed_at: str


@dataclass
class Source:
    id: int
    topic_id: int
    url: str
    title: str | None
    etag: str | None
    last_modified: str | None
    content_sha256: str | None
    fetched_at: str
    is_stale: bool


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def upsert_topic(
    slug: str,
    question: str,
    tldr: str,
    body_md: str,
    tags: str,
) -> int:
    """Lege Topic an oder aktualisiere; gib topic_id zurück."""
    ts = now_iso()
    with connect() as conn:
        row = conn.execute("SELECT id, created_at FROM topics WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            cur = conn.execute(
                """INSERT INTO topics (slug, question, tldr, body_md, tags, created_at, last_refreshed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (slug, question, tldr, body_md, tags, ts, ts),
            )
            return int(cur.lastrowid)
        conn.execute(
            """UPDATE topics
               SET question = ?, tldr = ?, body_md = ?, tags = ?, last_refreshed_at = ?
               WHERE id = ?""",
            (question, tldr, body_md, tags, ts, row["id"]),
        )
        return int(row["id"])


def replace_sources(topic_id: int, sources: list[dict]) -> None:
    """Ersetze alle Sources eines Topics. Erwartet dicts mit url, title, etag, last_modified, content_sha256."""
    ts = now_iso()
    with connect() as conn:
        conn.execute("DELETE FROM sources WHERE topic_id = ?", (topic_id,))
        for s in sources:
            conn.execute(
                """INSERT INTO sources (topic_id, url, title, etag, last_modified, content_sha256, fetched_at, is_stale)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    topic_id,
                    s["url"],
                    s.get("title"),
                    s.get("etag"),
                    s.get("last_modified"),
                    s.get("content_sha256"),
                    ts,
                ),
            )


def update_source_freshness(source_id: int, *, etag: str | None, last_modified: str | None,
                            content_sha256: str | None, is_stale: bool) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE sources SET etag = ?, last_modified = ?, content_sha256 = ?,
               fetched_at = ?, is_stale = ? WHERE id = ?""",
            (etag, last_modified, content_sha256, now_iso(), 1 if is_stale else 0, source_id),
        )


def mark_topic_refreshed(topic_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE topics SET last_refreshed_at = ? WHERE id = ?",
            (now_iso(), topic_id),
        )
        conn.execute("UPDATE sources SET is_stale = 0 WHERE topic_id = ?", (topic_id,))


def list_topics() -> list[Topic]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM topics ORDER BY last_refreshed_at DESC"
        ).fetchall()
    return [Topic(**dict(r)) for r in rows]


def get_topic(slug: str) -> Topic | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM topics WHERE slug = ?", (slug,)).fetchone()
    return Topic(**dict(row)) if row else None


def get_sources(topic_id: int) -> list[Source]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sources WHERE topic_id = ? ORDER BY id", (topic_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_stale"] = bool(d["is_stale"])
        out.append(Source(**d))
    return out


def all_sources() -> list[Source]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY topic_id, id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_stale"] = bool(d["is_stale"])
        out.append(Source(**d))
    return out


def topics_with_stale_sources() -> list[Topic]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT t.* FROM topics t
               JOIN sources s ON s.topic_id = t.id
               WHERE s.is_stale = 1
               GROUP BY t.id
               ORDER BY t.last_refreshed_at"""
        ).fetchall()
    return [Topic(**dict(r)) for r in rows]
