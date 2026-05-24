"""HTTP-basierte Quellen-Aktualitätsprüfung.

Strategie pro Quelle (ein einziger GET):
1. **Conditional Request**: gespeicherte Validatoren als ``If-None-Match`` /
   ``If-Modified-Since`` mitsenden. Antwortet der Server ``304 Not Modified``,
   gilt die Quelle definitiv als aktuell – ohne Body-Vergleich.
2. Bei ``200`` wird ein *normalisierter* Inhalts-Hash verglichen: aus HTML/XML
   werden Skripte, Styles, Kommentare und Tags entfernt und Whitespace
   kollabiert, bevor SHA-256 gebildet wird. So fallen pro-Request wechselnde
   Tokens (CSP-Nonces, Analytics, rotierende ETags, CDN-Edge-Varianten) weg,
   die sonst jede Quelle fälschlich als "stale" markieren.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass

import httpx

from . import store

USER_AGENT = "ResearcherAgent/0.1 (+https://github.com/)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
MAX_BODY_BYTES = 20 * 1024 * 1024  # 20 MiB; abbrechen, statt OOM zu riskieren
CHUNK_BYTES = 64 * 1024

# Volatiles Markup, das pro Request variiert, vor dem Hashen entfernen.
_SCRIPT_STYLE_RE = re.compile(
    rb"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(rb"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(rb"<[^>]+>")
_WS_RE = re.compile(rb"\s+")


@dataclass
class FreshnessResult:
    source_id: int
    url: str
    is_stale: bool
    etag: str | None
    last_modified: str | None
    content_sha256: str | None
    error: str | None = None


def _normalized_hash(body: bytes, content_type: str | None) -> str:
    """SHA-256 über normalisierten Inhalt. Für HTML/XML wird auf sichtbaren Text
    reduziert (Skripte/Styles/Kommentare/Tags raus, Whitespace kollabiert);
    andere Typen werden roh gehasht."""
    ct = (content_type or "").lower()
    if "html" in ct or "xml" in ct:
        body = _SCRIPT_STYLE_RE.sub(b" ", body)
        body = _COMMENT_RE.sub(b" ", body)
        body = _TAG_RE.sub(b" ", body)
        body = _WS_RE.sub(b" ", body).strip()
    return hashlib.sha256(body).hexdigest()


async def _fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[int | None, str | None, str | None, str | None, str | None]:
    """GET (optional conditional). Liefert ``(status, etag, last_modified, content_hash, error)``.
    ``content_hash`` ist ``None`` bei 304 oder Fehler."""
    req_headers = dict(HEADERS)
    if etag:
        req_headers["If-None-Match"] = etag
    if last_modified:
        req_headers["If-Modified-Since"] = last_modified
    try:
        async with client.stream("GET", url, headers=req_headers, follow_redirects=True) as resp:
            resp_etag = resp.headers.get("ETag")
            resp_last_mod = resp.headers.get("Last-Modified")
            if resp.status_code == 304:
                return 304, resp_etag, resp_last_mod, None, None
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type")
            buf = bytearray()
            async for chunk in resp.aiter_bytes(chunk_size=CHUNK_BYTES):
                buf += chunk
                if len(buf) > MAX_BODY_BYTES:
                    return (
                        resp.status_code,
                        resp_etag,
                        resp_last_mod,
                        None,
                        f"Body > {MAX_BODY_BYTES // (1024 * 1024)} MiB — Hash nicht berechnet",
                    )
            return (
                resp.status_code,
                resp_etag,
                resp_last_mod,
                _normalized_hash(bytes(buf), content_type),
                None,
            )
    except httpx.HTTPError as e:
        return None, None, None, None, f"GET-Fehler: {e}"


async def _check_one(client: httpx.AsyncClient, src: store.Source) -> FreshnessResult:
    status, etag, last_modified, new_hash, err = await _fetch(
        client, src.url, etag=src.etag, last_modified=src.last_modified
    )
    if err:
        return FreshnessResult(
            src.id, src.url, False, src.etag, src.last_modified, src.content_sha256, error=err
        )
    if status == 304:
        # Server bestätigt: unverändert. Validatoren ggf. auffrischen.
        return FreshnessResult(
            src.id, src.url, False, etag or src.etag,
            last_modified or src.last_modified, src.content_sha256,
        )
    # 200: über normalisierten Inhalts-Hash entscheiden – nicht über rohe Bytes/ETag,
    # die pro Request variieren. Ohne gespeicherten Hash gilt die Quelle als aktuell
    # (Baseline wird mit dem neuen Hash aufgefrischt).
    is_stale = bool(src.content_sha256 and new_hash and new_hash != src.content_sha256)
    return FreshnessResult(
        src.id, src.url, is_stale, etag or src.etag,
        last_modified or src.last_modified, new_hash or src.content_sha256,
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
    """Initialer Fetch beim Anlegen einer Quelle: ETag/Last-Modified/normalisierter SHA-256."""
    _status, etag, last_modified, content_hash, _err = await _fetch(client, url)
    return {"etag": etag, "last_modified": last_modified, "content_sha256": content_hash}


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
