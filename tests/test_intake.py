"""Tests für den Issue-Form-Parser."""
from __future__ import annotations

from researcher import intake


def test_parse_single_field():
    body = "### Frage\n\nWie härte ich Entra ID?"
    assert intake.parse_issue_form(body) == {"Frage": "Wie härte ich Entra ID?"}


def test_parse_multiple_fields_crlf():
    body = "### Name\r\n\r\nTalos\r\n\r\n### URL\r\n\r\nhttps://talos\r\n\r\n### Kategorie\r\n\r\nthreat-intel"
    assert intake.parse_issue_form(body) == {
        "Name": "Talos",
        "URL": "https://talos",
        "Kategorie": "threat-intel",
    }


def test_parse_no_response_becomes_empty():
    body = "### Name\n\nTalos\n\n### Kategorie\n\n_No response_"
    fields = intake.parse_issue_form(body)
    assert fields["Name"] == "Talos"
    assert fields["Kategorie"] == ""


def test_parse_empty_body():
    assert intake.parse_issue_form("") == {}
    assert intake.parse_issue_form(None) == {}


def test_action_from_labels():
    assert intake.action_from_labels(["intake:ask", "bug"]) == "ask"
    assert intake.action_from_labels(["intake:add-feed"]) == "add-feed"
    assert intake.action_from_labels(["enhancement"]) is None
    assert intake.action_from_labels([]) is None
