"""Tests für Feed-Ingestion, Auswahl-Logik und den Wochenlauf (ohne Netz)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from researcher import digest, store

UTC = timezone.utc

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test Feed</title>
<item><title>Entry One</title><link>https://t/1</link>
<pubDate>Wed, 20 May 2026 10:00:00 GMT</pubDate>
<description>&lt;p&gt;Hallo &amp;amp; Welt&lt;/p&gt;</description></item>
<item><title>Entry Two</title><link>https://t/2</link>
<pubDate>Tue, 19 May 2026 10:00:00 GMT</pubDate>
<description>Zweiter Eintrag</description></item>
</channel></rss>"""


# --- pure helpers ---------------------------------------------------------

def test_current_week_format():
    assert digest.current_week(datetime(2026, 1, 5, tzinfo=UTC)) == "2026-W02"


def test_clean_excerpt_strips_html_and_entities():
    assert digest._clean_excerpt("<p>Hallo &amp; <b>Welt</b></p>") == "Hallo & Welt"


def test_clean_excerpt_truncates():
    assert len(digest._clean_excerpt("x" * 1000, limit=50)) == 50


def test_entry_datetime_published_and_fallback():
    assert digest._entry_datetime({"published_parsed": (2026, 5, 20, 10, 0, 0, 0, 0, 0)}) == datetime(2026, 5, 20, 10, 0, tzinfo=UTC)
    assert digest._entry_datetime({"updated_parsed": (2026, 5, 1, 0, 0, 0, 0, 0, 0)}) == datetime(2026, 5, 1, tzinfo=UTC)
    assert digest._entry_datetime({}) is None


def _entry(url, published):
    return digest.FeedEntry("S", "c", f"T {url}", url, published, "ex")


def test_select_recent_windows_sorts_and_caps():
    cutoff = datetime(2026, 5, 14, tzinfo=UTC)
    entries = [
        _entry("https://old", datetime(2026, 5, 1, tzinfo=UTC)),    # vor cutoff -> raus
        _entry("https://mid", datetime(2026, 5, 18, tzinfo=UTC)),
        _entry("https://new", datetime(2026, 5, 20, tzinfo=UTC)),
        _entry("https://nodate", None),                              # bleibt, ans Ende
    ]
    out = digest._select_recent(entries, cutoff, cap=2)
    assert [e.url for e in out] == ["https://new", "https://mid"]


def test_select_recent_keeps_undated_within_cap():
    cutoff = datetime(2026, 5, 14, tzinfo=UTC)
    out = digest._select_recent([_entry("https://nodate", None)], cutoff, cap=5)
    assert [e.url for e in out] == ["https://nodate"]


# --- _fetch_feed (httpx gemockt) -----------------------------------------

def test_fetch_feed_parses_entries():
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "application/rss+xml"}, content=SAMPLE_RSS)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await digest._fetch_feed(client, {"name": "Test", "url": "https://t/feed", "category": "news"})

    entries = asyncio.run(go())
    assert [e.url for e in entries] == ["https://t/1", "https://t/2"]
    assert entries[0].title == "Entry One"
    assert entries[0].summary == "Hallo & Welt"
    assert entries[0].source_name == "Test"
    assert entries[0].published == datetime(2026, 5, 20, 10, 0, tzinfo=UTC)


def test_fetch_feed_returns_empty_on_http_error():
    def handler(request):
        return httpx.Response(500)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await digest._fetch_feed(client, {"name": "Test", "url": "https://t/feed"})

    assert asyncio.run(go()) == []


# --- run_weekly_scan (collect + agent gemockt) ---------------------------

def test_run_weekly_scan_persists_and_dedups(temp_db, monkeypatch):
    entries = [
        digest.FeedEntry("BSI", "advisory", "Titel A", "https://a/1", datetime(2026, 5, 20, tzinfo=UTC), "exA"),
        digest.FeedEntry("Heise", "news", "Titel B", "https://a/2", None, "exB"),
    ]
    monkeypatch.setattr(digest, "collect_entries", lambda *, now=None: entries)

    def fake_summarize(candidates):
        # Agent wählt a/1 und liefert zusätzlich eine halluzinierte URL (muss verworfen werden).
        return [
            {"url": "https://a/1", "title": "Titel A", "summary": "S", "why_relevant": "W", "attention": "AT"},
            {"url": "https://evil/halluziniert", "title": "X", "summary": "s"},
        ]
    monkeypatch.setattr(digest.agent, "summarize_digest", fake_summarize)

    now = datetime(2026, 5, 21, tzinfo=UTC)
    result = digest.run_weekly_scan(now=now)
    assert result["candidates"] == 2
    assert result["items"] == 1  # halluzinierte URL verworfen

    d = store.get_digest(digest.current_week(now))
    items = store.get_digest_items(d.id)
    assert len(items) == 1
    assert items[0].url == "https://a/1"
    assert items[0].source_name == "BSI"
    assert items[0].published_at == "2026-05-20"
    assert items[0].why_relevant == "W"
    # beide Kandidaten als gesehen markiert (Dedup über Wochen)
    assert store.filter_unseen(["https://a/1", "https://a/2"]) == []


def test_run_weekly_scan_no_entries(temp_db, monkeypatch):
    monkeypatch.setattr(digest, "collect_entries", lambda *, now=None: [])
    now = datetime(2026, 5, 21, tzinfo=UTC)
    result = digest.run_weekly_scan(now=now)
    assert result == {"week": digest.current_week(now), "candidates": 0, "items": 0}
    assert store.get_digest(digest.current_week(now)) is None
