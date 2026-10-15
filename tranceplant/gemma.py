"""Gemma 4 client: prompts, JSON parsing, retries and a disk cache.

Every job asks Gemma for JSON that must validate against a Pydantic model.
Bad output is sent back with the error and retried. Valid output is cached on
disk by a hash of the prompt, so re-running the demo costs no API calls and
still works offline.

Modes (TRANCEPLANT_GEMMA_MODE or GemmaClient(mode=...)):
  live     call the Gemini API, use the cache when possible (default)
  offline  cache only; a cache miss is an error
  fake     read saved responses from fixtures/gemma/<job>/<key>.json, so
           teammates can build without an API key

Gemma never decides what is OBSERVED. Response models ignore unknown keys, so
a "source" field in Gemma's output is dropped, and callers tag everything that
comes out of here as AI.
"""

from __future__ import annotations

import hashlib
import json
import os
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROMPT_VERSION = "1"
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

MODEL_LARGE = os.environ.get("TRANCEPLANT_MODEL_LARGE", "gemma-4-31b-it")
MODEL_SMALL = os.environ.get("TRANCEPLANT_MODEL_SMALL", "gemma-4-26b-a4b-it")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_OUTPUT_TOKENS_CAP = 65536


class GemmaError(RuntimeError):
    pass


class GemmaCacheMiss(GemmaError):
    pass


class GemmaTruncated(GemmaError):
    """The model used its whole output budget (mostly thinking) before answering."""


# --- Response models: the JSON shapes Gemma must return ---------------------


class Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


SeverityLevel = Literal["low", "medium", "high", "critical"]


class RiskExplanation(Lenient):
    summary: str = Field(description="2-3 plain-English sentences on why this dependency is risky")
    severity: SeverityLevel
    key_factors: list[str] = Field(default_factory=list, description="short bullet facts the summary relies on")


class UsageNote(Lenient):
    index: int = Field(description="the [n] number of the usage in the list")
    note: str = Field(description="one sentence on why this usage matters for the swap")


class UsageNotes(Lenient):
    notes: list[UsageNote]


class AlternativeItem(Lenient):
    name: str
    why_not: str = ""


class ReplacementChoice(Lenient):
    action: Literal["replace", "upgrade", "remove", "add", "stdlib"]
    replacement: str | None = Field(
        None, description="package or stdlib module to use instead; null when removing"
    )
    target_spec: str | None = Field(
        None, description="requirement line to declare, e.g. 'Jinja2>=3.1,<4'; null for stdlib or remove"
    )
    reasoning: str
    alternatives: list[AlternativeItem] = Field(default_factory=list)


class EditItem(Lenient):
    file: str = Field(description="path relative to the repo root")
    find: str = Field(description="exact text copied from the file, unique within it; empty only to create a new file")
    replace: str
    reason: str = ""


class MigrationDraft(Lenient):
    steps: list[str] = Field(description="ordered, human-readable migration steps")
    edits: list[EditItem]


class RepairDraft(Lenient):
    diagnosis: str = Field(description="what the failing log shows and why the migration caused it")
    steps: list[str] = Field(default_factory=list)
    edits: list[EditItem]


class GapItem(Lenient):
    description: str
    severity: SeverityLevel = "medium"
    related_tests: list[str] = Field(default_factory=list)


class SuggestedTestItem(Lenient):
    name: str
    purpose: str
    code: str | None = None


class GapReport(Lenient):
    gaps: list[GapItem] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    suggested_tests: list[SuggestedTestItem] = Field(default_factory=list)


class Ping(Lenient):
    ok: bool


@dataclass(frozen=True)
class Job:
    tier: Literal["large", "small"]
    schema: type[BaseModel]
    temperature: float = 0.2
    max_output_tokens: int = 8192


JOBS: dict[str, Job] = {
    # Gemma 4 always thinks first and its thoughts count against max_output_tokens
    # (the API rejects thinking_budget / thinking_level for Gemma), so leave room.
    "explain_risk": Job("small", RiskExplanation, 0.3, 16384),
    "annotate_usages": Job("small", UsageNotes, 0.3, 16384),
    "pick_replacement": Job("large", ReplacementChoice, 0.2, 16384),
    "write_migration": Job("large", MigrationDraft, 0.1, 16384),
    "repair_migration": Job("large", RepairDraft, 0.1, 16384),
    "explain_gaps": Job("small", GapReport, 0.3, 16384),
    "ping": Job("small", Ping, 0.0, 4096),
}


def model_for(job: str) -> str:
    return MODEL_LARGE if JOBS[job].tier == "large" else MODEL_SMALL


# --- Prompts ----------------------------------------------------------------


