import html
import json
import os
import shutil
from pathlib import Path

import pytest

from tranceplant.capsule import missing_evidence, write_capsule
from tranceplant.models import Capsule, Claim

FIXTURE = Path(__file__).parent / "fixtures" / "capsule-jinja2"  # from a real jinja2 trial in Docker


@pytest.fixture
def folder(tmp_path):
    target = tmp_path / "jinja2"
    shutil.copytree(FIXTURE, target)
    return target


@pytest.fixture
def capsule():
    return Capsule.model_validate_json((FIXTURE / "capsule.json").read_text())


def claims(obj):
    if isinstance(obj, Claim):
        yield obj
    elif hasattr(type(obj), "model_fields"):
        for name in type(obj).model_fields:
            yield from claims(getattr(obj, name))
    elif isinstance(obj, list):
        for item in obj:
            yield from claims(item)


def test_writes_every_capsule_file(folder, capsule):
    path = write_capsule(capsule, folder)
    assert path == folder / "capsule.json"
    assert Capsule.model_validate_json(path.read_text()) == capsule
    assert json.loads((folder / "capsule.schema.json").read_text())["title"] == "Capsule"
    for name in ("capsule.md", "capsule.html", "replay.sh", "migration.patch"):
        assert (folder / name).is_file()


def test_every_claim_appears_with_its_tag(folder, capsule):
    write_capsule(capsule, folder)
    md = (folder / "capsule.md").read_text()
    page = (folder / "capsule.html").read_text()
    found = list(claims(capsule))
    assert {c.source for c in found} == {"OBSERVED", "AI"}
    for c in found:
        assert f"`{c.source}` {c.claim}" in md
        assert f'<span class="tag {c.source.lower()}">{c.source}</span> {html.escape(c.claim)}' in page
        for path in c.evidence:
            assert f"[{path}]({path})" in md


def test_report_shows_the_verdict_and_per_test_changes(folder, capsule):
    write_capsule(capsule, folder)
    md = (folder / "capsule.md").read_text()
    assert md.startswith("# Dependency Exit Capsule: jinja2\n\n**Verdict: Accepted** `OBSERVED`")
    assert "| `tests/test_report.py::test_report_renders_all_sites` | failed | passed | **newly fixed** |" in md
    assert "**1 newly fixed** · 0 newly broken · 0 still failing · 9 still passing" in md
    assert "| `linkwatch/report.py:9` | attribute | `jinja2.Environment` |" in md
    assert "Evidence check" not in md


def test_html_escapes_model_text(folder, capsule):
    write_capsule(capsule, folder)
    page = (folder / "capsule.html").read_text()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_missing_evidence_is_listed_not_hidden(folder, capsule):
    (folder / "evidence" / "donor.junit.xml").unlink()
    assert missing_evidence(capsule, folder) == ["evidence/donor.junit.xml"]
    write_capsule(capsule, folder)
    assert "## Evidence check" in (folder / "capsule.md").read_text()
    assert "- `evidence/donor.junit.xml`" in (folder / "capsule.md").read_text()


def test_replay_script_reruns_this_folder(folder, capsule):
    write_capsule(capsule, folder)
    script = folder / "replay.sh"
    assert os.access(script, os.X_OK)
    text = script.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert 'exec tranceplant replay "$here" "$@"' in text
