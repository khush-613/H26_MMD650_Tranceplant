import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from tranceplant import web
from tranceplant.web import App, BadRequest, make_server, redact


@pytest.fixture
def server(tmp_path):
    srv = make_server(tmp_path / "capsules", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(url, data=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Content-Type"), err.read()


def make_capsule(folder, name, verdict="Accepted"):
    d = folder / name
    (d / "evidence").mkdir(parents=True)
    (d / "capsule.json").write_text(json.dumps({
        "verdict": verdict, "verdict_reason": {"claim": "0 regressions"},
        "proposal": {"action": "upgrade", "replacement": "Jinja2"},
        "comparison": {"newly_fixed": ["t1"], "regressions": []},
        "gaps": [{"status": "open"}, {"status": "resolved"}],
        "migration": [{}, {}], "control": {"backend": "docker"}, "gemma": {"models": ["m"]},
    }))
    (d / "capsule.html").write_text("<h1>report</h1>")
    (d / "evidence" / "control.log").write_text("log")
    return d


# --- argument building ------------------------------------------------------


def test_commands_are_built_as_argument_lists(tmp_path, linkwatch):
    app = App(tmp_path / "caps")
    argv, label = app.build_argv({"command": "trial", "repo": str(linkwatch), "dep": "jinja2",
                                  "gemma": "live", "backend": "uv"})
    assert argv[1:] == ["-m", "tranceplant.cli", "--gemma", "live", "trial", str(linkwatch), "jinja2",
                        "--out", str((tmp_path / "caps").resolve()), "--backend", "uv"]
    assert label == "trial jinja2"
    assert app.build_argv({"command": "scan", "repo": str(linkwatch), "explain": True})[0][-1] == "--explain"
    assert app.build_argv({"command": "xray", "repo": str(linkwatch), "dep": "requests", "notes": False})[0][-1] == "--no-notes"
    assert app.build_argv({"command": "gemma-check"})[0][-1] == "gemma-check"


def test_defaults_to_the_linkwatch_fixture_and_fake_gemma(tmp_path):
    argv, _ = App(tmp_path).build_argv({"command": "scan"})
    assert argv[3:6] == ["--gemma", "fake", "scan"] and argv[6] == str(web.DEFAULT_REPO.resolve())


@pytest.mark.parametrize("data, message", [
    ({"command": "rm"}, "command must be"),
    ({"command": "scan", "gemma": "turbo"}, "gemma must be"),
    ({"command": "scan", "repo": "/no/such/folder"}, "repo folder not found"),
    ({"command": "xray", "dep": "x; rm -rf /"}, "package name"),
    ({"command": "xray", "dep": "--help"}, "package name"),
    ({"command": "xray", "dep": ""}, "package name"),
    ({"command": "trial", "dep": "toml", "backend": "podman"}, "backend must be"),
    ({"command": "replay", "dep": "jinja2"}, "no capsule named"),
])
def test_bad_requests_are_rejected(tmp_path, data, message):
    with pytest.raises(BadRequest, match=message):
        App(tmp_path).build_argv(data)


def test_replay_only_targets_real_capsule_folders(tmp_path):
    app = App(tmp_path / "caps")
    make_capsule(tmp_path / "caps", "jinja2")
    argv, label = app.build_argv({"command": "replay", "dep": "jinja2"})
    assert argv[-2:] == ["replay", str((tmp_path / "caps" / "jinja2").resolve())] and label == "replay jinja2"
    (tmp_path / "caps" / "empty").mkdir()
    with pytest.raises(BadRequest):
        app.build_argv({"command": "replay", "dep": "empty"})


def test_redact_strips_ansi_and_credentials():
    text = "\x1b[1mok\x1b[0m key=abc123&x AIza" + "B" * 30 + " AQ." + "C" * 30
    assert redact(text) == "ok <redacted>&x <redacted> <redacted>"


def test_capsule_summaries(tmp_path):
    make_capsule(tmp_path, "jinja2")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "capsule.json").write_text("{nope")
    [c] = App(tmp_path).capsules()
    assert c["name"] == "jinja2" and c["verdict"] == "Accepted" and c["newly_fixed"] == 1
    assert c["open_gaps"] == 1 and c["rounds"] == 1 and c["backend"] == "docker"
    assert c["files"] == ["capsule.html", "capsule.json"]
    assert App(tmp_path / "missing").capsules() == []


# --- HTTP ---------------------------------------------------------------------


def test_page_and_static_files(server):
    _, base = server
    status, ctype, body = call(base + "/")
    assert status == 200 and ctype.startswith("text/html") and b"Tranceplant" in body
    for path, kind in (("/static/style.css", "text/css"), ("/static/app.js", "javascript")):
        status, ctype, _ = call(base + path)
        assert status == 200 and kind in ctype


@pytest.mark.parametrize("path", [
    "/static/../web.py", "/static/%2e%2e/web.py", "/capsules/../tranceplant/cli.py",
    "/capsules/%2e%2e/%2e%2e/.env", "/static/../../.env", "/nope",
])
def test_nothing_outside_the_allowed_folders_is_served(server, path):
    _, base = server
    status, _, _ = call(base + path)
    assert status == 404


def test_capsule_files_are_served(server):
    srv, base = server
    make_capsule(srv.app.capsules_dir, "jinja2")
    status, ctype, body = call(base + "/capsules/jinja2/capsule.html")
    assert status == 200 and ctype.startswith("text/html") and body == b"<h1>report</h1>"
    status, ctype, _ = call(base + "/capsules/jinja2/evidence/control.log")
    assert status == 200 and ctype.startswith("text/plain")
    assert json.loads(call(base + "/api/capsules")[2])[0]["name"] == "jinja2"


def test_status_never_contains_the_key(server, monkeypatch):
    _, base = server
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-super-secret-value-123")
    monkeypatch.setattr("tranceplant.sandbox.docker_available", lambda: False)
    status, _, body = call(base + "/api/status")
    data = json.loads(body)
    assert status == 200 and data["gemma_key_present"] is True and data["docker"] is False
    assert b"super-secret" not in body


@pytest.mark.parametrize("raw, code", [(b"hello", 400), (b"[1]", 400), (b'{"command": "rm"}', 400),
                                       (b"x" * 20000, 413)])
def test_bad_posts(server, raw, code):
    _, base = server
    assert call(base + "/api/jobs", raw=raw)[0] == code


def test_a_job_runs_and_reports_its_output(server):
    _, base = server
    status, _, body = call(base + "/api/jobs", {"command": "gemma-check", "gemma": "fake"})
    assert status == 201
    job = json.loads(body)
    deadline = time.time() + 60
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.3)
        job = json.loads(call(base + f"/api/jobs/{job['id']}")[2])
    assert job["status"] == "ok" and job["exit_code"] == 0
    assert "Gemma OK" in job["output"] and job["command"] == "--gemma fake gemma-check"
    listing = json.loads(call(base + "/api/jobs")[2])
    assert listing[0]["id"] == job["id"] and listing[0]["output"] == ""


def test_stop_ends_a_running_job(server, monkeypatch):
    srv, base = server
    monkeypatch.setattr(srv.app, "build_argv", lambda data: (["sleep", "30"], "sleep"))
    job = json.loads(call(base + "/api/jobs", {"command": "scan"})[2])
    call(base + f"/api/jobs/{job['id']}/stop", raw=b"{}")
    deadline = time.time() + 10
    while job["status"] == "running" and time.time() < deadline:
        time.sleep(0.2)
        job = json.loads(call(base + f"/api/jobs/{job['id']}")[2])
    assert job["status"] == "failed" and job["exit_code"] != 0
