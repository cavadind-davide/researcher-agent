"""Parser für GitHub-Issue-Form-Eingaben (interaktiver Intake von der Webseite).

GitHub rendert Issue-Forms als Markdown: pro Feld eine ``### <Label>``-Überschrift,
gefolgt vom Wert. Leere optionale Felder erscheinen als ``_No response_``.
Reine Funktionen, ohne Seiteneffekte — die Ausführung erfolgt in der CLI.
"""
from __future__ import annotations

import re

_HEADING = re.compile(r"^###[ \t]+(?P<label>.+?)[ \t]*$", re.MULTILINE)
_LABEL_PREFIX = "intake:"


def parse_issue_form(body: str | None) -> dict[str, str]:
    """Zerlege einen Issue-Form-Body in ``{Label: Wert}``."""
    text = (body or "").replace("\r\n", "\n")
    headings = list(_HEADING.finditer(text))
    fields: dict[str, str] = {}
    for i, m in enumerate(headings):
        label = m.group("label").strip()
        start = m.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        value = text[start:end].strip()
        if value == "_No response_":
            value = ""
        fields[label] = value
    return fields


def action_from_labels(labels: list[str]) -> str | None:
    """Erste ``intake:<aktion>``-Label-Aktion (z. B. ``ask``) oder ``None``."""
    for label in labels:
        if label.startswith(_LABEL_PREFIX):
            return label[len(_LABEL_PREFIX):]
    return None
