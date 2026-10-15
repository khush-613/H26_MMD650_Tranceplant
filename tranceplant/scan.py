"""Stage 1: Scan. Owner: Khush.

Read requirements*.txt and pyproject.toml, spot pins that disagree, and fetch
risk data (PyPI JSON, OSV.dev, deps.dev). Facts here are OBSERVED; the raw API
responses go in DependencyRisk.raw_sources (written to evidence/risk-sources.json).

The score is a sum of fixed points per finding, capped at 100. Each finding and
its points are listed in raw_sources["score"], so every number can be traced.
Every response is cached on disk, so a re-run needs no network.
"""

import ast
import io
import json
import os
import re
import threading
import tomllib
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .models import Claim, Declaration, Dependency, DependencyRisk, RiskIndicators, ScanReport, Vulnerability

TARGET_PYTHON = "3.12.3"  # what the sandboxes run; markers are evaluated against it
EVIDENCE = "evidence/risk-sources.json"  # cli.py writes raw_sources there, inside the capsule folder

# Standard-library modules removed by each Python version. distutils (gone in
# 3.12) is left out on purpose: setuptools ships a replacement for it.
REMOVED_MODULES = {
    "3.8": ["macpath"],
    "3.10": ["formatter", "parser", "symbol"],
    "3.11": ["binhex"],
    "3.12": ["asynchat", "asyncore", "imp", "smtpd"],
    "3.13": ["aifc", "audioop", "cgi", "cgitb", "chunk", "crypt", "imghdr", "lib2to3", "mailcap", "msilib",
             "nis", "nntplib", "ossaudiodev", "pipes", "sndhdr", "spwd", "sunau", "telnetlib", "uu", "xdrlib"],
}
MAX_WHEEL_BYTES = 3_000_000
SEVERITY_POINTS = {"critical": 25, "high": 18, "medium": 10, "low": 4}
GHSA_SEVERITY = {"LOW": "low", "MODERATE": "medium", "HIGH": "high", "CRITICAL": "critical"}

_http_lock = threading.Lock()
_http_client: httpx.Client | None = None


def scan_repo(repo: Path) -> ScanReport:
    from . import xray  # xray imports this module

    repo = Path(repo)
    declarations = read_declarations(repo)
    provided = {n.partition(".")[0] for name in declarations for n in xray.import_names(name)}
    undeclared = {xray.distribution_for(m): hits for m, hits in xray.imported_modules(repo).items() if m not in provided}
    names = sorted(declarations) + sorted(n for n in undeclared if n not in declarations)
    with ThreadPoolExecutor(max_workers=8) as pool:
        risks = list(pool.map(lambda n: _assess(repo, n, declarations.get(n, []), undeclared.get(n, [])), names))

    conflicts = [c for r in risks for c in r.indicators.findings if r.raw_sources.get("conflict") == c.claim]
    for risk in risks:
        if risk.dependency.imported_but_undeclared:
            _note_who_requires(risk, risks)
    risks.sort(key=lambda r: (-r.score, r.dependency.name))
    return ScanReport(repo=str(repo), python_requires=_requires_python(repo), dependencies=risks, conflicts=conflicts)


