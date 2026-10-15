"""Stage 4: Twin Sandbox Trial. Owner: Rajveer.

Run one side (control or donor) of a repository in an isolated environment
and save the evidence. The Docker backend is preferred. The uv backend runs
the same steps in a local virtual environment, for machines without Docker.
With backend="auto", Docker is used when it is reachable and uv otherwise.

Each call writes <evidence_dir>/<label>.junit.xml and <label>.log and returns
a SideRun whose paths are relative to evidence_dir.parent (the capsule folder).
setup_ok is False when the patch does not apply, the install fails, or the run
times out, so the caller can reject the swap instead of mistaking missing
results for a partial success.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from .compare import parse_junit
from .models import Side, SideRun

DEFAULT_TEST_COMMAND = ("python", "-m", "pytest", "-q")
DOCKER_IMAGE = os.environ.get("TRANCEPLANT_SANDBOX_IMAGE", "tranceplant-sandbox:py312")
UV_CACHE_VOLUME = "tranceplant-uv-cache"
DEFAULT_TIMEOUT = int(os.environ.get("TRANCEPLANT_SANDBOX_TIMEOUT", "600"))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETUP_FAILED = 97  # exit code the container script uses when installs fail
REPORT_NAME = ".tranceplant-results.xml"
PYTHON_MARKER = "TRANCEPLANT_PYTHON="

IGNORED_COPY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "build",
    "dist",
}

Backend = Literal["auto", "docker", "uv"]


class SandboxError(RuntimeError):
    """Raised when no backend is usable at all."""


@dataclass
class _Outcome:
    exit_code: int | None
    setup_ok: bool
    output: str
    timed_out: bool = False


# --- Public API ---------------------------------------------------------------


def run_side(
    repo: Path,
    side: Side,
    *,
    patch: Path | None,
    evidence_dir: Path,
    label: str,
    backend: Backend = "auto",
    test_command: Sequence[str] | str = DEFAULT_TEST_COMMAND,
    timeout_seconds: int | None = None,
) -> SideRun:
    source = Path(repo).expanduser().resolve()
    evidence = Path(evidence_dir).expanduser().resolve()
    command = tuple(shlex.split(test_command) if isinstance(test_command, str) else map(str, test_command))
    timeout = timeout_seconds or DEFAULT_TIMEOUT
    if not source.is_dir():
        raise SandboxError(f"Repository directory does not exist: {source}")
    if not command:
        raise SandboxError("The test command cannot be empty")
    if timeout <= 0:
        raise SandboxError("timeout_seconds must be greater than zero")
    evidence.mkdir(parents=True, exist_ok=True)
    log_path = evidence / f"{label}.log"
    xml_path = evidence / f"{label}.junit.xml"
    xml_path.unlink(missing_ok=True)

    chosen, client, note = _select_backend(backend)
    notes = [note] if note else []
    with tempfile.TemporaryDirectory(prefix=f"tranceplant-{label}-") as temporary:
        work = Path(temporary) / "repo"
        _copy_repository(source, work)
        outcome: _Outcome | None = None
        if patch is not None:
            ok, output = _apply_patch(work, Path(patch))
            if not ok:
                outcome = _Outcome(None, False, f"Migration patch did not apply cleanly:\n{output}\n")
        if outcome is None and chosen == "docker":
            try:
                outcome = _DockerRunner(client).run(work, command, timeout)
            except SandboxError as exc:
                if backend != "auto" or shutil.which("uv") is None:
                    raise
                notes.append(f"Docker failed; ran with uv instead: {exc}")
                chosen = "uv"
        if outcome is None:
            outcome = _run_uv(work, command, timeout)
        report = work / REPORT_NAME
        if outcome.setup_ok and report.is_file():
            shutil.copy2(report, xml_path)

    header = "".join(f"# {n}\n" for n in notes)
    if outcome.timed_out:
        header += f"# Timed out after {timeout}s\n"
    log_path.write_text(header + outcome.output, encoding="utf-8")
    results: list = []
    issues: list[str] = []
    if xml_path.is_file():
        results, issues = parse_junit(xml_path, repo=source)
    setup_ok = outcome.setup_ok and not outcome.timed_out and xml_path.is_file() and not issues
    if issues or (outcome.setup_ok and not xml_path.is_file() and not outcome.timed_out):
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("".join(f"# {i}\n" for i in issues) or "# The test run produced no JUnit report\n")
    root = evidence.parent
    return SideRun(
        side=side,
        label=label,
        command=shlex.join(command),
        exit_code=outcome.exit_code,
        setup_ok=setup_ok,
        results=results,
        junit_file=xml_path.relative_to(root).as_posix() if xml_path.is_file() else None,
        log_file=log_path.relative_to(root).as_posix(),
        python_version=_python_version(outcome.output),
        backend=chosen,
    )


@dataclass(frozen=True)
class TwinTrialResult:
    control: SideRun
    donor: SideRun
    evidence_dir: str
    source_revision: str | None
    source_dirty: bool | None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_revision": self.source_revision,
            "source_dirty": self.source_dirty,
            "evidence_dir": self.evidence_dir,
            "control": self.control.model_dump(mode="json"),
            "donor": self.donor.model_dump(mode="json"),
        }


def run_twin_trial(
    repo_path: str | Path,
    *,
    evidence_dir: str | Path,
    migration_patch: str | Path | None = None,
    test_command: Sequence[str] | str = DEFAULT_TEST_COMMAND,
    backend: Backend = "auto",
    timeout_seconds: int | None = None,
) -> TwinTrialResult:
    """Control and donor from the same source, with trial.json written next to the evidence."""
    source = Path(repo_path).expanduser().resolve()
    evidence = Path(evidence_dir).expanduser().resolve()
    revision, dirty = _source_state(source)
    common = dict(evidence_dir=evidence, backend=backend, test_command=test_command, timeout_seconds=timeout_seconds)
    control = run_side(source, "control", patch=None, label="control", **common)
    donor = run_side(source, "donor", patch=Path(migration_patch) if migration_patch else None, label="donor", **common)
    result = TwinTrialResult(control, donor, str(evidence), revision, dirty)
    write_trial_manifest(result, evidence / "trial.json")
    return result


def write_trial_manifest(result: TwinTrialResult, path: str | Path) -> Path:
    """Write the twin run metadata as a small JSON evidence file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


