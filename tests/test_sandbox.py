import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tranceplant import sandbox
from tranceplant.sandbox import SETUP_FAILED, SandboxError, _DockerRunner, run_side

PASSING_REPO = {
    "tests/test_ok.py": "def test_one():\n    assert True\n\ndef test_two():\n    assert 1 + 1 == 2\n",
    "requirements.txt": "",
}


def make_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def patch_text(old: str, new: str, path="tests/test_ok.py") -> str:
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,2 +1,2 @@\n"
            f"-{old}\n+{new}\n     assert True\n")


# --- helpers ------------------------------------------------------------------


def test_junitxml_options_are_replaced_by_ours():
    assert sandbox._without_junitxml(["pytest", "--junitxml", "x.xml", "-q", "--junitxml=y.xml"]) == ["pytest", "-q"]
    assert sandbox._pytest_addopts("-x --junitxml=old.xml", "--junitxml=new.xml") == "-x --junitxml=new.xml"
    assert sandbox._pytest_addopts("'unbalanced", "--junitxml=n.xml") == "--junitxml=n.xml"


def test_container_script_separates_setup_failures():
    script = sandbox._container_script(("python", "-m", "pytest", "-q"))
    assert f"|| exit {SETUP_FAILED}" in script
    assert script.splitlines()[-1] == "python -m pytest -q"
    assert "--junitxml=.tranceplant-results.xml" in script


def test_python_version_marker():
    assert sandbox._python_version("noise\nTRANCEPLANT_PYTHON=3.12.15\nmore") == "3.12.15"
    assert sandbox._python_version("$ python -c \"print('TRANCEPLANT_PYTHON=' + v)\"") is None


def test_copy_skips_venvs_git_and_secrets(tmp_path):
    src = make_repo(tmp_path / "src", {"a.py": "", ".env": "SECRET=1", ".env.local": "x", ".env.example": "K=",
                                       ".git/HEAD": "", ".venv/x": "", "pkg/__pycache__/c.pyc": ""})
    sandbox._copy_repository(src, tmp_path / "dst")
    names = {p.relative_to(tmp_path / "dst").as_posix() for p in (tmp_path / "dst").rglob("*")}
    assert names == {"a.py", ".env.example", "pkg"}


def test_patch_applies_inside_a_folder_nested_in_another_git_repo(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    repo = make_repo(outer / "work" / "repo", PASSING_REPO)
    patch = tmp_path / "p.patch"
    patch.write_text(patch_text("def test_one():", "def test_uno():"))
    ok, out = sandbox._apply_patch(repo, patch)
    assert ok, out
    assert "test_uno" in (repo / "tests/test_ok.py").read_text()
    assert sandbox._apply_patch(repo, tmp_path / "missing.patch")[0] is False


# --- backend selection --------------------------------------------------------


def _fake_docker(monkeypatch, ping_error=None):
    client = types.SimpleNamespace(ping=lambda: (_ for _ in ()).throw(ping_error) if ping_error else True)
    monkeypatch.setitem(sys.modules, "docker", types.SimpleNamespace(from_env=lambda: client))
    return client


def test_backend_selection(monkeypatch):
    with pytest.raises(SandboxError, match="backend must be"):
        sandbox._select_backend("podman")
    assert sandbox._select_backend("uv") == ("uv", None, None)
    client = _fake_docker(monkeypatch)
    assert sandbox._select_backend("auto") == ("docker", client, None)
    _fake_docker(monkeypatch, ConnectionError("no socket"))
    chosen, _, note = sandbox._select_backend("auto")
    assert chosen == "uv" and "Docker unavailable" in note
    with pytest.raises(SandboxError, match="Docker is unavailable"):
        sandbox._select_backend("docker")
    assert sandbox.docker_available() is False
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SandboxError, match="install either backend"):
        sandbox._select_backend("auto")


def test_bad_inputs(tmp_path):
    with pytest.raises(SandboxError, match="does not exist"):
        run_side(tmp_path / "nope", "control", patch=None, evidence_dir=tmp_path / "ev", label="control", backend="uv")
    repo = make_repo(tmp_path / "r", PASSING_REPO)
    with pytest.raises(SandboxError, match="cannot be empty"):
        run_side(repo, "control", patch=None, evidence_dir=tmp_path / "ev", label="c", backend="uv", test_command="")


