import io
import shutil
import textwrap
import zipfile
from pathlib import Path

import httpx
import pytest

from tranceplant import scan
from tranceplant.scan import EVIDENCE, pinned_declaration, read_declarations, removed_imports, scan_repo

FIXTURES = Path(__file__).parent / "fixtures"
LINKWATCH = FIXTURES / "linkwatch"


def where(decls):
    return [(d.file, d.line, d.spec) for d in decls]


def no_network(*args, **kwargs):
    raise httpx.ConnectError("tests never touch the network")


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Recorded PyPI, OSV.dev and deps.dev responses for LinkWatch; any cache miss fails the request."""
    shutil.copytree(FIXTURES / "scan-cache", tmp_path / "home" / "cache")
    monkeypatch.setenv("TRANCEPLANT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(scan, "_get", no_network)
    monkeypatch.setattr(scan, "_get_bytes", no_network)


@pytest.fixture
def report(offline):
    return scan_repo(LINKWATCH)


def risk(report, name):
    return report.get(name)


def findings(report, name):
    return [c.claim for c in risk(report, name).indicators.findings]


def test_declarations_from_both_files():
    decls = read_declarations(LINKWATCH)
    assert where(decls["pydantic"]) == [
        ("requirements.txt", 5, "pydantic==1.10.13"),
        ("pyproject.toml", 17, "pydantic>=2"),
    ]
    assert where(decls["setuptools"]) == [
        ("requirements.txt", 9, "setuptools==75.8.0"),
        ("pyproject.toml", 21, "setuptools==75.8.0"),
        ("pyproject.toml", 2, "setuptools>=61"),
    ]
    assert where(decls["nose"]) == [("requirements-dev.txt", 2, "nose==1.3.7")]
    assert "pyyaml" not in decls  # commented out
    assert "certifi" not in decls  # imported, never declared


def test_requirements_pin_wins_over_pyproject_range():
    decl, version = pinned_declaration(read_declarations(LINKWATCH)["pydantic"])
    assert (decl.file, version) == ("requirements.txt", "1.10.13")
    _, version = pinned_declaration(read_declarations(LINKWATCH)["six"])
    assert version is None


def test_nose_and_jinja2_top_the_table(report):
    assert [d.dependency.name for d in report.dependencies[:2]] == ["nose", "jinja2"]
    assert [d.score for d in report.dependencies] == sorted((d.score for d in report.dependencies), reverse=True)
    assert report.python_requires == ">=3.10"


def test_nose_imports_a_module_python_312_removed(report):
    assert findings(report, "nose")[0] == (
        "nose 1.3.7 imports `imp` (nose/importer.py:12), which Python 3.12 removed, "
        "so importing it fails on Python 3.12"
    )
    assert risk(report, "nose").indicators.last_release.isoformat() == "2015-06-02"


def test_jinja2_vulnerabilities_are_merged_across_osv_ids(report):
    jinja = risk(report, "jinja2")
    assert {v.id for v in jinja.indicators.vulnerabilities} == {
        "CVE-2025-27516", "CVE-2024-56326", "CVE-2024-34064", "CVE-2024-22195"}
    assert all(v.severity == "medium" and v.fixed_in for v in jinja.indicators.vulnerabilities)
    assert jinja.indicators.latest_version == "3.1.6"
    assert any("accepts any MarkupSafe>=0.23 with no upper bound" in f for f in findings(report, "jinja2"))


def test_pydantic_pins_disagree(report):
    [conflict] = report.conflicts
    assert conflict.claim == "requirements.txt:5 pins pydantic==1.10.13, which pyproject.toml:17 (`pydantic>=2`) does not allow"
    assert conflict.claim in findings(report, "pydantic")


def test_undeclared_and_unused(report):
    certifi = risk(report, "certifi")
    assert certifi.dependency.imported_but_undeclared and certifi.dependency.declared_in == []
    assert findings(report, "certifi") == [
        "certifi is imported (linkwatch/checker.py:4) but not declared in any requirements file or pyproject.toml; "
        "it installs only because requests 2.31.0 requires certifi>=2017.4.17"
    ]
    assert any("never imported or run" in f for f in findings(report, "six"))
    assert not any("never imported or run" in f for f in findings(report, "pytest"))  # run by the Makefile


def test_private_api_and_monkeypatch_raise_the_requests_score(report):
    assert "The repo calls private `requests.utils._parse_content_type_header` " \
           "(linkwatch/encoding.py:5, linkwatch/encoding.py:11)" in findings(report, "requests")
    assert "The repo rewrites `requests.Session.request` (linkwatch/http_patch.py:14)" in findings(report, "requests")


def test_every_finding_is_observed_and_the_score_adds_up(report):
    for d in report.dependencies:
        assert all(c.source == "OBSERVED" and c.evidence == [EVIDENCE] for c in d.indicators.findings)
        breakdown = d.raw_sources["score"]
        assert [b["finding"] for b in breakdown] == [c.claim for c in d.indicators.findings]
        assert d.score == min(100, sum(b["points"] for b in breakdown))
        assert d.raw_sources["errors"] == {}


def test_offline_with_no_cache_still_lists_every_dependency(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANCEPLANT_HOME", str(tmp_path))
    monkeypatch.setattr(scan, "_get", no_network)
    report = scan_repo(LINKWATCH)
    names = {d.dependency.name for d in report.dependencies}
    assert {"jinja2", "nose", "toml", "certifi"} <= names
    assert report.get("jinja2").raw_sources["errors"]  # the failures are recorded, not hidden
    assert report.conflicts  # declarations need no network


def wheel(**files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, source in files.items():
            zf.writestr(name, textwrap.dedent(source))
    return buf.getvalue()


def test_removed_imports_skip_guarded_fallbacks(monkeypatch, tmp_path):
    monkeypatch.setenv("TRANCEPLANT_HOME", str(tmp_path))
    data = wheel(**{"pkg/__init__.py": """\
        import imp
        try:
            import asynchat
        except ImportError:
            import smtpd
        if False:
            import asyncore

        def later():
            import asyncore
        """})
    monkeypatch.setattr(scan, "_get_bytes", lambda url: data)
    pypi = {"urls": [{"filename": "pkg-1.0-py3-none-any.whl", "size": len(data), "url": "https://x"}]}
    assert [(h["module"], h["line"], h["module_level"]) for h in removed_imports("pkg", "1.0", pypi)] == [
        ("imp", 1, True), ("asyncore", 10, False)]
