"""Stage 6: Exit Capsule. Owner: Khush.

cli.py has already written migration.patch and everything under evidence/
into capsule_dir. This writes capsule.json (validated against the schema),
capsule.md and capsule.html (from templates/), and replay.sh.

Every line of the report carries its OBSERVED or AI tag and links to the files
it came from. Evidence that a claim cites but that is missing from the folder
is listed at the end of the report instead of being hidden.
"""

import json
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import BaseModel

from .models import Capsule, Claim, capsule_json_schema

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"

REPLAY_SH = """\
#!/bin/sh
# Re-run this trial: the original and the migrated repo, same tests, then
# compare every test with the saved results. Exits 1 if any result changed.
# Options: --repo PATH (default: the path in capsule.json), --backend docker|uv
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
if command -v tranceplant >/dev/null 2>&1; then
    exec tranceplant replay "$here" "$@"
fi
exec python3 -m tranceplant.cli replay "$here" "$@"
"""

CHANGES = [
    ("newly_fixed", "newly fixed"),
    ("regressions", "newly broken"),
    ("still_failing", "still failing"),
    ("still_passing", "still passing"),
    ("only_in_control", "only in control"),
    ("only_in_donor", "only in donor"),
]


def write_capsule(capsule: Capsule, capsule_dir: Path) -> Path:
    capsule_dir = Path(capsule_dir)
    capsule_dir.mkdir(parents=True, exist_ok=True)
    text = capsule.model_dump_json(indent=1)
    Capsule.model_validate_json(text)  # what we write must load back under the schema
    (capsule_dir / "capsule.schema.json").write_text(json.dumps(capsule_json_schema(), indent=1) + "\n", encoding="utf-8")
    path = capsule_dir / "capsule.json"
    path.write_text(text + "\n", encoding="utf-8")

    missing = missing_evidence(capsule, capsule_dir)
    for item in missing:
        log.warning("capsule %s cites missing evidence: %s", capsule.dependency.name, item)
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default_for_string=False),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["cell"] = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
    view = report_view(capsule, missing)
    for name in ("capsule.md", "capsule.html"):
        (capsule_dir / name).write_text(env.get_template(f"{name}.j2").render(**view), encoding="utf-8")

    replay = capsule_dir / "replay.sh"
    replay.write_text(REPLAY_SH, encoding="utf-8")
    replay.chmod(0o755)
    return path


def missing_evidence(capsule: Capsule, capsule_dir: Path) -> list[str]:
    """Evidence paths cited anywhere in the capsule that are not in the folder."""
    cited = set()
    for model in _walk(capsule):
        cited.update(getattr(model, "evidence", None) or [])
        for field in ("junit_file", "log_file", "patch_file"):
            if getattr(model, field, None):
                cited.add(getattr(model, field))
    return sorted(p for p in cited if not (capsule_dir / p).exists())


def report_view(capsule: Capsule, missing: list[str]) -> dict:
    """Everything the templates show, already in display order."""
    c = capsule
    control = {r.name: r for r in c.control.results}
    donor = {r.name: r for r in c.donor.results}
    tests = []
    for field, label in CHANGES:
        for name in getattr(c.comparison, field):
            tests.append({
                "name": name,
                "change": label,
                "field": field,
                "control": control[name].outcome if name in control else "-",
                "donor": donor[name].outcome if name in donor else "-",
                "message": (donor.get(name) or control.get(name)).message if field == "regressions" else "",
            })
    risk = c.risk
    swap = c.proposal.replacement or "nothing"
    return {
        "c": c,
        "title": f"{c.dependency.name}: {c.proposal.action} to {swap}",
        "verdict": c.verdict,
        "reason": _row(c.verdict_reason),
        "claims": [_row(x) for x in c.claims],
        "declared": c.dependency.declared_in,
        "facts": _facts(risk),
        "findings": [_row(x) for x in risk.indicators.findings],
        "ai_summary": _row(risk.ai_summary) if risk.ai_summary else None,
        "usages": c.usages,
        "proposal": c.proposal,
        "reasoning": _row(c.proposal.reasoning),
        "attempts": [
            {
                "round": m.round,
                "label": "first draft" if m.round == 0 else f"repair round {m.round}",
                "steps": [_row(s) for s in m.steps],
                "files": sorted({e.file for e in m.edits}),
                "patch": m.patch_file,
                "applies": _row(m.patch_applies),
                "diagnosis": _row(m.diagnosis) if m.diagnosis else None,
            }
            for m in c.migration
        ],
        "sides": [c.control, *c.donor_rounds] if c.donor_rounds else [c.control, c.donor],
        "counts": [(label, len(getattr(c.comparison, field)), field) for field, label in CHANGES
                   if getattr(c.comparison, field) or field in ("newly_fixed", "regressions", "still_passing", "still_failing")],
        "tests": tests,
        "gaps": [{"tag": g.source, "text": g.description, "links": g.evidence, "severity": g.severity,
                  "status": g.status, "tests": g.related_tests} for g in c.gaps],
        "suggested": [{"tag": "OBSERVED" if t.ran else "AI", "name": t.name, "purpose": t.purpose, "code": t.code,
                       "ran": t.ran} for t in c.suggested_tests],
        "concerns": [_row(x) for x in c.concerns],
        "limitations": c.limitations,
        "gemma": c.gemma,
        "missing": missing,
    }


def _row(claim: Claim) -> dict:
    return {"tag": claim.source, "text": claim.claim, "links": claim.evidence}


def _facts(risk) -> list[tuple[str, str]]:
    i = risk.indicators
    vulns = ", ".join(f"{v.id} ({v.severity or 'unknown'})" for v in i.vulnerabilities) or "none known"
    rows = [
        ("Risk score", f"{risk.score}/100 (fixed rules, see findings)"),
        ("Latest version", i.latest_version or "unknown"),
        ("Last release", i.last_release.isoformat() if i.last_release else "unknown"),
        ("Licence", i.license or "unknown"),
        ("Known vulnerabilities", vulns),
        ("OpenSSF Scorecard", f"{i.scorecard}/10" if i.scorecard is not None else "no data"),
        ("Deprecated", {True: "yes", False: "no", None: "unknown"}[i.deprecated]),
    ]
    return rows


def _walk(obj):
    if isinstance(obj, BaseModel):
        yield obj
        for name in type(obj).model_fields:
            yield from _walk(getattr(obj, name))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk(item)
