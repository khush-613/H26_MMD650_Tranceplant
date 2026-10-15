"""Stage 2: Usage X-Ray. Owner: Khush.

Walk the repo's syntax tree (ast) and find every use of one dependency:
imports, aliases, attribute chains, private APIs, monkeypatches, and hidden
partner libraries. Gemma's one-line notes are added later by propose.annotate_usages.

Names are resolved through the imports, so `S()` after
`from requests import Session as S` is reported as `requests.Session`, which a
text search for "requests" misses. Makefiles, Dockerfiles and CI files are
searched too, because a test runner like nose is run, never imported.
X-Ray only parses the target; it never runs its code.
"""

import ast
import io
import logging
import os
import re
import sys
import tokenize
import warnings
from collections.abc import Iterator
from pathlib import Path

import httpx
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import scan
from .models import Usage, UsageKind

log = logging.getLogger(__name__)

SKIP_DIRS = {
    ".git", ".hg", ".venv", "venv", "env", ".tox", ".nox", "__pycache__",
    "build", "dist", "node_modules", "site-packages", ".mypy_cache", ".pytest_cache",
}

# PyPI projects whose top-level module is not simply the project name.
IMPORT_NAMES = {
    "setuptools": ["setuptools", "pkg_resources", "distutils", "_distutils_hack"],
    "python-dateutil": ["dateutil"],
    "pyyaml": ["yaml"],
    "beautifulsoup4": ["bs4"],
    "pillow": ["PIL"],
    "scikit-learn": ["sklearn"],
    "opencv-python": ["cv2"],
    "opencv-python-headless": ["cv2"],
    "attrs": ["attr", "attrs"],
    "protobuf": ["google.protobuf"],
    "python-dotenv": ["dotenv"],
    "pyjwt": ["jwt"],
    "pycryptodome": ["Crypto"],
    "pyserial": ["serial"],
    "msgpack-python": ["msgpack"],
    "pytest": ["pytest", "_pytest"],
    "google-genai": ["google.genai"],
}

COMMAND_FILES = {
    "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
    "Justfile", "justfile", "Procfile", "tox.ini", ".gitlab-ci.yml",
}
TOOL_SUFFIXES = ("tests", "test")  # nose installs a `nosetests` command
_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*")
_TOOL_SECTION = re.compile(r"^\[+\s*tool\.([A-Za-z0-9_\-]+)")
_BUILD_BACKEND = re.compile(r"""^build-backend\s*=\s*["']([A-Za-z0-9_]+)""")


def find_usages(repo: Path, dep: str) -> list[Usage]:
    repo = Path(repo)
    names = import_names(dep)
    found: set[tuple] = set()
    usages: list[Usage] = []
    for usage in [*_code_usages(repo, names), *_config_usages(repo, dep, names), *_partner_usages(repo, dep)]:
        key = (usage.file, usage.line, usage.kind, usage.symbol)
        if key not in found:
            found.add(key)
            usages.append(usage)
    return sorted(usages, key=lambda u: (u.file, u.line, u.symbol))


def import_names(dep: str) -> list[str]:
    name = canonicalize_name(dep)
    return IMPORT_NAMES.get(name, [name.replace("-", "_")])


def imported_modules(repo: Path) -> dict[str, list[Usage]]:
    """Import statements for every third-party top-level module, keyed by module name.

    Leaves out the standard library and the repo's own packages. Scan uses this
    to catch packages imported but never declared (certifi) and declared but
    never imported (six).
    """
    repo = Path(repo)
    local = set()
    modules: dict[str, list[Usage]] = {}
    for rel, tree, lines in _python_files(repo):
        parts = rel.removesuffix(".py").split("/")
        local.add(parts[1] if parts[0] == "src" and len(parts) > 1 else parts[0])
        for node, kind, symbol in _Finder(tree).hits:
            if kind in ("import", "from_import", "alias"):
                top = symbol.partition(".")[0]
                modules.setdefault(top, []).append(_usage(rel, lines, node.lineno, kind, symbol))
    return {
        top: hits for top, hits in sorted(modules.items())
        if top not in sys.stdlib_module_names and top not in local and top != "__future__"
    }


class _Finder(ast.NodeVisitor):
    """Collects (node, kind, symbol) for every name that resolves through an import."""

    def __init__(self, tree: ast.Module):
        self.bound = _bindings(tree)
        self.hits: list[tuple[ast.AST, UsageKind, str]] = []
        self.visit(tree)

    def resolve(self, node: ast.AST) -> str | None:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name) and node.id in self.bound:
            return ".".join([self.bound[node.id], *reversed(parts)])
        return None

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.hits.append((node, "alias" if a.asname else "import", a.name))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            return
        for a in node.names:
            symbol = node.module if a.name == "*" else f"{node.module}.{a.name}"
            kind = "alias" if a.asname and a.asname != a.name else "from_import"
            self.hits.append((node, kind, symbol))

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and (symbol := self.resolve(node)):
            self.hits.append((node, "attribute", symbol))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        symbol = self.resolve(node)
        if symbol is None:
            self.generic_visit(node)
        else:
            # Assigning to or deleting a library attribute rewrites it for everyone.
            kind = "attribute" if isinstance(node.ctx, ast.Load) else "monkeypatch"
            self.hits.append((node, kind, symbol))

    def visit_Call(self, node: ast.Call) -> None:
        args = node.args
        if target := self._patch_target(node):
            self.hits.append((node, "monkeypatch", target))
            args = args[1:]
        elif module := self._dynamic_import(node):
            self.hits.append((node, "import", module))
        if func := self.resolve(node.func):
            self.hits.append((node.func, "attribute", func))
        else:
            self.visit(node.func)
        for arg in args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)

    def _patch_target(self, node: ast.Call) -> str | None:
        """setattr(requests.Session, "request", f), monkeypatch.setattr(...), mock.patch("requests.get")."""
        parts = (_dotted(node.func) or "").split(".")
        if not node.args or (parts[-1] not in ("setattr", "delattr", "patch") and parts[-2:] != ["patch", "object"]):
            return None
        first, rest = node.args[0], node.args[1:]
        if _is_str(first):
            return first.value
        target = self.resolve(first)
        if target and rest and _is_str(rest[0]):
            target += "." + rest[0].value
        return target

    def _dynamic_import(self, node: ast.Call) -> str | None:
        func = self.resolve(node.func) or _dotted(node.func)
        if func in ("importlib.import_module", "__import__") and node.args and _is_str(node.args[0]):
            return node.args[0].value
        return None