def docker_available() -> bool:
    try:
        _select_backend("docker")
    except SandboxError:
        return False
    return True


# --- Preparation ----------------------------------------------------------------


def _source_state(repo: Path) -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True, timeout=10)
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
                                check=True, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, None
    return revision.stdout.strip() or None, bool(status.stdout.strip())


def _copy_repository(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in IGNORED_COPY_NAMES or name == ".env" or (name.startswith(".env.") and name != ".env.example")
        }

    try:
        shutil.copytree(source, destination, ignore=ignore, symlinks=True)
    except OSError as exc:
        raise SandboxError(f"Could not create trial copy from {source}: {exc}") from exc


def _apply_patch(repo: Path, patch_path: Path) -> tuple[bool, str]:
    if not patch_path.is_file():
        return False, f"patch file does not exist: {patch_path}"
    # The copy is not a git repo; stop git from finding an enclosing one and
    # resolving the patch's paths against the wrong root.
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(repo.parent))
    try:
        result = subprocess.run(["git", "apply", "--verbose", "--", str(patch_path)], cwd=repo, env=env,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not run git apply: {exc}"
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _select_backend(backend: str) -> tuple[str, object | None, str | None]:
    if backend not in {"auto", "docker", "uv"}:
        raise SandboxError("backend must be one of: auto, docker, uv")
    if backend == "uv":
        return "uv", None, None
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return "docker", client, None
    except Exception as exc:
        if backend == "docker":
            raise SandboxError(f"Docker is unavailable: {exc}") from exc
        if shutil.which("uv") is None:
            raise SandboxError("Docker is unavailable and uv was not found; install either backend") from exc
        return "uv", None, f"Docker unavailable; selected uv: {str(exc).splitlines()[0][:200]}"


# --- Docker -------------------------------------------------------------------


class _DockerRunner:
    def __init__(self, client: object, image: str = DOCKER_IMAGE) -> None:
        self.client = client
        self.image = image

    def ensure_image(self) -> None:
        """Build the image only when it is missing, so trials work offline once it exists."""
        try:
            self.client.images.get(self.image)  # type: ignore[attr-defined]
            if not os.environ.get("TRANCEPLANT_REBUILD_IMAGE"):
                return
        except Exception:
            pass
        dockerfile = PROJECT_ROOT / "Dockerfile.sandbox"
        with tempfile.TemporaryDirectory(prefix="tranceplant-image-") as context:
            # Build from a context holding only the Dockerfile, not the whole project.
            shutil.copy2(dockerfile, Path(context) / "Dockerfile")
            try:
                self.client.images.build(path=context, tag=self.image, rm=True)  # type: ignore[attr-defined]
            except Exception as exc:
                raise SandboxError(f"Could not build sandbox image {self.image}: {exc}") from exc

    def run(self, repo: Path, command: tuple[str, ...], timeout_seconds: int) -> _Outcome:
        self.ensure_image()
        script = _container_script(command)
        started = time.monotonic()
        container = None
        output = ""
        exit_code: int | None = None
        timed_out = False
        try:
            container = self.client.containers.run(  # type: ignore[attr-defined]
                self.image,
                command=["/bin/sh", "-c", script],
                detach=True,
                working_dir="/workspace",
                environment={"UV_LINK_MODE": "copy", "UV_PYTHON_DOWNLOADS": "never"},
                volumes={
                    str(repo): {"bind": "/workspace", "mode": "rw"},
                    UV_CACHE_VOLUME: {"bind": "/root/.cache/uv", "mode": "rw"},
                },
                network_mode="bridge",
                cap_drop=["ALL"],
                pids_limit=256,
                mem_limit="2g",
                tmpfs={"/tmp": "rw,nosuid,size=256m"},
            )
            exit_code = container.wait(timeout=timeout_seconds).get("StatusCode")
        except Exception as exc:
            if container is None:
                raise SandboxError(f"Could not start the Docker sandbox: {exc}") from exc
            if time.monotonic() - started >= timeout_seconds - 1 or "timed out" in str(exc).lower():
                timed_out = True
                try:
                    container.kill()
                except Exception:
                    pass
            else:
                output = f"Docker run error: {exc}\n"
                exit_code = -1
        finally:
            if container is not None:
                try:
                    raw = container.logs(stdout=True, stderr=True)
                    output += raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
                except Exception as exc:
                    output += f"\nCould not collect container logs: {exc}\n"
                try:
                    container.remove(force=True)
                except Exception:
                    pass
        setup_ok = exit_code != SETUP_FAILED and exit_code != -1 and not timed_out
        return _Outcome(exit_code if setup_ok else None, setup_ok, output, timed_out)


def _container_script(command: tuple[str, ...]) -> str:
    py = ".venv/bin/python"
    setup = [
        "uv venv -q .venv --python python3.12",
        f"uv pip install -q --python {py} pytest",
        f"if [ -f requirements.txt ]; then uv pip install -q --python {py} -r requirements.txt; fi",
        f"if [ -f requirements-dev.txt ]; then uv pip install -q --python {py} -r requirements-dev.txt; fi",
        f"if [ ! -f requirements.txt ] && [ -f pyproject.toml ]; then uv pip install -q --python {py} -e .; "
        f"elif [ -f requirements.txt ] && {{ [ -f pyproject.toml ] || [ -f setup.py ]; }}; "
        f"then uv pip install -q --python {py} --no-deps -e .; fi",
    ]
    lines = ["set -u", "{ " + " && ".join(setup) + f"; }} || exit {SETUP_FAILED}"]
    lines.append(f"{py} -c 'import platform; print(\"{PYTHON_MARKER}\" + platform.python_version())'")
    lines.append(f"export PYTEST_ADDOPTS={shlex.quote('--junitxml=' + REPORT_NAME)}")
    lines.append('export PATH="/workspace/.venv/bin:$PATH"')
    lines.append(" ".join(shlex.quote(arg) for arg in _without_junitxml(command)))
    return "\n".join(lines)


# --- uv -----------------------------------------------------------------------


def _run_uv(repo: Path, command: tuple[str, ...], timeout_seconds: int) -> _Outcome:
    uv = shutil.which("uv")
    if uv is None:
        raise SandboxError("The uv sandbox backend was selected, but uv is not installed")
    windows = os.name == "nt"
    bin_dir = repo / ".venv" / ("Scripts" if windows else "bin")
    python = bin_dir / ("python.exe" if windows else "python")

    def pip(*args: str) -> list[str]:
        return [uv, "pip", "install", "-q", "--python", str(python), *args]

    setup_commands = [[uv, "venv", "-q", "--python", "3.12", ".venv"], pip("pytest")]
    found_requirements = False
    for name in ("requirements.txt", "requirements-dev.txt"):
        if (repo / name).is_file():
            found_requirements = True
            setup_commands.append(pip("-r", name))
    if not found_requirements and (repo / "pyproject.toml").is_file():
        setup_commands.append(pip("-e", "."))
    elif found_requirements and ((repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file()):
        setup_commands.append(pip("--no-deps", "-e", "."))

    test_args = _without_junitxml(command)
    if test_args and test_args[0].lower() in {"python", "python3"}:
        test_args[0] = str(python)
    elif test_args and test_args[0].lower() == "pytest":
        test_args[0] = str(bin_dir / ("pytest.exe" if windows else "pytest"))

    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PYTEST_ADDOPTS"] = _pytest_addopts(env.get("PYTEST_ADDOPTS", ""), "--junitxml=" + REPORT_NAME)
    env["UV_PYTHON_DOWNLOADS"] = env.get("UV_PYTHON_DOWNLOADS", "automatic")

    parts: list[str] = []
    deadline = time.monotonic() + timeout_seconds

    def run(args: list[str]) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout_seconds)
        done = subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, timeout=remaining)
        parts.append(_format_command(args, done.stdout, done.stderr))
        return done

    try:
        for setup in setup_commands:
            if run(setup).returncode:
                return _Outcome(None, False, "".join(parts))
        run([str(python), "-c", f"import platform; print('{PYTHON_MARKER}' + platform.python_version())"])
        done = run(test_args)
        return _Outcome(done.returncode, True, "".join(parts))
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        parts.append(_format_command(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], stdout, stderr))
        return _Outcome(None, True, "".join(parts), timed_out=True)
    except OSError as exc:
        parts.append(f"Sandbox command could not start: {exc}\n")
        return _Outcome(None, False, "".join(parts))


# --- Helpers ------------------------------------------------------------------


def _without_junitxml(command: Sequence[str]) -> list[str]:
    """Use a stable report path even when the incoming command sets one."""
    result: list[str] = []
    skip_next = False
    for argument in command:
        if skip_next:
            skip_next = False
        elif argument == "--junitxml":
            skip_next = True
        elif not argument.startswith("--junitxml="):
            result.append(str(argument))
    return result


def _pytest_addopts(existing: str, report_argument: str) -> str:
    try:
        options = shlex.split(existing)
    except ValueError:
        options = []
    return shlex.join([*_without_junitxml(options), report_argument])


def _format_command(command: Sequence[str], stdout: str, stderr: str) -> str:
    pieces = [f"$ {shlex.join([str(part) for part in command])}\n"]
    if stdout:
        pieces.append(stdout)
    if stderr:
        pieces.append("\n[stderr]\n" + stderr)
    if not pieces[-1].endswith("\n"):
        pieces.append("\n")
    return "".join(pieces)


def _python_version(output: str) -> str | None:
    match = re.search("^" + re.escape(PYTHON_MARKER) + r"([0-9][\w.+-]*)$", output, re.MULTILINE)
    return match.group(1) if match else None
