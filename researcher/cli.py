"""Kommandozeilen-Interface für den Researcher-Agent."""
from __future__ import annotations

import http.server
import os
import socketserver
import sys
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import typer
from dotenv import load_dotenv

from . import agent, render, sources, store

ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _is_safe_url(url: str | None) -> bool:
    """Akzeptiere ausschließlich http(s)-URLs mit Host. Schützt vor javascript:/data:/file: in href."""
    if not url:
        return False
    try:
        p = urlparse(url.strip())
    except ValueError:
        return False
    return p.scheme.lower() in ALLOWED_URL_SCHEMES and bool(p.netloc)

# Erzwinge UTF-8 für stdout/stderr unter Windows (cp1252 erstickt sonst an ✓ etc.).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

load_dotenv()

app = typer.Typer(
    add_completion=False,
    help="IT-Sicherheitsarchitekt Researcher-Agent (Brave + Microsoft Learn).",
    no_args_is_help=True,
)


def _persist(payload: dict) -> int:
    """Schreibe Topic + Sources in die DB. Liefert topic_id zurück."""
    tldr_text = "\n".join(payload["tldr"])
    tags_csv = ",".join(payload.get("tags", []))
    topic_id = store.upsert_topic(
        slug=payload["slug"],
        question=payload["question"],
        tldr=tldr_text,
        body_md=payload["body_md"],
        tags=tags_csv,
    )

    safe_sources = []
    for s in payload["sources"]:
        if _is_safe_url(s.get("url")):
            safe_sources.append(s)
        else:
            typer.secho(f"  ⚠ Verworfene URL (unzulässiges Schema): {s.get('url')!r}", fg="yellow")

    if not safe_sources:
        typer.secho("Keine gültigen Quellen — DB nicht aktualisiert.", fg="red", err=True)
        raise typer.Exit(1)

    urls = [s["url"] for s in safe_sources]
    typer.echo(f"  Erstelle Baseline für {len(urls)} Quellen…")
    metas = sources.baseline_urls(urls)
    src_records = []
    for s, meta in zip(safe_sources, metas):
        src_records.append(
            {
                "url": s["url"],
                "title": s.get("title"),
                "etag": meta["etag"],
                "last_modified": meta["last_modified"],
                "content_sha256": meta["content_sha256"],
            }
        )
    store.replace_sources(topic_id, src_records)
    return topic_id


@app.command()
def init() -> None:
    """Initialisiere die SQLite-Datenbank."""
    store.init_db()
    typer.echo(f"DB initialisiert: {store.DB_PATH}")


@app.command()
def ask(question: Annotated[str, typer.Argument(help="Die Recherche-Frage.")]) -> None:
    """Führe eine neue Recherche durch und rendere die Webseite neu."""
    store.init_db()
    typer.echo(f"› Frage: {question}")
    typer.echo("› Recherche läuft …")
    payload = agent.research(question)
    topic_id = _persist(payload)
    render.render_all()
    typer.echo(f"✓ Topic gespeichert (id={topic_id}, slug={payload['slug']})")
    typer.echo(f"✓ HTML aktualisiert in {render.DIST_DIR}")


