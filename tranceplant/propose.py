"""Stage 3: Propose. Gemma picks a replacement and writes the migration.

Gemma's edits are exact find/replace snippets. We apply them ourselves, check
every touched Python file still parses (LibCST) and every TOML file still
loads, then turn the result into a git-style patch and confirm it applies with
`git apply --check`. That last check is the only OBSERVED fact in this stage;
everything Gemma wrote is tagged AI.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from packaging.utils import canonicalize_name

from .gemma import EditItem, GapReport, GemmaClient, MigrationDraft, RepairDraft, ReplacementChoice, RiskExplanation, UsageNotes
from .models import (
    AI,
    Alternative,
    Claim,
    CodeEdit,
    Comparison,
    DependencyRisk,
    Gap,
    MigrationAttempt,
    Proposal,
    SuggestedTest,
    Usage,
)

MAX_REPAIR_ROUNDS = 2
FULL_FILE_LINES = 400  # files longer than this are shown as windows around usages
WINDOW = 20
MAX_CONTEXT_CHARS = 80_000
LOG_TAIL_CHARS = 12_000

MANIFESTS = ("requirements.txt", "pyproject.toml", "setup.cfg", "setup.py")
MENTION_FILES = ("Makefile", "Dockerfile", "tox.ini", "noxfile.py", ".pre-commit-config.yaml")


class EditError(ValueError):
    """Gemma's edits could not be applied. The message is fed back to Gemma."""


class ProposeError(RuntimeError):
    pass


# --- Capsule folder layout --------------------------------------------------