def test_patch_that_does_not_apply_is_a_failed_setup_not_a_crash(tmp_path):
    repo = make_repo(tmp_path / "r", PASSING_REPO)
    patch = tmp_path / "p.patch"
    patch.write_text(patch_text("def test_zzz():", "def test_uno():"))
    run = run_side(repo, "donor", patch=patch, evidence_dir=tmp_path / "cap" / "evidence", label="donor", backend="uv")
    assert run.setup_ok is False and run.results == [] and run.junit_file is None
    assert run.log_file == "evidence/donor.log"
    assert "did not apply" in (tmp_path / "cap" / run.log_file).read_text()


# --- Docker runner with a fake client ----------------------------------------


class FakeContainer:
    def __init__(self, status=0, logs=b"log text\n", wait_error=None, repo=None, report=True):
        self.status, self._logs, self.wait_error = status, logs, wait_error
        self.repo, self.report = repo, report
        self.killed = self.removed = False

    def wait(self, timeout):
        if self.wait_error:
            raise self.wait_error
        if self.report and self.repo:
            (self.repo / sandbox.REPORT_NAME).write_text(
                '<testsuites><testsuite><testcase classname="tests.test_ok" name="test_one"/></testsuite></testsuites>')
        return {"StatusCode": self.status}

    def kill(self):
        self.killed = True

    def logs(self, stdout, stderr):
        return self._logs

    def remove(self, force):
        self.removed = True


class FakeClient:
    def __init__(self, container=None, has_image=True):
        self.container = container
        self.has_image = has_image
        self.builds, self.runs = [], []
        self.images = types.SimpleNamespace(get=self._get, build=self._build)
        self.containers = types.SimpleNamespace(run=self._run)

    def _get(self, tag):
        if not self.has_image:
            raise LookupError(tag)

    def _build(self, path, tag, rm):
        self.builds.append({"files": sorted(p.name for p in Path(path).iterdir()), "tag": tag})

    def _run(self, image, **kw):
        self.runs.append({"image": image, **kw})
        self.container.repo = Path(next(iter(kw["volumes"])))
        return self.container

    def ping(self):
        return True


def test_existing_image_is_not_rebuilt(monkeypatch):
    client = FakeClient()
    _DockerRunner(client).ensure_image()
    assert client.builds == []
    monkeypatch.setenv("TRANCEPLANT_REBUILD_IMAGE", "1")
    _DockerRunner(client).ensure_image()
    assert len(client.builds) == 1


def test_missing_image_is_built_from_a_context_holding_only_the_dockerfile():
    client = FakeClient(has_image=False)
    _DockerRunner(client, "img:test").ensure_image()
    assert client.builds == [{"files": ["Dockerfile"], "tag": "img:test"}]


def _docker_side(tmp_path, monkeypatch, container):
    client = FakeClient(container)
    monkeypatch.setattr(sandbox, "_select_backend", lambda b: ("docker", client, None))
    repo = make_repo(tmp_path / "r", PASSING_REPO)
    run = run_side(repo, "control", patch=None, evidence_dir=tmp_path / "cap" / "evidence", label="control",
                   backend="docker", timeout_seconds=5)
    return run, client, (tmp_path / "cap" / run.log_file).read_text()


def test_docker_run_collects_results_and_cleans_up(tmp_path, monkeypatch):
    container = FakeContainer(status=1, logs=b"TRANCEPLANT_PYTHON=3.12.15\n1 failed\n")
    run, client, log = _docker_side(tmp_path, monkeypatch, container)
    assert run.setup_ok and run.backend == "docker" and run.exit_code == 1 and run.python_version == "3.12.15"
    assert [r.name for r in run.results] == ["tests/test_ok.py::test_one"]
    assert run.junit_file == "evidence/control.junit.xml"
    assert container.removed and "1 failed" in log
    kw = client.runs[0]
    assert kw["cap_drop"] == ["ALL"] and kw["network_mode"] == "bridge"
    assert "/root/.cache/uv" in [v["bind"] for v in kw["volumes"].values()]
    assert kw["environment"]["UV_PYTHON_DOWNLOADS"] == "never"


