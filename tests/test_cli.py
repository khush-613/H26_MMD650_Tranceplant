import json
import os

import pytest
from typer.testing import CliRunner

import helpers
from helpers import install_stages
from tranceplant import cli, compare
from tranceplant.models import Capsule, SideRun, TestResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    from rich.console import Console

    monkeypatch.setattr(cli, "console", Console(width=250))

T_REPORT = "tests/test_report.py::test_report_renders_all_sites"
T_CONFIG = "tests/test_config.py::test_sites_file_loads"
T_CACHE = "tests/test_cache.py::test_cache_round_trip"


class FakeSandbox:
    """Scripted sandbox: outcomes per run, in call order for the donor side."""

    def __init__(self, control, *donors, donor_setup_ok=True):
        self.control = control
        self.donors = list(donors)
        self.donor_setup_ok = donor_setup_ok
        self.calls = []

    def __call__(self, repo, side, *, patch, evidence_dir, label, backend):
        self.calls.append({"side": side, "label": label, "patch": patch, "backend": backend})
        if side == "donor":
            assert patch is not None and patch.is_file()
        outcomes = self.control if side == "control" else self.donors.pop(0)
        setup_ok = side == "control" or self.donor_setup_ok
        junit = evidence_dir / f"{label}.junit.xml"
        log = evidence_dir / f"{label}.log"
        junit.write_text("<testsuite/>")
        log.write_text(f"{label} log: TypeError boom\n")
        root = evidence_dir.parent
        return SideRun(side=side, label=label, command="pytest", setup_ok=setup_ok, exit_code=0, backend="uv",
                       results=[TestResult(name=n, outcome=o) for n, o in outcomes.items()] if setup_ok else [],
                       junit_file=junit.relative_to(root).as_posix(), log_file=log.relative_to(root).as_posix())


