"""HTTP-basierte Quellen-Aktualitätsprüfung.

Strategie pro Quelle:
1. ``HEAD`` senden → ETag und Last-Modified mit Datenbank vergleichen.
2. Falls beide Header fehlen oder das HEAD-Ergebnis unklar ist: ``GET`` und SHA-256
   des Bodies gegen den gespeicherten Hash prüfen — gestreamt mit Größenlimit.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import store

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
USER_AGENT = "ResearcherAgent/0.1 (+https://github.com/)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
MAX_BODY_BYTES = 20 * 1024 * 1024  # 20 MiB; abbrechen, statt OOM zu riskieren
CHUNK_BYTES = 64 * 1024


@dataclass
class FreshnessResult:
    source_id: int
    url: str
    is_stale: bool
    etag: str | None
    last_modified: str | None
    content_sha256: str | None
    error: str | None = None


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _stream_hash(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None, str | None, str | None]:
    """GET den Body gestreamt, hashe SHA-256 mit Cap. Liefert (etag, last_modified, sha256, error)."""
    try:
        async with client.stream("GET", url, headers=HEADERS, follow_redirects=True) as resp:
            resp.raise_for_status()
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
            h = hashlib.sha256()
            size = 0
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK_BYTES):
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return etag, last_modified, None, f"Body > {MAX_BODY_BYTES // (1024 * 1024)} MiB — Hash nicht berechnet"
                h.update(chunk)
            return etag, last_modified, h.hexdigest(), None
    except httpx.HTTPError as e:
        return None, None, None, f"GET-Fehler: {e}"


async def _check_one(client: httpx.AsyncClient, src: store.Source) -> FreshnessResult:
    try:
        head = await client.head(src.url, headers=HEADERS, follow_redirects=True)
    except httpx.HTTPError as e:
        return FreshnessResult(src.id, src.url, False, src.etag, src.last_modified,
                               src.content_sha256, error=f"HEAD-Fehler: {e}")

    etag = head.headers.get("ETag")
    last_modified = head.headers.get("Last-Modified")

    if etag and src.etag and etag == src.etag:
        return FreshnessResult(src.id, src.url, False, etag, last_modified, src.content_sha256)
    if last_modified and src.last_modified and last_modified == src.last_modified:
        return FreshnessResult(src.id, src.url, False, etag, last_modified, src.content_sha256)

    if etag and src.etag and etag != src.etag:
        return FreshnessResult(src.id, src.url, True, etag, last_modified, src.content_sha256)
    if last_modified and src.last_modified and last_modified != src.last_modified:
        return FreshnessResult(src.id, src.url, True, etag, last_modified, src.content_sha256)

    # Header reichen nicht zur Entscheidung → GET (gestreamt) + Hash-Vergleich
    new_etag, new_last_mod, new_hash, err = await _stream_hash(client, src.url)
    if err:
        return FreshnessResult(src.id, src.url, False, etag, last_modified,
                               src.content_sha256, error=err)

    is_stale = bool(src.content_sha256 and new_hash and new_hash != src.content_sha256)
    return FreshnessResult(
        src.id,
        src.url,
        is_stale,
        new_etag or etag,
        new_last_mod or last_modified,
        new_hash,
    )


async def _check_all(sources: list[store.Source]) -> list[FreshnessResult]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        sem = asyncio.Semaphore(8)

        async def bounded(src: store.Source) -> FreshnessResult:
            async with sem:
                return await _check_one(client, src)

        return await asyncio.gather(*(bounded(s) for s in sources))


def check_sources(sources: list[store.Source]) -> list[FreshnessResult]:
    """Synchroner Wrapper für CLI-Nutzung."""
    return asyncio.run(_check_all(sources))


async def baseline_one(client: httpx.AsyncClient, url: str) -> dict:
    """Initialer Fetch beim Anlegen einer Quelle: liefert ETag/Last-Modified/SHA-256 (gestreamt)."""
    etag, last_modified, content_sha256, _err = await _stream_hash(client, url)
    return {"etag": etag, "last_modified": last_modified, "content_sha256": content_sha256}


async def _baseline_all(urls: list[str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        sem = asyncio.Semaphore(8)

        async def bounded(u: str) -> dict:
            async with sem:
                return await baseline_one(client, u)

        return await asyncio.gather(*(bounded(u) for u in urls))


def baseline_urls(urls: list[str]) -> list[dict]:
    """Synchroner Wrapper, der für jede URL ein Metadata-Dict zurückgibt."""
    return asyncio.run(_baseline_all(urls))
