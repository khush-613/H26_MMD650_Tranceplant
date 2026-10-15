"""Checkpoint 2: jinja2 through all six stages with the real scan, xray and capsule.

sandbox and compare are Rajveer's real modules. Scan answers from the recorded API responses in fixtures/scan-cache and
Gemma from the saved replies, so the network is only used to install packages.
"""

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tranceplant import cli, sandbox
from tranceplant.models import Capsule

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
T_REPORT = "tests/test_report.py::test_report_renders_all_sites"


@pytest.fixture(params=["docker", "uv"])
def backend(request, monkeypatch, tmp_path, needs_uv):
    if request.param == "docker" and not sandbox.docker_available():
        pytest.skip("Docker is not running")
    shutil.copytree(FIXTURES / "scan-cache", tmp_path / "home" / "cache")
    monkeypatch.setenv("TRANCEPLANT_HOME", str(tmp_path / "home"))
    return request.param


def test_jinja2_runs_all_six_stages(backend, linkwatch, tmp_path):
    out = tmp_path / "capsules"
    result = CliRunner().invoke(cli.app, ["--gemma", "fake", "trial", str(linkwatch), "jinja2", "--out", str(out),
                                          "--backend", backend], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    folder = out / "jinja2"
    cap = Capsule.model_validate_json((folder / "capsule.json").read_text())

    # stage 1 and 2 are the real ones, not stand-ins
    assert cap.risk.score == 70 and any("CVE-2025-27516" in f.claim for f in cap.risk.indicators.findings)
    assert ("requirements.txt", 4, "transitive", "MarkupSafe>=0.23") in {(u.file, u.line, u.kind, u.symbol) for u in cap.usages}
    # stages 4 and 5: the doc's baseline, then the swap fixes the report test
    assert sum(r.outcome == "passed" for r in cap.control.results) == 9
    assert cap.comparison.newly_fixed == [T_REPORT] and cap.comparison.regressions == []
    assert cap.verdict == "Accepted"
    # stage 6
    md = (folder / "capsule.md").read_text()
    assert "**Verdict: Accepted** `OBSERVED`" in md and "Evidence check" not in md
    assert (folder / "capsule.html").is_file() and os.access(folder / "replay.sh", os.X_OK)

    replay = CliRunner().invoke(cli.app, ["replay", str(folder)], catch_exceptions=False)
    assert replay.exit_code == 0, replay.output
    assert "Result still holds" in replay.output
