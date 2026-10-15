from pathlib import Path

from tranceplant.scan import pinned_declaration, read_declarations

LINKWATCH = Path(__file__).parent / "fixtures" / "linkwatch"


def where(decls):
    return [(d.file, d.line, d.spec) for d in decls]


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
