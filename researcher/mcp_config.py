"""MCP-Server-Konfiguration für den Researcher-Agent.

Liefert das ``mcp_servers``-Dict, das an ``ClaudeAgentOptions`` übergeben wird.
"""
from __future__ import annotations

import os
from typing import Any


def build_mcp_servers() -> dict[str, dict[str, Any]]:
    """Bauen die MCP-Server-Konfiguration auf Basis von Umgebungs­variablen.

    - Brave Search: stdio-Transport via ``npx`` (offizielles ``@brave/brave-search-mcp-server``).
    - Microsoft Learn: Remote Streamable-HTTP unter learn.microsoft.com/api/mcp.
    """
    servers: dict[str, dict[str, Any]] = {}

    brave_key = os.environ.get("BRAVE_API_KEY")
    if brave_key:
        servers["brave-search"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@brave/brave-search-mcp-server"],
            "env": {"BRAVE_API_KEY": brave_key},
        }

    servers["microsoft-learn"] = {
        "type": "http",
        "url": "https://learn.microsoft.com/api/mcp",
    }

    return servers


def allowed_tools() -> list[str]:
    """Welche Tools darf der Agent verwenden.

    MCP-Tools werden vom SDK als ``mcp__<server>__<tool>`` exponiert. Wir erlauben
    explizit alle Brave-Web-Search- und MS-Learn-Tools sowie WebFetch.
    """
    return [
        "mcp__brave-search__brave_web_search",
        "mcp__brave-search__brave_news_search",
        "mcp__microsoft-learn__microsoft_docs_search",
        "mcp__microsoft-learn__microsoft_docs_fetch",
        "WebFetch",
    ]