@app.command()
def refresh(
    topic: Annotated[
        str | None, typer.Option("--topic", help="Nur dieses Topic (slug) prüfen.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-Recherche unabhängig vom Stale-Status.")
    ] = False,
) -> None:
    """Prüfe Quellen auf Aktualisierungen und re-recherchiere veränderte Topics."""
    store.init_db()

    if topic:
        t = store.get_topic(topic)
        if not t:
            typer.secho(f"Topic '{topic}' nicht gefunden.", fg="red", err=True)
            raise typer.Exit(1)
        topics = [t]
    else:
        topics = store.list_topics()

    if not topics:
        typer.echo("Keine Topics vorhanden. Starte mit `researcher ask \"…\"`.")
        return

    stale_topic_ids: set[int] = set()
    typer.echo(f"› Prüfe {sum(len(store.get_sources(t.id)) for t in topics)} Quellen…")
    for t in topics:
        srcs = store.get_sources(t.id)
        if not srcs:
            continue
        results = sources.check_sources(srcs)
        for r in results:
            store.update_source_freshness(
                r.source_id,
                etag=r.etag,
                last_modified=r.last_modified,
                content_sha256=r.content_sha256,
                is_stale=r.is_stale,
            )
            if r.error:
                typer.secho(f"  ⚠ {r.url}: {r.error}", fg="yellow")
            elif r.is_stale:
                typer.secho(f"  ↻ stale: {r.url}", fg="yellow")
                stale_topic_ids.add(t.id)

    targets = [t for t in topics if t.id in stale_topic_ids] if not force else topics
    if not targets:
        typer.echo("✓ Alle Quellen aktuell – keine Re-Recherche nötig.")
        render.render_all()
        return

    for t in targets:
        srcs = store.get_sources(t.id)
        focus = [s.url for s in srcs if s.is_stale] if not force else [s.url for s in srcs]
        typer.echo(f"› Re-Recherche: {t.slug}")
        payload = agent.research(t.question, focus_urls=focus)
        payload["slug"] = t.slug  # behalte stabilen Slug
        _persist(payload)
        store.mark_topic_refreshed(t.id)

    render.render_all()
    typer.echo(f"✓ {len(targets)} Topic(s) aktualisiert.")


@app.command(name="list")
def list_topics_cmd() -> None:
    """Liste alle Topics in der Datenbank."""
    store.init_db()
    topics = store.list_topics()
    if not topics:
        typer.echo("Keine Topics.")
        return
    for t in topics:
        typer.echo(f"  {t.slug:50s}  {t.last_refreshed_at[:10]}  {t.question}")


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="HTTP-Port.")] = 8000,
) -> None:
    """Starte einen lokalen HTTP-Server für die generierte Webseite."""
    if not (render.DIST_DIR / "index.html").exists():
        store.init_db()
        render.render_all()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(render.DIST_DIR), **kw)

        def log_message(self, fmt: str, *args) -> None:
            pass

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        typer.echo(f"› Vorschau läuft unter {url}  (Strg+C zum Beenden)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            typer.echo("\n✓ Server gestoppt.")


@app.command()
def render_only() -> None:
    """Rendere die Webseite neu, ohne Recherche."""
    store.init_db()
    render.render_all()
    typer.echo(f"✓ HTML aktualisiert in {render.DIST_DIR}")


@app.command()
def doctor() -> None:
    """Prüfe die Konfiguration und Erreichbarkeit der MCP-Quellen."""
    ok = True

    if not os.environ.get("ANTHROPIC_API_KEY"):
        typer.secho("✗ ANTHROPIC_API_KEY nicht gesetzt", fg="red")
        ok = False
    else:
        typer.secho("✓ ANTHROPIC_API_KEY vorhanden", fg="green")

    if not os.environ.get("BRAVE_API_KEY"):
        typer.secho("✗ BRAVE_API_KEY nicht gesetzt", fg="red")
        ok = False
    else:
        typer.secho("✓ BRAVE_API_KEY vorhanden", fg="green")

    try:
        r = httpx.head("https://learn.microsoft.com/api/mcp", timeout=10, follow_redirects=True)
        if r.status_code < 500:
            typer.secho(f"✓ MS Learn MCP erreichbar (HTTP {r.status_code})", fg="green")
        else:
            typer.secho(f"✗ MS Learn MCP HTTP {r.status_code}", fg="red")
            ok = False
    except httpx.HTTPError as e:
        typer.secho(f"✗ MS Learn MCP nicht erreichbar: {e}", fg="red")
        ok = False

    db = Path(store.DB_PATH)
    typer.secho(f"{'✓' if db.exists() else '○'} DB: {db} ({'existiert' if db.exists() else 'noch nicht angelegt'})")

    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
