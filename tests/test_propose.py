import json
import subprocess

import pytest

from helpers import ScriptedBackend, copy_repo, find_usages, scan_repo
from tranceplant import propose
from tranceplant.gemma import GemmaClient, GemmaError
from tranceplant.models import CodeEdit, Comparison, Usage
from tranceplant.propose import (
    EditError,
    Workspace,
    apply_edits,
    context_files,
    git_apply_check,
    make_patch,
    render_files,
)


def E(file, find, replace):
    return CodeEdit(file=file, find=find, replace=replace)


def client(tmp_path, *replies):
    backend = ScriptedBackend(*replies)
    return GemmaClient("live", cache_dir=tmp_path / "cache", backend=backend, sleep=lambda s: None), backend


def git_apply(repo, patch_text):
    subprocess.run(["git", "apply", "-"], cwd=repo, input=patch_text, text=True, check=True)


# --- apply_edits ------------------------------------------------------------


def test_exact_edit(linkwatch):
    out = apply_edits(linkwatch, [E("requirements.txt", "Jinja2==2.11.3", "Jinja2>=3.1,<4")])
    assert list(out) == ["requirements.txt"]
    assert "Jinja2>=3.1,<4" in out["requirements.txt"] and "2.11.3" not in out["requirements.txt"]


def test_sequential_edits_to_one_file(linkwatch):
    out = apply_edits(linkwatch, [E("linkwatch/config.py", "import toml\n", "import tomllib\n"),
                                  E("linkwatch/config.py", "    data = toml.load(path)",
                                    '    with open(path, "rb") as fh:\n        data = tomllib.load(fh)')])
    assert "tomllib.load(fh)" in out["linkwatch/config.py"]


def test_ambiguous_find_is_rejected(linkwatch):
    with pytest.raises(EditError, match="appears 2 times"):
        apply_edits(linkwatch, [E("linkwatch/config.py", "    @validator(", "    @field_validator(")])


def test_missing_find_is_rejected_with_a_hint(linkwatch):
    with pytest.raises(EditError, match="not found in linkwatch/cache.py.*from appdir import"):
        apply_edits(linkwatch, [E("linkwatch/cache.py", "from appdir import user_cache_dir", "x")])


def test_all_errors_are_reported_together(linkwatch):
    with pytest.raises(EditError) as info:
        apply_edits(linkwatch, [E("nope.py", "a", "b"), E("requirements.txt", "", "x"), E("../etc/passwd", "a", "b")])
    msg = str(info.value)
    assert "edit 1" in msg and "edit 2" in msg and "edit 3" in msg


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.txt", "a/../../b"])
def test_paths_must_stay_inside_the_repo(linkwatch, path):
    with pytest.raises(EditError, match="inside"):
        apply_edits(linkwatch, [E(path, "", "x")])


def test_trailing_whitespace_differences_are_tolerated(linkwatch):
    out = apply_edits(linkwatch, [E("linkwatch/cache.py", "    os.makedirs(path, exist_ok=True)  \n    return path   ", "    os.makedirs(path, exist_ok=True)\n    return str(path)")])
    assert "    os.makedirs(path, exist_ok=True)\n    return str(path)\n\n" in out["linkwatch/cache.py"]


def test_indentation_differences_are_tolerated_and_reindented(linkwatch):
    find = "with open(path, encoding=\"utf-8\") as fh:\n    return json.load(fh)"
    replace = "with open(path, encoding=\"utf-8\") as fh:\n    data = json.load(fh)\nreturn data"
    out = apply_edits(linkwatch, [E("linkwatch/cache.py", find, replace)])
    assert "        data = json.load(fh)\n    return data\n" in out["linkwatch/cache.py"]


def test_new_file_is_created_with_empty_find(linkwatch):
    out = apply_edits(linkwatch, [E("linkwatch/compat.py", "", "import sys\n")])
    assert out == {"linkwatch/compat.py": "import sys\n"}


def test_python_syntax_errors_are_caught(linkwatch):
    with pytest.raises(EditError, match="no longer parses"):
        apply_edits(linkwatch, [E("linkwatch/cache.py", "import json", "import json(")])


