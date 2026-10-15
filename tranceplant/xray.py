"""Stage 2: Usage X-Ray. Owner: Khush.

Walk the repo's syntax tree (ast) and find every use of one dependency:
imports, aliases, attribute chains, private APIs, monkeypatches, and hidden
partner libraries. Gemma's one-line notes are added later by propose.annotate_usages.
"""

from pathlib import Path

from .models import Usage


def find_usages(repo: Path, dep: str) -> list[Usage]:
    raise NotImplementedError("xray.find_usages (owner: Khush)")
