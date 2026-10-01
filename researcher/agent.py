"""Orchestriert den Recherche-Lauf via Claude Agent SDK."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import deque
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    ResultMessage,
    TextBlock,
    query,
)
from json_repair import repair_json
from slugify import slugify

from .mcp_config import allowed_tools, build_mcp_servers, disallowed_tools

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompt_de.md"
DIGEST_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompt_digest_de.md"
ORG_CONTEXT_PATH = Path(__file__).resolve().parent / "prompts" / "org_context_de.md"


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def load_digest_prompt() -> str:
    return DIGEST_PROMPT_PATH.read_text(encoding="utf-8")


def load_org_context() -> str:
    return ORG_CONTEXT_PATH.read_text(encoding="utf-8")


def _with_org_context(system_prompt: str) -> str:
    """Hänge den Einsatzkontext (Auftraggeber-Profil) an einen System-Prompt an."""
    return system_prompt + "\n\n" + load_org_context()


def _research_system_prompt(focus_urls: list[str] | None = None) -> str:
    sp = load_system_prompt()
    if focus_urls:
        focus_block = "\n".join(f"- {u}" for u in focus_urls)
        sp += "\n\n# Zusatz: Fokus-Quellen (vorrangig prüfen)\n" + focus_block
    return _with_org_context(sp)


def _digest_system_prompt() -> str:
    return _with_org_context(load_digest_prompt())


def make_slug(question: str, max_length: int = 70) -> str:
    return slugify(question, max_length=max_length, word_boundary=True, save_order=True)


class AgentTransportError(RuntimeError):
    """Nicht vom SDK typisierter Transport-/Subprozess-Fehler (z. B. abgestürzter
    CLI-Prozess während des Message-Streamings). Das SDK wirft hierfür intern nur
    ein generisches ``Exception`` – wir fassen es hier zu einem gezielt
    behandelbaren, retrybaren Fehlertyp zusammen."""


_STDERR_TAIL_LINES = 30  # wie viele CLI-Stderr-Zeilen wir für Fehlermeldungen vorhalten


def _stderr_sink(tail: deque[str]):
    """Baut den ``stderr``-Callback für ``ClaudeAgentOptions``.

    Ohne registrierten Callback pipet das SDK den Stderr-Stream des CLI-Subprozesses
    gar nicht erst durch (siehe ``subprocess_cli.py``: ``stderr_dest = PIPE if
    self._options.stderr is not None else None``) – deshalb war die tatsächliche
    Absturzursache in den Actions-Logs bisher nie sichtbar, nur der feste Platzhalter
    "Check stderr output for details". Wir geben jede Zeile sofort ins CI-Log weiter
    und puffern zusätzlich die letzten Zeilen für die Fehlermeldung selbst.
    """

    def _sink(line: str) -> None:
        print(f"[claude-cli stderr] {line}", file=sys.stderr)
        tail.append(line)

    return _sink


# --- Kosten-/Token-Steuerung ---------------------------------------------
# Modelle per Umgebungsvariable überschreibbar (Alias wie "sonnet"/"haiku" oder
# volle Modell-ID). Recherche braucht Tiefe → Sonnet; das Briefing wählt nur aus
# vorgegebenen Kandidaten aus → Haiku reicht und ist deutlich günstiger.
DEFAULT_RESEARCH_MODEL = "sonnet"
DEFAULT_DIGEST_MODEL = "haiku"
# Jede Agent-Runde schickt den kompletten bisherigen Verlauf erneut mit – die
# Rundenzahl ist daher der größte Hebel für den Token-Verbrauch.
RESEARCH_MAX_TURNS = 12
DIGEST_MAX_TURNS = 3
# Harte Kostendeckel pro Lauf (USD) als Sicherheitsnetz gegen ausufernde Läufe.
DEFAULT_RESEARCH_BUDGET_USD = 2.0
DEFAULT_DIGEST_BUDGET_USD = 0.5


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"  ⚠ {name}={raw!r} ist keine Zahl – nutze {default}", file=sys.stderr)
        return default


def research_model() -> str:
    return os.environ.get("RESEARCHER_MODEL", "").strip() or DEFAULT_RESEARCH_MODEL


def digest_model() -> str:
    return os.environ.get("RESEARCHER_DIGEST_MODEL", "").strip() or DEFAULT_DIGEST_MODEL


class AgentBudgetExceeded(RuntimeError):
    """Der Lauf hat das Kostenbudget (``max_budget_usd``) überschritten. Bewusst
    NICHT retrybar: ein Retry würde dasselbe Budget erneut verbrauchen."""


def _log_usage(label: str, result: ResultMessage) -> None:
    """Token-Verbrauch und Kosten eines Laufs ins (CI-)Log schreiben, damit
    Einsparungen messbar bleiben."""
    usage = result.usage or {}
    cost = f"{result.total_cost_usd:.4f} USD" if result.total_cost_usd is not None else "?"
    print(
        f"  ⓘ Verbrauch [{label}]: Runden={result.num_turns} "
        f"input={usage.get('input_tokens', '?')} "
        f"cache_read={usage.get('cache_read_input_tokens', '?')} "
        f"cache_write={usage.get('cache_creation_input_tokens', '?')} "
        f"output={usage.get('output_tokens', '?')} Kosten={cost}",
        file=sys.stderr,
    )


async def _run_query(
    prompt: str,
    system_prompt: str,
    *,
    model: str | None = None,
    max_turns: int = RESEARCH_MAX_TURNS,
    max_budget_usd: float | None = None,
    use_tools: bool = True,
    label: str = "agent",
) -> str:
    """Sendet ``prompt`` mit gegebenem System-Prompt an den Agent und liefert den
    letzten nicht-leeren Assistant-Text zurück.

    ``use_tools=False`` startet den Agent ganz ohne Werkzeuge und MCP-Server
    (reine Textverarbeitung). Mit Werkzeugen wird die eingebaute Claude-Code-
    Werkzeugliste auf ``WebFetch`` beschränkt: ``allowed_tools`` regelt nur die
    Freigabe, nicht welche Werkzeug-Definitionen bei jeder Runde mitgeschickt
    werden – Bash/Read/Edit & Co. kosteten sonst bei jeder Runde Tokens.
    """
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        tools=["WebFetch"] if use_tools else [],
        mcp_servers=build_mcp_servers() if use_tools else {},
        allowed_tools=allowed_tools() if use_tools else [],
        disallowed_tools=disallowed_tools() if use_tools else [],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        stderr=_stderr_sink(stderr_tail),
    )

    final_text = ""
    result: ResultMessage | None = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        final_text = block.text
            elif isinstance(msg, ResultMessage):
                result = msg
    except (CLINotFoundError, CLIConnectionError, ProcessError):
        raise
    except Exception as exc:
        # Das SDK klassifiziert nicht jeden Subprozess-/Streaming-Fehler in einen
        # seiner eigenen Fehlertypen (siehe query.py: receive_messages() wirft bei
        # einer "error"-Message ein nacktes Exception). Ohne diesen Fang landet ein
        # flackernder Message-Reader nicht in _RETRYABLE_ERRORS und crasht den
        # gesamten Refresh-Lauf statt retried zu werden.
        message = str(exc)
        if stderr_tail:
            message += "\nCLI-Stderr (letzte {} Zeile(n)):\n{}".format(
                len(stderr_tail), "\n".join(stderr_tail)
            )
        raise AgentTransportError(message) from exc
    if result is not None:
        _log_usage(label, result)
        if result.subtype == "error_max_budget_usd":
            raise AgentBudgetExceeded(
                f"Kostenbudget von {max_budget_usd} USD überschritten ({label}) – Lauf abgebrochen."
            )
    return final_text


async def _run_agent(prompt: str, *, focus_urls: list[str] | None = None) -> str:
    """Recherche-Lauf mit dem Frage-System-Prompt (inkl. Org-Kontext)."""
    return await _run_query(
        prompt,
        _research_system_prompt(focus_urls),
        model=research_model(),
        max_turns=RESEARCH_MAX_TURNS,
        max_budget_usd=_env_float("RESEARCHER_MAX_BUDGET_USD", DEFAULT_RESEARCH_BUDGET_USD),
        label="recherche",
    )


async def _run_digest_agent(candidates_block: str) -> str:
    """Briefing-Lauf mit dem Digest-System-Prompt (inkl. Org-Kontext).

    Ohne Werkzeuge: der Agent wählt nur aus Titel und Auszug der Kandidaten aus.
    Seiten nachzuladen hat den Großteil der Briefing-Tokens verursacht."""
    return await _run_query(
        candidates_block,
        _digest_system_prompt(),
        model=digest_model(),
        max_turns=DIGEST_MAX_TURNS,
        max_budget_usd=_env_float("RESEARCHER_DIGEST_MAX_BUDGET_USD", DEFAULT_DIGEST_BUDGET_USD),
        use_tools=False,
        label="briefing",
    )


_JSON_FENCE = re.compile(r"```json\s*\n(?P<body>.*?)\n```", re.DOTALL | re.IGNORECASE)
# Trennt Metadaten-JSON (Teil 1) vom rohen Markdown-Body (Teil 2). Toleriert
# Schreibweisen wie "=== BODY_MD ===", zusätzliche "=" und Groß-/Kleinschreibung.
_BODY_SENTINEL = re.compile(r"\n?[ \t]*={3,}\s*BODY_MD\s*={3,}[ \t]*\n?", re.IGNORECASE)
# Optionaler umschließender Codefence um den Body (```markdown … ```), den wir abstreifen.
_WRAP_FENCE = re.compile(r"\A```[\w-]*\n(?P<body>.*?)\n```\Z", re.DOTALL)


def _loads_lenient(raw: str) -> dict[str, Any]:
    """Parse JSON, fall back to ``json_repair`` for typische LLM-Macken
    (trailing commas, smart quotes, fehlende Klammern)."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        repaired = repair_json(raw, return_objects=True)
        if not isinstance(repaired, dict):
            raise ValueError(
                f"json_repair lieferte {type(repaired).__name__}, erwartet dict"
            )
        return repaired