def _assess(repo: Path, name: str, decls: list[Declaration], imports: list) -> DependencyRisk:
    from . import xray

    pinned = pinned_declaration(decls)[1] if decls else None
    errors: dict[str, str] = {}

    def fetch(source, fn, *args):
        try:
            return fn(*args)
        except (httpx.HTTPError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            errors[source] = f"{type(exc).__name__}: {exc}"
            return None

    versions = fetch("deps.dev package", depsdev_package, name) or {}
    latest, latest_date = _latest(versions)
    if latest is None and not pinned:
        latest = ((fetch("pypi latest", pypi_json, name) or {}).get("info") or {}).get("version")
    version = pinned or latest
    pypi = fetch("pypi", pypi_json, name, version) if version else None
    dd_version = fetch("deps.dev version", depsdev_version, name, version) if version else None
    osv = fetch("osv", osv_query, name, version) if version else None
    repo_id = _source_repo(dd_version)
    project = fetch("deps.dev project", depsdev_project, repo_id) if repo_id else None
    removed = fetch("wheel", removed_imports, name, version, pypi) if pypi else None
    usages = [u for u in xray.find_usages(repo, name) if u.kind != "transitive"] if decls else []

    info = (pypi or {}).get("info", {})
    vulns = _vulnerabilities(name, osv)
    scorecard = (project or {}).get("scorecard") or {}
    licence = _licence(info, dd_version)
    deprecated = _deprecated(info, dd_version, versions, version) if (pypi or dd_version) else None

    scored: list[tuple[Claim, int]] = []

    def add(points: int, text: str):
        scored.append((Claim.observed(text, EVIDENCE), points))

    label = f"{name} {version}" if version else name
    for hit in removed or []:
        when = "so importing it fails" if hit["module_level"] else "so that code fails when it runs"
        add(40 if hit["module_level"] else 20, f"{label} imports `{hit['module']}` ({hit['file']}:{hit['line']}), "
                                               f"which Python {hit['removed_in']} removed, {when} on Python {_short(TARGET_PYTHON)}")
    if info.get("requires_python") and not _allows_target(info["requires_python"]):
        add(40, f"{label} declares Requires-Python '{info['requires_python']}', which excludes Python {_short(TARGET_PYTHON)}")
    if vulns:
        points = min(50, sum(SEVERITY_POINTS.get(v.severity or "", 8) for v in vulns))
        listed = ", ".join(f"{v.id} ({v.severity or 'severity unknown'})" for v in vulns)
        fixes = [f for v in vulns for f in v.fixed_in]
        fixed = f"; all are fixed by {max(fixes, key=_version_key)}" if fixes and all(v.fixed_in for v in vulns) else ""
        add(points, f"{len(vulns)} known {'vulnerability affects' if len(vulns) == 1 else 'vulnerabilities affect'} "
                    f"{label} (OSV.dev): {listed}{fixed}")
    if latest_date:
        years = _years_since(latest_date)
        if years >= 3:
            add(30 if years >= 5 else 20, f"No release of {name} since {latest} on {latest_date} ({years:.0f} years ago)")
    maintained = next((c for c in scorecard.get("checks", []) if c.get("name") == "Maintained"), None)
    if maintained and maintained.get("score") == 0:
        reason = maintained.get("reason", "").split(" -- ")[0]
        add(10, f"OpenSSF Scorecard 'Maintained' check for {repo_id} is 0/10: {reason}")
    if scorecard.get("overallScore") is not None and scorecard["overallScore"] < 4:
        add(5, f"OpenSSF Scorecard for {repo_id} is {scorecard['overallScore']}/10")
    if pinned and latest and pinned != latest:
        pinned_date = _published(versions, pinned)
        if _major(pinned) is not None and _major(latest) is not None and _major(pinned) < _major(latest):
            add(15, f"Pinned {pinned}{_on(pinned_date)} is a major version behind the latest {latest}{_on(latest_date)}")
        elif pinned_date and _years_since(pinned_date) >= 3:
            add(10, f"Pinned {pinned} is from {pinned_date}; the latest is {latest}{_on(latest_date)}")
    if deprecated:
        add(20, f"{name} is marked deprecated or inactive: {deprecated}")
    if licence and re.search(r"\b(A?GPL|LGPL|GNU)", licence):
        add(10, f"{name} is under a copyleft licence ({licence})")
    conflict = _conflict(name, decls, pinned)
    if conflict:
        add(20, conflict)
    if imports and not decls:
        where = ", ".join(sorted({f"{u.file}:{u.line}" for u in imports}))
        add(15, f"{name} is imported ({where}) but not declared in any requirements file or pyproject.toml")
    if decls and not usages:
        where = ", ".join(f"{d.file}:{d.line}" if d.line else d.file for d in decls)
        add(5, f"{name} is declared ({where}) but never imported or run anywhere in the repo")
    for kind, text, points in (("private_api", "calls private", 10), ("monkeypatch", "rewrites", 10)):
        hits = [u for u in usages if u.kind == kind]
        for symbol in sorted({u.symbol for u in hits}):
            where = ", ".join(f"{u.file}:{u.line}" for u in hits if u.symbol == symbol)
            add(points, f"The repo {text} `{symbol}` ({where})")
    for text in _loose_partners(name, version, versions, info):
        add(15, text)

    scored.sort(key=lambda s: -s[1])
    pinned_date = _published(versions, pinned) if pinned else None
    return DependencyRisk(
        dependency=Dependency(name=name, pinned=pinned, declared_in=decls, imported_but_undeclared=bool(imports and not decls)),
        indicators=RiskIndicators(
            latest_version=latest,
            last_release=latest_date,
            license=licence,
            vulnerabilities=vulns,
            scorecard=scorecard.get("overallScore"),
            deprecated=bool(deprecated) if deprecated is not None else None,
            findings=[c for c, _ in scored],
        ),
        score=min(100, sum(p for _, p in scored)),
        raw_sources={
            "assessed_version": version,
            "pinned_release": pinned_date.isoformat() if pinned_date else None,
            "declarations": [d.model_dump() for d in decls],
            "repo_usages": [u.model_dump(exclude={"note"}) for u in usages or imports],
            "score": [{"finding": c.claim, "points": p} for c, p in scored],
            "conflict": conflict,
            "removed_imports": removed,
            "pypi": pypi,
            "osv": osv,
            "deps.dev": {"version": dd_version, "project": project,
                         "releases": {v: versions.get(v) for v in {pinned, latest} if v}},
            "errors": errors,
        },
    )


def _note_who_requires(risk: DependencyRisk, risks: list[DependencyRisk]) -> None:
    """An undeclared import usually installs only because a declared package requires it."""
    env = target_environment()
    name = risk.dependency.name
    for other in risks:
        for raw in ((other.raw_sources.get("pypi") or {}).get("info") or {}).get("requires_dist") or []:
            try:
                req = Requirement(raw)
            except InvalidRequirement:
                continue
            if canonicalize_name(req.name) == name and (req.marker is None or req.marker.evaluate(env)):
                version = other.raw_sources.get("assessed_version")
                for i, claim in enumerate(risk.indicators.findings):
                    if claim.claim.startswith(f"{name} is imported ("):
                        text = f"{claim.claim}; it installs only because {other.dependency.name} {version} requires {req}"
                        risk.indicators.findings[i] = claim.model_copy(update={"claim": text})
                        risk.raw_sources["score"][i]["finding"] = text
                return


# --- Declarations -----------------------------------------------------------


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
        python_version=_short(TARGET_PYTHON),
        python_full_version=TARGET_PYTHON,
        implementation_name="cpython",
        platform_python_implementation="CPython",
        os_name="posix",
        sys_platform="linux",
        platform_system="Linux",
        extra="",
    )
    return env


