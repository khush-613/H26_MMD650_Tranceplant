"""Compare pytest JUnit XML reports and calculate a rule-based verdict."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TestCaseResult:
    test_id: str
    status: str
    duration_seconds: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class TestChange:
    test_id: str
    control_status: str | None
    donor_status: str | None
    control_detail: str | None = None
    donor_detail: str | None = None


@dataclass
class Comparison:
    still_passing: list[TestChange] = field(default_factory=list)
    still_failing: list[TestChange] = field(default_factory=list)
    newly_broken: list[TestChange] = field(default_factory=list)
    newly_fixed: list[TestChange] = field(default_factory=list)
    not_comparable: list[TestChange] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    open_gaps: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.newly_broken:
            return "Rejected"
        if self.open_gaps or self.not_comparable or self.issues:
            return "Partial"
        if not self.still_passing and not self.still_failing and not self.newly_fixed:
            return "Partial"
        return "Accepted"

    def to_dict(self) -> dict[str, object]:
        return {
            "still_passing": [asdict(item) for item in self.still_passing],
            "still_failing": [asdict(item) for item in self.still_failing],
            "newly_broken": [asdict(item) for item in self.newly_broken],
            "newly_fixed": [asdict(item) for item in self.newly_fixed],
            "not_comparable": [asdict(item) for item in self.not_comparable],
            "issues": list(self.issues),
            "open_gaps": list(self.open_gaps),
            "counts": self.counts,
            "verdict": self.verdict,
        }

    @property
    def counts(self) -> dict[str, int]:
        return {
            "still_passing": len(self.still_passing),
            "still_failing": len(self.still_failing),
            "newly_broken": len(self.newly_broken),
            "newly_fixed": len(self.newly_fixed),
            "not_comparable": len(self.not_comparable),
        }


def parse_junit(path: str | Path) -> tuple[dict[str, TestCaseResult], list[str]]:
    """Read one JUnit report, preserving failure/error text as observed detail."""

    report = Path(path)
    if not report.is_file():
        return {}, [f"JUnit report is missing: {report}"]
    try:
        root = ET.parse(report).getroot()
    except (ET.ParseError, OSError) as exc:
        return {}, [f"Could not read JUnit report {report}: {exc}"]

    results: dict[str, TestCaseResult] = {}
    seen: Counter[str] = Counter()
    for case in root.iter("testcase"):
        name = case.attrib.get("name", "<unnamed>")
        classname = case.attrib.get("classname", "")
        base_id = f"{classname}::{name}" if classname else name
        seen[base_id] += 1
        test_id = base_id if seen[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")
        marker = failure if failure is not None else error
        if marker is not None:
            status = "failed"
            detail = _node_detail(marker)
        elif skipped is not None:
            status = "skipped"
            detail = _node_detail(skipped)
        else:
            status = "passed"
            detail = None
        duration = _float_or_none(case.attrib.get("time"))
        results[test_id] = TestCaseResult(test_id, status, duration, detail)
    if not results:
        return {}, [f"JUnit report contains no test cases: {report}"]
    return results, []


def compare_junit(
    control_xml: str | Path,
    donor_xml: str | Path,
    *,
    open_gaps: list[str] | tuple[str, ...] = (),
) -> Comparison:
    """Classify each test as still passing/failing, newly broken/fixed.

    Tests present on only one side are recorded separately as not comparable,
    so a changed test collection cannot be mislabeled as a fix or regression.
    """

    control, control_issues = parse_junit(control_xml)
    donor, donor_issues = parse_junit(donor_xml)
    result = Comparison(issues=control_issues + donor_issues, open_gaps=list(open_gaps))
    for test_id in sorted(control.keys() | donor.keys()):
        before = control.get(test_id)
        after = donor.get(test_id)
        change = TestChange(
            test_id=test_id,
            control_status=before.status if before else None,
            donor_status=after.status if after else None,
            control_detail=before.detail if before else None,
            donor_detail=after.detail if after else None,
        )
        if (
            before is None
            or after is None
            or before.status == "skipped"
            or after.status == "skipped"
        ):
            result.not_comparable.append(change)
        elif before.status == "passed" and after.status == "passed":
            result.still_passing.append(change)
        elif before.status == "passed" and after.status != "passed":
            result.newly_broken.append(change)
        elif before.status != "passed" and after.status == "passed":
            result.newly_fixed.append(change)
        else:
            result.still_failing.append(change)
    return result


def compare_runs(control: object, donor: object, *, open_gaps: list[str] | tuple[str, ...] = ()) -> Comparison:
    """Convenience wrapper for ``TestRun`` objects returned by sandbox.py."""

    control_path = getattr(control, "junit_xml", control)
    donor_path = getattr(donor, "junit_xml", donor)
    return compare_junit(control_path, donor_path, open_gaps=open_gaps)


def _node_detail(node: ET.Element) -> str | None:
    message = node.attrib.get("message", "").strip()
    body = (node.text or "").strip()
    detail = "\n".join(part for part in (message, body) if part)
    return detail or None


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
