# Researcher Agent — IT-Sicherheitsarchitekt

Ein autonomer Recherche-Agent, der Fragen zu IT-Sicherheit über **Brave Search**
und den **Microsoft Learn MCP** recherchiert, vertrauenswürdige Quellen
auswertet und das Ergebnis als statische, durchsuchbare Webseite ablegt.

- **Persona:** IT-Sicherheitsarchitekt, Antworten auf Deutsch.
- **Quellenpriorität:** Hersteller-Doku → Standards (NIST, BSI, OWASP) → CVE → Reputable Blogs.
- **Aktualitätsprüfung:** `refresh` vergleicht ETag / Last-Modified / SHA-256 und re-recherchiert nur veränderte Themen.
- **Stale-Banner:** Themen, die älter als 21 Tage sind, werden auf der Seite markiert.
- **Wochen-Briefing:** `digest` scannt kuratierte RSS/Atom-Feeds (`researcher/feeds.json`)
  und fasst die relevanten neuen Einträge der Woche zusammen.
- **Intake über GitHub-Issues:** Neue Fragen, Archivierungen und Feed-Vorschläge lassen
  sich direkt über Issue-Formulare auslösen (nur Repo-Owner).

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
| `ask "<Frage>"` *(Wrapper)*       | One-Shot: Recherche → Commit → Push → Pages-Deploy triggern.          |
| `researcher ask "<Frage>"`        | Neue Recherche, schreibt Topic + Quellen in die DB, rendert HTML.      |
| `researcher refresh`              | Prüft alle Quellen auf Aktualisierungen, re-recherchiert stale Topics. |
| `researcher refresh --topic <slug>` | Nur dieses Topic.                                                     |
| `researcher refresh --force`      | Erzwingt Re-Recherche aller Topics.                                   |
| `researcher refresh --min-stale 3 --min-age-days 14` | Schwellen für die Re-Recherche (Standardwerte). |
| `researcher digest [--force]`     | Scannt die Feeds und erstellt das Wochen-Briefing (`--force`: bereits gesehene Einträge erneut verarbeiten). |
| `researcher archive-topic <slug>` | Verschiebt ein Topic ins Archiv (keine Frische-Prüfung mehr, bleibt einsehbar). |
| `researcher unarchive-topic <slug>` | Holt ein Topic aus dem Archiv zurück.                               |
| `researcher add-feed --url <url> --name <name> [--category <kat>]` | Validiert einen RSS/Atom-Feed und ergänzt ihn in `feeds.json`. |
| `researcher intake`               | Verarbeitet einen Issue-Intake (wird vom Workflow aufgerufen, liest `INTAKE_ACTION` / `ISSUE_BODY`). |
| `researcher list`                 | Listet alle gespeicherten Topics.                                     |
| `researcher serve [--port 8000]`  | Lokale HTTP-Vorschau aus `dist/`.                                     |
| `researcher render-only`          | Rendert nur HTML neu (ohne Netzwerkzugriff).                          |
| `researcher doctor`               | Prüft Keys + Erreichbarkeit der Quellen.                              |
| `researcher init`                 | Legt die SQLite-DB an (passiert auch automatisch).                    |

### `ask.cmd` als globaler Befehl

Aufruf direkt aus dem Projektordner: `ask "Deine Frage"`.
Damit `ask` von **überall** funktioniert, einmalig den Projektpfad in den
Windows-PATH aufnehmen — z. B. via PowerShell:

```powershell
[Environment]::SetEnvironmentVariable(
  "Path",
  $env:Path + ";C:\Projects\Researcher-Agent",
  "User"
)
```

Neues Terminal öffnen → `ask "..."` ist jetzt von jedem Verzeichnis aus
erreichbar.

## Intake über GitHub-Issues

Unter **Issues → New issue** stehen drei Formulare bereit
(`.github/ISSUE_TEMPLATE/`):

| Formular | Label | Wirkung |
| --- | --- | --- |
| Neue Recherche-Frage | `intake:ask` | Recherchiert die Frage und veröffentlicht das Ergebnis. |
| Recherche archivieren | `intake:archive` | Archiviert das Topic mit dem angegebenen Slug. |
| Feed vorschlagen | `intake:add-feed` | Validiert den Feed und nimmt ihn ins Wochen-Briefing auf. |

Der Workflow `.github/workflows/intake.yml` verarbeitet das Issue, committet die
Änderungen, deployt die Seite und kommentiert das Ergebnis im Issue. Issues von
anderen Personen als dem Repo-Owner werden abgelehnt und geschlossen.

## Wie funktioniert die Aktualitätsprüfung?

Beim ersten Speichern einer Quelle holt der Agent ETag / Last-Modified-Header
sowie einen SHA-256-Hash des Body. Diese Werte landen in `data/researcher.sqlite`.

`researcher refresh` führt pro Quelle einen einzigen conditional `GET`-Request aus
(`If-None-Match` / `If-Modified-Since` mit den gespeicherten Validatoren):

