"""Stage 1: Scan. Owner: Khush.

Read requirements*.txt and pyproject.toml, spot pins that disagree, and fetch
risk data (PyPI JSON, OSV.dev, deps.dev). Facts here are OBSERVED; the raw API
responses go in DependencyRisk.raw_sources (written to evidence/risk-sources.json).
"""

from pathlib import Path

from .models import ScanReport


def scan_repo(repo: Path) -> ScanReport:
    raise NotImplementedError("scan.scan_repo (owner: Khush)")
