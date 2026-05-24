"""Tests für Normalisierung und die Freshness-Entscheidung (HTTP gemockt)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from researcher import sources, store

HTML_CT = "text/html; charset=utf-8"


def _src(**kw) -> store.Source:
    base = dict(
        id=1, topic_id=1, url="https://example.test/page", title=None,
        etag=None, last_modified=None, content_sha256=None,
        fetched_at="2026-01-01T00:00:00+00:00", is_stale=False,
    )
    base.update(kw)
    return store.Source(**base)


def _check(handler, src: store.Source):
    async def _go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await sources._check_one(client, src)
    return asyncio.run(_go())


def _baseline(handler, url="https://example.test/page"):
    async def _go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await sources.baseline_one(client, url)
    return asyncio.run(_go())


# --- _normalized_hash -----------------------------------------------------

def test_normalized_hash_ignores_volatile_markup():
    b1 = b'<html><head><script nonce="abc">var t=1</script><style>.a{}</style></head><body><!-- ray 1 --><h1 data-n="z1">Titel</h1>  <p>Inhalt.</p></body></html>'
    b2 = b'<html><head>\n<script nonce="xyz">var t=9</script><style>.a{color:red}</style></head><body><!-- ray 9 -->\n<h1 data-n="z9">Titel</h1><p>Inhalt.</p>\n</body></html>'
    assert sources._normalized_hash(b1, HTML_CT) == sources._normalized_hash(b2, HTML_CT)


def test_normalized_hash_detects_visible_text_change():
    a = sources._normalized_hash(b"<p>Inhalt.</p>", HTML_CT)
    b = sources._normalized_hash(b"<p>Inhalt GEAENDERT.</p>", HTML_CT)
    assert a != b


def test_normalized_hash_non_html_is_raw():
    assert sources._normalized_hash(b"%PDF a", "application/pdf") != sources._normalized_hash(b"%PDF b", "application/pdf")


# --- _check_one -----------------------------------------------------------

def test_check_one_304_is_fresh_and_keeps_hash():
    def handler(request):
        assert request.headers.get("If-None-Match") == 'W/"v1"'
        return httpx.Response(304)
    src = _src(etag='W/"v1"', content_sha256="storedhash")
    res = _check(handler, src)
    assert res.is_stale is False
    assert res.content_sha256 == "storedhash"


def test_check_one_200_same_normalized_content_is_fresh():
    body = b"<html><body><h1>Titel</h1><p>Gleich.</p></body></html>"
    stored = sources._normalized_hash(body, HTML_CT)

    def handler(request):
        # variiert nur volatiles Markup -> normalisiert identisch
        return httpx.Response(200, headers={"Content-Type": HTML_CT},
                              content=b'<html><body><!-- x --><h1>Titel</h1>\n<p>Gleich.</p></body></html>')
    res = _check(handler, _src(content_sha256=stored))
    assert res.is_stale is False


def test_check_one_200_changed_content_is_stale():
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": HTML_CT},
                              content=b"<html><body><p>Neuer Inhalt.</p></body></html>")
    res = _check(handler, _src(content_sha256="alterhash"))
    assert res.is_stale is True
    assert res.content_sha256 != "alterhash"


def test_check_one_200_without_stored_hash_is_not_stale():
    def handler(request):
        return httpx.Response(200, headers={"Content-Type": HTML_CT},
                              content=b"<html><body><p>Erstmalig.</p></body></html>")
    res = _check(handler, _src(content_sha256=None))
    assert res.is_stale is False
    assert res.content_sha256 is not None  # Baseline wird gesetzt


def test_check_one_http_error_sets_error_not_stale():
    def handler(request):
        return httpx.Response(500)
    res = _check(handler, _src(content_sha256="h"))
    assert res.is_stale is False
    assert res.error is not None
    assert res.content_sha256 == "h"  # alter Stand bleibt erhalten


def test_check_one_rotating_etag_with_stable_content_is_fresh():
    """Server rotiert die ETag pro Request, aber liefert keinen 304 und gleichen Inhalt:
    Entscheidung darf NICHT an der ETag hängen, sondern am normalisierten Hash."""
    body = b"<html><body><p>Stabil.</p></body></html>"
    stored = sources._normalized_hash(body, HTML_CT)

    def handler(request):
        return httpx.Response(200, headers={"Content-Type": HTML_CT, "ETag": '"random-per-request"'},
                              content=body)
    res = _check(handler, _src(etag='"old"', content_sha256=stored))
    assert res.is_stale is False


def test_check_one_respects_body_cap(monkeypatch):
    monkeypatch.setattr(sources, "MAX_BODY_BYTES", 8)

    def handler(request):
        return httpx.Response(200, headers={"Content-Type": HTML_CT}, content=b"x" * 1000)
    res = _check(handler, _src(content_sha256="h"))
    assert res.error is not None
    assert res.is_stale is False


# --- baseline_one ---------------------------------------------------------

def test_baseline_one_returns_validators_and_hash():
    def handler(request):
        return httpx.Response(200, headers={
            "Content-Type": HTML_CT, "ETag": '"e1"', "Last-Modified": "Wed, 01 Jan 2026 00:00:00 GMT",
        }, content=b"<html><body><p>Inhalt.</p></body></html>")
    meta = _baseline(handler)
    assert meta["etag"] == '"e1"'
    assert meta["last_modified"] == "Wed, 01 Jan 2026 00:00:00 GMT"
    assert meta["content_sha256"] is not None
