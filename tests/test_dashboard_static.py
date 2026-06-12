"""Sanity checks for the static dashboard asset (route wiring is ticket 05's job)."""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "clauderouter" / "static" / "dashboard.html"
)

_EXTERNAL_RESOURCE_RE = re.compile(
    r"<(?:script[^>]+src|link[^>]+href)\s*=\s*[\"']https?://", re.IGNORECASE
)


def test_dashboard_file_exists_and_non_empty() -> None:
    assert DASHBOARD_PATH.is_file()
    content = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert content.strip()


def test_dashboard_has_no_external_resources() -> None:
    content = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert not _EXTERNAL_RESOURCE_RE.search(content)


def test_dashboard_references_control_endpoints() -> None:
    content = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert "/control/status" in content
    assert "/control/traffic" in content
