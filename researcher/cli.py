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

from . import agent, digest as digest_mod, feeds, intake as intake_mod, render, sources, store

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
    rebaseline: Annotated[
        bool,
        typer.Option(
            "--rebaseline",
            help="Nur Quellen-Baseline (ETag/Last-Modified/Hash) neu setzen, keine Re-Recherche. "
            "Einmalig nach einem Wechsel des Frische-Hash-Algorithmus nutzen.",
        ),
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

    if rebaseline:
        total = 0
        for t in topics:
            srcs = store.get_sources(t.id)
            if not srcs:
                continue
            metas = sources.baseline_urls([s.url for s in srcs])
            for s, meta in zip(srcs, metas):
                store.update_source_freshness(
                    s.id,
                    etag=meta["etag"],
                    last_modified=meta["last_modified"],
                    content_sha256=meta["content_sha256"],
                    is_stale=False,
                )
                total += 1
        typer.echo(f"✓ Baseline für {total} Quelle(n) neu gesetzt – keine Re-Recherche.")
        render.render_all()
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


@app.command()
def digest(
    force: Annotated[
        bool,
        typer.Option("--force", help="Aktuelle Kandidaten erneut verarbeiten (ignoriert bereits gesehene URLs)."),
    ] = False,
) -> None:
    """Scanne die kuratierten Security-Feeds und erstelle das Wochen-Briefing."""
    store.init_db()
    typer.echo("› Wöchentliches Security-Briefing – scanne Feeds …")
    result = digest_mod.run_weekly_scan(force=force)
    if result["candidates"] == 0:
        typer.echo("✓ Keine neuen Feed-Einträge – kein Briefing erstellt.")
    else:
        typer.echo(
            f"✓ Woche {result['week']}: {result['items']} Item(s) aus "
            f"{result['candidates']} Kandidat(en)."
        )
    render.render_all()
    typer.echo(f"✓ HTML aktualisiert in {render.DIST_DIR}")


@app.command()
def archive_topic(
    slug: Annotated[str, typer.Argument(help="Slug des zu archivierenden Topics.")],
) -> None:
    """Verschiebe ein Topic ins Archiv (keine Frische-Prüfung mehr, bleibt einsehbar)."""
    store.init_db()
    if not store.set_topic_archived(slug, True):
        typer.secho(f"Topic '{slug}' nicht gefunden.", fg="red", err=True)
        raise typer.Exit(1)
    render.render_all()
    typer.echo(f"✓ Topic '{slug}' archiviert.")


@app.command()
def unarchive_topic(
    slug: Annotated[str, typer.Argument(help="Slug des zu reaktivierenden Topics.")],
) -> None:
    """Hole ein Topic aus dem Archiv zurück (wird wieder auf Aktualität geprüft)."""
    store.init_db()
    if not store.set_topic_archived(slug, False):
        typer.secho(f"Topic '{slug}' nicht gefunden.", fg="red", err=True)
        raise typer.Exit(1)
    render.render_all()
    typer.echo(f"✓ Topic '{slug}' reaktiviert.")


@app.command()
def add_feed(
    url: Annotated[str, typer.Option("--url", help="Feed-URL (RSS/Atom).")],
    name: Annotated[str, typer.Option("--name", help="Anzeigename der Quelle.")],
    category: Annotated[str, typer.Option("--category", help="Kategorie (z.B. advisory, news).")] = "",
) -> None:
    """Validiere einen RSS/Atom-Feed und ergänze ihn fürs Wochen-Briefing."""
    if not _is_safe_url(url):
        typer.secho(f"Ungültige URL (nur http/https erlaubt): {url!r}", fg="red", err=True)
        raise typer.Exit(1)
    if feeds.feed_exists(url):
        typer.secho(f"Feed bereits vorhanden: {url}", fg="yellow")
        raise typer.Exit(0)
    typer.echo(f"› Prüfe Feed: {url}")
    ok, info = digest_mod.validate_feed(url)
    if not ok:
        typer.secho(f"✗ Feed nicht valide: {info}", fg="red", err=True)
        raise typer.Exit(1)
    feeds.append_feed(name=name, url=url, category=category)
    typer.echo(f"✓ Feed ergänzt: {name} — {url}  (erkannt als: {info})")


@app.command()
def intake() -> None:
    """Verarbeite einen GitHub-Issue-Intake. Liest ``INTAKE_ACTION`` und ``ISSUE_BODY``
    aus der Umgebung (kein Shell-Interpolieren von Nutzereingaben) und führt die
    Aktion aus. Rendern/Deploy übernimmt der Workflow."""
    action = os.environ.get("INTAKE_ACTION", "").strip()
    fields = intake_mod.parse_issue_form(os.environ.get("ISSUE_BODY", ""))
    store.init_db()

    if action == "ask":
        question = (fields.get("Frage") or "").strip()
        if not question:
            typer.secho("Keine Frage angegeben.", fg="red", err=True)
            raise typer.Exit(1)
        payload = agent.research(question)
        _persist(payload)
        typer.echo("RESULT: Recherche gespeichert.")
        typer.echo(f"SLUG: {payload['slug']}")
    elif action == "archive":
        slug = (fields.get("Slug") or "").strip()
        if not slug:
            typer.secho("Kein Slug angegeben.", fg="red", err=True)
            raise typer.Exit(1)
        if not store.set_topic_archived(slug, True):
            typer.secho(f"Topic '{slug}' nicht gefunden.", fg="red", err=True)
            raise typer.Exit(1)
        typer.echo(f"RESULT: Topic '{slug}' archiviert.")
    elif action == "add-feed":
        url = (fields.get("URL") or "").strip()
        name = (fields.get("Name") or "").strip()
        category = (fields.get("Kategorie") or "").strip()
        if not name or not _is_safe_url(url):
            typer.secho(f"Name fehlt oder ungültige URL: {url!r}", fg="red", err=True)
            raise typer.Exit(1)
        if feeds.feed_exists(url):
            typer.echo(f"RESULT: Feed bereits vorhanden ({url}).")
            return
        ok, info = digest_mod.validate_feed(url)
        if not ok:
            typer.secho(f"Feed nicht valide: {info}", fg="red", err=True)
            raise typer.Exit(1)
        feeds.append_feed(name=name, url=url, category=category)
        typer.echo(f"RESULT: Feed '{name}' ergänzt ({info}).")
    else:
        typer.secho(f"Unbekannte Intake-Aktion: {action!r}", fg="red", err=True)
        raise typer.Exit(1)


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