1. Antwortet der Server mit `304 Not Modified`, gilt die Quelle als aktuell —
   ohne Body-Vergleich.
2. Bei `200` wird stattdessen ein normalisierter Inhalts-Hash (SHA-256, bei
   HTML/XML ohne Skripte/Styles/Tags) mit dem gespeicherten Wert verglichen.

Ein Topic wird erst dann vom Agent erneut bearbeitet, wenn **mindestens 3 seiner
Quellen** verändert sind (`--min-stale`) **und** die letzte Recherche **mindestens
14 Tage** zurückliegt (`--min-age-days`). Veränderte Quellen werden bis zur
nächsten Re-Recherche gemerkt und über mehrere Läufe aufsummiert. Nur diese
Quellen werden im System-Prompt als Fokus übergeben. `--force` ignoriert die
Schwellen.

## Token-Verbrauch & Kosten

| Stellschraube | Standard | Überschreiben |
| --- | --- | --- |
| Modell Recherche | `sonnet` | `RESEARCHER_MODEL` |
| Modell Briefing | `sonnet` | `RESEARCHER_DIGEST_MODEL` |
| Kostendeckel je Recherche | 3.0 USD | `RESEARCHER_MAX_BUDGET_USD` |
| Kostendeckel Briefing | 1.0 USD | `RESEARCHER_DIGEST_MAX_BUDGET_USD` |
| Max. Agent-Runden | 12 (Recherche) / 3 (Briefing) | `agent.py` |
| Kandidaten je Feed | 8 | `digest.py` (`MAX_PER_FEED`) |

- Die Recherche bekommt nur die nötigen Werkzeuge (`WebFetch`, Brave Web/News, MS Learn);
  das Briefing läuft ganz ohne Werkzeuge und wertet nur Titel und Auszug aus.
- Ein Lauf, der seinen Kostendeckel überschreitet, wird abgebrochen und **nicht** wiederholt.
- Jeder Agent-Lauf schreibt seinen Verbrauch ins Log (`ⓘ Verbrauch [recherche|briefing]: …`).

## Struktur

```
researcher/
├── cli.py            # typer-CLI
├── agent.py          # Claude Agent SDK + Output-Parsing
├── mcp_config.py     # Brave + Microsoft Learn MCP-Server
├── digest.py         # Wochen-Briefing aus den Feeds
├── feeds.py          # Laden/Ergänzen der Feed-Liste
├── feeds.json        # kuratierte RSS/Atom-Feeds
├── intake.py         # Parser für GitHub-Issue-Formulare
├── prompts/
│   ├── system_prompt_de.md         # Recherche
│   ├── system_prompt_digest_de.md  # Wochen-Briefing
│   └── org_context_de.md           # Organisations-Kontext
├── sources.py        # ETag / Last-Modified / SHA-256
├── store.py          # SQLite
├── render.py         # Jinja2 → dist/
├── templates/        # base, index, topic, archive, weekly
└── static/
tests/                 # pytest-Suite
data/
├── researcher.sqlite  # committed (Quell-Historie zwischen Workflow-Läufen)
└── cache/             # lokal, nicht im Repo
dist/                  # generierte statische Seite (deploy-ready)
.github/
├── workflows/         # pages.yml, intake.yml, tests.yml
├── ISSUE_TEMPLATE/    # Intake-Formulare
└── dependabot.yml     # wöchentliche Updates für uv + Actions
```

## Tests

```bash
uv sync --extra dev
uv run pytest -q
```

Der Workflow `tests.yml` führt die Suite bei jedem Push und Pull Request aus.

## Deployment auf GitHub Pages

`dist/` ist ein vollständiges statisches Bundle. Das Deployment übernimmt
`.github/workflows/pages.yml` (jeden Montag 06:00 UTC sowie manuell auslösbar):

1. `researcher refresh`: re-recherchiert veränderte Topics.
2. `researcher digest`: erstellt das Wochen-Briefing (manuell mit Option *force*).
3. Schlägt Refresh oder Digest fehl, wird trotzdem deployt, der Lauf zeigt dann
   aber eine sichtbare Warnung an.
4. Die aktualisierte `data/researcher.sqlite` wird zurück nach `main` committet.
5. `dist/` wird auf GitHub Pages deployt; bei transienten Deploy-Fehlern wird der
   Lauf automatisch bis zu dreimal wiederholt.

Einrichtung für ein neues Repo:

1. Code pushen (ohne `.env`, `data/cache/`).
2. **Settings → Pages** auf "GitHub Actions" stellen.
3. **Secrets:** `ANTHROPIC_API_KEY` und `BRAVE_API_KEY` als Repository-Secrets setzen.
4. In `intake.yml` ggf. `PAGES_BASE` auf die eigene Pages-URL anpassen.

> `data/researcher.sqlite` ist bewusst eingecheckt, damit Quell-Historie und
> Frische-Stände zwischen Workflow-Läufen erhalten bleiben.

## Lizenz

Privat / unspezifiziert.
