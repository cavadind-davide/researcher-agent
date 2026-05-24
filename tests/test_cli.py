"""Tests für CLI-Hilfsfunktionen: URL-Safety und Persistenz-Filter."""
from __future__ import annotations

import pytest
import typer

from researcher import cli, store


@pytest.mark.parametrize("url", [
    "https://example.com/x",
    "http://example.com",
    "https://sub.domain.tld/path?q=1",
])
def test_is_safe_url_accepts_http(url):
    assert cli._is_safe_url(url) is True


@pytest.mark.parametrize("url", [
    None, "", "   ",
    "javascript:alert(1)",
    "data:text/html,<script>",
    "file:///etc/passwd",
    "ftp://example.com",
    "https://",            # kein Host
    "not a url",
])
def test_is_safe_url_rejects_unsafe(url):
    assert cli._is_safe_url(url) is False


def _payload(sources):
    return {
        "slug": "s1", "question": "Frage?", "tldr": ["A"], "tags": ["iam"],
        "body_md": "## H", "sources": sources,
    }


def test_persist_drops_unsafe_urls(temp_db, monkeypatch):
    monkeypatch.setattr(
        cli.sources, "baseline_urls",
        lambda urls: [{"etag": None, "last_modified": None, "content_sha256": "h"} for _ in urls],
    )
    payload = _payload([
        {"url": "https://ok.test", "title": "OK"},
        {"url": "javascript:alert(1)", "title": "Böse"},
    ])
    tid = cli._persist(payload)
    srcs = store.get_sources(tid)
    assert [s.url for s in srcs] == ["https://ok.test"]


def test_persist_exits_when_no_safe_sources(temp_db, monkeypatch):
    monkeypatch.setattr(
        cli.sources, "baseline_urls",
        lambda urls: [{"etag": None, "last_modified": None, "content_sha256": "h"} for _ in urls],
    )
    payload = _payload([{"url": "javascript:alert(1)", "title": "Böse"}])
    with pytest.raises(typer.Exit):
        cli._persist(payload)


# --- archive-topic / unarchive-topic --------------------------------------

def test_archive_topic_cli(temp_db, monkeypatch, tmp_path):
    monkeypatch.setattr(cli.render, "DIST_DIR", tmp_path / "dist")
    store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    cli.archive_topic("s1")
    assert store.get_topic("s1").archived is True
    cli.unarchive_topic("s1")
    assert store.get_topic("s1").archived is False


def test_archive_topic_cli_unknown_exits(temp_db):
    with pytest.raises(typer.Exit):
        cli.archive_topic("gibtsnicht")


# --- add-feed -------------------------------------------------------------

def test_add_feed_valid(tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli.feeds, "FEEDS_PATH", p)
    monkeypatch.setattr(cli.digest_mod, "validate_feed", lambda url: (True, "Test Feed"))
    cli.add_feed(url="https://example.test/rss", name="Test", category="news")
    assert cli.feeds.load_feeds() == [
        {"name": "Test", "url": "https://example.test/rss", "category": "news"}
    ]


def test_add_feed_invalid_feed_exits(tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli.feeds, "FEEDS_PATH", p)
    monkeypatch.setattr(cli.digest_mod, "validate_feed", lambda url: (False, "kaputt"))
    with pytest.raises(typer.Exit):
        cli.add_feed(url="https://example.test/rss", name="Test")
    assert cli.feeds.load_feeds() == []  # nichts angehängt


def test_add_feed_unsafe_url_exits(tmp_path, monkeypatch):
    # validate_feed darf gar nicht erst erreicht werden
    monkeypatch.setattr(cli.digest_mod, "validate_feed", lambda url: (_ for _ in ()).throw(AssertionError("nicht erreichen")))
    with pytest.raises(typer.Exit):
        cli.add_feed(url="javascript:alert(1)", name="X")


def test_add_feed_duplicate_exits(tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    p.write_text('[{"name": "A", "url": "https://a", "category": ""}]', encoding="utf-8")
    monkeypatch.setattr(cli.feeds, "FEEDS_PATH", p)
    with pytest.raises(typer.Exit):
        cli.add_feed(url="https://a", name="A2")
    assert len(cli.feeds.load_feeds()) == 1


# --- intake (GitHub-Issue-Dispatch) ---------------------------------------

def test_intake_ask(temp_db, monkeypatch):
    monkeypatch.setattr(
        cli.sources, "baseline_urls",
        lambda urls: [{"etag": None, "last_modified": None, "content_sha256": "h"} for _ in urls],
    )
    monkeypatch.setattr(cli.agent, "research", lambda q: {
        "slug": "meine-frage", "question": q, "tldr": ["A"], "tags": ["iam"],
        "body_md": "## H", "sources": [{"url": "https://ok.test", "title": "T"}],
    })
    monkeypatch.setenv("INTAKE_ACTION", "ask")
    monkeypatch.setenv("ISSUE_BODY", "### Frage\n\nMeine Frage?")
    cli.intake()
    assert store.get_topic("meine-frage") is not None


def test_intake_archive(temp_db, monkeypatch):
    store.upsert_topic(slug="s1", question="Q", tldr="A", body_md="b", tags="")
    monkeypatch.setenv("INTAKE_ACTION", "archive")
    monkeypatch.setenv("ISSUE_BODY", "### Slug\n\ns1")
    cli.intake()
    assert store.get_topic("s1").archived is True


def test_intake_add_feed(temp_db, tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    p.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(cli.feeds, "FEEDS_PATH", p)
    monkeypatch.setattr(cli.digest_mod, "validate_feed", lambda url: (True, "Feed"))
    monkeypatch.setenv("INTAKE_ACTION", "add-feed")
    monkeypatch.setenv(
        "ISSUE_BODY",
        "### Name\n\nTalos\n\n### URL\n\nhttps://talos.test/rss\n\n### Kategorie\n\nthreat-intel",
    )
    cli.intake()
    feeds_now = cli.feeds.load_feeds()
    assert feeds_now == [{"name": "Talos", "url": "https://talos.test/rss", "category": "threat-intel"}]


def test_intake_unknown_action_exits(temp_db, monkeypatch):
    monkeypatch.setenv("INTAKE_ACTION", "bogus")
    monkeypatch.setenv("ISSUE_BODY", "")
    with pytest.raises(typer.Exit):
        cli.intake()
