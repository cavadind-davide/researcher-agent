"""Tests für die Datums-/Archiv-Logik und einen render_all-Smoke-Test."""
from __future__ import annotations

from datetime import date, datetime, timezone

from researcher import render, store


# --- pure helpers ---------------------------------------------------------

def test_week_monday():
    # ISO 2026-W01 beginnt am 2025-12-29 (1. Jan 2026 ist ein Donnerstag).
    assert render._week_monday("2026-W01") == date(2025, 12, 29)
    assert render._week_monday("2026-W21") == date(2026, 5, 18)


def test_digest_is_recent_boundary():
    monday = render._week_monday("2026-W21")  # 2026-05-18
    assert render._digest_is_recent("2026-W21", monday) is True
    assert render._digest_is_recent("2026-W21", date(2026, 6, 18)) is True   # genau 31 Tage
    assert render._digest_is_recent("2026-W21", date(2026, 6, 19)) is False  # 32 Tage


def test_digest_label():
    assert render._digest_label("2026-W21") == "KW 21 · 2026"
    assert render._digest_label("2026-W02") == "KW 2 · 2026"


# --- render_all smoke -----------------------------------------------------

def _current_week() -> str:
    y, w, _ = datetime.now(timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def test_render_all_writes_pages_and_splits_archive(temp_db, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    monkeypatch.setattr(render, "DIST_DIR", dist)

    store.upsert_topic(slug="t1", question="Eine Frage?", tldr="A", body_md="## H\nText", tags="iam")

    recent_week = _current_week()
    rid = store.upsert_digest(recent_week)
    store.replace_digest_items(rid, [{
        "title": "Kritische RCE in Foo", "url": "https://a/1", "source_name": "BSI",
        "summary": "Eine Zusammenfassung.", "why_relevant": "Betrifft Edge-Geräte.",
        "attention": "Sofort patchen.", "published_at": "2026-05-20",
    }])

    old_id = store.upsert_digest("2020-W01")
    store.replace_digest_items(old_id, [{"title": "Alter Eintrag", "url": "https://a/old"}])

    render.render_all()

    index = (dist / "index.html").read_text(encoding="utf-8")
    assert "Aktuelle Wochen-Briefings" in index
    assert f"weekly/{recent_week}.html" in index
    assert "Kritische RCE in Foo" in index
    # Archiv erscheint nun (einklappbar) am Index-Ende ...
    assert "weekly/2020-W01.html" in index
    assert "Archiv" in index
    # ... aber aktuelle Briefings stehen oben, das Archiv darunter.
    assert index.index(f"weekly/{recent_week}.html") < index.index("weekly/2020-W01.html")
    # und die Recherchen-Sektion liegt zwischen aktuellen Briefings und Archiv.
    assert index.index('class="briefings"') < index.index('class="topics"') < index.index('class="archive-briefings"')

    weekly = (dist / "weekly" / f"{recent_week}.html").read_text(encoding="utf-8")
    assert "Betrifft Edge-Geräte." in weekly
    assert "Sofort patchen." in weekly
    assert "https://a/1" in weekly

    assert (dist / "weekly" / "2020-W01.html").exists()  # auch archivierte Wochen haben eine Seite

    archive = (dist / "archive.html").read_text(encoding="utf-8")
    assert "2020-W01" in archive
    assert "Alter Eintrag" in archive


def test_render_weekly_shows_severity_badge(temp_db, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    monkeypatch.setattr(render, "DIST_DIR", dist)
    week = _current_week()
    did = store.upsert_digest(week)
    store.replace_digest_items(did, [
        {"title": "Kritische RCE", "url": "https://a/1", "severity": "aktiv-ausgenutzt"},
        {"title": "Hintergrund", "url": "https://a/2"},  # ohne severity -> kein Badge
    ])
    render.render_all()
    weekly = (dist / "weekly" / f"{week}.html").read_text(encoding="utf-8")
    assert 'class="sev sev-aktiv-ausgenutzt"' in weekly
    # Item ohne severity erzeugt kein leeres Badge
    assert weekly.count('class="sev ') == 1


def test_render_status_and_archived_topics(temp_db, monkeypatch, tmp_path):
    dist = tmp_path / "dist"
    monkeypatch.setattr(render, "DIST_DIR", dist)

    store.upsert_topic(slug="active1", question="Aktive Frage?", tldr="A", body_md="b", tags="iam")
    store.upsert_topic(slug="arch1", question="Archivierte Frage?", tldr="A", body_md="b", tags="")
    store.set_topic_archived("arch1", True)

    render.render_all()
    index = (dist / "index.html").read_text(encoding="utf-8")

    # Status-Sektion
    assert 'class="status"' in index
    assert "aktive Recherchen" in index

    # Aktive im Hauptteil, archivierte in eigener (einklappbarer) Sektion
    assert "Aktive Frage?" in index
    assert 'class="archive-topics"' in index
    assert "Archivierte Frage?" in index
    assert index.index("Aktive Frage?") < index.index('class="archive-topics"')
    assert index.index('class="archive-topics"') < index.index("Archivierte Frage?")

    # Beide Topic-Seiten existieren weiterhin
    assert (dist / "topics" / "active1.html").exists()
    assert (dist / "topics" / "arch1.html").exists()
