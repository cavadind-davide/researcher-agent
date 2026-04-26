"""Orchestriert den Recherche-Lauf via Claude Agent SDK."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)
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


def _extract_json(text: str) -> dict[str, Any]:
    m = _JSON_FENCE.search(text)
    if m:
        return json.loads(m.group("body"))
    # Fallback: erstes "{...}" Objekt
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "Konnte kein JSON in der Agent-Antwort finden. "
            f"Antwort beginnt mit: {text[:300]!r}"
        )
    return json.loads(text[start : end + 1])


def _validate(payload: dict[str, Any]) -> None:
    required = {"tldr", "tags", "body_md", "sources"}
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"Agent-Antwort fehlen Felder: {missing}")
    if not isinstance(payload["tldr"], list) or len(payload["tldr"]) == 0:
        raise ValueError("'tldr' muss eine nicht-leere Liste sein")
    if not isinstance(payload["sources"], list) or len(payload["sources"]) == 0:
        raise ValueError("'sources' muss eine nicht-leere Liste sein")


def research(question: str, *, focus_urls: list[str] | None = None) -> dict[str, Any]:
    """Führe eine vollständige Recherche aus und gib das geparste Ergebnis zurück."""
    text = asyncio.run(_run_agent(question, focus_urls=focus_urls))
    payload = _extract_json(text)
    _validate(payload)
    payload["slug"] = make_slug(question)
    payload["question"] = question
    return payload