def _bindings(tree: ast.Module) -> dict[str, str]:
    """Map each local name an import creates to the dotted name it stands for."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    bound[a.asname] = a.name
                else:
                    top = a.name.partition(".")[0]
                    bound[top] = top
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for a in node.names:
                if a.name != "*":
                    bound[a.asname or a.name] = f"{node.module}.{a.name}"
    return bound


def _code_usages(repo: Path, names: list[str]) -> Iterator[Usage]:
    for rel, tree, lines in _python_files(repo):
        for node, kind, symbol in _Finder(tree).hits:
            if not any(symbol == n or symbol.startswith(n + ".") for n in names):
                continue
            if kind != "monkeypatch" and _is_private(symbol):
                kind = "private_api"
            yield _usage(rel, lines, node.lineno, kind, symbol)


def _config_usages(repo: Path, dep: str, names: list[str]) -> Iterator[Usage]:
    """Commands that run the dependency, and pyproject sections that configure it."""
    wanted = {canonicalize_name(dep)} | {canonicalize_name(n) for n in names}
    for path in _walk(repo):
        rel = path.relative_to(repo).as_posix()
        if rel == "pyproject.toml":
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for n, line in enumerate(lines, 1):
                m = _TOOL_SECTION.match(line.strip()) or _BUILD_BACKEND.match(line.strip())
                if m and canonicalize_name(m.group(1)) in wanted:
                    yield _usage(rel, lines, n, "config", m.group(1))
        elif _is_command_file(rel):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for n, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                for token in _TOKEN.findall(line):
                    if _runs(canonicalize_name(token), wanted):
                        yield _usage(rel, lines, n, "config", token)


def _partner_usages(repo: Path, dep: str) -> Iterator[Usage]:
    """Libraries the pinned version pulls in, e.g. Jinja2 2.11.3 needs MarkupSafe."""
    decls = scan.read_declarations(repo).get(canonicalize_name(dep))
    if not decls:
        return
    decl, version = scan.pinned_declaration(decls)
    try:
        requires = scan.pypi_json(dep, version)["info"].get("requires_dist") or []
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        log.warning("no PyPI metadata for %s %s (%s); partner libraries not listed", dep, version or "latest", exc)
        return
    env = scan.target_environment()
    for raw in requires:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if req.marker is None or req.marker.evaluate(env):
            yield Usage(file=decl.file, line=decl.line or 1, kind="transitive",
                        symbol=f"{req.name}{req.specifier}", code=decl.spec)


def _python_files(repo: Path) -> Iterator[tuple[str, ast.Module, list[str]]]:
    for path in _walk(repo):
        if path.suffix != ".py":
            continue
        rel = path.relative_to(repo).as_posix()
        source = path.read_bytes()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)  # e.g. invalid escapes in the target
                tree = ast.parse(source, filename=rel)
            encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            log.warning("skipping %s: %s", rel, exc)
            continue
        text = source.decode(encoding, errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        yield rel, tree, text.split("\n")


def _walk(repo: Path) -> Iterator[Path]:
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(files):
            yield Path(root) / name


def _usage(rel: str, lines: list[str], line: int, kind: UsageKind, symbol: str) -> Usage:
    code = lines[line - 1].strip() if 0 < line <= len(lines) else ""
    return Usage(file=rel, line=line, kind=kind, symbol=symbol, code=code)


def _is_command_file(rel: str) -> bool:
    name = rel.rpartition("/")[2]
    return (
        name in COMMAND_FILES
        or name.startswith(("Dockerfile.", "Containerfile."))
        or name.endswith(".sh")
        or (rel.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")))
    )


def _runs(token: str, wanted: set[str]) -> bool:
    return any(token == n or token.startswith(n + "-") or token in {n + s for s in TOOL_SUFFIXES} for n in wanted)


def _is_private(symbol: str) -> bool:
    return any(p.startswith("_") and not (p.startswith("__") and p.endswith("__")) for p in symbol.split("."))


def _is_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    return None


if __name__ == "__main__":
    for u in find_usages(Path(sys.argv[1]), sys.argv[2]):
        print(f"{u.file}:{u.line:<4} {u.kind:<12} {u.symbol:<45} {u.code}")
