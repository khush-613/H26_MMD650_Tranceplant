import json
import string
import types

import pytest

from helpers import HttpError, ScriptedBackend
from tranceplant import gemma
from tranceplant.gemma import (
    JOBS,
    MODEL_LARGE,
    MODEL_SMALL,
    GeminiBackend,
    GemmaCacheMiss,
    GemmaClient,
    GemmaError,
    extract_json,
    prompts_dir,
    render_prompt,
    response_text,
)

RISK = {"package": "jinja2", "pinned": "2.11.3", "declared_in": "requirements.txt", "python_version": "3.12",
        "indicators": {"license": "BSD"}, "findings": "- test fails"}
GOOD_RISK = {"summary": "Old and broken.", "severity": "high", "key_factors": ["x"]}


def client(tmp_path, *replies, **kw):
    backend = ScriptedBackend(*replies)
    return GemmaClient("live", cache_dir=tmp_path / "cache", backend=backend, sleep=lambda s: None, **kw), backend


def placeholders(text):
    return {m.group("named") or m.group("braced") for m in string.Template.pattern.finditer(text)
            if m.group("named") or m.group("braced")}


# --- prompts ----------------------------------------------------------------


def test_every_job_has_a_prompt_and_every_prompt_a_job():
    files = {p.stem for p in prompts_dir().glob("*.txt")}
    assert files == set(JOBS)


@pytest.mark.parametrize("job", sorted(JOBS))
def test_prompt_renders_with_all_its_variables(job):
    text = (prompts_dir() / f"{job}.txt").read_text()
    names = placeholders(text)
    rendered = render_prompt(job, {n: f"<{n}>" for n in names})
    for n in names:
        assert f"<{n}>" in rendered
    assert "$" not in rendered.split("JSON Schema")[0], "unrendered placeholder"
    assert json.dumps(JOBS[job].schema.model_json_schema(), indent=1) in rendered


def test_missing_prompt_variable_is_a_clear_error():
    with pytest.raises(GemmaError, match="needs variable"):
        render_prompt("explain_risk", {"package": "x"})


def test_dollar_signs_in_values_are_left_alone():
    out = render_prompt("annotate_usages", {"package": "x", "replacement": "y", "usages": "price = '$5' $HOME"})
    assert "price = '$5' $HOME" in out


def test_non_string_values_become_json():
    out = render_prompt("explain_risk", dict(RISK, indicators={"license": "BSD", "vulnerabilities": []}))
    assert '"license": "BSD"' in out


# --- parsing ----------------------------------------------------------------


@pytest.mark.parametrize("reply", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    'Sure! Here it is:\n```\n{"a": 1}\n```\nHope that helps.',
    'Answer: {"a": 1} done',
    'set {x} then {"a": 1}',
])
def test_extract_json_handles_fences_and_prose(reply):
    assert extract_json(reply) == {"a": 1}


def test_extract_json_rejects_non_objects():
    with pytest.raises(ValueError):
        extract_json("[1, 2, 3]")
    with pytest.raises(ValueError):
        extract_json("no json here")


# --- client -----------------------------------------------------------------


def test_routes_jobs_to_the_right_model(tmp_path):
    c, backend = client(tmp_path, GOOD_RISK, {"steps": ["s"], "edits": [{"file": "a", "find": "", "replace": "x"}]})
    assert c.run("explain_risk", RISK).model == MODEL_SMALL
    variables = {n: "v" for n in placeholders((prompts_dir() / "write_migration.txt").read_text())}
    assert c.run("write_migration", variables).model == MODEL_LARGE
    assert [call["model"] for call in backend.calls] == [MODEL_SMALL, MODEL_LARGE]
    assert c.models_used == {MODEL_SMALL, MODEL_LARGE}


def test_good_reply_is_parsed_and_cached(tmp_path):
    c, backend = client(tmp_path, "```json\n" + json.dumps(GOOD_RISK) + "\n```")
    result = c.run("explain_risk", RISK)
    assert result.data.severity == "high"
    assert result.origin == "api" and result.attempts == 1
    assert c.api_calls == 1

    c2, backend2 = client(tmp_path)  # same cache dir, empty script
    again = c2.run("explain_risk", RISK)
    assert again.origin == "cache" and again.data == result.data
    assert backend2.calls == [] and c2.cache_hits == 1


