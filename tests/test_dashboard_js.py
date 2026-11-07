"""Runs the dashboard's JavaScript in a simulated browser (jsdom). Optional: needs Node.js and
`npm install` inside tests/js. It is skipped automatically when those are missing."""
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).parent / "js"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed (this test is optional)")
@pytest.mark.skipif(not (JS_DIR / "node_modules" / "jsdom").exists(), reason="run `npm install` in tests/js first (optional)")
def test_dashboard_runs_safely_in_a_simulated_browser():
    result = subprocess.run(["node", "--test", "dashboard.test.js"], cwd=JS_DIR, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-1000:]