def test_broken_toml_is_caught(linkwatch):
    with pytest.raises(EditError, match="TOML"):
        apply_edits(linkwatch, [E("pyproject.toml", '    "Jinja2==2.11.3",', '    "Jinja2>=3.1,')])


def test_edits_build_on_previous_state(linkwatch):
    state = apply_edits(linkwatch, [E("requirements.txt", "Jinja2==2.11.3", "Jinja2>=3.0")])
    state = apply_edits(linkwatch, [E("requirements.txt", "Jinja2>=3.0", "Jinja2>=3.1,<4")], state)
    assert "Jinja2>=3.1,<4" in state["requirements.txt"]


def test_reverting_a_file_drops_it_from_the_state(linkwatch):
    state = apply_edits(linkwatch, [E("requirements.txt", "six\n", "")])
    state = apply_edits(linkwatch, [E("requirements.txt", "chardet==5.2.0\n", "six\nchardet==5.2.0\n")], state)
    assert state == {}


# --- patches ----------------------------------------------------------------


def test_patch_applies_and_reproduces_the_edit(linkwatch):
    changed = apply_edits(linkwatch, [E("linkwatch/cache.py", "from appdirs", "from platformdirs"),
                                      E("requirements.txt", "appdirs==1.4.4", "platformdirs>=4"),
                                      E("linkwatch/compat.py", "", "X = 1\n")])
    patch = make_patch(linkwatch, changed)
    assert patch.count("diff --git") == 3 and "new file mode 100644" in patch
    ok, out = git_apply_check(linkwatch, patch)
    assert ok, out
    git_apply(linkwatch, patch)
    for rel, text in changed.items():
        assert (linkwatch / rel).read_text() == text


def test_patch_handles_missing_trailing_newline(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "a.txt").write_text("one\ntwo")
    changed = apply_edits(repo, [E("a.txt", "two", "three")])
    patch = make_patch(repo, changed)
    assert "\\ No newline at end of file" in patch
    ok, out = git_apply_check(repo, patch)
    assert ok, out
    git_apply(repo, patch)
    assert (repo / "a.txt").read_text() == "one\nthree"


def test_patch_check_works_in_a_folder_nested_in_another_git_repo(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    repo = copy_repo(outer / "vendor" / "linkwatch", git=False)
    patch = make_patch(repo, apply_edits(repo, [E("requirements.txt", "six\n", "")]))
    ok, out = git_apply_check(repo, patch)
    assert ok, out


def test_broken_patch_fails_the_check(linkwatch):
    ok, out = git_apply_check(linkwatch, "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")
    assert not ok and out
    assert git_apply_check(linkwatch, "") == (False, "the patch is empty")


# --- context ----------------------------------------------------------------


def test_context_includes_usages_manifests_and_build_files_that_mention_the_dep(linkwatch):
    usages = find_usages(linkwatch, "toml")
    files = context_files(linkwatch, "toml", usages)
    assert files[0] == "linkwatch/config.py"
    assert {"requirements.txt", "pyproject.toml", "requirements-dev.txt"} <= set(files)
    assert "Makefile" not in files
    nose_files = context_files(linkwatch, "nose", [])
    assert "Makefile" in nose_files  # `nosetests tests/` lives there


def test_render_files_windows_long_files(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(1000)))
    usage = Usage(file="big.py", line=500, kind="import", symbol="x")
    text = render_files(repo, ["big.py"], [usage])
    assert "x499 = 499" in text and "x10 = 10\n" not in text and "lines omitted" in text
    assert text.startswith("<<<FILE big.py>>>")


# --- Gemma-backed jobs ------------------------------------------------------

JINJA_CHOICE = {"action": "upgrade", "replacement": "Jinja2", "target_spec": "Jinja2>=3.1,<4",
                "reasoning": "Same API in 3.1.", "alternatives": [{"name": "Mako", "why_not": "rewrite"}]}
JINJA_MIGRATION = {"steps": ["raise the pin"], "edits": [
    {"file": "requirements.txt", "find": "Jinja2==2.11.3", "replace": "Jinja2>=3.1,<4"},
    {"file": "pyproject.toml", "find": '    "Jinja2==2.11.3",', "replace": '    "Jinja2>=3.1,<4",'}]}


