"""Orchestriert den Recherche-Lauf via Claude Agent SDK."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)
from json_repair import repair_json
from slugify import slugify

from .mcp_config import allowed_tools, build_mcp_servers

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "system_prompt_de.md"


def load_system_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def make_slug(question: str, max_length: int = 70) -> str:
    return slugify(question, max_length=max_length, word_boundary=True, save_order=True)


async def _run_agent(prompt: str, *, focus_urls: list[str] | None = None) -> str:
    """Sendet ``prompt`` an den Agent und liefert den letzten Assistant-Text zurück."""
    system_prompt = load_system_prompt()
    if focus_urls:
        focus_block = "\n".join(f"- {u}" for u in focus_urls)
        system_prompt += (
            "\n\n# Zusatz: Fokus-Quellen (vorrangig prüfen)\n" + focus_block
        )

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers=build_mcp_servers(),
        allowed_tools=allowed_tools(),
        permission_mode="bypassPermissions",
        max_turns=25,
    )

    final_text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    final_text = block.text
    return final_text


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


def research(question: str, *, focus_urls: list[str] | None = None) -> dict[str, Any]:
    """Führe eine vollständige Recherche aus und gib das geparste Ergebnis zurück."""
    last_exc: Exception = ValueError("Keine Versuche durchgeführt")
    for attempt in range(_MAX_RETRIES + 1):
        if attempt:
            print(
                f"  ⚠ JSON-Parse-Fehler (Versuch {attempt}/{_MAX_RETRIES}): {last_exc} – wiederhole…",
                file=sys.stderr,
            )
        text = asyncio.run(_run_agent(question, focus_urls=focus_urls))
        try:
            payload = _parse_response(text)
            _validate(payload)
            payload["slug"] = make_slug(question)
            payload["question"] = question
            return payload
        except ValueError as exc:
            last_exc = exc
    raise last_exc