@dataclass
class Workspace:
    """A capsule folder. All evidence paths in claims are relative to `root`."""

    root: Path

    def __post_init__(self):
        self.root = Path(self.root)
        self.evidence.mkdir(parents=True, exist_ok=True)

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    def rel(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def gemma_path(self, job: str, suffix: str = "") -> Path:
        return self.evidence / "gemma" / f"{job}{suffix}.json"


def _save(result, ws: Workspace | None, job: str, suffix: str = "") -> list[str]:
    if ws is None:
        return []
    return [ws.rel(result.save(ws.gemma_path(job, suffix)))]


# --- Repo reading -----------------------------------------------------------


def _safe_rel(path: str) -> str:
    p = PurePosixPath(path.replace("\\", "/").strip())
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise EditError(f"path {path!r} must be relative to the repo root and stay inside it")
    return p.as_posix().removeprefix("./")


def read_text(repo: Path, rel: str) -> str | None:
    path = repo / rel
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None
    except UnicodeDecodeError:
        return None


def _mentions(text: str, names: list[str]) -> bool:
    low = text.lower()
    return any(n.lower() in low for n in names)


def _name_variants(name: str) -> list[str]:
    canon = canonicalize_name(name)
    return sorted({name, canon, canon.replace("-", "_")})


def context_files(repo: Path, dep: str, usages: list[Usage], state: dict[str, str] | None = None) -> list[str]:
    """Which files Gemma needs to see: usage sites, manifests, and build files that mention the dep."""
    state = state or {}
    names = _name_variants(dep)
    ordered: list[str] = []

    def add(rel: str):
        if rel not in ordered:
            ordered.append(rel)

    for u in usages:
        if u.kind != "transitive":
            add(_safe_rel(u.file))
    for rel in sorted(state):
        add(rel)
    for name in MANIFESTS:
        if (repo / name).is_file():
            add(name)
    for path in sorted(repo.glob("requirements*.txt")):
        add(path.name)
    for name in MENTION_FILES:
        text = state.get(name) or read_text(repo, name)
        if text and _mentions(text, names):
            add(name)
    return [rel for rel in ordered if rel in state or (repo / rel).is_file()]


def render_files(repo: Path, rels: list[str], usages: list[Usage], state: dict[str, str] | None = None) -> str:
    state = state or {}
    lines_by_file: dict[str, list[int]] = {}
    for u in usages:
        lines_by_file.setdefault(u.file, []).append(u.line)
    chunks, total = [], 0
    for rel in rels:
        text = state[rel] if rel in state else read_text(repo, rel)
        if text is None:
            continue
        lines = text.splitlines(keepends=True)
        if len(lines) > FULL_FILE_LINES and lines_by_file.get(rel):
            keep: set[int] = set()
            for n in lines_by_file[rel]:
                keep.update(range(max(0, n - 1 - WINDOW), min(len(lines), n + WINDOW)))
            body, prev = [], -2
            for i in sorted(keep):
                if i != prev + 1:
                    body.append(f"... (lines omitted, showing from line {i + 1}) ...\n")
                body.append(lines[i])
                prev = i
            text = "".join(body)
        chunk = f"<<<FILE {rel}>>>\n{text}{'' if text.endswith(chr(10)) else chr(10)}<<<END FILE {rel}>>>\n"
        if total + len(chunk) > MAX_CONTEXT_CHARS:
            chunks.append(f"(further files omitted to fit the context: {', '.join(rels[rels.index(rel):])})\n")
            break
        chunks.append(chunk)
        total += len(chunk)
    return "\n".join(chunks)


def render_usages(usages: list[Usage]) -> str:
    if not usages:
        return "(no usages found in the code)"
    return "\n".join(f"[{i}] {u.file}:{u.line} {u.kind} {u.symbol} :: {u.code.strip()}" for i, u in enumerate(usages, 1))


def _declared(risk: DependencyRisk) -> str:
    decl = risk.dependency.declared_in
    if not decl:
        return "not declared anywhere"
    return "; ".join(f"{d.file}{':' + str(d.line) if d.line else ''} `{d.spec}`" for d in decl)


# --- Applying edits ---------------------------------------------------------


def _fuzzy_replace(text: str, find: str, replace: str) -> str | None:
    """Match `find` line by line ignoring trailing whitespace, then ignoring indentation.

    Only a unique match is accepted. With an indentation-insensitive match the
    replacement is re-indented by the same offset.
    """
    lines = text.splitlines(keepends=True)
    needle = find.strip("\n").splitlines()
    if not needle:
        return None
    for strict in (True, False):
        norm = (lambda s: s.rstrip()) if strict else (lambda s: s.strip())
        want = [norm(n) for n in needle]
        hits = [i for i in range(len(lines) - len(want) + 1) if [norm(x) for x in lines[i : i + len(want)]] == want]
        if len(hits) != 1:
            if len(hits) > 1:
                return None
            continue
        i = hits[0]
        new = replace.strip("\n")
        if not strict:
            have = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
            had = needle[0][: len(needle[0]) - len(needle[0].lstrip())]
            new = "\n".join(have + ln[len(had):] if ln.startswith(had) else ln for ln in new.splitlines())
        last = lines[i + len(want) - 1]
        ending = "\n" if last.endswith("\n") else ""
        block = (new + ending) if new else ""
        return "".join(lines[:i]) + block + "".join(lines[i + len(want) :])
    return None


def check_syntax(path: str, text: str) -> None:
    if path.endswith(".py"):
        try:
            import libcst

            libcst.parse_module(text)
        except ImportError:  # pragma: no cover - libcst is a declared dependency
            compile(text, path, "exec")
        except Exception as exc:
            raise EditError(f"{path} no longer parses as Python after the edits: {str(exc).splitlines()[0]}") from None
    elif path.endswith(".toml"):
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise EditError(f"{path} is no longer valid TOML after the edits: {exc}") from None


def apply_edits(repo: Path, edits: list[CodeEdit], state: dict[str, str] | None = None) -> dict[str, str]:
    """Apply edits on top of `state` (path -> current text; missing paths are read from repo).

    Returns the new state: every file that differs from the original repo.
    Raises EditError listing every edit that failed, so Gemma can fix them all at once.
    """
    current = dict(state or {})
    errors: list[str] = []
    for n, edit in enumerate(edits, 1):
        try:
            rel = _safe_rel(edit.file)
        except EditError as exc:
            errors.append(f"edit {n}: {exc}")
            continue
        text = current[rel] if rel in current else read_text(repo, rel)
        if text is None:
            if edit.find == "":
                current[rel] = edit.replace
            else:
                errors.append(f"edit {n}: file {rel} does not exist (use an empty find to create it)")
            continue
        if edit.find == "":
            errors.append(f"edit {n}: empty find on existing file {rel}; quote the exact text to replace")
            continue
        count = text.count(edit.find)
        if count == 1:
            current[rel] = text.replace(edit.find, edit.replace, 1)
        elif count > 1:
            errors.append(f"edit {n}: find text appears {count} times in {rel}; include more surrounding lines")
        else:
            fixed = _fuzzy_replace(text, edit.find, edit.replace)
            if fixed is None:
                first = edit.find.strip().splitlines()[0] if edit.find.strip() else ""
                errors.append(f"edit {n}: find text not found in {rel} (it starts with {first[:80]!r}); copy it exactly from the file")
            else:
                current[rel] = fixed
    if errors:
        raise EditError("\n".join(errors))
    changed: dict[str, str] = {}
    for rel, text in current.items():
        check_syntax(rel, text)
        if text != read_text(repo, rel):
            changed[rel] = text
    return changed


# --- Patches ----------------------------------------------------------------


def _lines(text: str) -> list[str]:
    out = text.splitlines(keepends=True)
    if out and not out[-1].endswith("\n"):
        out[-1] += "\n\\ No newline at end of file\n"
    return out


def make_patch(repo: Path, changed: dict[str, str]) -> str:
    """A git-style unified diff from the original repo to `changed`, ready for `git apply`."""
    parts = []
    for rel in sorted(changed):
        old = read_text(repo, rel)
        new = changed[rel]
        header = [f"diff --git a/{rel} b/{rel}\n"]
        if old is None:
            header.append("new file mode 100644\n")
        body = difflib.unified_diff(
            _lines(old or ""),
            _lines(new),
            fromfile="/dev/null" if old is None else f"a/{rel}",
            tofile=f"b/{rel}",
        )
        body = list(body)
        if body:
            parts.append("".join(header + body))
    return "".join(parts)


def git_apply_check(repo: Path, patch_text: str) -> tuple[bool, str]:
    if not patch_text.strip():
        return False, "the patch is empty"
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo).resolve().parent))
    proc = subprocess.run(
        ["git", "apply", "--check", "--verbose", "-"],
        cwd=repo,
        input=patch_text,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _record_patch(ws: Workspace, repo: Path, changed: dict[str, str], rnd: int) -> tuple[str, Claim]:
    patch = make_patch(repo, changed)
    patch_path = ws.root / f"migration.round{rnd}.patch"
    patch_path.write_text(patch, encoding="utf-8")
    ok, output = git_apply_check(repo, patch)
    log_path = ws.evidence / f"patch-check.round{rnd}.log"
    log_path.write_text(f"$ git apply --check --verbose {patch_path.name}\nexit: {0 if ok else 1}\n{output}\n", encoding="utf-8")
    files = ", ".join(sorted(changed)) or "no files"
    verb = "applies cleanly" if ok else "does NOT apply"
    claim = Claim.observed(f"Round {rnd} patch ({files}) {verb} to the original commit", ws.rel(patch_path), ws.rel(log_path))
    return ws.rel(patch_path), claim


def _to_edits(items: list[EditItem]) -> list[CodeEdit]:
    return [CodeEdit(file=i.file, find=i.find, replace=i.replace, reason=i.reason) for i in items]


def replay_state(repo: Path, attempts: list[MigrationAttempt]) -> dict[str, str]:
    state: dict[str, str] = {}
    for attempt in attempts:
        state = apply_edits(repo, attempt.edits, state)
    return state


# --- Gemma jobs -------------------------------------------------------------


def explain_risk(client: GemmaClient, risk: DependencyRisk, python_version: str, ws: Workspace | None = None) -> DependencyRisk:
    """Fill in ai_summary / ai_severity. Returns a new DependencyRisk."""
    result = client.run(
        "explain_risk",
        {
            "package": risk.dependency.name,
            "pinned": risk.dependency.pinned or "not pinned",
            "declared_in": _declared(risk),
            "python_version": python_version,
            "indicators": risk.indicators.model_dump(mode="json", exclude={"findings"}),
            "findings": "\n".join(f"- {c.claim}" for c in risk.indicators.findings) or "(none)",
        },
        fixture_key=risk.dependency.name,
    )
    data: RiskExplanation = result.data
    evidence = _save(result, ws, "explain_risk")
    return risk.model_copy(update={"ai_summary": Claim.ai(data.summary, *evidence), "ai_severity": data.severity})


def annotate_usages(
    client: GemmaClient, dep: str, usages: list[Usage], replacement: str | None = None, ws: Workspace | None = None
) -> list[Usage]:
    """Add Gemma's one-line note to each usage (tagged AI). Usages without a note keep note=None."""
    if not usages:
        return []
    out = list(usages)
    batch = 40
    for start in range(0, len(usages), batch):
        chunk = usages[start : start + batch]
        result = client.run(
            "annotate_usages",
            {"package": dep, "replacement": replacement or "not chosen yet", "usages": render_usages(chunk)},
            fixture_key=dep,
        )
        evidence = _save(result, ws, "annotate_usages", f".{start // batch}" if len(usages) > batch else "")
        data: UsageNotes = result.data
        for note in data.notes:
            i = start + note.index - 1
            if start <= i < start + len(chunk) and note.note.strip():
                out[i] = out[i].model_copy(update={"note": Claim.ai(note.note.strip(), *evidence)})
    return out


def choose_replacement(
    client: GemmaClient,
    risk: DependencyRisk,
    usages: list[Usage],
    *,
    python_version: str,
    requires_python: str | None,
    other_dependencies: list[str],
    ws: Workspace | None = None,
) -> Proposal:
    dep = risk.dependency
    result = client.run(
        "pick_replacement",
        {
            "package": dep.name,
            "pinned": dep.pinned or "not pinned",
            "declared_in": _declared(risk),
            "python_version": python_version,
            "requires_python": requires_python or "not stated",
            "other_dependencies": ", ".join(other_dependencies) or "(none)",
            "risk_summary": risk.ai_summary.claim if risk.ai_summary else "(no summary)",
            "usages": render_usages(usages),
        },
        fixture_key=dep.name,
    )
    data: ReplacementChoice = result.data
    evidence = _save(result, ws, "pick_replacement")
    return Proposal(
        dependency=dep.name,
        action=data.action,
        replacement=data.replacement,
        target_spec=data.target_spec,
        reasoning=Claim.ai(data.reasoning, *evidence),
        alternatives=[Alternative(name=a.name, why_not=a.why_not) for a in data.alternatives],
        model=result.model,
    )


def _apply_validator(repo: Path, state: dict[str, str]):
    holder: dict[str, dict[str, str]] = {}

    def validate(data) -> None:
        if not data.edits:
            raise EditError("the edit list is empty; the migration needs at least one edit")
        changed = apply_edits(repo, _to_edits(data.edits), state)
        if changed == state:
            raise EditError("the edits do not change any file")
        holder["changed"] = changed

    return validate, holder


def draft_migration(
    client: GemmaClient,
    repo: Path,
    ws: Workspace,
    risk: DependencyRisk,
    proposal: Proposal,
    usages: list[Usage],
    *,
    python_version: str,
    requires_python: str | None,
) -> MigrationAttempt:
    repo = Path(repo)
    dep = risk.dependency.name
    files = context_files(repo, dep, usages)
    validate, holder = _apply_validator(repo, {})
    result = client.run(
        "write_migration",
        {
            "package": dep,
            "action": proposal.action,
            "replacement": proposal.replacement or "(nothing)",
            "target_spec": proposal.target_spec or "(no new declaration)",
            "reasoning": proposal.reasoning.claim,
            "python_version": python_version,
            "requires_python": requires_python or "not stated",
            "usages": render_usages(usages),
            "files": render_files(repo, files, usages),
        },
        fixture_key=dep,
        validate=validate,
    )
    data: MigrationDraft = result.data
    evidence = _save(result, ws, "write_migration", ".round0")
    patch_file, applies = _record_patch(ws, repo, holder["changed"], 0)
    return MigrationAttempt(
        round=0,
        steps=[Claim.ai(s, *evidence) for s in data.steps],
        edits=_to_edits(data.edits),
        patch_file=patch_file,
        patch_applies=applies,
    )


def repair_migration(
    client: GemmaClient,
    repo: Path,
    ws: Workspace,
    proposal: Proposal,
    attempts: list[MigrationAttempt],
    usages: list[Usage],
    *,
    failing_tests: list[str],
    log_text: str,
) -> MigrationAttempt:
    """One repair round: Gemma sees the cumulative patch and the failure, and edits on top of it."""
    repo = Path(repo)
    rnd = len(attempts)
    if rnd < 1:
        raise ProposeError("repair needs a first draft")
    if rnd > MAX_REPAIR_ROUNDS:
        raise ProposeError(f"at most {MAX_REPAIR_ROUNDS} repair rounds")
    state = replay_state(repo, attempts)
    current_patch = (ws.root / attempts[-1].patch_file).read_text(encoding="utf-8")
    files = context_files(repo, proposal.dependency, usages, state)
    validate, holder = _apply_validator(repo, state)
    tail = log_text[-LOG_TAIL_CHARS:]
    result = client.run(
        "repair_migration",
        {
            "round": str(rnd),
            "max_rounds": str(MAX_REPAIR_ROUNDS),
            "package": proposal.dependency,
            "replacement": proposal.replacement or "(nothing)",
            "target_spec": proposal.target_spec or "(no new declaration)",
            "current_patch": current_patch,
            "failing_tests": "\n".join(f"- {t}" for t in failing_tests) or "(no test names; see the log)",
            "test_log": tail,
            "files": render_files(repo, files, usages, state),
        },
        fixture_key=f"{proposal.dependency}.round{rnd}",
        validate=validate,
    )
    data: RepairDraft = result.data
    evidence = _save(result, ws, "repair_migration", f".round{rnd}")
    patch_file, applies = _record_patch(ws, repo, holder["changed"], rnd)
    return MigrationAttempt(
        round=rnd,
        steps=[Claim.ai(s, *evidence) for s in data.steps],
        edits=_to_edits(data.edits),
        patch_file=patch_file,
        patch_applies=applies,
        diagnosis=Claim.ai(data.diagnosis, *evidence),
    )


def explain_gaps(
    client: GemmaClient,
    repo: Path,
    ws: Workspace,
    proposal: Proposal,
    attempts: list[MigrationAttempt],
    usages: list[Usage],
    comparison: Comparison,
    *,
    python_version: str,
    requires_python: str | None,
) -> tuple[list[Gap], list[Claim], list[SuggestedTest]]:
    repo = Path(repo)
    patch = (ws.root / attempts[-1].patch_file).read_text(encoding="utf-8") if attempts else "(no patch)"
    readme = next((read_text(repo, n) for n in ("README.md", "README.rst", "README.txt", "README") if read_text(repo, n)), None)
    result = client.run(
        "explain_gaps",
        {
            "package": proposal.dependency,
            "replacement": proposal.replacement or "(nothing)",
            "action": proposal.action,
            "comparison": json.dumps(comparison.model_dump(mode="json"), indent=1),
            "patch": patch[:LOG_TAIL_CHARS],
            "usages": render_usages(usages),
            "requires_python": requires_python or "not stated",
            "python_version": python_version,
            "docs": (readme or "(no README)")[:8000],
        },
        fixture_key=proposal.dependency,
    )
    data: GapReport = result.data
    evidence = _save(result, ws, "explain_gaps")
    gaps = [
        Gap(description=g.description, source=AI, evidence=evidence, severity=g.severity, related_tests=g.related_tests)
        for g in data.gaps
        if g.description.strip()
    ]
    concerns = [Claim.ai(c, *evidence) for c in data.concerns if c.strip()]
    tests = [SuggestedTest(name=t.name, purpose=t.purpose, code=t.code) for t in data.suggested_tests]
    return gaps, concerns, tests
