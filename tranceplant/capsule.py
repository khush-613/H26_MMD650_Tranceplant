"""Stage 6: Exit Capsule. Owner: Khush.

cli.py has already written migration.patch and everything under evidence/
into capsule_dir. This writes capsule.json (validated against the schema),
capsule.md (from templates/capsule.md.j2) and replay.sh.
"""

from pathlib import Path

from .models import Capsule


def write_capsule(capsule: Capsule, capsule_dir: Path) -> Path:
    raise NotImplementedError("capsule.write_capsule (owner: Khush)")
