"""The capsule schema: the shared contract every stage codes against.

Frozen at checkpoint 1. Change it only after telling the whole team.

Rule that runs through everything: each fact carries a source tag.
OBSERVED means the tool ran or read it and links to the raw evidence.
AI means Gemma 4 suggested it and nobody has verified it yet.
The tool, never the model, decides which tag a fact gets.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

Source = Literal["OBSERVED", "AI"]
OBSERVED: Source = "OBSERVED"
AI: Source = "AI"

Severity = Literal["low", "medium", "high", "critical"]
Verdict = Literal["Accepted", "Partial", "Rejected"]
Outcome = Literal["passed", "failed", "error", "skipped"]
Side = Literal["control", "donor"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Claims -----------------------------------------------------------------


class Claim(Model):
    """One tagged statement. Evidence paths are relative to the capsule folder."""

    claim: str
    source: Source
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _observed_needs_evidence(self) -> Claim:
        if self.source == OBSERVED and not self.evidence:
            raise ValueError("an OBSERVED claim must link to at least one evidence file")
        return self

    @classmethod
    def observed(cls, claim: str, *evidence: str) -> Claim:
        return cls(claim=claim, source=OBSERVED, evidence=list(evidence))

    @classmethod
    def ai(cls, claim: str, *evidence: str) -> Claim:
        return cls(claim=claim, source=AI, evidence=list(evidence))


# --- Stage 1: Scan ----------------------------------------------------------


class Declaration(Model):
    """Where a dependency is declared, e.g. requirements.txt line 4 `Jinja2==2.11.3`."""

    file: str
    spec: str
    line: int | None = None


class Dependency(Model):
    name: str  # canonical name (packaging.utils.canonicalize_name)
    pinned: str | None = None  # exact version if pinned with ==, else None
    declared_in: list[Declaration] = Field(default_factory=list)
    imported_but_undeclared: bool = False  # e.g. certifi in checker.py


class Vulnerability(Model):
    id: str
    summary: str = ""
    severity: str | None = None
    fixed_in: list[str] = Field(default_factory=list)


class RiskIndicators(Model):
    """Raw facts from PyPI, OSV.dev, deps.dev and our own runs. All OBSERVED."""

    latest_version: str | None = None
    last_release: date | None = None
    license: str | None = None
    vulnerabilities: list[Vulnerability] = Field(default_factory=list)
    scorecard: float | None = None  # OpenSSF Scorecard, 0-10
    deprecated: bool | None = None
    findings: list[Claim] = Field(default_factory=list)  # e.g. "nose crashes on Python 3.12"


class DependencyRisk(Model):
    """One row of `tranceplant scan`."""

    dependency: Dependency
    indicators: RiskIndicators = Field(default_factory=RiskIndicators)
    score: int = Field(0, ge=0, le=100)  # rule-based, higher = riskier
    ai_summary: Claim | None = None  # filled by Gemma (explain_risk), always AI
    ai_severity: Severity | None = None
    raw_sources: dict = Field(default_factory=dict)  # PyPI / OSV / deps.dev responses


class ScanReport(Model):
    repo: str
    python_requires: str | None = None
    dependencies: list[DependencyRisk] = Field(default_factory=list)
    conflicts: list[Claim] = Field(default_factory=list)  # e.g. pydantic 1.10 vs >=2

    def get(self, name: str) -> DependencyRisk | None:
        from packaging.utils import canonicalize_name

        wanted = canonicalize_name(name)
        for item in self.dependencies:
            if canonicalize_name(item.dependency.name) == wanted:
                return item
        return None


# --- Stage 2: Usage X-Ray ---------------------------------------------------

UsageKind = Literal[
    "import",  # import requests
    "from_import",  # from requests import Session
    "alias",  # from requests import Session as S
    "attribute",  # requests.exceptions.Timeout
    "private_api",  # requests.utils._parse_content_type_header
    "monkeypatch",  # requests.Session.request = _request
    "transitive",  # Jinja2 depends on MarkupSafe
    "config",  # Makefile / Dockerfile / CI mentions
]


class Usage(Model):
    file: str
    line: int
    kind: UsageKind
    symbol: str
    code: str = ""  # the source line
    note: Claim | None = None  # Gemma's plain-English note, always AI


# --- Stage 3: Propose -------------------------------------------------------

Action = Literal["replace", "upgrade", "remove", "add", "stdlib"]


class Alternative(Model):
    name: str
    why_not: str = ""


class Proposal(Model):
    dependency: str
    action: Action
    replacement: str | None = None  # package name, stdlib module, or None for remove
    target_spec: str | None = None  # e.g. "Jinja2>=3.1,<4"
    reasoning: Claim  # AI
    alternatives: list[Alternative] = Field(default_factory=list)
    model: str


class CodeEdit(Model):
    """Replace one exact snippet in one file. An empty `find` on a missing file creates it."""

    file: str
    find: str
    replace: str
    reason: str = ""


class MigrationAttempt(Model):
    round: int = Field(ge=0)  # 0 = first draft, 1..2 = repair rounds
    steps: list[Claim] = Field(default_factory=list)  # AI
    edits: list[CodeEdit] = Field(default_factory=list)  # this round's edits only
    patch_file: str  # cumulative patch from the original commit, relative to capsule
    patch_applies: Claim  # OBSERVED: result of `git apply --check`
    diagnosis: Claim | None = None  # repair rounds: Gemma's read of the failure, AI


# --- Stage 4: Twin Sandbox Trial --------------------------------------------


class TestResult(Model):
    __test__ = False  # not a pytest class

    name: str  # pytest node id, e.g. tests/test_report.py::test_report_renders_all_sites
    outcome: Outcome
    duration: float = 0.0
    message: str = ""


class SideRun(Model):
    side: Side
    label: str  # file stem, e.g. "control", "donor.round1"
    command: str
    exit_code: int | None = None
    setup_ok: bool = True  # dependency install / patch apply succeeded
    results: list[TestResult] = Field(default_factory=list)
    junit_file: str | None = None  # relative to capsule folder
    log_file: str | None = None
    python_version: str | None = None
    backend: Literal["docker", "uv"] = "docker"


# --- Stage 5: Compare -------------------------------------------------------


class Comparison(Model):
    still_passing: list[str] = Field(default_factory=list)
    still_failing: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)  # passed in control, broken in donor
    newly_fixed: list[str] = Field(default_factory=list)  # broken in control, passes in donor
    only_in_control: list[str] = Field(default_factory=list)
    only_in_donor: list[str] = Field(default_factory=list)


class Gap(Model):
    description: str
    source: Source
    evidence: list[str] = Field(default_factory=list)
    severity: Severity = "medium"
    status: Literal["open", "resolved"] = "open"
    related_tests: list[str] = Field(default_factory=list)


class SuggestedTest(Model):
    name: str
    purpose: str
    code: str | None = None
    ran: bool = False
    source: Source = AI  # becomes OBSERVED only if we actually ran it


# --- Stage 6: Exit Capsule --------------------------------------------------


class RepoInfo(Model):
    path: str
    commit: str | None = None
    python_version: str | None = None
    test_command: str = "python -m pytest --junitxml=<file>"


class GemmaInfo(Model):
    model_family: str = "Gemma 4"
    models: list[str] = Field(default_factory=list)
    access: str = "Gemini API (Google AI Studio) via the google-genai Python SDK"
    terms: str = "Gemma 4 licence (Apache 2.0) plus the Gemini API Terms of Service"
    calls: int = 0
    cache_hits: int = 0


KNOWN_LIMITATIONS = [
    "We can only verify what the repo's own tests cover; behaviour without a test is unchecked.",
    "Only Python and pip-style dependencies are handled.",
    "Trials run on Linux only; Windows and macOS behaviour (such as per-user cache folders) is unverified.",
    "Suggested extra tests are listed but stay tagged AI unless we actually ran them.",
]


class Capsule(Model):
    schema_version: str = SCHEMA_VERSION
    tool_version: str
    created_at: datetime = Field(default_factory=_now)
    repo: RepoInfo
    dependency: Dependency
    risk: DependencyRisk
    usages: list[Usage] = Field(default_factory=list)
    proposal: Proposal
    migration: list[MigrationAttempt] = Field(default_factory=list)  # all rounds, in order
    control: SideRun
    donor: SideRun  # the final donor run
    donor_rounds: list[SideRun] = Field(default_factory=list)  # every donor run, in order
    comparison: Comparison
    gaps: list[Gap] = Field(default_factory=list)
    suggested_tests: list[SuggestedTest] = Field(default_factory=list)
    concerns: list[Claim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=lambda: list(KNOWN_LIMITATIONS))
    verdict: Verdict
    verdict_reason: Claim  # OBSERVED: computed by fixed rules from the comparison
    claims: list[Claim] = Field(default_factory=list)  # headline claims for the report
    gemma: GemmaInfo = Field(default_factory=GemmaInfo)

    @property
    def final_patch(self) -> str | None:
        return self.migration[-1].patch_file if self.migration else None


def capsule_json_schema() -> dict:
    return Capsule.model_json_schema()
