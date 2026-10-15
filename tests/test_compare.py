import pytest

from tranceplant.compare import compare, compare_junit, counts, decide_verdict, parse_junit
from tranceplant.models import Comparison, Gap, TestResult


def R(name, outcome):
    return TestResult(name=name, outcome=outcome)


def junit(path, *cases):
    body = "".join(cases)
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{body}</testsuite></testsuites>')
    return path


def case(cls, name, inner="", time="0.01"):
    return f'<testcase classname="{cls}" name="{name}" time="{time}">{inner}</testcase>'


# --- compare ------------------------------------------------------------------


def test_every_test_lands_in_exactly_one_group():
    control = [R("a", "passed"), R("b", "passed"), R("c", "failed"), R("d", "error"), R("e", "passed")]
    donor = [R("a", "passed"), R("b", "failed"), R("c", "passed"), R("d", "failed"), R("f", "passed")]
    c = compare(control, donor)
    assert c.still_passing == ["a"]
    assert c.regressions == ["b"]
    assert c.newly_fixed == ["c"]
    assert c.still_failing == ["d"]
    assert c.only_in_control == ["e"]
    assert c.only_in_donor == ["f"]
    assert sum(counts(c).values()) == 6


def test_error_counts_as_broken():
    assert compare([R("a", "passed")], [R("a", "error")]).regressions == ["a"]


def test_a_test_skipped_after_the_swap_is_a_regression():
    assert compare([R("a", "passed")], [R("a", "skipped")]).regressions == ["a"]
    assert compare([R("a", "skipped")], [R("a", "passed")]).newly_fixed == ["a"]
    assert compare([R("a", "skipped")], [R("a", "skipped")]).still_failing == ["a"]


def test_empty_sides():
    assert compare([], []) == Comparison()


# --- verdict ------------------------------------------------------------------

OPEN = Gap(description="py3.10 has no tomllib", source="AI")
CLOSED = OPEN.model_copy(update={"status": "resolved"})


@pytest.mark.parametrize("comparison, gaps, verdict", [
    (Comparison(still_passing=["a"], newly_fixed=["b"]), [], "Accepted"),
    (Comparison(still_passing=["a"], still_failing=["b"]), [], "Accepted"),
    (Comparison(still_passing=["a"]), [CLOSED], "Accepted"),
    (Comparison(still_passing=["a"]), [OPEN], "Partial"),
    (Comparison(still_passing=["a"], only_in_control=["b"]), [], "Partial"),
    (Comparison(still_passing=["a"], only_in_donor=["b"]), [], "Partial"),
    (Comparison(), [], "Partial"),  # nothing compared at all
    (Comparison(still_passing=["a"], regressions=["b"]), [], "Rejected"),
    (Comparison(regressions=["b"], newly_fixed=["c"]), [OPEN], "Rejected"),
])
def test_verdict_rules(comparison, gaps, verdict):
    assert decide_verdict(comparison, gaps) == verdict


# --- JUnit parsing ------------------------------------------------------------


def test_parse_junit_outcomes_and_messages(tmp_path):
    path = junit(
        tmp_path / "r.xml",
        case("tests.test_a", "test_ok"),
        case("tests.test_a", "test_fail", '<failure message="assert 1 == 2">trace here</failure>'),
        case("tests.test_a", "test_err", '<error message="fixture broke"/>'),
        case("tests.test_a", "test_skip", '<skipped message="no network"/>'),
    )
    results, issues = parse_junit(path)
    assert issues == []
    by = {r.name: r for r in results}
    assert by["tests.test_a::test_ok"].outcome == "passed" and by["tests.test_a::test_ok"].duration == 0.01
    assert by["tests.test_a::test_fail"].outcome == "failed"
    assert by["tests.test_a::test_fail"].message == "assert 1 == 2\ntrace here"
    assert by["tests.test_a::test_err"].outcome == "error"
    assert by["tests.test_a::test_skip"].outcome == "skipped"


def test_parse_junit_builds_pytest_node_ids_from_the_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_x.py").write_text("")
    path = junit(tmp_path / "r.xml", case("tests.unit.test_x", "test_a"), case("tests.unit.test_x.TestK", "test_b"),
                 case("not.a.module", "test_c"))
    names = [r.name for r in parse_junit(path, repo=repo)[0]]
    assert names == ["tests/unit/test_x.py::test_a", "tests/unit/test_x.py::TestK::test_b", "not.a.module::test_c"]


def test_parse_junit_keeps_duplicate_names_apart(tmp_path):
    path = junit(tmp_path / "r.xml", case("t", "test_p"), case("t", "test_p", "<failure/>"))
    assert [(r.name, r.outcome) for r in parse_junit(path)[0]] == [("t::test_p", "passed"), ("t::test_p#2", "failed")]


def test_parse_junit_truncates_huge_messages(tmp_path):
    path = junit(tmp_path / "r.xml", case("t", "x", f'<failure message="m">{"y" * 10000}</failure>'))
    assert len(parse_junit(path)[0][0].message) == 2000


@pytest.mark.parametrize("content, problem", [
    (None, "missing"),
    ("<not xml", "Could not read"),
    ('<testsuites><testsuite name="pytest"/></testsuites>', "no test cases"),
])
def test_parse_junit_reports_problems(tmp_path, content, problem):
    path = tmp_path / "r.xml"
    if content is not None:
        path.write_text(content)
    results, issues = parse_junit(path)
    assert results == [] and problem in issues[0]


def test_compare_junit_directly(tmp_path):
    a = junit(tmp_path / "a.xml", case("t", "x", "<failure/>"), case("t", "y"))
    b = junit(tmp_path / "b.xml", case("t", "x"), case("t", "y"))
    c = compare_junit(a, b)
    assert c.newly_fixed == ["t::x"] and c.still_passing == ["t::y"]
