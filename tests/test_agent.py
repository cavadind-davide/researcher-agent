"""Unit-Tests für die Antwort-Parser und die Retry-Logik des Agents."""
from __future__ import annotations

import pytest

from researcher import agent

VALID = """```json
{"tldr": ["Kernaussage A"], "tags": ["iam"], "sources": [{"url": "https://x", "title": "T"}]}
```
===BODY_MD===
## Risiken & Bedrohungen
Quelle "Conditional Access" widerspricht Quelle "NIST" bei der Definition.
Mehrere Zeilen, `inline code`, und "weitere Zitate"."""


# --- _loads_lenient -------------------------------------------------------

def test_loads_lenient_valid():
    assert agent._loads_lenient('{"a": 1}') == {"a": 1}


def test_loads_lenient_repairs_trailing_comma():
    assert agent._loads_lenient('{"a": 1,}') == {"a": 1}


def test_loads_lenient_non_dict_raises():
    # Ungültiges JSON erzwingt den Repair-Pfad; repariert zu einer Liste -> ValueError.
    with pytest.raises(ValueError):
        agent._loads_lenient("[1, 2,]")


# --- _extract_meta_json ---------------------------------------------------

def test_extract_meta_json_from_fence():
    text = '```json\n{"tldr": ["x"]}\n```'
    assert agent._extract_meta_json(text) == {"tldr": ["x"]}


def test_extract_meta_json_brace_fallback():
    assert agent._extract_meta_json('Vorwort {"a": 1} Nachwort') == {"a": 1}


def test_extract_meta_json_missing_raises():
    with pytest.raises(ValueError):
        agent._extract_meta_json("kein json hier")


# --- _parse_response ------------------------------------------------------

def test_parse_response_keeps_raw_body_with_quotes():
    p = agent._parse_response(VALID)
    assert p["tldr"] == ["Kernaussage A"]
    assert p["body_md"].startswith("## Risiken")
    assert '"Conditional Access"' in p["body_md"]


def test_parse_response_strips_wrapping_fence():
    text = (
        '```json\n{"tldr": ["x"], "tags": [], "sources": [{"url": "https://a"}]}\n```\n'
        "===BODY_MD===\n```markdown\n## H\nText\n```"
    )
    assert agent._parse_response(text)["body_md"] == "## H\nText"


def test_parse_response_tolerates_sentinel_variants():
    text = (
        '```json\n{"tldr": ["x"], "tags": [], "sources": [{"url": "https://a"}]}\n```\n'
        "==== body_md ====\n## Body"
    )
    assert agent._parse_response(text)["body_md"] == "## Body"


def test_parse_response_missing_sentinel_raises():
    with pytest.raises(ValueError):
        agent._parse_response('```json\n{"tldr": ["x"]}\n```')


def test_parse_response_empty_body_raises():
    text = '```json\n{"tldr": ["x"], "tags": [], "sources": []}\n```\n===BODY_MD===\n   '
    with pytest.raises(ValueError):
        agent._parse_response(text)


# --- research (SDK gemockt) ----------------------------------------------

class _FakeAgent:
    """Ersetzt agent._run_agent; liefert je Aufruf die nächste Antwort
    (letzte wird wiederholt), und zählt die Aufrufe."""

    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.calls = 0

    async def __call__(self, question, *, focus_urls=None):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def test_research_success_first_try(monkeypatch):
    fake = _FakeAgent(VALID)
    monkeypatch.setattr(agent, "_run_agent", fake)
    payload = agent.research("Wie härte ich Entra ID?")
    assert fake.calls == 1
    assert payload["tldr"] == ["Kernaussage A"]
    assert payload["question"] == "Wie härte ich Entra ID?"
    assert payload["slug"] == agent.make_slug("Wie härte ich Entra ID?")
    assert payload["body_md"].startswith("## Risiken")


