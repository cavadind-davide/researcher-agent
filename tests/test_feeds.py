"""Tests für die Feeds-JSON-Config (laden, Existenz, anhängen)."""
from __future__ import annotations

import json

from researcher import feeds


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_feeds_and_exists(tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    _write(p, [{"name": "A", "url": "https://a", "category": "x"}])
    monkeypatch.setattr(feeds, "FEEDS_PATH", p)
    assert feeds.load_feeds() == [{"name": "A", "url": "https://a", "category": "x"}]
    assert feeds.feed_exists("https://a") is True
    assert feeds.feed_exists("https://b") is False


def test_append_feed_new(tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    _write(p, [])
    monkeypatch.setattr(feeds, "FEEDS_PATH", p)
    assert feeds.append_feed("A", "https://a", "news") is True
    assert feeds.load_feeds() == [{"name": "A", "url": "https://a", "category": "news"}]


def test_append_feed_duplicate(tmp_path, monkeypatch):
    p = tmp_path / "feeds.json"
    _write(p, [{"name": "A", "url": "https://a", "category": ""}])
    monkeypatch.setattr(feeds, "FEEDS_PATH", p)
    assert feeds.append_feed("A2", "https://a", "x") is False
    assert len(feeds.load_feeds()) == 1


def test_real_feeds_json_is_valid():
    # Die mitgelieferte feeds.json laedt und ist nicht leer.
    data = feeds.load_feeds()
    assert isinstance(data, list) and len(data) >= 1
    assert all("url" in f and "name" in f for f in data)
