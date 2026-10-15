"""Run a control and donor copy of a repository in isolated environments.

The Docker backend is preferred.  The ``uv`` backend is provided for machines
where Docker is unavailable, matching the project's documented demo fallback.
Both backends write the same JUnit and log artifacts for :mod:`compare`.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_TEST_COMMAND = ("python", "-m", "pytest", "-q")
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


class SandboxError(RuntimeError):
    """Raised when a trial cannot be prepared or started."""


@dataclass(frozen=True)
class TestRun:
    """Observed result and evidence paths for one side of a twin trial."""

    side: str
    command: tuple[str, ...]
    exit_code: int | None
    duration_seconds: float
    junit_xml: str
    log: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["command"] = list(self.command)
        result["passed"] = self.passed
        return result


@dataclass(frozen=True)
class TwinTrialResult:
    """The control/donor pair and its common source revision, if available."""

    backend: str
    backend_note: str | None
    source_revision: str | None
    source_dirty: bool | None
    control: TestRun
    donor: TestRun
    evidence_dir: str

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "backend_note": self.backend_note,
            "source_revision": self.source_revision,
            "source_dirty": self.source_dirty,
            "control": self.control.to_dict(),
            "donor": self.donor.to_dict(),
            "evidence_dir": self.evidence_dir,
        }


def run_twin_trial(
    repo_path: str | Path,
    *,
    evidence_dir: str | Path,
    migration_patch: str | Path | None = None,
    replacement: str | None = None,
    test_command: Sequence[str] | str = DEFAULT_TEST_COMMAND,
    backend: str = "auto",
    timeout_seconds: int = 600,
    docker_image: str = "tranceplant-sandbox:py312",
) -> TwinTrialResult:
    """Run the same test command on clean control and patched donor copies.

    ``replacement`` is an optional pip requirement such as
    ``Jinja2>=3.1,<3.2``. Use ``None`` for standard-library replacements or
    when the migration patch updates the dependency manifest itself.
    """

    source = Path(repo_path).expanduser().resolve()
    evidence = Path(evidence_dir).expanduser().resolve()
    patch_path = Path(migration_patch).expanduser().resolve() if migration_patch else None
    command = tuple(shlex.split(test_command) if isinstance(test_command, str) else (str(part) for part in test_command))
    if not source.is_dir():
        raise SandboxError(f"Repository directory does not exist: {source}")
    if not command:
        raise SandboxError("The test command cannot be empty")
    if timeout_seconds <= 0:
        raise SandboxError("timeout_seconds must be greater than zero")
    if patch_path is not None and not patch_path.is_file():
        raise SandboxError(f"Migration patch does not exist: {patch_path}")

    evidence.mkdir(parents=True, exist_ok=True)
    revision, source_dirty = _source_state(source)
    selected_backend, docker_client, backend_note = _select_backend(backend)

    with tempfile.TemporaryDirectory(prefix="tranceplant-trial-") as temporary:
        work = Path(temporary)
        control_repo = work / "control"
        donor_repo = work / "donor"
        _copy_repository(source, control_repo)
        # Derive the donor from the already captured control snapshot so both
        # sides cannot diverge if the source tree changes during preparation.
        _copy_repository(control_repo, donor_repo)
        if patch_path is not None:
            _apply_patch(donor_repo, patch_path)

        if selected_backend == "docker":
            assert docker_client is not None
            runner = _DockerRunner(docker_client, docker_image)
            try:
                runner.build()
                control_result = runner.run(
                    "control", control_repo, evidence, command, None, timeout_seconds
                )
                donor_result = runner.run(
                    "donor", donor_repo, evidence, command, replacement, timeout_seconds
                )
            except SandboxError as exc:
                if backend != "auto" or shutil.which("uv") is None:
                    raise
                backend_note = f"Docker failed; reran both sides with uv: {exc}"
                selected_backend = "uv"
        if selected_backend == "uv":
            control_result = _run_uv(
                "control", control_repo, evidence, command, None, timeout_seconds
            )
            donor_result = _run_uv(
                "donor", donor_repo, evidence, command, replacement, timeout_seconds
            )

    result = TwinTrialResult(
        backend=selected_backend,
        backend_note=backend_note,
        source_revision=revision,
        source_dirty=source_dirty,
        control=control_result,
        donor=donor_result,
        evidence_dir=str(evidence),
    )
    write_trial_manifest(result, evidence / "trial.json")
    return result


def _source_state(repo: Path) -> tuple[str | None, bool | None]:
    try:
        revision_result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        status_result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    return revision_result.stdout.strip() or None, bool(status_result.stdout.strip())


def _copy_repository(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in IGNORED_COPY_NAMES
            or name == ".env"
            or (name.startswith(".env.") and name != ".env.example")
        }

    try:
        shutil.copytree(source, destination, ignore=ignore, symlinks=True)
    except OSError as exc:
        raise SandboxError(f"Could not create trial copy from {source}: {exc}") from exc


def _apply_patch(repo: Path, patch_path: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "apply", "--", str(patch_path)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"Could not apply migration patch: {exc}") from exc
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        raise SandboxError(f"Migration patch did not apply cleanly: {details}")


def _select_backend(backend: str) -> tuple[str, object | None, str | None]:
    if backend not in {"auto", "docker", "uv"}:
        raise SandboxError("backend must be one of: auto, docker, uv")
    if backend == "uv":
        return "uv", None, None

    try:
        import docker  # type: ignore[import-not-found]

        client = docker.from_env()
        client.ping()
        return "docker", client, None
    except Exception as exc:
        if backend == "docker":
            raise SandboxError(f"Docker is unavailable: {exc}") from exc
        if shutil.which("uv") is None:
            raise SandboxError(
                "Docker is unavailable and uv was not found; install either backend"
            ) from exc
        return "uv", None, f"Docker unavailable; selected uv: {exc}"


class _DockerRunner:
    def __init__(self, client: object, image: str) -> None:
        self.client = client
        self.image = image

    def build(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        try:
            self.client.images.build(  # type: ignore[attr-defined]
                path=str(project_root),
                dockerfile="Dockerfile.sandbox",
                tag=self.image,
                rm=True,
            )
        except Exception as exc:
            raise SandboxError(f"Could not build sandbox image {self.image}: {exc}") from exc

    def run(
        self,
        side: str,
        repo: Path,
        evidence: Path,
        command: tuple[str, ...],
        replacement: str | None,
        timeout_seconds: int,
    ) -> TestRun:
        junit_in_repo = repo / ".tranceplant-results.xml"
        script = _container_script(command, replacement, junit_in_repo="/workspace/.tranceplant-results.xml")
        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        output = ""
        container = None
        try:
            container = self.client.containers.run(  # type: ignore[attr-defined]
                self.image,
                command=["/bin/sh", "-lc", script],
                detach=True,
                working_dir="/workspace",
                volumes={str(repo): {"bind": "/workspace", "mode": "rw"}},
                network_mode="bridge",
                cap_drop=["ALL"],
                pids_limit=256,
                mem_limit="2g",
                tmpfs={"/tmp": "rw,nosuid,size=256m"},
            )
            status = container.wait(timeout=timeout_seconds)
            exit_code = status.get("StatusCode")
        except Exception as exc:
            elapsed = time.monotonic() - started
            is_timeout = elapsed >= timeout_seconds - 1 or "timeout" in str(exc).lower()
            if container is not None and is_timeout:
                timed_out = True
                try:
                    container.kill()
                    container.wait(timeout=10)
                except Exception:
                    pass
            elif container is None:
                raise SandboxError(f"Could not start {side} Docker sandbox: {exc}") from exc
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

        elapsed = time.monotonic() - started
        return _save_run(side, command, exit_code, elapsed, junit_in_repo, evidence, output, timed_out)


def _run_uv(
    side: str,
    repo: Path,
    evidence: Path,
    command: tuple[str, ...],
    replacement: str | None,
    timeout_seconds: int,
) -> TestRun:
    uv = shutil.which("uv")
    if uv is None:
        raise SandboxError("The uv sandbox backend was selected, but uv is not installed")
    if os.name == "nt":
        python = repo / ".venv" / "Scripts" / "python.exe"
        bin_dir = repo / ".venv" / "Scripts"
        junit_in_repo = repo / ".tranceplant-results.xml"
        junit_arg = "--junitxml=.tranceplant-results.xml"
        venv_command = [uv, "venv", "--python", "3.12", ".venv"]
        python_arg = str(python)
    else:
        python = repo / ".venv" / "bin" / "python"
        bin_dir = repo / ".venv" / "bin"
        junit_in_repo = repo / ".tranceplant-results.xml"
        junit_arg = "--junitxml=.tranceplant-results.xml"
        venv_command = [uv, "venv", "--python", "3.12", ".venv"]
        python_arg = str(python)

    setup_commands = [venv_command]
    setup_commands.append([uv, "pip", "install", "--python", python_arg, "pytest"])
    requirements = [repo / "requirements.txt", repo / "requirements-dev.txt"]
    found_requirements = False
    for requirements_file in requirements:
        if requirements_file.is_file():
            found_requirements = True
            setup_commands.append(
                [uv, "pip", "install", "--python", python_arg, "-r", str(requirements_file)]
            )
    if not found_requirements and (repo / "pyproject.toml").is_file():
        setup_commands.append([uv, "pip", "install", "--python", python_arg, "-e", "."])
    elif found_requirements and (
        (repo / "pyproject.toml").is_file() or (repo / "setup.py").is_file()
    ):
        setup_commands.append(
            [uv, "pip", "install", "--python", python_arg, "--no-deps", "-e", "."]
        )
    if replacement:
        setup_commands.append(
            [uv, "pip", "install", "--python", python_arg, replacement]
        )

    test_args = _without_junitxml(command)
    if test_args and test_args[0].lower() in {"python", "python3"}:
        test_args[0] = str(python)
    elif test_args and test_args[0].lower() == "pytest":
        test_args[0] = str(bin_dir / ("pytest.exe" if os.name == "nt" else "pytest"))

    output_parts: list[str] = []
    started = time.monotonic()
    deadline = started + timeout_seconds
    exit_code: int | None = None
    timed_out = False
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["PYTEST_ADDOPTS"] = _pytest_addopts(env.get("PYTEST_ADDOPTS", ""), junit_arg)
    try:
        for setup in setup_commands:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(setup, timeout_seconds)
            completed = subprocess.run(
                setup,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=remaining,
            )
            output_parts.append(_format_command(setup, completed.stdout, completed.stderr))
            if completed.returncode:
                exit_code = completed.returncode
                break
        if exit_code is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(test_args, timeout_seconds)
            completed = subprocess.run(
                test_args,
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                timeout=remaining,
            )
            output_parts.append(_format_command(test_args, completed.stdout, completed.stderr))
            exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        output_parts.append(_format_command(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)], stdout, stderr))
        exit_code = None
    except OSError as exc:
        output_parts.append(f"Sandbox command could not start: {exc}\n")
        exit_code = -1

    return _save_run(
        side,
        command,
        exit_code,
        time.monotonic() - started,
        junit_in_repo,
        evidence,
        "".join(output_parts),
        timed_out,
    )


def _container_script(
    command: tuple[str, ...], replacement: str | None, *, junit_in_repo: str
) -> str:
    lines = [
        "set -eu",
        "uv venv .venv",
        "uv pip install --python .venv/bin/python pytest",
        'if [ -f requirements.txt ]; then uv pip install --python .venv/bin/python -r requirements.txt; fi',
        'if [ -f requirements-dev.txt ]; then uv pip install --python .venv/bin/python -r requirements-dev.txt; fi',
        'if [ ! -f requirements.txt ] && [ -f pyproject.toml ]; then uv pip install --python .venv/bin/python -e .; '
        'elif [ -f requirements.txt ] && { [ -f pyproject.toml ] || [ -f setup.py ]; }; '
        'then uv pip install --python .venv/bin/python --no-deps -e .; fi',
    ]
    if replacement:
        lines.append(f"uv pip install --python .venv/bin/python {shlex.quote(replacement)}")
    args = _without_junitxml(command)
    report_argument = "--junitxml=" + Path(junit_in_repo).name
    lines.append(f"export PYTEST_ADDOPTS={shlex.quote(report_argument)}")
    lines.extend(["export PATH=\"/workspace/.venv/bin:$PATH\"", " ".join(shlex.quote(arg) for arg in args)])
    return "\n".join(lines)


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
    result: list[str] = []
    skip_next = False
    for option in options:
        if skip_next:
            skip_next = False
        elif option == "--junitxml":
            skip_next = True
        elif not option.startswith("--junitxml="):
            result.append(option)
    result.append(report_argument)
    return shlex.join(result)


def _save_run(
    side: str,
    command: tuple[str, ...],
    exit_code: int | None,
    duration: float,
    junit_source: Path,
    evidence: Path,
    output: str,
    timed_out: bool,
) -> TestRun:
    log_path = evidence / f"{side}.log"
    xml_path = evidence / f"{side}.junit.xml"
    log_path.write_text(output, encoding="utf-8")
    if junit_source.is_file():
        shutil.copy2(junit_source, xml_path)
    else:
        xml_path.unlink(missing_ok=True)
    return TestRun(
        side=side,
        command=command,
        exit_code=exit_code,
        duration_seconds=round(duration, 3),
        junit_xml=str(xml_path),
        log=str(log_path),
        timed_out=timed_out,
    )


def _format_command(command: Sequence[str], stdout: str, stderr: str) -> str:
    pieces = [f"$ {shlex.join([str(part) for part in command])}\n"]
    if stdout:
        pieces.append(stdout)
    if stderr:
        pieces.append("\n[stderr]\n" + stderr)
    if not pieces[-1].endswith("\n"):
        pieces.append("\n")
    return "".join(pieces)


def write_trial_manifest(result: TwinTrialResult, path: str | Path) -> Path:
    """Write the twin run metadata as a small JSON evidence file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return destination
