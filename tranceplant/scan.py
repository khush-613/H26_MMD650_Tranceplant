"""Stage 1: Scan. Owner: Khush.

Read requirements*.txt and pyproject.toml, spot pins that disagree, and fetch
risk data (PyPI JSON, OSV.dev, deps.dev). Facts here are OBSERVED; the raw API
responses go in DependencyRisk.raw_sources (written to evidence/risk-sources.json).
"""

import json
import os
import tomllib
from pathlib import Path

import httpx
from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .models import Declaration, ScanReport

TARGET_PYTHON = "3.12.3"  # what the sandboxes run; markers are evaluated against it


def scan_repo(repo: Path) -> ScanReport:
    raise NotImplementedError("scan.scan_repo (owner: Khush)")


def read_declarations(repo: Path) -> dict[str, list[Declaration]]:
    """Every place each dependency is declared, keyed by canonical name.

    requirements files come first, so their exact pins win over pyproject ranges.
    """
    found: dict[str, list[Declaration]] = {}
    req_files = sorted(repo.glob("requirements*.txt")) + sorted(repo.glob("requirements/*.txt"))
    for path in req_files:
        rel = path.relative_to(repo).as_posix()
        for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            spec = raw.split(" #", 1)[0].strip()
            if spec and not spec.startswith(("#", "-")):
                _add(found, rel, n, spec)
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        lines = text.splitlines()
        used: set[int] = set()
        for spec in _pyproject_specs(tomllib.loads(text)):
            _add(found, "pyproject.toml", _line_of(spec, lines, used), spec)
    return found


def pinned_declaration(decls: list[Declaration]) -> tuple[Declaration, str | None]:
    """The declaration that decides the installed version, and its exact pin if it has one."""
    for decl in decls:
        specs = list(Requirement(decl.spec).specifier)
        if len(specs) == 1 and specs[0].operator in ("==", "===") and "*" not in specs[0].version:
            return decl, specs[0].version
    return decls[0], None


def target_environment() -> dict[str, str]:
    env = default_environment()
    env.update(
        python_version=TARGET_PYTHON.rsplit(".", 1)[0],
        python_full_version=TARGET_PYTHON,
        implementation_name="cpython",
        platform_python_implementation="CPython",
        os_name="posix",
        sys_platform="linux",
        platform_system="Linux",
        extra="",
    )
    return env


def pypi_json(name: str, version: str | None = None) -> dict:
    """PyPI JSON API response, cached on disk so re-runs work offline."""
    cache = Path(os.environ.get("TRANCEPLANT_HOME", ".tranceplant")) / "cache" / "pypi"
    path = cache / f"{canonicalize_name(name)}-{version or 'latest'}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    url = f"https://pypi.org/pypi/{name}/{version}/json" if version else f"https://pypi.org/pypi/{name}/json"
    resp = httpx.get(url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _add(found: dict[str, list[Declaration]], file: str, line: int, spec: str) -> None:
    try:
        name = canonicalize_name(Requirement(spec).name)
    except InvalidRequirement:
        return
    found.setdefault(name, []).append(Declaration(file=file, spec=spec, line=line))


def _pyproject_specs(data: dict) -> list[str]:
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        specs += extra
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    specs += data.get("build-system", {}).get("requires", [])
    return specs


def _line_of(spec: str, lines: list[str], used: set[int]) -> int | None:
    for i, line in enumerate(lines):
        if i not in used and (f'"{spec}"' in line or f"'{spec}'" in line):
            used.add(i)
            return i + 1
    return None
