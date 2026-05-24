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
