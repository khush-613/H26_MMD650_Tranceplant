"""Typer commands and the wiring between the six stages.

Stage contracts (other modules implement these; see README "Stage contracts"):
  scan.scan_repo(repo) -> ScanReport
  xray.find_usages(repo, dep) -> list[Usage]
  sandbox.run_side(repo, side, *, patch, evidence_dir, label, backend) -> SideRun
  compare.compare(control_results, donor_results) -> Comparison
  compare.decide_verdict(comparison, gaps) -> Verdict
  capsule.write_capsule(capsule, capsule_dir) -> Path
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import __version__, propose
from .gemma import GemmaClient, GemmaError
from .models import (
    OBSERVED,
    Capsule,
    Claim,
    Comparison,
    Gap,
    GemmaInfo,
    MigrationAttempt,
    RepoInfo,
    ScanReport,
    SideRun,
    capsule_json_schema,
)

app = typer.Typer(
    help="Find risky dependencies, swap them in a sandbox, test both sides, and ship the evidence.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
console = Console()

STAGE_OWNERS = {"scan": "Khush", "xray": "Khush", "capsule": "Khush", "sandbox": "Rajveer", "compare": "Rajveer"}


class StageMissing(RuntimeError):
    pass


def stage(module: str, attr: str) -> Callable:
    """Look up a stage function at call time, so the CLI runs before every stage is merged."""
    try:
        mod = importlib.import_module(f"tranceplant.{module}")
    except ModuleNotFoundError as exc:
        if exc.name != f"tranceplant.{module}":
            raise
        raise StageMissing(f"stage '{module}' is not merged yet (owner: {STAGE_OWNERS.get(module, '?')})") from None
    fn = getattr(mod, attr, None)
    if fn is None:
        raise StageMissing(f"tranceplant.{module} has no {attr}() yet (owner: {STAGE_OWNERS.get(module, '?')})")

    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except NotImplementedError:  # still the skeleton stub
            raise StageMissing(
                f"stage '{module}.{attr}' is still a stub (owner: {STAGE_OWNERS.get(module, '?')})"
            ) from None

    return call


# --- Shared state -----------------------------------------------------------


class Settings:
    gemma_mode: str | None = None
    cache_dir: Path | None = None
    client: GemmaClient | None = None


settings = Settings()


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader: KEY=VALUE lines, existing environment wins."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def gemma() -> GemmaClient:
    if settings.client is None:
        settings.client = GemmaClient(settings.gemma_mode, cache_dir=settings.cache_dir)
    return settings.client


@app.callback()
def main(
    gemma_mode: Annotated[
        Optional[str],
        typer.Option("--gemma", help="live (API + cache), offline (cache only) or fake (fixtures/gemma)."),
    ] = None,
    cache_dir: Annotated[Optional[Path], typer.Option(help="Where Gemma replies are cached.")] = None,
):
    load_dotenv()
    settings.gemma_mode = gemma_mode
    settings.cache_dir = cache_dir
    settings.client = None


def fail(message: str, code: int = 1):
    console.print(f"[bold red]error:[/] {escape(message)}")
    raise typer.Exit(code)


def tag(source: str) -> str:
    return "[green]OBSERVED[/]" if source == OBSERVED else "[magenta]AI[/]"


def _repo(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_dir():
        fail(f"repo folder {path} does not exist")
    return path


def git_commit(repo: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip() or None


# --- Pipeline ---------------------------------------------------------------


def _counts(results) -> str:
    by: dict[str, int] = {}
    for r in results:
        by[r.outcome] = by.get(r.outcome, 0) + 1
    return ", ".join(f"{n} {k}" for k, n in sorted(by.items())) or "no test results"


def _log_text(ws: propose.Workspace, run: SideRun) -> str:
    if run.log_file and (ws.root / run.log_file).is_file():
        return (ws.root / run.log_file).read_text(encoding="utf-8", errors="replace")
    return ""


def _needs_repair(run: SideRun, comparison: Comparison) -> list[str]:
    if not run.setup_ok:
        return ["(donor sandbox setup failed: install or patch apply)"]
    return comparison.regressions + comparison.only_in_control


def _finalise_donor(ws: propose.Workspace, run: SideRun) -> SideRun:
    """Copy the last donor run to the canonical evidence/donor.* names."""
    update = {}
    for attr, suffix in (("junit_file", ".junit.xml"), ("log_file", ".log")):
        rel = getattr(run, attr)
        if rel and (ws.root / rel).is_file():
            target = ws.evidence / f"donor{suffix}"
            if (ws.root / rel).resolve() != target.resolve():
                shutil.copyfile(ws.root / rel, target)
            update[attr] = ws.rel(target)
    return run.model_copy(update=update)


def headline_claims(ws: propose.Workspace, proposal, control: SideRun, donor: SideRun, comparison: Comparison) -> list[Claim]:
    evidence = [p for p in (control.junit_file, donor.junit_file) if p] or [p for p in (control.log_file, donor.log_file) if p]
    swap = proposal.replacement or f"{proposal.dependency} removed"
    claims = []
    if evidence:
        claims.append(Claim.observed(f"Control (original {proposal.dependency}): {_counts(control.results)}", *evidence))
        claims.append(Claim.observed(f"Donor ({swap}): {_counts(donor.results)}", *evidence))
        for name in comparison.newly_fixed:
            claims.append(Claim.observed(f"{name} fails with the original and passes with {swap}", *evidence))
        for name in comparison.regressions:
            claims.append(Claim.observed(f"{name} passes with the original and fails with {swap}", *evidence))
    claims.append(proposal.reasoning)
    return claims


def trial_pipeline(
    repo: Path,
    dep: str,
    out: Path,
    *,
    python_version: str = "3.12",
    backend: str = "auto",
    max_repairs: int = propose.MAX_REPAIR_ROUNDS,
    report: ScanReport | None = None,
    say: Callable[[str], None] = lambda msg: None,
) -> Capsule:
    """Run stages 1-6 for one dependency and write its capsule folder."""
    client = gemma()
    calls0, hits0 = client.api_calls, client.cache_hits
    max_repairs = max(0, min(max_repairs, propose.MAX_REPAIR_ROUNDS))
    report = report or stage("scan", "scan_repo")(repo)
    risk = report.get(dep)
    if risk is None:
        raise ValueError(f"'{dep}' is not among the dependencies scan found in {repo}")
    name = risk.dependency.name
    ws = propose.Workspace(out / name)
    if risk.raw_sources:
        (ws.evidence / "risk-sources.json").write_text(json.dumps(risk.raw_sources, indent=1, default=str), encoding="utf-8")

    say(f"[1/6] scan: {name} (score {risk.score})")
    say("[2/6] usage x-ray")
    usages = stage("xray", "find_usages")(repo, name)

    say("[3/6] propose: Gemma explains the risk and picks a replacement")
    risk = propose.explain_risk(client, risk, python_version, ws)
    others = [d.dependency.name for d in report.dependencies if d.dependency.name != name]
    proposal = propose.choose_replacement(
        client, risk, usages, python_version=python_version, requires_python=report.python_requires,
        other_dependencies=others, ws=ws,
    )
    usages = propose.annotate_usages(client, name, usages, proposal.replacement, ws)
    say(f"      {proposal.action} -> {proposal.replacement or '(nothing)'}; Gemma writes the migration")
    attempts: list[MigrationAttempt] = [
        propose.draft_migration(client, repo, ws, risk, proposal, usages,
                                python_version=python_version, requires_python=report.python_requires)
    ]

    run_side = stage("sandbox", "run_side")
    compare = stage("compare", "compare")
    say("[4/6] twin sandbox trial: control")
    control = run_side(repo, "control", patch=None, evidence_dir=ws.evidence, label="control", backend=backend)
    donor_runs: list[SideRun] = []
    while True:
        rnd = attempts[-1].round
        say(f"[4/6] twin sandbox trial: donor (round {rnd})")
        donor = run_side(repo, "donor", patch=ws.root / attempts[-1].patch_file, evidence_dir=ws.evidence,
                         label=f"donor.round{rnd}", backend=backend)
        donor_runs.append(donor)
        say("[5/6] compare")
        comparison = compare(control.results, donor.results)
        broken = _needs_repair(donor, comparison)
        if not broken or rnd >= max_repairs:
            break
        say(f"      {len(broken)} broken; Gemma repair round {rnd + 1}")
        try:
            attempts.append(propose.repair_migration(
                client, repo, ws, proposal, attempts, usages, failing_tests=broken, log_text=_log_text(ws, donor),
            ))
        except GemmaError as exc:
            say(f"      repair failed: {exc}")
            break

    donor = _finalise_donor(ws, donor)
    shutil.copyfile(ws.root / attempts[-1].patch_file, ws.root / "migration.patch")

    gaps, concerns, suggested = propose.explain_gaps(
        client, repo, ws, proposal, attempts, usages, comparison,
        python_version=python_version, requires_python=report.python_requires,
    )
    junits = [p for p in (control.junit_file, donor.junit_file) if p] or [p for p in (control.log_file, donor.log_file) if p]
    observed_gaps = [
        Gap(description=f"Regression: {t} passes with the original but fails after the swap", source=OBSERVED,
            evidence=junits, severity="high", related_tests=[t])
        for t in comparison.regressions
    ]
    gaps = observed_gaps + gaps
    verdict = stage("compare", "decide_verdict")(comparison, gaps)
    if not donor.setup_ok:
        verdict = "Rejected"
    open_gaps = [g for g in gaps if g.status == "open"]
    rounds = len(attempts) - 1
    reason = (
        f"{verdict}: {len(comparison.regressions)} regression(s) after {rounds} repair round(s), "
        f"{len(open_gaps)} open gap(s), {len(comparison.newly_fixed)} newly fixed"
        + ("" if donor.setup_ok else "; donor sandbox setup failed")
    )
    verdict_evidence = junits + ([d.log_file for d in [donor] if d.log_file and not donor.setup_ok])

    say("[6/6] exit capsule")
    capsule = Capsule(
        tool_version=__version__,
        repo=RepoInfo(path=str(repo), commit=git_commit(repo), python_version=control.python_version or python_version),
        dependency=risk.dependency,
        risk=risk,
        usages=usages,
        proposal=proposal,
        migration=attempts,
        control=control,
        donor=donor,
        donor_rounds=donor_runs,
        comparison=comparison,
        gaps=gaps,
        suggested_tests=suggested,
        concerns=concerns,
        verdict=verdict,
        verdict_reason=Claim.observed(reason, *(verdict_evidence or [attempts[-1].patch_file])),
        claims=headline_claims(ws, proposal, control, donor, comparison),
        gemma=GemmaInfo(
            models=sorted(client.models_used),
            calls=client.api_calls - calls0,
            cache_hits=client.cache_hits - hits0,
        ),
    )
    stage("capsule", "write_capsule")(capsule, ws.root)
    return capsule


# --- Commands ---------------------------------------------------------------


def _run_or_fail(fn: Callable, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (StageMissing, GemmaError, propose.ProposeError, ValueError) as exc:
        fail(str(exc))


def _scan(repo: Path) -> ScanReport:
    return _run_or_fail(lambda: stage("scan", "scan_repo")(repo))


@app.command()
def scan(
    repo: Annotated[Path, typer.Argument(help="Path to the Python repo.")],
    explain: Annotated[bool, typer.Option(help="Ask Gemma for a plain-English risk summary of each risky dependency.")] = False,
    min_score: Annotated[int, typer.Option(help="Only explain dependencies at or above this score.")] = 40,
    python: Annotated[str, typer.Option(help="Python version the repo targets.")] = "3.12",
    as_json: Annotated[bool, typer.Option("--json", help="Print the scan report as JSON.")] = False,
):
    """List dependencies with risk scores."""
    repo = _repo(repo)
    report = _scan(repo)
    deps = sorted(report.dependencies, key=lambda d: d.score, reverse=True)
    if explain:
        explained = []
        for d in deps:
            explained.append(_run_or_fail(propose.explain_risk, gemma(), d, python) if d.score >= min_score else d)
        deps = explained
    if as_json:
        print(report.model_copy(update={"dependencies": deps}).model_dump_json(indent=1))
        return
    table = Table(title=f"Dependency risk: {repo.name}")
    for col, kw in (("Package", {"style": "bold"}), ("Pinned", {}), ("Declared in", {}), ("Score", {"justify": "right"}),
                    ("Why", {"overflow": "fold"})):
        table.add_column(col, **kw)
    for d in deps:
        why = [f"{tag(c.source)} {escape(c.claim)}" for c in d.indicators.findings[:2]]
        if d.ai_summary:
            why.append(f"{tag('AI')} [{d.ai_severity}] {escape(d.ai_summary.claim)}")
        declared = ", ".join(sorted({x.file for x in d.dependency.declared_in})) or "[yellow]not declared[/]"
        table.add_row(d.dependency.name, d.dependency.pinned or "-", declared, str(d.score), "\n".join(why))
    console.print(table)
    for c in report.conflicts:
        console.print(f"{tag(c.source)} {escape(c.claim)}")


@app.command()
def xray(
    repo: Annotated[Path, typer.Argument(help="Path to the Python repo.")],
    dep: Annotated[str, typer.Argument(help="Dependency name, e.g. requests.")],
    notes: Annotated[bool, typer.Option(help="Ask Gemma for a one-line note on each usage.")] = True,
):
    """Show every place a dependency is used, including aliases, private APIs and monkeypatches."""
    repo = _repo(repo)
    usages = _run_or_fail(lambda: stage("xray", "find_usages")(repo, dep))
    if notes and usages:
        usages = _run_or_fail(propose.annotate_usages, gemma(), dep, usages)
    table = Table(title=f"Usage X-Ray: {dep} in {repo.name}")
    table.add_column("Where", style="bold")
    table.add_column("Kind")
    table.add_column("Code", overflow="fold")
    table.add_column("Why it matters", overflow="fold")
    highlight = {"alias": "yellow", "private_api": "red", "monkeypatch": "red", "transitive": "cyan"}
    for u in usages:
        kind = f"[{highlight[u.kind]}]{u.kind}[/]" if u.kind in highlight else u.kind
        note = f"{tag('AI')} {escape(u.note.claim)}" if u.note else ""
        table.add_row(f"{u.file}:{u.line}", kind, escape(u.code.strip()), note)
    console.print(table)
    console.print(f"{tag('OBSERVED')} {len(usages)} usage(s) found by AST scan")


def _print_capsule(capsule: Capsule, folder: Path):
    colour = {"Accepted": "green", "Partial": "yellow", "Rejected": "red"}[capsule.verdict]
    c = capsule.comparison
    table = Table(title=f"{capsule.dependency.name} -> {capsule.proposal.replacement or '(removed)'}")
    table.add_column("Group")
    table.add_column("Tests", justify="right")
    table.add_column("Names", overflow="fold")
    for label, names, style in (("newly fixed", c.newly_fixed, "green"), ("regressions", c.regressions, "red"),
                                ("still passing", c.still_passing, ""), ("still failing", c.still_failing, "yellow")):
        table.add_row(f"[{style}]{label}[/]" if style else label, str(len(names)), escape(", ".join(names)))
    console.print(table)
    for claim in capsule.claims:
        console.print(f"{tag(claim.source)} {escape(claim.claim)}")
    for gap in capsule.gaps:
        console.print(f"{tag(gap.source)} gap ({gap.severity}): {escape(gap.description)}")
    console.print(f"[bold {colour}]Verdict: {capsule.verdict}[/]  {escape(capsule.verdict_reason.claim)}")
    console.print(f"Capsule: {folder}")


@app.command()
def trial(
    repo: Annotated[Path, typer.Argument(help="Path to the Python repo.")],
    dep: Annotated[str, typer.Argument(help="Dependency to transplant, e.g. jinja2.")],
    out: Annotated[Path, typer.Option(help="Where capsule folders go.")] = Path("capsules"),
    python: Annotated[str, typer.Option(help="Python version the repo targets.")] = "3.12",
    backend: Annotated[str, typer.Option(help="Sandbox backend: auto (Docker if running, else uv), docker or uv.")] = "auto",
    max_repairs: Annotated[int, typer.Option(help="Gemma repair rounds after regressions (max 2).")] = 2,
):
    """Propose a replacement, run both sandboxes and write the capsule."""
    repo = _repo(repo)
    capsule = _run_or_fail(trial_pipeline, repo, dep, out.resolve(), python_version=python, backend=backend,
                           max_repairs=max_repairs, say=lambda m: console.print(f"[dim]{escape(m)}[/]"))
    _print_capsule(capsule, out.resolve() / capsule.dependency.name)


@app.command("run")
def run_all(
    repo: Annotated[Path, typer.Argument(help="Path to the Python repo.")],
    all_: Annotated[bool, typer.Option("--all", help="Trial every dependency at or above --min-score.")] = False,
    deps: Annotated[Optional[list[str]], typer.Option("--dep", help="Trial only these dependencies.")] = None,
    min_score: Annotated[int, typer.Option(help="Risk score threshold for --all.")] = 40,
    out: Annotated[Path, typer.Option(help="Where capsule folders go.")] = Path("capsules"),
    python: Annotated[str, typer.Option(help="Python version the repo targets.")] = "3.12",
    backend: Annotated[str, typer.Option(help="Sandbox backend: auto (Docker if running, else uv), docker or uv.")] = "auto",
):
    """Run all six stages for every risky dependency."""
    repo = _repo(repo)
    if not all_ and not deps:
        fail("pass --all or at least one --dep")
    report = _scan(repo)
    if deps:
        chosen = list(deps)
    else:
        chosen = [d.dependency.name for d in sorted(report.dependencies, key=lambda d: d.score, reverse=True)
                  if d.score >= min_score]
    if not chosen:
        console.print(f"No dependency scored {min_score} or more.")
        return
    table = Table(title=f"Tranceplant: {repo.name}")
    for col in ("Dependency", "Swap", "Verdict", "Newly fixed", "Regressions", "Open gaps"):
        table.add_column(col)
    failures = 0
    for name in chosen:
        console.rule(name)
        try:
            capsule = trial_pipeline(repo, name, out.resolve(), python_version=python, backend=backend, report=report,
                                     say=lambda m: console.print(f"[dim]{escape(m)}[/]"))
        except StageMissing as exc:
            fail(str(exc))
        except (GemmaError, propose.ProposeError, ValueError, OSError) as exc:
            failures += 1
            console.print(f"[red]{escape(name)} failed:[/] {escape(str(exc))}")
            table.add_row(name, "-", "[red]error[/]", "-", "-", "-")
            continue
        colour = {"Accepted": "green", "Partial": "yellow", "Rejected": "red"}[capsule.verdict]
        table.add_row(name, f"{capsule.proposal.action} -> {capsule.proposal.replacement or '-'}",
                      f"[{colour}]{capsule.verdict}[/]", str(len(capsule.comparison.newly_fixed)),
                      str(len(capsule.comparison.regressions)), str(sum(g.status == 'open' for g in capsule.gaps)))
    console.print(table)
    if failures:
        raise typer.Exit(1)


@app.command()
def replay(
    capsule_dir: Annotated[Path, typer.Argument(help="A capsule folder, e.g. capsules/jinja2.")],
    repo: Annotated[Optional[Path], typer.Option(help="Repo to replay against (default: the path saved in the capsule).")] = None,
    backend: Annotated[Optional[str], typer.Option(help="Sandbox backend (default: the one the capsule used).")] = None,
):
    """Re-run a saved trial and check the per-test results still hold."""
    capsule_dir = capsule_dir.expanduser().resolve()
    path = capsule_dir / "capsule.json"
    if not path.is_file():
        fail(f"{path} not found")
    try:
        capsule = Capsule.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        fail(f"{path} is not a valid capsule: {exc}")
    repo_path = _repo(repo or Path(capsule.repo.path))
    patch = capsule_dir / "migration.patch"
    if not patch.is_file():
        fail(f"{patch} not found")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    ws = propose.Workspace(capsule_dir / "replays" / stamp)
    run_side = _run_or_fail(stage, "sandbox", "run_side")
    compare = _run_or_fail(stage, "compare", "compare")
    side_backend = backend or capsule.control.backend
    console.print(f"[dim]replaying {capsule.dependency.name} against {repo_path} ({side_backend})[/]")
    control = run_side(repo_path, "control", patch=None, evidence_dir=ws.evidence, label="control", backend=side_backend)
    donor = run_side(repo_path, "donor", patch=patch, evidence_dir=ws.evidence, label="donor", backend=side_backend)
    comparison = compare(control.results, donor.results)

    table = Table(title=f"Replay: {capsule.dependency.name}")
    for col in ("Test", "Saved control", "Now control", "Saved donor", "Now donor", "Same?"):
        table.add_column(col)
    saved_c = {r.name: r.outcome for r in capsule.control.results}
    saved_d = {r.name: r.outcome for r in capsule.donor.results}
    now_c = {r.name: r.outcome for r in control.results}
    now_d = {r.name: r.outcome for r in donor.results}
    drift = 0
    for name in sorted(set(saved_c) | set(saved_d) | set(now_c) | set(now_d)):
        same = saved_c.get(name) == now_c.get(name) and saved_d.get(name) == now_d.get(name)
        drift += not same
        table.add_row(escape(name), *(str(x.get(name, "-")) for x in (saved_c, now_c, saved_d, now_d)),
                      "[green]yes[/]" if same else "[red]no[/]")
    console.print(table)
    summary = {"replayed_at": stamp, "drifted_tests": drift, "comparison": comparison.model_dump(mode="json"),
               "control": control.model_dump(mode="json"), "donor": donor.model_dump(mode="json")}
    (ws.root / "replay.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    if drift:
        console.print(f"[red]{drift} test(s) changed since the capsule was made.[/] Details: {ws.root}")
        raise typer.Exit(1)
    console.print(f"[green]Result still holds:[/] every test matches the saved trial. Details: {ws.root}")


@app.command()
def schema(
    output: Annotated[Optional[Path], typer.Option("-o", help="Write the JSON Schema to this file.")] = None,
):
    """Print the capsule JSON Schema (generated from models.py)."""
    text = json.dumps(capsule_json_schema(), indent=1)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        console.print(f"Wrote {output}")
    else:
        print(text)


@app.command("gemma-check")
def gemma_check():
    """Make one tiny Gemma call to check the API key and model access."""
    client = gemma()
    result = _run_or_fail(client.ping)
    console.print(f"Gemma OK: model {result.model}, origin {result.origin}, reply {escape(result.raw.strip()[:80])}")


if __name__ == "__main__":
    app()
