"""Test doubles.

ScriptedBackend replays canned Gemma replies. The stand-in scan/xray/capsule
stages below follow the stage contracts in cli.py so the wiring can be tested
before Khush's modules are merged; sandbox and compare are Rajveer's real ones.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

from packaging.utils import canonicalize_name

from tranceplant import compare, sandbox
from tranceplant.models import (
    Declaration,
    Dependency,
    DependencyRisk,
    RiskIndicators,
    ScanReport,
    Usage,
)

LINKWATCH = Path(__file__).parent / "fixtures" / "linkwatch"


class ScriptedBackend:
    """Returns queued replies in order. An Exception in the queue is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def generate(self, model, messages, temperature, max_output_tokens):
        self.calls.append({"model": model, "messages": list(messages), "temperature": temperature,
                           "max_output_tokens": max_output_tokens})
        if not self.replies:
            raise AssertionError("ScriptedBackend ran out of replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


class HttpError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


def copy_repo(dest: Path, git: bool = True) -> Path:
    shutil.copytree(LINKWATCH, dest)
    if git:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "linkwatch"]):
            subprocess.run(cmd, cwd=dest, check=True, env=env)
    return dest


def install_stages(monkeypatch, **modules: dict) -> None:
    """Register fake tranceplant.<stage> modules, e.g. install_stages(mp, scan={"scan_repo": fn})."""
    import tranceplant

    for name, attrs in modules.items():
        mod = types.ModuleType(f"tranceplant.{name}")
        for key, value in attrs.items():
            setattr(mod, key, value)
        monkeypatch.setitem(sys.modules, f"tranceplant.{name}", mod)
        monkeypatch.setattr(tranceplant, name, mod, raising=False)


# --- Stand-in stages for LinkWatch -------------------------------------------

REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==\s*([^\s;#]+))?")


def scan_repo(repo: Path) -> ScanReport:
    repo = Path(repo)
    deps: dict[str, Dependency] = {}
    for fname in ("requirements.txt", "requirements-dev.txt"):
        path = repo / fname
        if not path.is_file():
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = REQ_RE.match(line)
            name = canonicalize_name(m.group(1))
            dep = deps.setdefault(name, Dependency(name=name, pinned=m.group(2)))
            dep.declared_in.append(Declaration(file=fname, spec=line.strip(), line=n))
    scores = {"jinja2": 90, "nose": 85, "toml": 60, "appdirs": 55, "pydantic": 50}
    return ScanReport(
        repo=str(repo),
        python_requires=">=3.10",
        dependencies=[DependencyRisk(dependency=d, indicators=RiskIndicators(), score=scores.get(n, 10))
                      for n, d in deps.items()],
    )


def find_usages(repo: Path, dep: str) -> list[Usage]:
    module = {"appdirs": "appdirs", "toml": "toml", "jinja2": "jinja2", "requests": "requests"}.get(dep, dep)
    out = []
    for path in sorted(Path(repo).rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        source = path.read_text()
        lines = source.splitlines()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == module:
                        out.append(Usage(file=rel, line=node.lineno, kind="alias" if a.asname else "import",
                                         symbol=a.name, code=lines[node.lineno - 1]))
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == module:
                for a in node.names:
                    kind = "private_api" if a.name.startswith("_") else "alias" if a.asname else "from_import"
                    out.append(Usage(file=rel, line=node.lineno, kind=kind, symbol=f"{node.module}.{a.name}",
                                     code=lines[node.lineno - 1]))
    return out


def write_capsule(capsule, capsule_dir: Path) -> Path:
    path = Path(capsule_dir) / "capsule.json"
    path.write_text(capsule.model_dump_json(indent=1))
    return path


STAGES = {
    "scan": {"scan_repo": scan_repo},
    "xray": {"find_usages": find_usages},
    "sandbox": {"run_side": sandbox.run_side},
    "compare": {"compare": compare.compare, "decide_verdict": compare.decide_verdict},
    "capsule": {"write_capsule": write_capsule},
}
