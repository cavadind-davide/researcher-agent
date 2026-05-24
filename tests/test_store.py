"""Round-trip-Tests für den SQLite-Store (gegen Temp-DB)."""
from __future__ import annotations

from researcher import store


def test_upsert_and_get_topic(temp_db):
    tid = store.upsert_topic(slug="s1", question="Frage?", tldr="A\nB", body_md="## H", tags="iam,cloud")
    t = store.get_topic("s1")
    assert t is not None
    assert t.id == tid
    assert t.question == "Frage?"
    assert t.tldr == "A\nB"
    assert t.body_md == "## H"
    assert t.tags == "iam,cloud"


def test_upsert_topic_updates_existing(temp_db):
    tid1 = store.upsert_topic(slug="s1", question="Q1", tldr="A", body_md="b", tags="")
    tid2 = store.upsert_topic(slug="s1", question="Q2", tldr="A2", body_md="b2", tags="cloud")
    assert tid1 == tid2  # gleicher Slug -> Update, kein zweites Topic
    assert len(store.list_topics()) == 1
    assert store.get_topic("s1").question == "Q2"


def test_replace_and_get_sources(temp_db):
    tid = store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    store.replace_sources(tid, [
        {"url": "https://a", "title": "A", "etag": "e", "last_modified": "lm", "content_sha256": "h"},
        {"url": "https://b", "title": None, "etag": None, "last_modified": None, "content_sha256": None},
    ])
    srcs = store.get_sources(tid)
    assert [s.url for s in srcs] == ["https://a", "https://b"]
    assert srcs[0].etag == "e"
    assert srcs[0].is_stale is False


def test_replace_sources_overwrites(temp_db):
    tid = store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    store.replace_sources(tid, [{"url": "https://a"}])
    store.replace_sources(tid, [{"url": "https://b"}, {"url": "https://c"}])
    assert [s.url for s in store.get_sources(tid)] == ["https://b", "https://c"]


def test_update_source_freshness(temp_db):
    tid = store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    store.replace_sources(tid, [{"url": "https://a", "content_sha256": "old"}])
    sid = store.get_sources(tid)[0].id
    store.update_source_freshness(sid, etag="e2", last_modified="lm2", content_sha256="new", is_stale=True)
    s = store.get_sources(tid)[0]
    assert s.etag == "e2"
    assert s.content_sha256 == "new"
    assert s.is_stale is True


def test_mark_topic_refreshed_clears_stale(temp_db):
    tid = store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    store.replace_sources(tid, [{"url": "https://a"}])
    sid = store.get_sources(tid)[0].id
    store.update_source_freshness(sid, etag=None, last_modified=None, content_sha256="h", is_stale=True)
    store.mark_topic_refreshed(tid)
    assert store.get_sources(tid)[0].is_stale is False


# --- Digests --------------------------------------------------------------

def test_upsert_digest_is_idempotent_per_week(temp_db):
    d1 = store.upsert_digest("2026-W21")
    d2 = store.upsert_digest("2026-W21")
    assert d1 == d2
    assert len(store.list_digests()) == 1


def test_replace_and_get_digest_items(temp_db):
    did = store.upsert_digest("2026-W21")
    store.replace_digest_items(did, [
        {
            "title": "Kritische RCE in Foo", "url": "https://a/1", "source_name": "BSI",
            "summary": "Zusammenfassung.", "why_relevant": "Betrifft Edge-Geräte.",
            "attention": "Sofort patchen.", "published_at": "2026-05-20",
        },
        {"title": "Item ohne Extras", "url": "https://a/2"},
    ])
    items = store.get_digest_items(did)
    assert [i.url for i in items] == ["https://a/1", "https://a/2"]
    assert items[0].source_name == "BSI"
    assert items[0].why_relevant == "Betrifft Edge-Geräte."
    assert items[1].summary is None


def test_replace_digest_items_overwrites(temp_db):
    did = store.upsert_digest("2026-W21")
    store.replace_digest_items(did, [{"title": "alt", "url": "https://a/1"}])
    store.replace_digest_items(did, [{"title": "neu", "url": "https://a/2"}])
    assert [i.url for i in store.get_digest_items(did)] == ["https://a/2"]


def test_seen_entries_dedup(temp_db):
    urls = ["https://a/1", "https://a/2", "https://a/3"]
    assert store.filter_unseen(urls) == urls  # anfangs nichts gesehen
    store.mark_seen(["https://a/1", "https://a/3"])
    assert store.filter_unseen(urls) == ["https://a/2"]
    store.mark_seen(["https://a/1"])  # erneut markieren ist idempotent
    assert store.filter_unseen(urls) == ["https://a/2"]


def test_filter_unseen_empty(temp_db):
    assert store.filter_unseen([]) == []


# --- Archivierung ---------------------------------------------------------

def test_archive_and_unarchive_topic(temp_db):
    store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    assert store.get_topic("s1").archived is False
    assert store.set_topic_archived("s1", True) is True
    assert store.get_topic("s1").archived is True
    assert store.list_topics() == []                       # Default ohne Archiv
    assert len(store.list_topics(include_archived=True)) == 1
    store.set_topic_archived("s1", False)
    assert len(store.list_topics()) == 1


def test_set_topic_archived_unknown_slug(temp_db):
    assert store.set_topic_archived("gibtsnicht", True) is False


def test_migration_adds_archived_to_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE topics (
             id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, question TEXT NOT NULL,
             tldr TEXT, body_md TEXT, tags TEXT, created_at TEXT NOT NULL, last_refreshed_at TEXT NOT NULL
           );"""
    )
    conn.execute(
        "INSERT INTO topics (slug, question, created_at, last_refreshed_at) VALUES ('s','q','t','t')"
    )
    conn.commit()
    conn.close()

    store.init_db(db)  # soll die fehlende Spalte ergänzen

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(topics)")}
    assert "archived" in cols
    assert conn.execute("SELECT archived FROM topics WHERE slug='s'").fetchone()["archived"] == 0
    conn.close()
