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
  last_refreshed_at TEXT NOT NULL,
  archived INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS digests (
  id INTEGER PRIMARY KEY,
  week TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_items (
  id INTEGER PRIMARY KEY,
  digest_id INTEGER NOT NULL REFERENCES digests(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  source_name TEXT,
  summary TEXT,
  why_relevant TEXT,
  attention TEXT,
  published_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_entries (
  url TEXT PRIMARY KEY,
  seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digest_items_digest ON digest_items(digest_id);
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
    archived: bool = False


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


@dataclass
class Digest:
    id: int
    week: str
    created_at: str
    generated_at: str


@dataclass
class DigestItem:
    id: int
    digest_id: int
    title: str
    url: str
    source_name: str | None
    summary: str | None
    why_relevant: str | None
    attention: str | None
    published_at: str | None
    created_at: str


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


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotente Schema-Migrationen für bestehende DBs (CREATE TABLE IF NOT
    EXISTS ändert vorhandene Tabellen nicht)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(topics)")}
    if "archived" not in cols:
        conn.execute("ALTER TABLE topics ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


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


def _topic_from_row(row) -> Topic:
    d = dict(row)
    d["archived"] = bool(d.get("archived", 0))
    return Topic(**d)


def list_topics(include_archived: bool = False) -> list[Topic]:
    sql = "SELECT * FROM topics"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY last_refreshed_at DESC"
    with connect() as conn:
        rows = conn.execute(sql).fetchall()
    return [_topic_from_row(r) for r in rows]


def get_topic(slug: str) -> Topic | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM topics WHERE slug = ?", (slug,)).fetchone()
    return _topic_from_row(row) if row else None


def set_topic_archived(slug: str, archived: bool) -> bool:
    """Setze/entferne den Archiv-Status. Liefert True, wenn ein Topic getroffen wurde."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE topics SET archived = ? WHERE slug = ?",
            (1 if archived else 0, slug),
        )
        return cur.rowcount > 0


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
    return [_topic_from_row(r) for r in rows]


# --- Wöchentliches Briefing (Digests) ------------------------------------

def upsert_digest(week: str) -> int:
    """Lege den Digest einer ISO-Woche an oder hole die bestehende id; aktualisiere generated_at."""
    ts = now_iso()
    with connect() as conn:
        row = conn.execute("SELECT id FROM digests WHERE week = ?", (week,)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO digests (week, created_at, generated_at) VALUES (?, ?, ?)",
                (week, ts, ts),
            )
            return int(cur.lastrowid)
        conn.execute("UPDATE digests SET generated_at = ? WHERE id = ?", (ts, row["id"]))
        return int(row["id"])


def replace_digest_items(digest_id: int, items: list[dict]) -> None:
    """Ersetze alle Items eines Digests. Erwartet dicts mit
    title, url, source_name, summary, why_relevant, attention, published_at."""
    ts = now_iso()
    with connect() as conn:
        conn.execute("DELETE FROM digest_items WHERE digest_id = ?", (digest_id,))
        for it in items:
            conn.execute(
                """INSERT INTO digest_items
                   (digest_id, title, url, source_name, summary, why_relevant, attention, published_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    digest_id,
                    it["title"],
                    it["url"],
                    it.get("source_name"),
                    it.get("summary"),
                    it.get("why_relevant"),
                    it.get("attention"),
                    it.get("published_at"),
                    ts,
                ),
            )


def list_digests() -> list[Digest]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM digests ORDER BY week DESC").fetchall()
    return [Digest(**dict(r)) for r in rows]


def get_digest(week: str) -> Digest | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM digests WHERE week = ?", (week,)).fetchone()
    return Digest(**dict(row)) if row else None


def get_digest_items(digest_id: int) -> list[DigestItem]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM digest_items WHERE digest_id = ? ORDER BY id", (digest_id,)
        ).fetchall()
    return [DigestItem(**dict(r)) for r in rows]


def mark_seen(urls: list[str]) -> None:
    """Merke URLs als bereits verarbeitet (Dedup über Wochen hinweg)."""
    ts = now_iso()
    with connect() as conn:
        for u in urls:
            conn.execute(
                "INSERT OR IGNORE INTO seen_entries (url, seen_at) VALUES (?, ?)", (u, ts)
            )


def filter_unseen(urls: list[str]) -> list[str]:
    """Gib nur die URLs zurück, die noch nicht in ``seen_entries`` stehen (Reihenfolge bleibt erhalten)."""
    if not urls:
        return []
    with connect() as conn:
        rows = conn.execute("SELECT url FROM seen_entries").fetchall()
    seen = {r["url"] for r in rows}
    return [u for u in urls if u not in seen]
