"""Runs the kiosk's JavaScript against the real rendered page under jsdom.

The kiosk has two input paths competing for one keyboard: an HID reader that
types card values into a hidden sink, and a human typing a PUID. Which one owns
focus is not expressible in a Python test, and getting it wrong silently
misroutes a member's ID as a card number — so it is tested here, for real.

Requires node and jsdom (`npm install --no-save jsdom`); skipped without them.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "kiosk_dom_test.mjs"
KIOSK_JS = ROOT / "app" / "static" / "kiosk.js"
ENROLL_HARNESS = ROOT / "tests" / "enroll_dom_test.mjs"
ENROLL_JS = ROOT / "app" / "static" / "enroll.js"

node = shutil.which("node")
jsdom_installed = (ROOT / "node_modules" / "jsdom").exists()

pytestmark = pytest.mark.skipif(
    not node or not jsdom_installed,
    reason="needs node and jsdom (npm install --no-save jsdom)",
)


def _render(db, path: str) -> str:
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            page = client.get(path)
            assert page.status_code == 200
            return page.text
    finally:
        app.dependency_overrides.clear()


def _run_harness(harness: Path, rendered: Path, script: Path) -> None:
    result = subprocess.run(
        [node, str(harness), str(rendered), str(script)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"{harness.name} checks failed:\n{result.stdout}\n{result.stderr}"
    )


def test_kiosk_javascript_behaves(db, tmp_path):
    rendered = tmp_path / "kiosk.html"
    rendered.write_text(_render(db, "/"), encoding="utf-8")
    _run_harness(HARNESS, rendered, KIOSK_JS)


def test_enrollment_javascript_behaves(db, tmp_path):
    """The enrollment page submits to two different endpoints depending on the
    mode it is in. Which one it picks is only observable from the browser."""
    rendered = tmp_path / "enroll.html"
    rendered.write_text(_render(db, "/enroll"), encoding="utf-8")
    _run_harness(ENROLL_HARNESS, rendered, ENROLL_JS)