def test_cache_key_changes_with_the_prompt(tmp_path):
    c, backend = client(tmp_path, GOOD_RISK, GOOD_RISK)
    c.run("explain_risk", RISK)
    c.run("explain_risk", dict(RISK, pinned="2.11.2"))
    assert len(backend.calls) == 2


def test_invalid_reply_is_sent_back_with_the_error(tmp_path):
    c, backend = client(tmp_path, {"summary": "x", "severity": "apocalyptic"}, GOOD_RISK)
    result = c.run("explain_risk", RISK)
    assert result.attempts == 2
    second = backend.calls[1]["messages"]
    assert [role for role, _ in second] == ["user", "model", "user"]
    assert "severity" in second[2][1] and "rejected" in second[2][1]


def test_gives_up_after_the_fix_attempts(tmp_path):
    c, backend = client(tmp_path, "nope", "still nope", "never")
    with pytest.raises(GemmaError, match="no usable reply after 3 tries"):
        c.run("explain_risk", RISK)
    assert len(backend.calls) == 3


def test_validate_hook_errors_trigger_a_retry(tmp_path):
    seen = []

    def validate(data):
        seen.append(data.severity)
        if data.severity != "high":
            raise ValueError("severity must be high for this package")

    c, backend = client(tmp_path, dict(GOOD_RISK, severity="low"), GOOD_RISK)
    assert c.run("explain_risk", RISK, validate=validate).data.severity == "high"
    assert seen == ["low", "high"]
    assert "severity must be high" in backend.calls[1]["messages"][2][1]


def test_cached_reply_that_no_longer_validates_is_regenerated(tmp_path):
    c, _ = client(tmp_path, GOOD_RISK)
    c.run("explain_risk", RISK)
    c2, backend2 = client(tmp_path, dict(GOOD_RISK, summary="fresh"))

    def validate(data):
        if data.summary != "fresh":
            raise ValueError("stale")

    assert c2.run("explain_risk", RISK, validate=validate).data.summary == "fresh"
    assert len(backend2.calls) == 1


def test_gemma_cannot_tag_its_own_output_as_observed(tmp_path):
    c, _ = client(tmp_path, dict(GOOD_RISK, source="OBSERVED", evidence=["fake.log"]))
    data = c.run("explain_risk", RISK).data
    assert "source" not in data.model_dump()


def test_offline_mode_uses_cache_and_never_calls_the_api(tmp_path):
    c, _ = client(tmp_path, GOOD_RISK)
    c.run("explain_risk", RISK)
    off = GemmaClient("offline", cache_dir=tmp_path / "cache", backend=ScriptedBackend())
    assert off.run("explain_risk", RISK).origin == "cache"
    with pytest.raises(GemmaCacheMiss):
        off.run("explain_risk", dict(RISK, package="nose"))


def test_fake_mode_reads_fixtures(tmp_path):
    folder = tmp_path / "fx" / "explain_risk"
    folder.mkdir(parents=True)
    (folder / "jinja2.json").write_text(json.dumps(GOOD_RISK))
    (folder / "default.json").write_text(json.dumps(dict(GOOD_RISK, severity="low")))
    c = GemmaClient("fake", fixtures_dir=tmp_path / "fx", backend=ScriptedBackend())
    assert c.run("explain_risk", RISK, fixture_key="jinja2").data.severity == "high"
    assert c.run("explain_risk", RISK, fixture_key="nose").data.severity == "low"
    assert c.run("explain_risk", RISK).origin == "fake"
    with pytest.raises(GemmaCacheMiss):
        c.run("annotate_usages", {"package": "x", "replacement": "y", "usages": "u"})


def test_fake_mode_reports_a_broken_fixture(tmp_path):
    folder = tmp_path / "fx" / "explain_risk"
    folder.mkdir(parents=True)
    (folder / "default.json").write_text('{"summary": "x"}')
    with pytest.raises(GemmaError, match="not usable"):
        GemmaClient("fake", fixtures_dir=tmp_path / "fx").run("explain_risk", RISK)


def test_shipped_fixtures_all_validate():
    root = gemma.PROJECT_DIR / "fixtures" / "gemma"
    files = list(root.glob("*/*.json"))
    assert files
    for path in files:
        JOBS[path.parent.name].schema.model_validate(extract_json(path.read_text()))


