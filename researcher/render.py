"""Rendert die SQLite-Daten als statische Webseite ins ``dist/``-Verzeichnis."""
from __future__ import annotations

import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from . import store

PKG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_DIR.parent
DIST_DIR = PROJECT_ROOT / "dist"
STALE_DAYS = 21
ARCHIVE_DAYS = 31  # Briefings älter als ~1 Monat wandern ins Archiv
# Für die Intake-Buttons (Issue-Form-Links); in CI aus GITHUB_REPOSITORY.
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "cavadind-davide/researcher-agent")

_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True}).enable("table")


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(PKG_DIR / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["markdown"] = lambda s: _md.render(s or "")
    env.filters["fmt_date"] = _fmt_date
    return env


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return iso


def _days_since(iso: str) -> int:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def _is_stale(topic: store.Topic) -> bool:
    return _days_since(topic.last_refreshed_at) > STALE_DAYS


def _week_monday(week: str) -> date:
    """Montag der ISO-Woche ``YYYY-Www``."""
    year_s, week_s = week.split("-W")
    return datetime.fromisocalendar(int(year_s), int(week_s), 1).date()


def _digest_is_recent(week: str, today: date) -> bool:
    """True, solange die Woche höchstens ``ARCHIVE_DAYS`` Tage zurückliegt."""
    return (today - _week_monday(week)).days <= ARCHIVE_DAYS


def _digest_label(week: str) -> str:
    year_s, week_s = week.split("-W")
    return f"KW {int(week_s)} · {year_s}"


def _split_tags(tags: str | None) -> list[str]:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def _split_tldr(tldr: str | None) -> list[str]:
    return [b.strip() for b in (tldr or "").split("\n") if b.strip()]


def _ensure_dirs() -> None:
    (DIST_DIR / "topics").mkdir(parents=True, exist_ok=True)
    (DIST_DIR / "weekly").mkdir(parents=True, exist_ok=True)


def _digest_views() -> list[dict]:
    today = datetime.now(timezone.utc).date()
    views = []
    for d in store.list_digests():
        items = store.get_digest_items(d.id)
        views.append(
            {
                "week": d.week,
                "label": _digest_label(d.week),
                "generated_at": d.generated_at,
                "item_count": len(items),
                "is_recent": _digest_is_recent(d.week, today),
                "items": [
                    {
                        "title": it.title,
                        "url": it.url,
                        "source_name": it.source_name,
                        "summary": it.summary,
                        "why_relevant": it.why_relevant,
                        "attention": it.attention,
                        "severity": it.severity,
                        "published_at": it.published_at,
                    }
                    for it in items
                ],
            }
        )
    return views


def _topic_view(t: store.Topic) -> dict:
    return {
        "slug": t.slug,
        "question": t.question,
        "tldr": _split_tldr(t.tldr),
        "tags": _split_tags(t.tags),
        "last_refreshed_at": t.last_refreshed_at,
        "created_at": t.created_at,
        "is_stale": _is_stale(t),
    }


def _copy_static() -> None:
    src = PKG_DIR / "static"
    dst = DIST_DIR / "assets"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.is_file():
            shutil.copy2(item, dst / item.name)


def render_all() -> None:
    """Rendere Index- und alle Topic-Seiten."""
    _ensure_dirs()
    _copy_static()

    env = _env()
    all_topics = store.list_topics(include_archived=True)
    active_topics = [t for t in all_topics if not t.archived]
    archived_topics = [t for t in all_topics if t.archived]
    topic_views = [_topic_view(t) for t in active_topics]
    archived_topic_views = [_topic_view(t) for t in archived_topics]

    digest_views = _digest_views()
    recent_digests = [v for v in digest_views if v["is_recent"]]
    archived_digests = [v for v in digest_views if not v["is_recent"]]

    any_stale = any(v["is_stale"] for v in topic_views)
    status = {
        "topics_active": len(active_topics),
        "topics_archived": len(archived_topics),
        "topics_stale": sum(1 for v in topic_views if v["is_stale"]),
        "briefings": len(digest_views),
        "last_refreshed_at": max((t.last_refreshed_at for t in all_topics), default=None),
    }
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rendered_index = env.get_template("index.html").render(
        topics=topic_views,
        archived_topics=archived_topic_views,
        status=status,
        recent_digests=recent_digests,
        archived_digests=archived_digests,
        any_stale=any_stale,
        stale_days=STALE_DAYS,
        generated_at=generated_at,
        repo=REPO_SLUG,
    )
    (DIST_DIR / "index.html").write_text(rendered_index, encoding="utf-8")

    weekly_tpl = env.get_template("weekly.html")
    for d in digest_views:
        rendered_week = weekly_tpl.render(digest=d, generated_at=generated_at)
        (DIST_DIR / "weekly" / f"{d['week']}.html").write_text(rendered_week, encoding="utf-8")

    rendered_archive = env.get_template("archive.html").render(
        digests=archived_digests,
        archive_days=ARCHIVE_DAYS,
        generated_at=generated_at,
    )
    (DIST_DIR / "archive.html").write_text(rendered_archive, encoding="utf-8")

    topic_tpl = env.get_template("topic.html")
    for t in all_topics:
        sources = store.get_sources(t.id)
        rendered = topic_tpl.render(
            topic={
                "slug": t.slug,
                "question": t.question,
                "tldr": _split_tldr(t.tldr),
                "body_md": t.body_md or "",
                "tags": _split_tags(t.tags),
                "last_refreshed_at": t.last_refreshed_at,
                "created_at": t.created_at,
                "is_stale": _is_stale(t),
            },
            sources=sources,
            stale_days=STALE_DAYS,
            generated_at=generated_at,
        )
        (DIST_DIR / "topics" / f"{t.slug}.html").write_text(rendered, encoding="utf-8")
