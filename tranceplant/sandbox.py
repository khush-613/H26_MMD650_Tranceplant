"""Stage 4: Twin Sandbox Trial. Owner: Rajveer.

Build one side from the same commit (Docker, or a uv virtualenv as the
fallback), apply `patch` for the donor side, install, and run
`python -m pytest --junitxml=...`.

Write <evidence_dir>/<label>.junit.xml and <evidence_dir>/<label>.log and
return paths relative to evidence_dir.parent (the capsule folder).
Set setup_ok=False if the patch or the install fails.
"""

from pathlib import Path
from typing import Literal

from .models import Side, SideRun


def run_side(
    repo: Path,
    side: Side,
    *,
    patch: Path | None,
    evidence_dir: Path,
    label: str,
    backend: Literal["docker", "uv"] = "docker",
) -> SideRun:
    raise NotImplementedError("sandbox.run_side (owner: Rajveer)")
