"""Rendert die SQLite-Daten als statische Webseite ins ``dist/``-Verzeichnis."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markdown_it import MarkdownIt

from . import store

PKG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PKG_DIR.parent
DIST_DIR = PROJECT_ROOT / "dist"
STALE_DAYS = 21

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


def _split_tags(tags: str | None) -> list[str]:
    if not tags:
        return []
    return [t.strip() for t in tags.split(",") if t.strip()]


def _split_tldr(tldr: str | None) -> list[str]:
    return [b.strip() for b in (tldr or "").split("\n") if b.strip()]


def _ensure_dirs() -> None:
    (DIST_DIR / "topics").mkdir(parents=True, exist_ok=True)


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
    topics = store.list_topics()

    topic_views = []
    for t in topics:
        topic_views.append(
            {
                "slug": t.slug,
                "question": t.question,
                "tldr": _split_tldr(t.tldr),
                "tags": _split_tags(t.tags),
                "last_refreshed_at": t.last_refreshed_at,
                "created_at": t.created_at,
                "is_stale": _is_stale(t),
            }
        )

    any_stale = any(v["is_stale"] for v in topic_views)
    rendered_index = env.get_template("index.html").render(
        topics=topic_views,
        any_stale=any_stale,
        stale_days=STALE_DAYS,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    (DIST_DIR / "index.html").write_text(rendered_index, encoding="utf-8")

    topic_tpl = env.get_template("topic.html")
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in topics:
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