def _add(found: dict[str, list[Declaration]], file: str, line: int | None, spec: str) -> None:
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


def _requires_python(repo: Path) -> str | None:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return None
    return tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("requires-python")


def _conflict(name: str, decls: list[Declaration], pinned: str | None) -> str | None:
    if not pinned:
        return None
    pin, _ = pinned_declaration(decls)
    for decl in decls:
        spec = Requirement(decl.spec).specifier
        if not spec.contains(pinned, prereleases=True):
            return (f"{pin.file}:{pin.line} pins {name}=={pinned}, which {decl.file}"
                    f"{':' + str(decl.line) if decl.line else ''} (`{decl.spec}`) does not allow")
    return None


# --- Fetching (all cached on disk) ------------------------------------------


def pypi_json(name: str, version: str | None = None) -> dict:
    """PyPI JSON API response for one release, without the long description."""
    def get():
        url = f"https://pypi.org/pypi/{name}/{version}/json" if version else f"https://pypi.org/pypi/{name}/json"
        data = _get(url)
        data["info"].pop("description", None)
        data.pop("releases", None)
        data["urls"] = [{k: f.get(k) for k in ("filename", "packagetype", "size", "url", "upload_time_iso_8601")}
                        for f in data.get("urls", [])]
        return data

    return _cached("pypi", f"{canonicalize_name(name)}-{version or 'latest'}", get)


def depsdev_package(name: str) -> dict[str, dict]:
    """Every release of a package: {version: {"published": ..., "default": ..., "deprecated": ...}}."""
    def get():
        data = _get(f"https://api.deps.dev/v3/systems/pypi/packages/{quote(canonicalize_name(name))}")
        return {v["versionKey"]["version"]: {"published": v.get("publishedAt"), "default": v.get("isDefault", False),
                                             "deprecated": v.get("isDeprecated", False)}
                for v in data.get("versions", [])}

    return _cached("deps.dev-package", canonicalize_name(name), get)


