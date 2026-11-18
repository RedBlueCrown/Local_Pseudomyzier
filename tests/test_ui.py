"""Browser smoke test: upload -> review -> decide -> manual edit -> export.

Needs Node.js with Playwright and a Chromium for it; skipped otherwise.
Screenshots: set ANON_UI_SHOTS=<dir>.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from anon.app.server import create_app  # noqa: E402
from fixtures import decision_vgh  # noqa: E402

SCRIPT = Path(__file__).parent / "ui" / "smoke.js"


def _playwright_available() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    probe = subprocess.run([node, "-e", "require('playwright')"], capture_output=True, cwd=SCRIPT.parent)
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _playwright_available(), reason="Node.js + Playwright not installed")


@pytest.fixture()
def live_app(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(tmp_path / "work"), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(5)


def test_review_flow_in_browser(live_app, tmp_path):
    docx = tmp_path / "Urteil VGH 2026-21.docx"
    docx.write_bytes(decision_vgh())
    shots = os.environ.get("ANON_UI_SHOTS", "-")
    if shots != "-":
        Path(shots).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["node", str(SCRIPT), live_app, str(docx), shots], capture_output=True, text=True,
                            timeout=240, cwd=SCRIPT.parent)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout + result.stderr
