# Researcher Agent — IT-Sicherheitsarchitekt

Ein autonomer Recherche-Agent, der Fragen zu IT-Sicherheit über **Brave Search**
und den **Microsoft Learn MCP** recherchiert, vertrauenswürdige Quellen
auswertet und das Ergebnis als statische, durchsuchbare Webseite ablegt.

- **Persona:** IT-Sicherheitsarchitekt, Antworten auf Deutsch.
- **Quellenpriorität:** Hersteller-Doku → Standards (NIST, BSI, OWASP) → CVE → Reputable Blogs.
- **Aktualitätsprüfung:** `refresh` vergleicht ETag / Last-Modified / SHA-256 und re-recherchiert nur veränderte Themen.
- **Stale-Banner:** Themen, die älter als 21 Tage sind, werden auf der Seite markiert.

---

## Voraussetzungen

- **Python ≥ 3.12**
- **Node.js / npx** (für den Brave-Search-MCP-Server)
- **API-Keys:**
  - `ANTHROPIC_API_KEY` — für das Claude Agent SDK
  - `BRAVE_API_KEY` — kostenloser Tier verfügbar unter https://api-dashboard.search.brave.com/
- Microsoft Learn MCP läuft remote unter `https://learn.microsoft.com/api/mcp` und benötigt **keine** Authentifizierung.

## Installation

```bash
# Repo / Projektordner
cd C:\Projects\Researcher-Agent

# Abhängigkeiten installieren (uv empfohlen)
uv sync
# alternativ:
python -m venv .venv && .venv\Scripts\activate
pip install -e .

# Konfiguration
copy .env.example .env
# .env editieren und Keys eintragen
```

## Erste Schritte

```bash
# Konfiguration prüfen
researcher doctor

# DB anlegen (passiert auch automatisch bei `ask`)
researcher init

# Erste Recherche
researcher ask "Wie härte ich Microsoft Entra ID Conditional Access gegen Session-Hijacking?"

# Webseite lokal anschauen
researcher serve
# → http://127.0.0.1:8000/
```

## Befehle

| Befehl                            | Zweck                                                                 |
| --------------------------------- | --------------------------------------------------------------------- |
| `researcher ask "<Frage>"`        | Neue Recherche, schreibt Topic + Quellen in die DB, rendert HTML.      |
| `researcher refresh`              | Prüft alle Quellen auf Aktualisierungen, re-recherchiert stale Topics. |
| `researcher refresh --topic <slug>` | Nur dieses Topic.                                                     |
| `researcher refresh --force`      | Erzwingt Re-Recherche aller Topics.                                   |
| `researcher list`                 | Listet alle gespeicherten Topics.                                     |
| `researcher serve [--port 8000]`  | Lokale HTTP-Vorschau aus `dist/`.                                     |
| `researcher render-only`          | Rendert nur HTML neu (ohne Netzwerkzugriff).                          |
| `researcher doctor`               | Prüft Keys + Erreichbarkeit der Quellen.                              |

## Wie funktioniert die Aktualitätsprüfung?

Beim ersten Speichern einer Quelle holt der Agent ETag / Last-Modified-Header
sowie einen SHA-256-Hash des Body. Diese Werte landen in `data/researcher.sqlite`.

`researcher refresh` führt pro Quelle:

1. einen `HEAD`-Request aus und vergleicht ETag und Last-Modified.
2. Falls beide Header fehlen oder übereinstimmen, wird zusätzlich der Inhalt geladen
   und sein SHA-256-Hash mit dem gespeicherten Wert verglichen.

Topics, deren Quellen verändert wurden, werden vom Agent erneut bearbeitet —
nur diese Quellen werden im System-Prompt als Fokus übergeben.

## Struktur

```
researcher/
├── cli.py            # typer-CLI
├── agent.py          # Claude Agent SDK + Output-Parsing
├── mcp_config.py     # Brave + Microsoft Learn MCP-Server
├── prompts/
│   └── system_prompt_de.md
├── sources.py        # ETag / Last-Modified / SHA-256
├── store.py          # SQLite
├── render.py         # Jinja2 → dist/
├── templates/
└── static/
data/
├── researcher.sqlite
└── cache/
dist/                  # generierte statische Seite (deploy-ready)
```

## Migration zu GitHub Pages

`dist/` ist bereits ein vollständiges statisches Bundle — kein Umbau nötig.

1. Repo auf GitHub anlegen, Code pushen (ohne `.env`, `data/cache/`).
2. **Settings → Pages** auf "GitHub Actions" stellen.
3. **Secrets:** `ANTHROPIC_API_KEY` und `BRAVE_API_KEY` als Repository-Secrets setzen.
4. Beispiel-Workflow `.github/workflows/pages.yml`:

```yaml
name: Build & Deploy
on:
  workflow_dispatch:
  schedule:
    - cron: "0 6 * * 1"   # jeden Montag 06:00 UTC

permissions:
  contents: read
  pages: write
  id-token: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: actions/setup-node@v4
        with: { node-version: "20" }
      - run: pip install -r requirements-lock.txt -e . --no-deps
      - env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          BRAVE_API_KEY: ${{ secrets.BRAVE_API_KEY }}
        run: researcher refresh
      - uses: actions/upload-pages-artifact@v3
        with: { path: dist }

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/deploy-pages@v4
        id: deployment
```

> Für Pages-Deploys sollte `data/researcher.sqlite` ins Repo eingecheckt werden,
> damit Quell-Historie und Frische-Stände zwischen Workflow-Läufen erhalten bleiben.

## Lizenz

Privat / unspezifiziert.