def _extract_meta_json(segment: str) -> dict[str, Any]:
    """Hole das Metadaten-JSON (tldr/tags/sources) aus dem Antwort-Kopf."""
    m = _JSON_FENCE.search(segment)
    if m:
        return _loads_lenient(m.group("body"))
    start = segment.find("{")
    end = segment.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "Konnte kein Metadaten-JSON in der Agent-Antwort finden. "
            f"Kopf beginnt mit: {segment[:300]!r}"
        )
    return _loads_lenient(segment[start : end + 1])


def _parse_response(text: str) -> dict[str, Any]:
    """Zerlege die zweiteilige Agent-Antwort: Metadaten-JSON + roher Markdown-Body.

    Der Body steht hinter ``BODY_MD``-Sentinel und braucht daher kein
    JSON-Escaping – das war die Hauptquelle der JSONDecodeErrors.
    """
    parts = _BODY_SENTINEL.split(text, maxsplit=1)
    if len(parts) != 2:
        raise ValueError(
            "BODY_MD-Sentinel fehlt in der Agent-Antwort. "
            f"Antwort endet mit: {text[-300:]!r}"
        )
    head, body = parts
    payload = _extract_meta_json(head)
    body = body.strip()
    wrap = _WRAP_FENCE.match(body)
    if wrap:
        body = wrap.group("body").strip()
    if not body:
        raise ValueError("Markdown-Body hinter BODY_MD-Sentinel ist leer")
    payload["body_md"] = body
    return payload