def depsdev_version(name: str, version: str) -> dict:
    url = f"https://api.deps.dev/v3/systems/pypi/packages/{quote(canonicalize_name(name))}/versions/{quote(version)}"
    return _cached("deps.dev-version", f"{canonicalize_name(name)}-{version}", lambda: _get(url))


def depsdev_project(project_id: str) -> dict:
    url = f"https://api.deps.dev/v3/projects/{quote(project_id, safe='')}"
    return _cached("deps.dev-project", project_id.replace("/", "_"), lambda: _get(url))


def osv_query(name: str, version: str) -> dict:
    body = {"package": {"name": canonicalize_name(name), "ecosystem": "PyPI"}, "version": version}
    return _cached("osv", f"{canonicalize_name(name)}-{version}",
                   lambda: _get("https://api.osv.dev/v1/query", body))


def latest_version(name: str) -> str | None:
    return _latest(depsdev_package(name))[0]


def removed_imports(name: str, version: str, pypi: dict) -> list[dict]:
    """Imports of standard-library modules the target Python no longer has, read from the release's wheel.

    The wheel is read in memory and only parsed, never installed or run.
    Imports under a try or an if are skipped: they usually have a fallback.
    """
    wheel = next((f for f in pypi.get("urls", []) if f["filename"].endswith("-none-any.whl")
                  and (f.get("size") or 0) <= MAX_WHEEL_BYTES), None)
    if wheel is None:
        return []

    def scan_wheel():
        removed = {m: v for v, mods in REMOVED_MODULES.items() if Version(v) <= Version(_short(TARGET_PYTHON)) for m in mods}
        hits = []
        with zipfile.ZipFile(io.BytesIO(_get_bytes(wheel["url"]))) as zf:
            for member in sorted(zf.namelist()):
                if not member.endswith(".py"):
                    continue
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", SyntaxWarning)
                        tree = ast.parse(zf.read(member))
                except (SyntaxError, ValueError):
                    continue
                for node, module_level in _unguarded_imports(tree):
                    modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for module in modules:
                        top = module.partition(".")[0]
                        if top in removed and not getattr(node, "level", 0):
                            hits.append({"file": member, "line": node.lineno, "module": top,
                                         "removed_in": removed[top], "module_level": module_level})
        return hits

    return _cached("removed-imports", f"{canonicalize_name(name)}-{version}-py{_short(TARGET_PYTHON)}", scan_wheel)