def prompts_dir() -> Path:
    override = os.environ.get("TRANCEPLANT_PROMPTS_DIR")
    if override:
        return Path(override)
    bundled = PACKAGE_DIR / "_prompts"
    return bundled if bundled.is_dir() else PROJECT_DIR / "prompts"


def schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "Reply with a single JSON object and nothing else: no prose, no markdown.\n"
        "It must validate against this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), indent=1)
    )


def render_prompt(job: str, variables: dict[str, Any]) -> str:
    path = prompts_dir() / f"{job}.txt"
    if not path.is_file():
        raise GemmaError(f"missing prompt file {path}")
    template = string.Template(path.read_text(encoding="utf-8"))
    values = {k: v if isinstance(v, str) else json.dumps(v, indent=1, default=str) for k, v in variables.items()}
    try:
        body = template.substitute(values)
    except KeyError as exc:
        raise GemmaError(f"prompt {job}.txt needs variable {exc.args[0]!r}") from None
    return body.rstrip() + "\n\n" + schema_instructions(JOBS[job].schema)


# --- Parsing ----------------------------------------------------------------


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (fences and prose allowed)."""
    cleaned = text.strip()
    if "```" in cleaned:
        for block in cleaned.split("```")[1::2]:
            block = block.removeprefix("json").removeprefix("JSON").strip()
            try:
                value = json.loads(block)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    decoder = json.JSONDecoder()
    start = cleaned.find("{")
    while start != -1:
        try:
            value, _ = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            start = cleaned.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = cleaned.find("{", start + 1)
    raise ValueError("no JSON object found in the reply")


def _feedback(error: Exception) -> str:
    if isinstance(error, ValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in error.errors()[:10]
        )
    else:
        problems = str(error)
    return (
        "Your reply was rejected:\n"
        f"{problems}\n\n"
        "Fix every problem and reply again with the complete corrected JSON object only."
    )


# --- Backends ---------------------------------------------------------------

Message = tuple[Literal["user", "model"], str]


class Backend(Protocol):
    def generate(self, model: str, messages: list[Message], temperature: float, max_output_tokens: int) -> str: ...


def response_text(response: Any) -> str:
    """Join the non-thought text parts of the first candidate."""
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or []
        texts = [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
        if texts:
            return "".join(texts)
        reason = getattr(candidates[0], "finish_reason", None)
        if "MAX_TOKENS" in str(reason):
            raise GemmaTruncated(f"Gemma returned no text (finish_reason={reason})")
        raise GemmaError(f"Gemma returned no text (finish_reason={reason})")
    text = getattr(response, "text", None)
    if text:
        return text
    raise GemmaError("Gemma returned no candidates")


class GeminiBackend:
    """Gemma 4 through the Gemini API (Google AI Studio)."""

    def __init__(self, api_key: str | None = None):
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise GemmaError("GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
        from google import genai

        self._client = genai.Client(api_key=key)

    def generate(self, model: str, messages: list[Message], temperature: float, max_output_tokens: int) -> str:
        from google.genai import types

        contents = [types.Content(role=role, parts=[types.Part(text=text)]) for role, text in messages]
        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(temperature=temperature, max_output_tokens=max_output_tokens),
        )
        return response_text(response)


# --- Cache ------------------------------------------------------------------


class DiskCache:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def put(self, key: str, record: dict) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
        tmp.replace(path)


# --- Client -----------------------------------------------------------------


@dataclass
class GemmaResult:
    job: str
    model: str
    data: BaseModel
    raw: str
    prompt: str
    cache_key: str
    origin: Literal["api", "cache", "fake"]
    attempts: int = 1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def transcript(self) -> dict:
        return {
            "source": "AI",
            "job": self.job,
            "model": self.model,
            "origin": self.origin,
            "attempts": self.attempts,
            "cache_key": self.cache_key,
            "created_at": self.created_at,
            "prompt": self.prompt,
            "raw_response": self.raw,
            "parsed": self.data.model_dump(mode="json"),
        }

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.transcript(), indent=1), encoding="utf-8")
        return path


Validator = Callable[[BaseModel], None]


class GemmaClient:
    def __init__(
        self,
        mode: Literal["live", "offline", "fake"] | None = None,
        *,
        cache_dir: Path | str | None = None,
        fixtures_dir: Path | str | None = None,
        backend: Backend | None = None,
        api_key: str | None = None,
        max_fix_attempts: int = 2,
        max_api_retries: int = 5,
        requests_per_minute: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.mode = mode or os.environ.get("TRANCEPLANT_GEMMA_MODE", "live")
        if self.mode not in ("live", "offline", "fake"):
            raise GemmaError(f"unknown Gemma mode {self.mode!r}")
        self.cache = DiskCache(Path(cache_dir or os.environ.get("TRANCEPLANT_CACHE_DIR", ".tranceplant/cache/gemma")))
        self.fixtures_dir = Path(fixtures_dir or os.environ.get("TRANCEPLANT_FIXTURES_DIR", PROJECT_DIR / "fixtures" / "gemma"))
        self._backend = backend
        self._api_key = api_key
        self.max_fix_attempts = max_fix_attempts
        self.max_api_retries = max_api_retries
        rpm = requests_per_minute or float(os.environ.get("TRANCEPLANT_GEMMA_RPM", "15"))
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call = 0.0
        self._sleep = sleep
        self.api_calls = 0
        self.cache_hits = 0
        self.models_used: set[str] = set()

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            self._backend = GeminiBackend(self._api_key)
        return self._backend

    def run(
        self,
        job: str,
        variables: dict[str, Any],
        *,
        fixture_key: str | None = None,
        validate: Validator | None = None,
    ) -> GemmaResult:
        if job not in JOBS:
            raise GemmaError(f"unknown Gemma job {job!r}")
        spec = JOBS[job]
        model = model_for(job)
        prompt = render_prompt(job, variables)
        key = hashlib.sha256(
            json.dumps({"v": PROMPT_VERSION, "job": job, "model": model, "prompt": prompt}).encode()
        ).hexdigest()
        if self.mode == "fake":
            # Say so in the capsule: no model was called, a saved reply stood in for it.
            self.models_used.add(f"{model} (saved fixture, not called)")
            return self._from_fixture(job, model, prompt, key, fixture_key, validate)
        self.models_used.add(model)

        cached = self.cache.get(key)
        if cached is not None:
            try:
                data = spec.schema.model_validate(cached["parsed"])
                if validate:
                    validate(data)
            except (ValidationError, ValueError, KeyError):
                cached = None  # stale against the current repo state; ask again
            else:
                self.cache_hits += 1
                return GemmaResult(job, model, data, cached.get("raw_response", ""), prompt, key, "cache",
                                   cached.get("attempts", 1), cached.get("created_at", ""))
        if self.mode == "offline":
            raise GemmaCacheMiss(f"offline mode and no cached Gemma reply for job {job!r} (key {key[:12]})")

        messages: list[Message] = [("user", prompt)]
        last_error: Exception | None = None
        budget = spec.max_output_tokens
        for attempt in range(1, self.max_fix_attempts + 2):
            try:
                raw = self._call(model, messages, spec, budget)
            except GemmaTruncated as exc:
                last_error = exc
                budget = min(budget * 2, MAX_OUTPUT_TOKENS_CAP)
                continue
            try:
                data = spec.schema.model_validate(extract_json(raw))
                if validate:
                    validate(data)
            except (ValidationError, ValueError) as exc:
                last_error = exc
                messages += [("model", raw), ("user", _feedback(exc))]
                continue
            result = GemmaResult(job, model, data, raw, prompt, key, "api", attempt)
            self.cache.put(key, result.transcript())
            return result
        raise GemmaError(f"Gemma job {job!r} gave no usable reply after {self.max_fix_attempts + 1} tries: {last_error}")

    def _from_fixture(self, job, model, prompt, key, fixture_key, validate) -> GemmaResult:
        folder = self.fixtures_dir / job
        candidates = ([folder / f"{fixture_key}.json"] if fixture_key else []) + [folder / "default.json"]
        for path in candidates:
            if path.is_file():
                raw = path.read_text(encoding="utf-8")
                try:
                    data = JOBS[job].schema.model_validate(extract_json(raw))
                    if validate:
                        validate(data)
                except (ValidationError, ValueError) as exc:
                    raise GemmaError(f"fake Gemma fixture {path} is not usable: {exc}") from exc
                self.cache_hits += 1
                return GemmaResult(job, model, data, raw, prompt, key, "fake")
        raise GemmaCacheMiss(f"no fake Gemma fixture for job {job!r} (looked for {', '.join(map(str, candidates))})")

    def _call(self, model: str, messages: list[Message], spec: Job, max_output_tokens: int | None = None) -> str:
        delay = 2.0
        for attempt in range(self.max_api_retries + 1):
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
            self._last_call = time.monotonic()
            try:
                self.api_calls += 1
                return self.backend.generate(model, messages, spec.temperature,
                                             max_output_tokens or spec.max_output_tokens)
            except GemmaError:
                raise
            except Exception as exc:  # SDK errors carry an HTTP status in .code
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if status in RETRYABLE_STATUS and attempt < self.max_api_retries:
                    self._sleep(delay)
                    delay = min(delay * 2, 60.0)
                    continue
                raise GemmaError(f"Gemini API call to {model} failed: {exc}") from exc
        raise AssertionError("unreachable")

    def ping(self) -> GemmaResult:
        return self.run("ping", {})
