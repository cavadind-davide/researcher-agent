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