def _unguarded_imports(tree: ast.AST, module_level: bool = True):
    """(import node, is it at module level) for every import not under a try or an if."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node, module_level
        elif not isinstance(node, (ast.Try, ast.TryStar, ast.If)):
            nested = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            yield from _unguarded_imports(node, module_level and not nested)


def _http() -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(timeout=20, follow_redirects=True, transport=httpx.HTTPTransport(retries=2),
                                        headers={"User-Agent": "tranceplant (dependency risk scan)"})
        return _http_client


def _get(url: str, body: dict | None = None) -> dict:
    resp = _http().post(url, json=body) if body is not None else _http().get(url)
    resp.raise_for_status()
    return resp.json()


def _get_bytes(url: str) -> bytes:
    resp = _http().get(url)
    resp.raise_for_status()
    return resp.content


def _cached(kind: str, key: str, fetch):
    path = Path(os.environ.get("TRANCEPLANT_HOME", ".tranceplant")) / "cache" / kind / f"{key}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return data


# --- Reading the responses --------------------------------------------------


def _latest(versions: dict[str, dict]) -> tuple[str | None, date | None]:
    for v, meta in versions.items():
        if meta.get("default"):
            return v, _date(meta.get("published"))
    return None, None


def _published(versions: dict[str, dict], version: str | None) -> date | None:
    return _date((versions.get(version) or {}).get("published")) if version else None


def _source_repo(dd_version: dict | None) -> str | None:
    for rel in (dd_version or {}).get("relatedProjects", []):
        if rel.get("relationType") == "SOURCE_REPO":
            return rel["projectKey"]["id"]
    return None


def _vulnerabilities(name: str, osv: dict | None) -> list[Vulnerability]:
    """OSV lists the same issue under several ids (GHSA, PYSEC); merge entries that share an alias."""
    groups: list[tuple[set[str], list[dict]]] = []
    for entry in (osv or {}).get("vulns", []):
        ids = {entry["id"], *entry.get("aliases", [])}
        matches = [g for g in groups if g[0] & ids]
        merged = (ids.union(*(g[0] for g in matches)), [e for g in matches for e in g[1]] + [entry])
        groups = [g for g in groups if g not in matches] + [merged]
    vulns = []
    for ids, entries in groups:
        severity = next((GHSA_SEVERITY.get(e.get("database_specific", {}).get("severity", "")) for e in entries
                         if e.get("database_specific", {}).get("severity")), None)
        fixed = sorted({ev["fixed"] for e in entries for a in e.get("affected", [])
                        if canonicalize_name(a.get("package", {}).get("name", "")) == canonicalize_name(name)
                        for r in a.get("ranges", []) for ev in r.get("events", []) if "fixed" in ev}, key=_version_key)
        cve = sorted(i for i in ids if i.startswith("CVE-"))
        summary = next((e.get("summary") for e in entries if e.get("summary")), "")
        vulns.append(Vulnerability(id=cve[0] if cve else sorted(ids)[0], summary=summary, severity=severity, fixed_in=fixed))
    return sorted(vulns, key=lambda v: v.id, reverse=True)


def _licence(info: dict, dd_version: dict | None) -> str | None:
    if info.get("license_expression"):
        return info["license_expression"]
    text = (info.get("license") or "").strip()
    if text and len(text) <= 60 and "\n" not in text:
        return text
    classifiers = [c.rsplit(" :: ", 1)[-1] for c in info.get("classifiers", []) if c.startswith("License ::")]
    if classifiers:
        return classifiers[0]
    known = [x for x in (dd_version or {}).get("licenses", []) if x != "non-standard"]
    return known[0] if known else None


def _deprecated(info: dict, dd_version: dict | None, versions: dict, version: str | None) -> str:
    reasons = []
    if (dd_version or {}).get("isDeprecated"):
        reasons.append(f"deps.dev marks {version} deprecated ({dd_version.get('deprecatedReason') or 'no reason given'})")
    status = ((dd_version or {}).get("projectStatus") or {}).get("status")
    if status and status != "active":
        reasons.append(f"deps.dev project status is '{status}'")
    if info.get("yanked"):
        reasons.append(f"PyPI release {version} is yanked ({info.get('yanked_reason') or 'no reason given'})")
    if "Development Status :: 7 - Inactive" in info.get("classifiers", []):
        reasons.append("PyPI classifier 'Development Status :: 7 - Inactive'")
    return "; ".join(reasons)


def _loose_partners(name: str, version: str | None, versions: dict, info: dict) -> list[str]:
    """Partner libraries with no upper bound that have since moved on by a major version."""
    released = _published(versions, version)
    if not released or (versions.get(version) or {}).get("default"):
        return []
    env = target_environment()
    out = []
    for raw in info.get("requires_dist") or []:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if req.marker is not None and not req.marker.evaluate(env):
            continue
        if any(s.operator in ("<", "<=", "==", "~=", "===") for s in req.specifier):
            continue
        try:
            partner_versions = depsdev_package(req.name)
        except (httpx.HTTPError, ValueError, KeyError):
            continue
        partner, partner_date = _latest(partner_versions)
        floor = max((s.version for s in req.specifier if s.operator in (">=", ">")), key=_version_key, default="0")
        if partner and partner_date and partner_date > released and _major(partner) is not None \
                and _major(partner) < 1000 and _major(partner) > (_major(floor) or 0):
            out.append(f"{name} {version} (released {released}) accepts any {req.name}{req.specifier or ''} with no "
                       f"upper bound, so pip installs {req.name} {partner} (released {partner_date})")
    return out


def _allows_target(requires_python: str) -> bool:
    try:
        return SpecifierSet(requires_python).contains(TARGET_PYTHON, prereleases=True)
    except InvalidSpecifier:
        return True


def _major(version: str) -> int | None:
    try:
        return Version(version).major
    except InvalidVersion:
        return None


def _version_key(version: str):
    try:
        return (1, Version(version))
    except InvalidVersion:
        return (0, version)


def _date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _years_since(day: date) -> float:
    return (datetime.now(timezone.utc).date() - day).days / 365.25


def _on(day: date | None) -> str:
    return f" ({day})" if day else ""


def _short(version: str) -> str:
    return ".".join(version.split(".")[:2])
