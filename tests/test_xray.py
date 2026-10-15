import textwrap
from pathlib import Path

import httpx
import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from tranceplant import scan
from tranceplant.xray import find_usages, import_names, imported_modules

LINKWATCH = Path(__file__).parent / "fixtures" / "linkwatch"

REQUIRES_DIST = {
    "jinja2": ["MarkupSafe>=0.23", 'Babel>=0.8; extra == "i18n"'],
    "requests": [
        "charset-normalizer<4,>=2",
        "idna<4,>=2.5",
        "urllib3<3,>=1.21.1",
        "certifi>=2017.4.17",
        'PySocks!=1.5.7,>=1.5.6; extra == "socks"',
        'chardet<6,>=3.0.2; extra == "use-chardet-on-py3"',
    ],
}


@pytest.fixture(autouse=True)
def pypi(monkeypatch):
    calls = []

    def fake(name, version=None):
        calls.append((canonicalize_name(name), version))
        return {"info": {"requires_dist": REQUIRES_DIST.get(canonicalize_name(name), [])}}

    monkeypatch.setattr(scan, "pypi_json", fake)
    return calls


def found(dep, kind=None):
    return {(u.file, u.line, u.kind, u.symbol) for u in find_usages(LINKWATCH, dep) if kind in (None, u.kind)}


def test_requests_alias_private_api_and_monkeypatch():
    assert found("requests") - found("requests", "transitive") == {
        ("linkwatch/checker.py", 5, "import", "requests"),
        ("linkwatch/checker.py", 6, "alias", "requests.Session"),
        # `session = S()` never mentions "requests"; grep misses this one
        ("linkwatch/checker.py", 13, "attribute", "requests.Session"),
        ("linkwatch/checker.py", 35, "attribute", "requests.exceptions.ConnectionError"),
        ("linkwatch/checker.py", 38, "attribute", "requests.exceptions.Timeout"),
        ("linkwatch/encoding.py", 5, "private_api", "requests.utils._parse_content_type_header"),
        ("linkwatch/encoding.py", 11, "private_api", "requests.utils._parse_content_type_header"),
        ("linkwatch/http_patch.py", 1, "import", "requests"),
        ("linkwatch/http_patch.py", 5, "attribute", "requests.Session.request"),
        ("linkwatch/http_patch.py", 14, "monkeypatch", "requests.Session.request"),
    }


def test_requests_partners_come_from_the_pinned_version(pypi):
    partners = find_usages(LINKWATCH, "requests")
    partners = [u for u in partners if u.kind == "transitive"]
    assert {Requirement(u.symbol).name for u in partners} == {"charset-normalizer", "idna", "urllib3", "certifi"}
    assert {(u.file, u.line, u.code) for u in partners} == {("requirements.txt", 3, "requests==2.31.0")}
    assert pypi == [("requests", "2.31.0")]


def test_jinja2_hidden_partner_markupsafe():
    assert found("Jinja2", "transitive") == {("requirements.txt", 4, "transitive", "MarkupSafe>=0.23")}
    assert ("linkwatch/report.py", 9, "attribute", "jinja2.Environment") in found("jinja2")


def test_pydantic_v1_api_through_from_import():
    assert found("pydantic") == {
        ("linkwatch/config.py", 4, "from_import", "pydantic.BaseModel"),
        ("linkwatch/config.py", 4, "from_import", "pydantic.validator"),
        ("linkwatch/config.py", 7, "attribute", "pydantic.BaseModel"),
        ("linkwatch/config.py", 14, "attribute", "pydantic.validator"),
        ("linkwatch/config.py", 20, "attribute", "pydantic.validator"),
        ("linkwatch/config.py", 27, "attribute", "pydantic.BaseModel"),
    }


def test_setuptools_is_used_as_pkg_resources_and_build_backend():
    assert found("setuptools") == {
        ("linkwatch/resources.py", 1, "import", "pkg_resources"),
        ("linkwatch/resources.py", 7, "attribute", "pkg_resources.resource_filename"),
        ("pyproject.toml", 3, "config", "setuptools"),
        ("pyproject.toml", 30, "config", "setuptools"),
        ("pyproject.toml", 33, "config", "setuptools"),
    }


def test_nose_is_only_run_from_the_makefile():
    assert found("nose") == {("Makefile", 14, "config", "nosetests")}


def test_toml_and_appdirs_calls():
    assert ("linkwatch/config.py", 37, "attribute", "toml.load") in found("toml")
    assert ("linkwatch/cache.py", 10, "attribute", "appdirs.user_cache_dir") in found("appdirs")


def test_declared_but_unused():
    assert find_usages(LINKWATCH, "six") == []


def test_offline_pypi_still_returns_code_usages(monkeypatch):
    def down(name, version=None):
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(scan, "pypi_json", down)
    assert found("Jinja2", "transitive") == set()
    assert found("Jinja2")


def test_imported_modules_skips_stdlib_and_local_code():
    assert set(imported_modules(LINKWATCH)) == {
        "appdirs", "certifi", "chardet", "click", "dateutil", "jinja2",
        "pkg_resources", "pydantic", "pytest", "requests", "rich", "toml",
    }


def test_import_names():
    assert import_names("Jinja2") == ["jinja2"]
    assert import_names("python-dateutil") == ["dateutil"]
    assert "pkg_resources" in import_names("setuptools")


def test_patches_dynamic_imports_and_module_aliases(tmp_path):
    (tmp_path / "test_patching.py").write_text(textwrap.dedent("""\
        import importlib
        from unittest import mock

        import requests
        import requests.adapters as ad


        def test_patches(monkeypatch, fake):
            setattr(requests.Session, "send", fake)
            monkeypatch.setattr(requests, "get", fake)
            with mock.patch("requests.post"):
                pass
            del requests.codes
            ad.DEFAULT_RETRIES += 1
            importlib.import_module("requests.compat")
        """))
    assert {(u.line, u.kind, u.symbol) for u in find_usages(tmp_path, "requests")} == {
        (4, "import", "requests"),
        (5, "alias", "requests.adapters"),
        (9, "monkeypatch", "requests.Session.send"),
        (10, "monkeypatch", "requests.get"),
        (11, "monkeypatch", "requests.post"),
        (13, "monkeypatch", "requests.codes"),
        (14, "monkeypatch", "requests.adapters.DEFAULT_RETRIES"),
        (15, "import", "requests.compat"),
    }


def test_unparseable_files_are_skipped(tmp_path):
    (tmp_path / "py2.py").write_text("print 'hello'\nimport requests\n")
    (tmp_path / "ok.py").write_text("import requests\n")
    assert [(u.file, u.line) for u in find_usages(tmp_path, "requests")] == [("ok.py", 1)]
