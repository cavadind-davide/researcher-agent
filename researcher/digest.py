"""Wöchentliches Security-Briefing.

Ablauf: kuratierte Feeds (:mod:`researcher.feeds`) einsammeln → neue Einträge der
letzten Woche bestimmen (Dedup über :func:`store.filter_unseen`) → den Agent die
relevanten auswählen und zusammenfassen lassen → als Wochen-Digest persistieren.
"""
from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from . import agent, store
from .feeds import load_feeds

USER_AGENT = "ResearcherAgent/0.1 (+https://github.com/)"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
WINDOW_DAYS = 7
MAX_PER_FEED = 12  # Kandidaten je Feed begrenzen (Kosten/Volumen)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


@dataclass
class FeedEntry:
    source_name: str
    category: str
    title: str
    url: str
    published: datetime | None
    summary: str

    def as_candidate(self) -> dict:
        return {
            "source_name": self.source_name,
            "category": self.category,
            "title": self.title,
            "url": self.url,
            "published_at": self.published.date().isoformat() if self.published else None,
            "summary": self.summary,
        }


def validate_feed(url: str) -> tuple[bool, str]:
    """Prüfe, ob ``url`` ein abrufbarer, parsebarer RSS/Atom-Feed ist.
    Liefert ``(ok, Titel-oder-Fehlertext)``."""
    try:
        with httpx.Client(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        return False, f"HTTP-Fehler: {e}"
    parsed = feedparser.parse(resp.content)
    title = (parsed.feed.get("title") or "").strip()
    if not parsed.entries and not title:
        return False, "kein parsebarer Feed (weder Einträge noch Titel gefunden)"
    return True, title or url


def current_week(now: datetime | None = None) -> str:
    """ISO-Woche als ``YYYY-Www`` (z. B. ``2026-W21``)."""
    now = now or datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def _clean_excerpt(text: str | None, limit: int = 500) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()[:limit]


def _entry_datetime(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _select_recent(entries: list[FeedEntry], cutoff: datetime, cap: int) -> list[FeedEntry]:
    """Einträge im Zeitfenster (oder ohne Datum), neueste zuerst, auf ``cap`` begrenzt."""
    windowed = [e for e in entries if e.published is None or e.published >= cutoff]
    windowed.sort(key=lambda e: e.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return windowed[:cap]


async def _fetch_feed(client: httpx.AsyncClient, feed: dict) -> list[FeedEntry]:
    try:
        resp = await client.get(feed["url"])
        resp.raise_for_status()
    except httpx.HTTPError:
        return []
    parsed = feedparser.parse(resp.content)
    out: list[FeedEntry] = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        out.append(
            FeedEntry(
                source_name=feed["name"],
                category=feed.get("category", ""),
                title=title,
                url=link,
                published=_entry_datetime(e),
                summary=_clean_excerpt(e.get("summary", "")),
            )
        )
    return out


async def _fetch_all(feeds: list[dict]) -> list[list[FeedEntry]]:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        sem = asyncio.Semaphore(6)

        async def bounded(feed: dict) -> list[FeedEntry]:
            async with sem:
                return await _fetch_feed(client, feed)

        return await asyncio.gather(*(bounded(f) for f in feeds))


def collect_entries(*, now: datetime | None = None, force: bool = False) -> list[FeedEntry]:
    """Hole alle Feeds, behalte neue Einträge im Zeitfenster (pro Feed begrenzt).
    Ohne ``force`` werden bereits gesehene URLs herausgefiltert; mit ``force``
    werden die aktuellen Kandidaten erneut verarbeitet (z. B. nach Prompt-Änderungen)."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=WINDOW_DAYS)
    per_feed = asyncio.run(_fetch_all(load_feeds()))
    collected: list[FeedEntry] = []
    for entries in per_feed:
        collected.extend(_select_recent(entries, cutoff, MAX_PER_FEED))
    if force:
        return collected
    unseen = set(store.filter_unseen([e.url for e in collected]))
    return [e for e in collected if e.url in unseen]


def _enrich(items: list[dict], by_url: dict[str, FeedEntry]) -> list[dict]:
    """Verbinde die analytischen Felder des Agents mit den faktischen Feed-Daten.
    Items mit unbekannter URL (nicht unter den Kandidaten) werden verworfen."""
    enriched: list[dict] = []
    for it in items:
        url = (it.get("url") or "").strip()
        src = by_url.get(url)
        if src is None:
            continue  # keine erfundenen Quellen zulassen
        enriched.append(
            {
                "title": it.get("title") or src.title,
                "url": url,
                "source_name": src.source_name,
                "summary": it.get("summary"),
                "why_relevant": it.get("why_relevant"),
                "attention": it.get("attention"),
                "severity": it.get("severity"),
                "published_at": src.published.date().isoformat() if src.published else None,
            }
        )
    return enriched


def run_weekly_scan(*, now: datetime | None = None, force: bool = False) -> dict:
    """Vollständiger Wochenlauf. Gibt eine kleine Zusammenfassung
    ``{week, candidates, items}`` zurück. ``force`` verarbeitet die aktuellen
    Kandidaten erneut (ignoriert ``seen_entries``)."""
    week = current_week(now)
    entries = collect_entries(now=now, force=force)
    if not entries:
        return {"week": week, "candidates": 0, "items": 0}

    items = agent.summarize_digest([e.as_candidate() for e in entries])
    enriched = _enrich(items, {e.url: e for e in entries})

    digest_id = store.upsert_digest(week)
    store.replace_digest_items(digest_id, enriched)
    store.mark_seen([e.url for e in entries])
    return {"week": week, "candidates": len(entries), "items": len(enriched)}
