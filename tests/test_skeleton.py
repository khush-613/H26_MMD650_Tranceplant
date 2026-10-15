import importlib

import pytest

MODULES = ["models", "scan", "xray", "gemma", "propose", "sandbox", "compare", "capsule", "cli"]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(f"tranceplant.{name}")


def test_capsule_schema_builds():
    from tranceplant.models import capsule_json_schema

    assert capsule_json_schema()["title"] == "Capsule"