def test_research_retries_then_succeeds(monkeypatch):
    fake = _FakeAgent("kaputt, kein sentinel", VALID)
    monkeypatch.setattr(agent, "_run_agent", fake)
    payload = agent.research("Frage")
    assert fake.calls == 2
    assert payload["tldr"] == ["Kernaussage A"]


def test_research_retries_on_validation_failure(monkeypatch):
    missing_sources = (
        '```json\n{"tldr": ["A"], "tags": []}\n```\n===BODY_MD===\n## B\ntext'
    )
    fake = _FakeAgent(missing_sources, VALID)
    monkeypatch.setattr(agent, "_run_agent", fake)
    payload = agent.research("Frage")
    assert fake.calls == 2
    assert "sources" in payload


def test_research_raises_after_exhausting_retries(monkeypatch):
    fake = _FakeAgent("dauerhaft kaputt")
    monkeypatch.setattr(agent, "_run_agent", fake)
    with pytest.raises(ValueError):
        agent.research("Frage")
    assert fake.calls == agent._MAX_RETRIES + 1


# --- Digest -------------------------------------------------------------

DIGEST_OK = """```json
[{"url": "https://a/1", "title": "T", "summary": "S", "why_relevant": "W", "attention": "AT", "extra": "ignored"}]
```"""


class _FakeDigestAgent:
    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.calls = 0

    async def __call__(self, candidates_block):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


def test_extract_items_from_fence():
    assert agent._extract_items('```json\n[{"url": "x"}]\n```') == [{"url": "x"}]


def test_extract_items_bracket_fallback_and_repair():
    assert agent._extract_items('Text [{"url": "x",}] Ende') == [{"url": "x"}]


def test_extract_items_non_array_raises():
    with pytest.raises(ValueError):
        agent._extract_items('```json\n{"url": "x"}\n```')


def test_extract_items_missing_raises():
    with pytest.raises(ValueError):
        agent._extract_items("gar kein array")


def test_format_candidates_includes_url_and_title():
    block = agent._format_candidates([
        {"title": "Titel X", "url": "https://a/1", "source_name": "BSI", "category": "advisory", "summary": "Auszug"},
    ])
    assert "https://a/1" in block
    assert "Titel X" in block
    assert "BSI" in block


def test_summarize_digest_empty_candidates_skips_agent(monkeypatch):
    fake = _FakeDigestAgent(DIGEST_OK)
    monkeypatch.setattr(agent, "_run_digest_agent", fake)
    assert agent.summarize_digest([]) == []
    assert fake.calls == 0


def test_summarize_digest_normalizes_and_drops_extra_fields(monkeypatch):
    monkeypatch.setattr(agent, "_run_digest_agent", _FakeDigestAgent(DIGEST_OK))
    items = agent.summarize_digest([{"url": "https://a/1", "title": "x"}])
    assert items == [{"url": "https://a/1", "title": "T", "summary": "S", "why_relevant": "W", "attention": "AT"}]


def test_summarize_digest_empty_array_is_valid(monkeypatch):
    monkeypatch.setattr(agent, "_run_digest_agent", _FakeDigestAgent("```json\n[]\n```"))
    assert agent.summarize_digest([{"url": "https://a/1", "title": "x"}]) == []


def test_summarize_digest_drops_items_without_url(monkeypatch):
    resp = '```json\n[{"title": "ohne url"}, {"url": "https://a/2", "title": "ok"}]\n```'
    monkeypatch.setattr(agent, "_run_digest_agent", _FakeDigestAgent(resp))
    items = agent.summarize_digest([{"url": "https://a/2", "title": "x"}])
    assert [i["url"] for i in items] == ["https://a/2"]


def test_summarize_digest_retries_then_succeeds(monkeypatch):
    fake = _FakeDigestAgent("kein array hier", DIGEST_OK)
    monkeypatch.setattr(agent, "_run_digest_agent", fake)
    items = agent.summarize_digest([{"url": "https://a/1", "title": "x"}])
    assert fake.calls == 2
    assert items[0]["url"] == "https://a/1"
