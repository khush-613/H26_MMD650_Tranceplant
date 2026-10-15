"""Stage 5: Compare. Owner: Rajveer.

Per-test diff of control vs donor from pytest JUnit XML, and the verdict
rules. No AI is involved here, so everything this module says is OBSERVED.

  Accepted = zero regressions, no open gaps, and every test compared on both sides
  Partial  = zero regressions, but an open gap or tests that only ran on one side
  Rejected = at least one regression left after the repair rounds
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .models import Comparison, Gap, Outcome, TestResult, Verdict

MAX_MESSAGE = 2000


def parse_junit(path: str | Path, *, repo: str | Path | None = None) -> tuple[list[TestResult], list[str]]:
    """Read one JUnit report. Returns the results and any problems reading it.

    With `repo`, names become pytest node ids (tests/test_x.py::test_y) by
    finding the module file behind the dotted classname.
    """
    report = Path(path)
    if not report.is_file():
        return [], [f"JUnit report is missing: {report}"]
    try:
        root = ET.parse(report).getroot()
    except (ET.ParseError, OSError) as exc:
        return [], [f"Could not read JUnit report {report}: {exc}"]

    results: list[TestResult] = []
    seen: Counter[str] = Counter()
    for case in root.iter("testcase"):
        base_id = _node_id(case.attrib.get("classname", ""), case.attrib.get("name", "<unnamed>"), repo)
        seen[base_id] += 1
        test_id = base_id if seen[base_id] == 1 else f"{base_id}#{seen[base_id]}"
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        outcome: Outcome
        if failure is not None:
            outcome, detail = "failed", _node_detail(failure)
        elif error is not None:
            outcome, detail = "error", _node_detail(error)
        elif skipped is not None:
            outcome, detail = "skipped", _node_detail(skipped)
        else:
            outcome, detail = "passed", ""
        results.append(TestResult(name=test_id, outcome=outcome, duration=_float(case.attrib.get("time")),
                                  message=detail[:MAX_MESSAGE]))
    if not results:
        return [], [f"JUnit report contains no test cases: {report}"]
    return results, []


def compare(control: list[TestResult], donor: list[TestResult]) -> Comparison:
    """Sort every test into one group. A skipped test counts as not passing:
    a test that passed before and is skipped after the swap is a regression.
    Tests present on only one side are kept apart, so a changed test
    collection cannot be mislabelled as a fix or a regression.
    """
    before = {r.name: r.outcome == "passed" for r in control}
    after = {r.name: r.outcome == "passed" for r in donor}
    result = Comparison()
    for name in sorted(before.keys() | after.keys()):
        if name not in after:
            result.only_in_control.append(name)
        elif name not in before:
            result.only_in_donor.append(name)
        elif before[name] and after[name]:
            result.still_passing.append(name)
        elif before[name]:
            result.regressions.append(name)
        elif after[name]:
            result.newly_fixed.append(name)
        else:
            result.still_failing.append(name)
    return result


def decide_verdict(comparison: Comparison, gaps: list[Gap]) -> Verdict:
    if comparison.regressions:
        return "Rejected"
    compared = comparison.still_passing + comparison.still_failing + comparison.newly_fixed
    if any(g.status == "open" for g in gaps) or comparison.only_in_control or comparison.only_in_donor or not compared:
        return "Partial"
    return "Accepted"


def compare_junit(control_xml: str | Path, donor_xml: str | Path, *, repo: str | Path | None = None) -> Comparison:
    """Convenience: compare two JUnit files directly."""
    control, _ = parse_junit(control_xml, repo=repo)
    donor, _ = parse_junit(donor_xml, repo=repo)
    return compare(control, donor)


def counts(comparison: Comparison) -> dict[str, int]:
    return {k: len(v) for k, v in comparison.model_dump().items()}


def _node_id(classname: str, name: str, repo: str | Path | None) -> str:
    if not classname:
        return name
    parts = classname.split(".")
    if repo is not None:
        for i in range(len(parts), 0, -1):
            module = "/".join(parts[:i]) + ".py"
            if (Path(repo) / module).is_file():
                return "::".join([module, *parts[i:], name])
    return f"{classname}::{name}"


def _node_detail(node: ET.Element) -> str:
    message = node.attrib.get("message", "").strip()
    body = (node.text or "").strip()
    return "\n".join(part for part in (message, body) if part)


def _float(value: str | None) -> float:
    try:
        return float(value) if value is not None else 0.0
    except ValueError:
        return 0.0