def _validate(payload: dict[str, Any]) -> None:
    required = {"tldr", "tags", "body_md", "sources"}
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"Agent-Antwort fehlen Felder: {missing}")
    if not isinstance(payload["tldr"], list) or len(payload["tldr"]) == 0:
        raise ValueError("'tldr' muss eine nicht-leere Liste sein")
    if not isinstance(payload["sources"], list) or len(payload["sources"]) == 0:
        raise ValueError("'sources' muss eine nicht-leere Liste sein")


_MAX_RETRIES = 2
# Wiederholbare Fehler: kaputtes/unvollständiges JSON (ValueError) sowie transiente
# Subprozess-/Verbindungsfehler des Agent-SDK (z. B. ProcessError mit nativem Crash,
# oder AgentTransportError für nicht typisierte Message-Reader-Abstürze).
# CLINotFoundError ist NICHT transient (Konfigurationsfehler) und wird durchgereicht.
_RETRYABLE_ERRORS = (ValueError, ProcessError, CLIConnectionError, AgentTransportError)


def research(question: str, *, focus_urls: list[str] | None = None) -> dict[str, Any]:
    """Führe eine vollständige Recherche aus und gib das geparste Ergebnis zurück."""
    last_exc: Exception = ValueError("Keine Versuche durchgeführt")
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            print(
                f"  ⚠ Recherche-Fehler (Versuch {attempt}/{_MAX_RETRIES}): {last_exc} – wiederhole…",
                file=sys.stderr,
            )
        try:
            text = asyncio.run(_run_agent(question, focus_urls=focus_urls))
            payload = _parse_response(text)
            _validate(payload)
            payload["slug"] = make_slug(question)
            payload["question"] = question
            return payload
        except CLINotFoundError:
            raise
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
    raise last_exc