@pytest.fixture
def jinja(linkwatch):
    report = scan_repo(linkwatch)
    return report.get("jinja2"), find_usages(linkwatch, "jinja2"), report


def test_choose_replacement_is_tagged_ai(tmp_path, jinja):
    risk, usages, report = jinja
    c, backend = client(tmp_path, JINJA_CHOICE)
    ws = Workspace(tmp_path / "capsule")
    proposal = propose.choose_replacement(c, risk, usages, python_version="3.12", requires_python=">=3.10",
                                          other_dependencies=["requests"], ws=ws)
    assert proposal.action == "upgrade" and proposal.target_spec == "Jinja2>=3.1,<4"
    assert proposal.reasoning.source == "AI"
    assert proposal.reasoning.evidence == ["evidence/gemma/pick_replacement.json"]
    assert (ws.root / proposal.reasoning.evidence[0]).is_file()
    prompt = backend.calls[0]["messages"][0][1]
    assert "linkwatch/report.py" in prompt and "requires-python: >=3.10" in prompt


def test_draft_migration_retries_bad_edits_then_records_the_patch(tmp_path, jinja, linkwatch):
    risk, usages, _ = jinja
    bad = {"steps": ["x"], "edits": [{"file": "requirements.txt", "find": "jinja2==2.11.3", "replace": "Jinja2>=3.1"}]}
    c, backend = client(tmp_path, JINJA_CHOICE, bad, JINJA_MIGRATION)
    ws = Workspace(tmp_path / "capsule")
    proposal = propose.choose_replacement(c, risk, usages, python_version="3.12", requires_python=None,
                                          other_dependencies=[], ws=ws)
    attempt = propose.draft_migration(c, linkwatch, ws, risk, proposal, usages, python_version="3.12",
                                      requires_python=None)
    feedback = backend.calls[2]["messages"][-1][1]
    assert "not found in requirements.txt" in feedback
    assert "<<<FILE requirements.txt>>>" in backend.calls[1]["messages"][0][1]
    assert attempt.round == 0 and attempt.patch_file == "migration.round0.patch"
    assert attempt.patch_applies.source == "OBSERVED" and "applies cleanly" in attempt.patch_applies.claim
    for path in attempt.patch_applies.evidence:
        assert (ws.root / path).is_file()
    assert all(s.source == "AI" for s in attempt.steps)
    patch = (ws.root / attempt.patch_file).read_text()
    assert "+Jinja2>=3.1,<4" in patch and patch.count("diff --git") == 2
    assert (ws.evidence / "gemma" / "write_migration.round0.json").is_file()


def test_draft_migration_rejects_an_empty_edit_list(tmp_path, jinja, linkwatch):
    risk, usages, _ = jinja
    empty = {"steps": [], "edits": []}
    c, _ = client(tmp_path, JINJA_CHOICE, empty, empty, empty)
    ws = Workspace(tmp_path / "capsule")
    proposal = propose.choose_replacement(c, risk, usages, python_version="3.12", requires_python=None,
                                          other_dependencies=[])
    with pytest.raises(GemmaError, match="edit list is empty"):
        propose.draft_migration(c, linkwatch, ws, risk, proposal, usages, python_version="3.12", requires_python=None)


