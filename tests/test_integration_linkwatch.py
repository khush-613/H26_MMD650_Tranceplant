"""End to end on the real LinkWatch repo: real uv sandboxes, real pytest, saved Gemma replies.

The sandbox/compare/scan/xray here are the stand-ins from helpers.py (uv
virtualenvs, the doc's Docker fallback), so this proves the wiring, the
migration patches and the repair loop against LinkWatch's actual test suite.
Each test runs twice: in uv virtualenvs and in Docker containers (tranceplant-sandbox image).
Slow (~6 s per test run because of test_slow_is_up) and needs network for package installs.
"""

import json
import shutil

import pytest
from rich.console import Console
from typer.testing import CliRunner

import helpers
from helpers import install_stages
from tranceplant import cli
from tranceplant.gemma import PROJECT_DIR
from tranceplant.models import Capsule

pytestmark = pytest.mark.integration

T_REPORT = "tests/test_report.py::test_report_renders_all_sites"
T_CONFIG = "tests/test_config.py::test_sites_file_loads"


@pytest.fixture(params=["uv", "docker"])
def env(request, linkwatch, monkeypatch, tmp_path, needs_uv):
    if request.param == "docker" and not helpers.docker_ready():
        pytest.skip(f"docker or the {helpers.SANDBOX_IMAGE} image is not available")
    monkeypatch.setattr(cli, "trial_backend", request.param, raising=False)
    install_stages(monkeypatch, **helpers.STAGES)
    monkeypatch.setattr(cli, "console", Console(width=250))
    fx = tmp_path / "fx"
    shutil.copytree(PROJECT_DIR / "fixtures" / "gemma", fx)
    monkeypatch.setenv("TRANCEPLANT_FIXTURES_DIR", str(fx))
    return linkwatch, fx, tmp_path / "capsules"


def trial(repo, dep, out):
    result = CliRunner().invoke(cli.app, ["--gemma", "fake", "trial", str(repo), dep, "--out", str(out),
                                          "--backend", cli.trial_backend], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    cap = Capsule.model_validate_json((out / dep / "capsule.json").read_text())
    assert cap.control.backend == cap.donor.backend == cli.trial_backend
    return cap, result.output


def outcomes(run):
    return {r.name: r.outcome for r in run.results}


def test_jinja2_upgrade_is_accepted_and_fixes_the_report_test(env):
    repo, _, out = env
    cap, output = trial(repo, "jinja2", out)
    control = outcomes(cap.control)
    assert len(control) == 10
    assert control[T_REPORT] == "failed"  # soft_unicode ImportError with Jinja2 2.11.3
    assert sum(o == "passed" for o in control.values()) == 9  # the baseline in the design doc
    assert "soft_unicode" in (out / "jinja2" / "evidence" / "control.junit.xml").read_text()
    assert cap.comparison.newly_fixed == [T_REPORT]
    assert cap.comparison.regressions == []
    assert cap.verdict == "Accepted", output
    assert cap.migration[0].patch_applies.source == "OBSERVED"
    assert "applies cleanly" in cap.migration[0].patch_applies.claim

    # replay re-runs both sides and confirms the result still holds
    result = CliRunner().invoke(cli.app, ["replay", str(out / "jinja2")], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert "Result still holds" in result.output


def test_toml_to_tomllib_is_partial_because_of_python_310(env):
    repo, _, out = env
    cap, _ = trial(repo, "toml", out)
    assert cap.comparison.regressions == [] and cap.comparison.newly_fixed == []
    assert cap.verdict == "Partial"
    assert any("3.10" in g.description and g.source == "AI" for g in cap.gaps)
    assert "import tomllib" in (out / "toml" / "migration.patch").read_text()


def test_appdirs_to_platformdirs_is_partial(env):
    repo, _, out = env
    cap, _ = trial(repo, "appdirs", out)
    assert cap.comparison.regressions == []
    assert outcomes(cap.donor)["tests/test_cache.py::test_cache_round_trip"] == "passed"
    assert cap.verdict == "Partial"


def test_real_regression_is_repaired_by_a_second_round(env):
    """Gemma's first draft forgets tomllib needs binary mode; the real test run catches it; round 1 fixes it."""
    repo, fx, out = env
    draft = json.loads((fx / "write_migration" / "toml.json").read_text())
    for edit in draft["edits"]:
        if "toml.load(path)" in edit["find"]:
            edit["replace"] = "    with open(path) as fh:\n        data = tomllib.load(fh)"
    (fx / "write_migration" / "toml.json").write_text(json.dumps(draft))
    (fx / "repair_migration").mkdir()
    (fx / "repair_migration" / "toml.round1.json").write_text(json.dumps({
        "diagnosis": "tomllib.load raises TypeError on a text-mode file; it must be opened with 'rb'.",
        "steps": ["Open the sites file in binary mode"],
        "edits": [{"file": "linkwatch/config.py", "find": "    with open(path) as fh:",
                   "replace": '    with open(path, "rb") as fh:'}]}))
    (fx / "explain_gaps" / "toml.json").write_text(json.dumps({"gaps": []}))

    cap, output = trial(repo, "toml", out)
    first, final = cap.donor_rounds
    assert outcomes(first)[T_CONFIG] == "failed"  # observed regression in round 0
    assert "binary mode" in first.results[[r.name for r in first.results].index(T_CONFIG)].message
    assert outcomes(final)[T_CONFIG] == "passed"
    assert [m.round for m in cap.migration] == [0, 1]
    assert cap.comparison.regressions == []
    assert cap.verdict == "Accepted", output
    final_patch = (out / "toml" / "migration.patch").read_text()
    assert 'with open(path, "rb") as fh:' in final_patch
    assert (out / "toml" / "evidence" / "donor.round0.junit.xml").is_file()
    assert (out / "toml" / "evidence" / "donor.junit.xml").is_file()