def test_docker_setup_failure(tmp_path, monkeypatch):
    run, _, log = _docker_side(tmp_path, monkeypatch, FakeContainer(status=SETUP_FAILED, logs=b"no matching dist\n",
                                                                    report=False))
    assert run.setup_ok is False and run.exit_code is None and run.results == []
    assert "no matching dist" in log


def test_docker_timeout_kills_the_container(tmp_path, monkeypatch):
    container = FakeContainer(wait_error=TimeoutError("Read timed out."), report=False)
    run, _, log = _docker_side(tmp_path, monkeypatch, container)
    assert container.killed and container.removed
    assert run.setup_ok is False and "Timed out after 5s" in log


def test_auto_falls_back_to_uv_when_docker_breaks(tmp_path, monkeypatch):
    class Broken(FakeClient):
        def _run(self, image, **kw):
            raise RuntimeError("daemon went away")

    monkeypatch.setattr(sandbox, "_select_backend", lambda b: ("docker", Broken(), None))
    called = {}

    def fake_uv(repo, command, timeout):
        called["yes"] = True
        (repo / sandbox.REPORT_NAME).write_text(
            '<testsuites><testsuite><testcase classname="tests.test_ok" name="test_one"/></testsuite></testsuites>')
        return sandbox._Outcome(0, True, "uv ran\n")

    monkeypatch.setattr(sandbox, "_run_uv", fake_uv)
    repo = make_repo(tmp_path / "r", PASSING_REPO)
    run = run_side(repo, "control", patch=None, evidence_dir=tmp_path / "ev", label="control", backend="auto")
    assert called and run.backend == "uv" and run.setup_ok
    assert "Docker failed; ran with uv instead: Could not start the Docker sandbox: daemon went away" in (
        tmp_path / "ev" / "control.log").read_text()
    with pytest.raises(SandboxError):
        run_side(repo, "control", patch=None, evidence_dir=tmp_path / "ev", label="control", backend="docker")


# --- real uv runs (slow) -------------------------------------------------------


@pytest.mark.integration
def test_real_uv_run_and_report(tmp_path, needs_uv):
    repo = make_repo(tmp_path / "r", {**PASSING_REPO, "tests/test_bad.py": "def test_bad():\n    assert False\n"})
    run = run_side(repo, "control", patch=None, evidence_dir=tmp_path / "cap" / "evidence", label="control",
                   backend="uv")
    assert run.setup_ok and run.exit_code == 1 and run.python_version.startswith("3.12")
    assert {r.name: r.outcome for r in run.results} == {
        "tests/test_bad.py::test_bad": "failed", "tests/test_ok.py::test_one": "passed",
        "tests/test_ok.py::test_two": "passed"}
    assert not (repo / ".venv").exists(), "the source repo must stay untouched"


@pytest.mark.integration
def test_real_uv_timeout(tmp_path, needs_uv):
    repo = make_repo(tmp_path / "r", {"tests/test_slow.py": "import time\n\ndef test_slow():\n    time.sleep(60)\n"})
    run = run_side(repo, "donor", patch=None, evidence_dir=tmp_path / "ev", label="donor", backend="uv",
                   timeout_seconds=15)
    assert run.setup_ok is False and run.results == []
    assert "Timed out after 15s" in (tmp_path / "ev" / "donor.log").read_text()


@pytest.mark.integration
def test_real_install_failure_on_both_backends(tmp_path, needs_uv):
    repo = make_repo(tmp_path / "r", {**PASSING_REPO, "requirements.txt": "jinja2==99.0.0\n"})
    backends = ["uv"] + (["docker"] if sandbox.docker_available() else [])
    for b in backends:
        run = run_side(repo, "donor", patch=None, evidence_dir=tmp_path / b, label="donor", backend=b)
        assert run.setup_ok is False and run.results == [], b
        assert "99.0.0" in (tmp_path / b / "donor.log").read_text(), b