def stages(monkeypatch, sandbox=None, **overrides):
    mods = {k: dict(v) for k, v in helpers.STAGES.items()}
    if sandbox is not None:
        mods["sandbox"] = {"run_side": sandbox}
    for key, value in overrides.items():
        mods[key] = value
    install_stages(monkeypatch, **mods)


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Fake Gemma fixtures dir, pre-filled with copies of the shipped ones."""
    import shutil

    from tranceplant.gemma import PROJECT_DIR

    target = tmp_path / "fx"
    shutil.copytree(PROJECT_DIR / "fixtures" / "gemma", target)
    monkeypatch.setenv("TRANCEPLANT_FIXTURES_DIR", str(target))
    return target


def put(fx, job, key, obj):
    (fx / job).mkdir(exist_ok=True)
    (fx / job / f"{key}.json").write_text(json.dumps(obj))


def invoke(*args):
    return runner.invoke(cli.app, ["--gemma", "fake", *map(str, args)], catch_exceptions=False)


def load(capsule_dir):
    return Capsule.model_validate_json((capsule_dir / "capsule.json").read_text())


# --- stubs and errors -------------------------------------------------------


@pytest.mark.parametrize("args", [["scan", "{repo}"], ["xray", "{repo}", "requests"], ["trial", "{repo}", "jinja2"],
                                  ["run", "{repo}", "--all"]])
def test_skeleton_stubs_give_a_clear_message(linkwatch, args):
    result = invoke(*[a.format(repo=linkwatch) for a in args])
    assert result.exit_code == 1
    assert "still a stub (owner: Khush)" in result.output


def test_missing_stage_module_message(linkwatch, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "tranceplant.compare", None)  # import fails like an unmerged module
    with pytest.raises(cli.StageMissing, match="Rajveer"):
        cli.stage("compare", "compare")


def test_missing_function_message(monkeypatch):
    install_stages(monkeypatch, compare={"compare": lambda c, d: None})
    with pytest.raises(cli.StageMissing, match="has no decide_verdict"):
        cli.stage("compare", "decide_verdict")


def test_bad_repo_path(tmp_path):
    result = invoke("scan", tmp_path / "nope")
    assert result.exit_code == 1 and "does not exist" in result.output


def test_unknown_dependency(linkwatch, monkeypatch, fx, tmp_path):
    stages(monkeypatch, FakeSandbox({}))
    result = invoke("trial", linkwatch, "flask", "--out", tmp_path / "caps")
    assert result.exit_code == 1 and "'flask' is not among" in result.output


# --- scan / xray ------------------------------------------------------------


def test_scan_table(linkwatch, monkeypatch):
    stages(monkeypatch)
    result = invoke("scan", linkwatch)
    assert result.exit_code == 0, result.output
    assert "jinja2" in result.output and "2.11.3" in result.output
    assert result.output.index("jinja2") < result.output.index("python-dateutil")  # sorted by score


def test_scan_json_with_gemma_explanations(linkwatch, monkeypatch, fx):
    stages(monkeypatch)
    result = invoke("scan", linkwatch, "--json", "--explain", "--min-score", "80")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    by_name = {d["dependency"]["name"]: d for d in data["dependencies"]}
    assert by_name["jinja2"]["ai_summary"]["source"] == "AI"
    assert "soft_unicode" in by_name["jinja2"]["ai_summary"]["claim"]
    assert by_name["toml"]["ai_summary"] is None  # below --min-score


def test_xray_with_notes(linkwatch, monkeypatch, fx):
    stages(monkeypatch)
    put(fx, "annotate_usages", "requests", {"notes": [{"index": 2, "note": "Aliased import grep would miss."}]})
    result = invoke("xray", linkwatch, "requests")
    assert result.exit_code == 0, result.output
    assert "checker.py:6" in result.output and "alias" in result.output and "private_api" in result.output
    assert "Aliased import grep would miss." in result.output
    assert "usage(s) found by AST scan" in result.output


def test_xray_without_notes_makes_no_gemma_calls(linkwatch, monkeypatch, tmp_path):
    stages(monkeypatch)
    monkeypatch.setenv("TRANCEPLANT_FIXTURES_DIR", str(tmp_path / "empty"))  # any Gemma call would fail
    result = invoke("xray", linkwatch, "requests", "--no-notes")
    assert result.exit_code == 0, result.output


# --- trial ------------------------------------------------------------------

CONTROL = {T_REPORT: "failed", T_CONFIG: "passed", T_CACHE: "passed"}
ALL_PASS = {T_REPORT: "passed", T_CONFIG: "passed", T_CACHE: "passed"}


def test_trial_accepted(linkwatch, monkeypatch, fx, tmp_path):
    sandbox = FakeSandbox(CONTROL, ALL_PASS)
    stages(monkeypatch, sandbox)
    out = tmp_path / "caps"
    result = invoke("trial", linkwatch, "Jinja2", "--out", out, "--backend", "uv")
    assert result.exit_code == 0, result.output
    assert "Verdict: Accepted" in result.output
    folder = out / "jinja2"
    cap = load(folder)
    assert cap.verdict == "Accepted" and cap.comparison.newly_fixed == [T_REPORT]
    assert cap.verdict_reason.source == "OBSERVED"
    assert [s["label"] for s in sandbox.calls] == ["control", "donor.round0"]
    assert all(s["backend"] == "uv" for s in sandbox.calls)
    assert (folder / "migration.patch").read_text() == (folder / "migration.round0.patch").read_text()
    assert cap.donor.junit_file == "evidence/donor.junit.xml" and (folder / "evidence/donor.junit.xml").is_file()
    assert cap.dependency.pinned == "2.11.3" and cap.risk.ai_summary.source == "AI"
    assert cap.gemma.models and cap.gemma.cache_hits > 0
    assert any(f"{T_REPORT} fails with the original" in c.claim and c.source == "OBSERVED" for c in cap.claims)
    # every evidence link in the capsule points at a real file
    for claim in [*cap.claims, cap.verdict_reason, *(s for m in cap.migration for s in m.steps),
                  *(m.patch_applies for m in cap.migration)]:
        for path in claim.evidence:
            assert (folder / path).is_file(), path
    assert cap.repo.commit and len(cap.repo.commit) == 40


def test_trial_partial_when_gemma_flags_a_gap(linkwatch, monkeypatch, fx, tmp_path):
    stages(monkeypatch, FakeSandbox(CONTROL, CONTROL))
    out = tmp_path / "caps"
    result = invoke("trial", linkwatch, "toml", "--out", out)
    assert result.exit_code == 0, result.output
    cap = load(out / "toml")
    assert cap.verdict == "Partial"
    assert cap.gaps[0].source == "AI" and "3.11" in cap.gaps[0].description
    assert "Verdict: Partial" in result.output


def test_trial_repairs_a_regression(linkwatch, monkeypatch, fx, tmp_path):
    regressed = dict(CONTROL, **{T_CONFIG: "failed"})
    sandbox = FakeSandbox(CONTROL, regressed, CONTROL)
    stages(monkeypatch, sandbox)
    put(fx, "explain_gaps", "toml", {"gaps": []})
    put(fx, "repair_migration", "toml.round1", {"diagnosis": "binary mode", "edits": [
        {"file": "linkwatch/config.py", "find": "import tomllib", "replace": "import tomllib  # stdlib"}]})
    out = tmp_path / "caps"
    result = invoke("trial", linkwatch, "toml", "--out", out)
    assert result.exit_code == 0, result.output
    cap = load(out / "toml")
    assert [s["label"] for s in sandbox.calls] == ["control", "donor.round0", "donor.round1"]
    assert sandbox.calls[2]["patch"].name == "migration.round1.patch"
    assert [m.round for m in cap.migration] == [0, 1] and cap.migration[1].diagnosis.claim == "binary mode"
    assert len(cap.donor_rounds) == 2 and cap.verdict == "Accepted"
    final = (out / "toml" / "migration.patch").read_text()
    assert "import tomllib  # stdlib" in final and "tomllib.load(fh)" in final  # cumulative


def test_trial_rejected_after_two_failed_repairs(linkwatch, monkeypatch, fx, tmp_path):
    regressed = dict(CONTROL, **{T_CONFIG: "failed"})
    sandbox = FakeSandbox(CONTROL, regressed, regressed, regressed)
    stages(monkeypatch, sandbox)
    put(fx, "repair_migration", "toml.round1", {"diagnosis": "d1", "edits": [
        {"file": "linkwatch/config.py", "find": "import tomllib", "replace": "import tomllib  # r1"}]})
    put(fx, "repair_migration", "toml.round2", {"diagnosis": "d2", "edits": [
        {"file": "linkwatch/config.py", "find": "import tomllib  # r1", "replace": "import tomllib  # r2"}]})
    out = tmp_path / "caps"
    result = invoke("trial", linkwatch, "toml", "--out", out)
    assert result.exit_code == 0, result.output
    cap = load(out / "toml")
    assert len(sandbox.calls) == 4, "control + 3 donor runs: never more than 2 repairs"
    assert cap.verdict == "Rejected" and cap.comparison.regressions == [T_CONFIG]
    assert cap.gaps[0].source == "OBSERVED" and T_CONFIG in cap.gaps[0].description
    assert "2 repair round(s)" in cap.verdict_reason.claim


def test_max_repairs_zero(linkwatch, monkeypatch, fx, tmp_path):
    sandbox = FakeSandbox(CONTROL, dict(CONTROL, **{T_CONFIG: "failed"}))
    stages(monkeypatch, sandbox)
    result = invoke("trial", linkwatch, "toml", "--out", tmp_path / "caps", "--max-repairs", "0")
    assert result.exit_code == 0 and len(sandbox.calls) == 2
    assert load(tmp_path / "caps" / "toml").verdict == "Rejected"


def test_donor_setup_failure_is_rejected_even_without_regressions(linkwatch, monkeypatch, fx, tmp_path):
    sandbox = FakeSandbox(CONTROL, {}, donor_setup_ok=False)
    stages(monkeypatch, sandbox, compare={"compare": compare.compare, "decide_verdict": lambda c, g: "Accepted"})
    result = invoke("trial", linkwatch, "jinja2", "--out", tmp_path / "caps", "--max-repairs", "0")
    assert result.exit_code == 0, result.output
    cap = load(tmp_path / "caps" / "jinja2")
    assert cap.verdict == "Rejected" and "setup failed" in cap.verdict_reason.claim


def test_failed_repair_keeps_the_last_good_attempt(linkwatch, monkeypatch, fx, tmp_path):
    sandbox = FakeSandbox(CONTROL, dict(CONTROL, **{T_CONFIG: "failed"}))
    stages(monkeypatch, sandbox)
    put(fx, "repair_migration", "toml.round1", {"diagnosis": "d", "edits": [
        {"file": "linkwatch/config.py", "find": "does not exist", "replace": "x"}]})
    result = invoke("trial", linkwatch, "toml", "--out", tmp_path / "caps")
    assert result.exit_code == 0, result.output
    assert "repair failed" in result.output
    assert [m.round for m in load(tmp_path / "caps" / "toml").migration] == [0]


# --- run --all ----------------------------------------------------------------


def test_run_all_picks_by_score_and_reports_each(linkwatch, monkeypatch, fx, tmp_path):
    donors = [ALL_PASS, CONTROL, CONTROL]  # jinja2, toml, appdirs (scores 90, 60, 55)
    sandbox = FakeSandbox(CONTROL, *donors)
    stages(monkeypatch, sandbox)
    # nose (85) has no migration fixture, so it errors and the run continues
    result = invoke("run", linkwatch, "--all", "--min-score", "55", "--out", tmp_path / "caps")
    assert result.exit_code == 1, result.output  # one dependency failed
    assert "nose failed" in result.output
    out = result.output
    assert "Accepted" in out and out.count("Partial") >= 2
    assert {p.name for p in (tmp_path / "caps").iterdir() if (p / "capsule.json").is_file()} == {"jinja2", "toml", "appdirs"}


def test_run_specific_deps(linkwatch, monkeypatch, fx, tmp_path):
    stages(monkeypatch, FakeSandbox(CONTROL, ALL_PASS))
    result = invoke("run", linkwatch, "--dep", "jinja2", "--out", tmp_path / "caps")
    assert result.exit_code == 0, result.output
    assert "Accepted" in result.output


def test_run_needs_all_or_dep(linkwatch, monkeypatch):
    stages(monkeypatch)
    result = invoke("run", linkwatch)
    assert result.exit_code == 1 and "--all or at least one --dep" in result.output


# --- replay -------------------------------------------------------------------


def test_replay_matches_then_detects_drift(linkwatch, monkeypatch, fx, tmp_path):
    stages(monkeypatch, FakeSandbox(CONTROL, ALL_PASS))
    out = tmp_path / "caps"
    assert invoke("trial", linkwatch, "jinja2", "--out", out).exit_code == 0

    replay_box = FakeSandbox(CONTROL, ALL_PASS)
    stages(monkeypatch, replay_box)
    result = invoke("replay", out / "jinja2")
    assert result.exit_code == 0, result.output
    assert "Result still holds" in result.output
    assert replay_box.calls[1]["patch"] == (out / "jinja2" / "migration.patch").resolve()
    assert replay_box.calls[0]["backend"] == "uv"  # the backend saved in the capsule
    replays = list((out / "jinja2" / "replays").iterdir())
    assert json.loads((replays[0] / "replay.json").read_text())["drifted_tests"] == 0

    stages(monkeypatch, FakeSandbox(CONTROL, dict(ALL_PASS, **{T_CACHE: "failed"})))
    result = invoke("replay", out / "jinja2")
    assert result.exit_code == 1 and "1 test(s) changed" in result.output


def test_replay_errors(tmp_path):
    assert "not found" in invoke("replay", tmp_path).output
    (tmp_path / "capsule.json").write_text("{}")
    result = invoke("replay", tmp_path)
    assert result.exit_code == 1 and "not a valid capsule" in result.output


# --- misc -------------------------------------------------------------------


def test_schema_command(tmp_path):
    result = invoke("schema")
    assert json.loads(result.output)["title"] == "Capsule"
    invoke("schema", "-o", tmp_path / "s.json")
    assert json.loads((tmp_path / "s.json").read_text())["title"] == "Capsule"


def test_gemma_check_fake():
    result = invoke("gemma-check")
    assert result.exit_code == 0 and "Gemma OK" in result.output and "fake" in result.output


def test_gemma_check_live_without_key_fails_cleanly():
    result = runner.invoke(cli.app, ["--gemma", "live", "gemma-check"], catch_exceptions=False)
    assert result.exit_code == 1 and "GEMINI_API_KEY is not set" in result.output


def test_dotenv_does_not_override_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nTP_A=from_file\nTP_B='quoted'\nTP_EMPTY=\nnot a line\n")
    monkeypatch.setenv("TP_A", "from_env")
    monkeypatch.delenv("TP_B", raising=False)
    cli.load_dotenv(env)
    assert os.environ["TP_A"] == "from_env" and os.environ["TP_B"] == "quoted" and "TP_EMPTY" not in os.environ
    monkeypatch.delenv("TP_B")
