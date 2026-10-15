"""Run the sandbox and compare stages for the Jinja2 demo, without Gemma.

From the project root::

    python -m tranceplant.demo_jinja2 --backend auto

The runner writes the raw control/donor evidence, trial.json and
comparison.json under ``capsules/jinja2/evidence``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Sequence

from .compare import compare, counts, decide_verdict
from .sandbox import DEFAULT_TEST_COMMAND, SandboxError, run_twin_trial

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LINKWATCH = PROJECT_ROOT / "tests" / "fixtures" / "linkwatch"
MIGRATION_PATCH = PROJECT_ROOT / "demo" / "patches" / "jinja2.patch"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LinkWatch Jinja2 twin trial")
    parser.add_argument("repo", nargs="?", type=Path, default=DEFAULT_LINKWATCH)
    parser.add_argument("--evidence-dir", type=Path, default=PROJECT_ROOT / "capsules" / "jinja2" / "evidence")
    parser.add_argument("--backend", choices=("auto", "docker", "uv"), default="auto")
    parser.add_argument("--timeout", type=int, default=600, help="timeout per sandbox in seconds")
    parser.add_argument("--test-command", default=shlex.join(DEFAULT_TEST_COMMAND),
                        help='test command string (default: "python -m pytest -q")')
    args = parser.parse_args(argv)

    try:
        trial = run_twin_trial(args.repo, evidence_dir=args.evidence_dir, migration_patch=MIGRATION_PATCH,
                               test_command=args.test_command, backend=args.backend, timeout_seconds=args.timeout)
    except SandboxError as exc:
        parser.error(str(exc))

    comparison = compare(trial.control.results, trial.donor.results)
    verdict = "Rejected" if not trial.donor.setup_ok else decide_verdict(comparison, [])
    comparison_path = args.evidence_dir / "comparison.json"
    comparison_path.write_text(
        json.dumps({**comparison.model_dump(), "counts": counts(comparison), "verdict": verdict}, indent=2) + "\n",
        encoding="utf-8",
    )
    for run in (trial.control, trial.donor):
        print(f"{run.side}: backend={run.backend}, python={run.python_version}, setup_ok={run.setup_ok}, "
              f"exit={run.exit_code}, log={run.log_file}")
    print(f"Verdict: {verdict}")
    print(f"Counts: {counts(comparison)}")
    print(f"Comparison file: {comparison_path}")
    return 1 if verdict == "Rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