def test_repair_builds_a_cumulative_patch(tmp_path, linkwatch):
    report = scan_repo(linkwatch)
    risk, usages = report.get("toml"), find_usages(linkwatch, "toml")
    choice = dict(JINJA_CHOICE, action="stdlib", replacement="tomllib", target_spec=None)
    broken = {"steps": ["swap import"], "edits": [
        {"file": "linkwatch/config.py", "find": "import toml\n", "replace": "import tomllib\n"},
        {"file": "linkwatch/config.py", "find": "    data = toml.load(path)", "replace": "    data = tomllib.load(open(path))"}]}
    fix = {"diagnosis": "tomllib.load needs a binary file.", "steps": ["open in rb"], "edits": [
        {"file": "linkwatch/config.py", "find": "    data = tomllib.load(open(path))",
         "replace": '    with open(path, "rb") as fh:\n        data = tomllib.load(fh)'}]}
    c, backend = client(tmp_path, choice, broken, fix)
    ws = Workspace(tmp_path / "capsule")
    proposal = propose.choose_replacement(c, risk, usages, python_version="3.12", requires_python=None,
                                          other_dependencies=[])
    first = propose.draft_migration(c, linkwatch, ws, risk, proposal, usages, python_version="3.12",
                                    requires_python=None)
    second = propose.repair_migration(c, linkwatch, ws, proposal, [first], usages,
                                      failing_tests=["tests/test_config.py::test_sites_file_loads"],
                                      log_text="TypeError: File must be opened in binary mode")
    prompt = backend.calls[2]["messages"][0][1]
    assert "repair round 1 of 2" in prompt and "binary mode" in prompt and "tomllib.load(open(path))" in prompt
    assert second.round == 1 and second.diagnosis.source == "AI"
    patch = (ws.root / second.patch_file).read_text()
    assert "-import toml" in patch and 'with open(path, "rb")' in patch and "open(path))" not in patch
    git_apply(linkwatch, patch)
    assert "tomllib.load(fh)" in (linkwatch / "linkwatch/config.py").read_text()

    with pytest.raises(propose.ProposeError):
        propose.repair_migration(c, linkwatch, ws, proposal, [first, second, second], usages, failing_tests=[],
                                 log_text="")


def test_explain_risk_and_gaps_are_tagged_ai(tmp_path, jinja, linkwatch):
    risk, usages, _ = jinja
    gaps = {"gaps": [{"description": "Python 3.10 has no tomllib", "severity": "high"}, {"description": "  "}],
            "concerns": ["only Linux tested", ""], "suggested_tests": [{"name": "t", "purpose": "p"}],
            "source": "OBSERVED"}
    c, _ = client(tmp_path, {"summary": "old", "severity": "high"}, JINJA_CHOICE, JINJA_MIGRATION, gaps)
    ws = Workspace(tmp_path / "capsule")
    risk2 = propose.explain_risk(c, risk, "3.12", ws)
    assert risk2.ai_summary.source == "AI" and risk2.ai_severity == "high" and risk.ai_summary is None
    proposal = propose.choose_replacement(c, risk2, usages, python_version="3.12", requires_python=None,
                                          other_dependencies=[])
    attempt = propose.draft_migration(c, linkwatch, ws, risk2, proposal, usages, python_version="3.12",
                                      requires_python=None)
    g, concerns, tests = propose.explain_gaps(c, linkwatch, ws, proposal, [attempt], usages,
                                              Comparison(newly_fixed=["t1"]), python_version="3.12",
                                              requires_python=">=3.10")
    assert [x.description for x in g] == ["Python 3.10 has no tomllib"]
    assert g[0].source == "AI" and g[0].status == "open"
    assert [x.claim for x in concerns] == ["only Linux tested"] and concerns[0].source == "AI"
    assert tests[0].source == "AI" and tests[0].ran is False
    transcript = json.loads((ws.evidence / "gemma" / "explain_gaps.json").read_text())
    assert transcript["source"] == "AI"


def test_annotate_usages_maps_notes_by_index(tmp_path, linkwatch):
    usages = find_usages(linkwatch, "requests")
    assert len(usages) >= 4
    notes = {"notes": [{"index": 1, "note": "first"}, {"index": 3, "note": "third"},
                       {"index": 99, "note": "out of range"}, {"index": 2, "note": "  "}]}
    c, backend = client(tmp_path, notes)
    out = propose.annotate_usages(c, "requests", usages)
    assert out[0].note.claim == "first" and out[0].note.source == "AI"
    assert out[1].note is None and out[2].note.claim == "third"
    assert all(u.note is None for u in out[3:])
    assert "[1] linkwatch/checker.py" in backend.calls[0]["messages"][0][1]
    assert propose.annotate_usages(c, "requests", []) == []


def test_annotate_usages_batches_long_lists(tmp_path):
    usages = [Usage(file="a.py", line=i, kind="import", symbol="x") for i in range(1, 46)]
    c, backend = client(tmp_path, {"notes": [{"index": 40, "note": "n40"}]}, {"notes": [{"index": 5, "note": "n45"}]})
    out = propose.annotate_usages(c, "x", usages)
    assert len(backend.calls) == 2
    assert out[39].note.claim == "n40" and out[44].note.claim == "n45"
