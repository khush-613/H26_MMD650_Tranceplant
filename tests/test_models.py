import json

import pytest
from pydantic import ValidationError

from tranceplant.models import Capsule, Claim, Comparison, Proposal, RepoInfo, ScanReport, SideRun, capsule_json_schema


def test_observed_claim_needs_evidence():
    with pytest.raises(ValidationError, match="evidence"):
        Claim(claim="tests pass", source="OBSERVED")
    assert Claim.observed("tests pass", "evidence/donor.junit.xml").evidence == ["evidence/donor.junit.xml"]


def test_ai_claim_may_have_no_evidence():
    assert Claim.ai("probably fine").source == "AI"


def test_unknown_source_rejected():
    with pytest.raises(ValidationError):
        Claim(claim="x", source="VERIFIED", evidence=["a"])


def test_models_forbid_unknown_fields():
    with pytest.raises(ValidationError):
        Claim(claim="x", source="AI", confidence=0.9)


def test_scan_report_lookup_is_case_and_separator_insensitive():
    report = ScanReport.model_validate({"repo": ".", "dependencies": [
        {"dependency": {"name": "python-dateutil"}}, {"dependency": {"name": "jinja2"}}]})
    assert report.get("Jinja2").dependency.name == "jinja2"
    assert report.get("python_dateutil").dependency.name == "python-dateutil"
    assert report.get("flask") is None


def test_capsule_round_trip_and_schema():
    side = SideRun(side="control", label="control", command="pytest")
    capsule = Capsule(
        tool_version="0.1.0",
        repo=RepoInfo(path="/repo"),
        dependency={"name": "jinja2", "pinned": "2.11.3"},
        risk={"dependency": {"name": "jinja2"}, "score": 90},
        proposal=Proposal(dependency="jinja2", action="upgrade", replacement="Jinja2",
                          reasoning=Claim.ai("api unchanged"), model="gemma-4-31b-it"),
        control=side,
        donor=side.model_copy(update={"side": "donor", "label": "donor"}),
        comparison=Comparison(newly_fixed=["t1"]),
        verdict="Accepted",
        verdict_reason=Claim.observed("0 regressions", "evidence/donor.junit.xml"),
    )
    again = Capsule.model_validate_json(capsule.model_dump_json())
    assert again == capsule
    assert again.limitations, "known limitations go into every capsule"
    schema = capsule_json_schema()
    assert schema["title"] == "Capsule"
    assert "Claim" in schema["$defs"]
    json.dumps(schema)


def test_verdict_must_be_one_of_three():
    with pytest.raises(ValidationError):
        Capsule.model_validate({"verdict": "Maybe"})