# --- Wöchentliches Briefing ----------------------------------------------

_DIGEST_FIELDS = ("url", "title", "summary", "why_relevant", "attention", "severity")


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    lines = ["# Kandidaten dieser Woche", ""]
    for i, c in enumerate(candidates, 1):
        lines.append(f"## {i}. {c.get('title', '').strip()}")
        lines.append(f"- Quelle: {c.get('source_name')} ({c.get('category')})")
        lines.append(f"- URL: {c['url']}")
        if c.get("published_at"):
            lines.append(f"- Datum: {c['published_at']}")
        if c.get("summary"):
            lines.append(f"- Auszug: {c['summary']}")
        lines.append("")
    return "\n".join(lines)


def _extract_items(text: str) -> list[dict[str, Any]]:
    """Parse das JSON-Array der Digest-Items (mit ``json_repair``-Fallback)."""
    m = _JSON_FENCE.search(text)
    if m:
        raw = m.group("body")
    else:
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                f"Kein JSON-Array in der Digest-Antwort. Beginnt mit: {text[:300]!r}"
            )
        raw = text[start : end + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = repair_json(raw, return_objects=True)
    if not isinstance(data, list):
        raise ValueError(f"Digest-Antwort ist kein Array, sondern {type(data).__name__}")
    return data


def summarize_digest(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lass den Agent die relevanten Kandidaten auswählen und je Item
    ``{url, title, summary, why_relevant, attention}`` liefern. Ein leeres
    Array (nichts Relevantes) ist ein gültiges Ergebnis."""
    if not candidates:
        return []
    last_exc: Exception = ValueError("Keine Versuche durchgeführt")
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            print(
                f"  ⚠ Digest-Fehler (Versuch {attempt}/{_MAX_RETRIES}): {last_exc} – wiederhole…",
                file=sys.stderr,
            )
        try:
            text = asyncio.run(_run_digest_agent(_format_candidates(candidates)))
            items = _extract_items(text)
            return [
                {k: it.get(k) for k in _DIGEST_FIELDS}
                for it in items
                if isinstance(it, dict) and it.get("url")
            ]
        except CLINotFoundError:
            raise
        except _RETRYABLE_ERRORS as exc:
            last_exc = exc
    raise last_exc
