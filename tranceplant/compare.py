"""Stage 5: Compare. Owner: Rajveer.

Per-test diff of control vs donor, and the verdict rules (no AI involved):
  Accepted = zero regressions and no open gaps
  Partial  = zero regressions but at least one open gap
  Rejected = at least one regression left after the repair rounds
"""

from .models import Comparison, Gap, TestResult, Verdict


def compare(control: list[TestResult], donor: list[TestResult]) -> Comparison:
    raise NotImplementedError("compare.compare (owner: Rajveer)")


def decide_verdict(comparison: Comparison, gaps: list[Gap]) -> Verdict:
    raise NotImplementedError("compare.decide_verdict (owner: Rajveer)")