def test_rate_limits_are_retried_with_backoff(tmp_path):
    sleeps = []
    backend = ScriptedBackend(HttpError(429), HttpError(503), GOOD_RISK)
    c = GemmaClient("live", cache_dir=tmp_path / "c", backend=backend, sleep=sleeps.append)
    assert c.run("explain_risk", RISK).data.severity == "high"
    assert sleeps == [2.0, 4.0]


def test_client_errors_are_not_retried(tmp_path):
    c, backend = client(tmp_path, HttpError(400))
    with pytest.raises(GemmaError, match="HTTP 400"):
        c.run("explain_risk", RISK)
    assert len(backend.calls) == 1


def test_rate_limit_gives_up_eventually(tmp_path):
    c, backend = client(tmp_path, *[HttpError(429)] * 3, max_api_retries=2)
    with pytest.raises(GemmaError):
        c.run("explain_risk", RISK)
    assert len(backend.calls) == 3


def test_requests_are_spaced_to_the_rpm_limit(tmp_path):
    sleeps = []
    c = GemmaClient("live", cache_dir=tmp_path / "c", backend=ScriptedBackend(GOOD_RISK, GOOD_RISK),
                    requests_per_minute=6, sleep=sleeps.append)
    c.run("explain_risk", RISK)
    c.run("explain_risk", dict(RISK, pinned="1"))
    assert len(sleeps) == 1 and 9 < sleeps[0] <= 10


def test_unknown_job_and_mode():
    with pytest.raises(GemmaError):
        GemmaClient("live", backend=ScriptedBackend()).run("write_poem", {})
    with pytest.raises(GemmaError):
        GemmaClient("turbo")


def test_transcript_is_tagged_ai(tmp_path):
    c, _ = client(tmp_path, GOOD_RISK)
    result = c.run("explain_risk", RISK)
    saved = json.loads(result.save(tmp_path / "t" / "x.json").read_text())
    assert saved["source"] == "AI" and saved["parsed"] == GOOD_RISK and "Package: jinja2" in saved["prompt"]


# --- Gemini backend ---------------------------------------------------------


def _part(text, thought=False):
    return types.SimpleNamespace(text=text, thought=thought)


def _response(*parts, finish="STOP"):
    content = types.SimpleNamespace(parts=list(parts))
    return types.SimpleNamespace(candidates=[types.SimpleNamespace(content=content, finish_reason=finish)])


def test_response_text_skips_thoughts():
    assert response_text(_response(_part("thinking...", True), _part('{"ok":'), _part(" true}"))) == '{"ok": true}'


def test_response_text_errors_when_empty():
    with pytest.raises(GemmaError, match="MAX_TOKENS"):
        response_text(_response(_part("only thoughts", True), finish="MAX_TOKENS"))
    with pytest.raises(GemmaError):
        response_text(types.SimpleNamespace(candidates=[], text=None))


def test_gemini_backend_needs_a_key():
    with pytest.raises(GemmaError, match="GEMINI_API_KEY"):
        GeminiBackend()


def test_gemini_backend_calls_the_sdk(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return _response(_part('{"ok": true}'))

    class FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key
            self.models = FakeModels()

    import google.genai

    monkeypatch.setattr(google.genai, "Client", FakeClient)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    backend = GeminiBackend()
    text = backend.generate("gemma-4-26b-a4b-it", [("user", "hi"), ("model", "x"), ("user", "again")], 0.3, 100)
    assert text == '{"ok": true}'
    assert captured["api_key"] == "test-key"
    assert captured["model"] == "gemma-4-26b-a4b-it"
    assert [c.role for c in captured["contents"]] == ["user", "model", "user"]
    assert captured["contents"][2].parts[0].text == "again"
    assert captured["config"].temperature == 0.3 and captured["config"].max_output_tokens == 100


def test_real_sdk_error_type_is_retryable(tmp_path):
    from google.genai import errors

    err = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
    assert err.code == 429
    c, backend = client(tmp_path, err, GOOD_RISK)
    assert c.run("explain_risk", RISK).data.severity == "high"
    assert len(backend.calls) == 2


def test_ping_in_fake_mode():
    assert GemmaClient("fake").ping().data.ok is True

