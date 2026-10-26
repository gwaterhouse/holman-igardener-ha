"""Policy tests. These do not import Home Assistant."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.holman_igardener.recovery import (
    BACKOFF_STEPS_S,
    UNREACHABLE_REPAIR_S,
    backoff_seconds,
    ready_in,
    repair_due,
    unreachable_issue_id,
)

ROOT = Path(__file__).resolve().parents[1]


def test_backoff_grows_then_caps():
    assert backoff_seconds(0) == 0
    assert [backoff_seconds(n) for n in range(1, 6)] == [
        *BACKOFF_STEPS_S,
        BACKOFF_STEPS_S[-1],
    ]


def test_ready_in_waits_for_the_later_deadline():
    assert ready_in(100, 90, 80) == 0
    assert ready_in(100, 130, 110) == 30
    assert ready_in(100, 110, 150) == 50


def test_repair_due_only_after_the_outage_window():
    assert repair_due(1000, None, False) is False
    assert repair_due(1000, 0, True) is False
    assert repair_due(1000, 1000 - UNREACHABLE_REPAIR_S + 1, False) is False
    assert repair_due(1000, 1000 - UNREACHABLE_REPAIR_S, False) is True
    assert repair_due(50, 0, False, after=50) is True
    assert repair_due(49, 0, False, after=50) is False


def test_issue_id_strips_colons():
    assert unreachable_issue_id("C5:6A:D4:A1:4A:88") == "unreachable_c56ad4a14a88"


def test_issue_strings_match_and_name_the_power_cycle():
    strings = json.loads(
        (ROOT / "custom_components/holman_igardener/strings.json").read_text()
    )
    english = json.loads(
        (ROOT / "custom_components/holman_igardener/translations/en.json").read_text()
    )
    assert strings == english
    issue = strings["issues"]["unreachable"]
    assert "{address}" in issue["title"]
    assert "{address}" in issue["description"]
    assert "power-cycle" in issue["description"]
