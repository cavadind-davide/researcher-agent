"""Kuratierte Quellen-Feeds für das wöchentliche Security-Briefing.

Die Liste liegt editierbar in ``feeds.json`` (Name, Feed-URL, Kategorie) und wird
hier geladen bzw. um neue Feeds ergänzt. ``digest.py`` nutzt ``load_feeds()``;
``researcher add-feed`` hängt validierte Feeds über ``append_feed()`` an.
"""
from __future__ import annotations

import json
from pathlib import Path

FEEDS_PATH = Path(__file__).resolve().parent / "feeds.json"


def load_feeds() -> list[dict[str, str]]:
    return json.loads(FEEDS_PATH.read_text(encoding="utf-8"))


def feed_exists(url: str) -> bool:
    return any(f.get("url") == url for f in load_feeds())


def append_feed(name: str, url: str, category: str = "") -> bool:
    """Hänge einen Feed an ``feeds.json`` an. Liefert ``False``, wenn die URL
    bereits vorhanden ist (kein Doppeleintrag)."""
    feeds = load_feeds()
    if any(f.get("url") == url for f in feeds):
        return False
    feeds.append({"name": name, "url": url, "category": category})
    FEEDS_PATH.write_text(json.dumps(feeds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
