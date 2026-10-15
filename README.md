# Tranceplant

Tranceplant finds risky dependencies in a Python repo, swaps each one for a safer replacement inside a sandbox, runs the same tests on the old and new versions, and packs the evidence into a **Dependency Exit Capsule**. Track 1: Best Use of Gemma 4 (Dependency Resilience). Target repo: [rishrek/LinkWatch](https://github.com/rishrek/LinkWatch), vendored at `tests/fixtures/linkwatch`.

Every fact is tagged `OBSERVED` (we ran or read it, linked to evidence) or `AI` (Gemma 4 suggested it). The tool decides the tag, never the model.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -p .venv -e ".[dev]"
cp .env.example .env   # add GEMINI_API_KEY; never commit .env
.venv/bin/python -m pytest
```

## Layout and owners

Each person owns separate files, so merges stay conflict-free. Only touch your own files; change `models.py` only after telling the team.

```text
tranceplant/
├── models.py      shared capsule schema (frozen at checkpoint 1)
├── cli.py         Typer commands + stage wiring          Bhoumik
├── gemma.py       Gemma 4 client, cache, JSON parsing    Bhoumik
├── propose.py     Stage 3: replacement + migration patch Bhoumik
├── scan.py        Stage 1: dependencies + risk data      Khush
├── xray.py        Stage 2: AST usage finder              Khush
├── capsule.py     Stage 6: capsule.json/.md, replay.sh   Khush
├── templates/     capsule.md.j2                          Khush
├── sandbox.py     Stage 4: control + donor test runs     Rajveer
└── compare.py     Stage 5: per-test diff + verdict       Rajveer
prompts/           one .txt per Gemma job                 Bhoumik
fixtures/gemma/    saved fake Gemma replies               Bhoumik
Dockerfile.sandbox                                        Rajveer
tests/             our tests; tests/fixtures/linkwatch is the target repo
```

## Stage contracts

`cli.py` calls these. The signatures are already stubbed in each file; keep them as they are.

| Module | Function | Returns |
| --- | --- | --- |
| `scan.py` | `scan_repo(repo: Path)` | `ScanReport` |
| `xray.py` | `find_usages(repo: Path, dep: str)` | `list[Usage]` |
| `sandbox.py` | `run_side(repo, side, *, patch, evidence_dir, label, backend)` | `SideRun` (writes `<label>.junit.xml` and `<label>.log` into `evidence_dir`; paths relative to the capsule folder) |
| `compare.py` | `compare(control, donor)` | `Comparison` |
| `compare.py` | `decide_verdict(comparison, gaps)` | `"Accepted"`, `"Partial"` or `"Rejected"` |
| `capsule.py` | `write_capsule(capsule, capsule_dir)` | path to `capsule.json` |

Capsule folder layout: `capsules/<dep>/capsule.json`, `capsule.md`, `migration.patch`, `replay.sh`, `evidence/`.

## Commands

| Command | What it does |
| --- | --- |
| `tranceplant scan <repo>` | Lists dependencies with risk scores |
| `tranceplant xray <repo> <dep>` | Shows every place the dependency is used |
| `tranceplant trial <repo> <dep>` | Proposes a replacement and runs both sandboxes |
| `tranceplant run <repo> --all` | Runs all six stages for every risky dependency |
| `tranceplant replay capsules/<dep>` | Re-runs a saved trial |

## Working rules

- One branch per person (`bhoumik`, `khush`, `rajveer`); merge into `main` at each checkpoint.
- Commit and push at least once an hour.
- Until `gemma.py` lands, use the saved fake replies in `fixtures/gemma/` (`--gemma fake`).
