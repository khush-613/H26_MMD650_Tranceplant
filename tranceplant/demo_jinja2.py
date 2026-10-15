"""Run the Rajveer-owned sandbox and compare stages for the Jinja2 demo.

From the project root::

    python -m tranceplant.demo_jinja2 --backend docker

The runner writes the raw control/donor evidence and a comparison.json under
``capsules/jinja2``. This can be handed to the capsule stage as observed input.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Sequence

from .compare import compare_runs
from .sandbox import DEFAULT_TEST_COMMAND, SandboxError, run_twin_trial


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LINKWATCH = Path(__file__).resolve().parents[3] / "LinkWatch"
MIGRATION_PATCH = PROJECT_ROOT / "demo" / "patches" / "jinja2.patch"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LinkWatch Jinja2 twin trial")
    parser.add_argument("repo", nargs="?", type=Path, default=DEFAULT_LINKWATCH)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=PROJECT_ROOT / "capsules" / "jinja2" / "evidence",
    )
    parser.add_argument("--backend", choices=("auto", "docker", "uv"), default="auto")
    parser.add_argument("--timeout", type=int, default=600, help="timeout per sandbox in seconds")
    parser.add_argument(
        "--test-command",
        default=shlex.join(DEFAULT_TEST_COMMAND),
        help='test command string (default: "python -m pytest -q")',
    )
    args = parser.parse_args(argv)

    try:
        trial = run_twin_trial(
            args.repo,
            evidence_dir=args.evidence_dir,
            migration_patch=MIGRATION_PATCH,
            test_command=args.test_command,
            backend=args.backend,
            timeout_seconds=args.timeout,
        )
    except SandboxError as exc:
        parser.error(str(exc))

    comparison = compare_runs(trial.control, trial.donor)
    comparison_path = args.evidence_dir / "comparison.json"
    comparison_path.parent.mkdir(parents=True, exist_ok=True)
    comparison_path.write_text(
        json.dumps(comparison.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"Backend: {trial.backend}")
    if trial.backend_note:
        print(f"Backend note: {trial.backend_note}")
    print(f"Control: exit={trial.control.exit_code}, log={trial.control.log}")
    print(f"Donor: exit={trial.donor.exit_code}, log={trial.donor.log}")
    print(f"Comparison: {comparison.verdict}")
    print(f"Counts: {comparison.counts}")
    print(f"Evidence: {args.evidence_dir}")
    print(f"Comparison file: {comparison_path}")
    return 1 if comparison.verdict == "Rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
