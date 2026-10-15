"""A small local web page for trying Tranceplant: `tranceplant ui`.

Standard library only. The server listens on 127.0.0.1, runs the same CLI
commands in a subprocess (so the page tests the real pipeline), streams their
output to the page, and serves the capsule folders so reports and evidence
can be opened in the browser.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

STATIC_DIR = Path(__file__).resolve().parent / "web"
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPO = PROJECT_DIR / "tests" / "fixtures" / "linkwatch"

COMMANDS = {"scan", "xray", "trial", "replay", "gemma-check"}
BACKENDS = {"auto", "docker", "uv"}
GEMMA_MODES = {"live", "offline", "fake"}
DEP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Never let a credential reach the page, even if a library prints one.
SECRET_RE = re.compile(r"(AIza[0-9A-Za-z_-]{20,}|AQ\.[0-9A-Za-z_.-]{20,}|key=[^&\s\"']+)")
MAX_OUTPUT = 400_000


def redact(text: str) -> str:
    return SECRET_RE.sub("<redacted>", ANSI_RE.sub("", text))


class BadRequest(ValueError):
    pass


class Job:
    def __init__(self, argv: list[str], label: str):
        self.id = uuid.uuid4().hex[:12]
        self.argv = argv
        self.label = label
        self.started = time.time()
        self.finished: float | None = None
        self.exit_code: int | None = None
        self.output = ""
        self._lock = threading.Lock()
        self.process: subprocess.Popen | None = None

    def start(self, cwd: Path) -> None:
        # NO_COLOR keeps ANSI codes out; TERM=dumb would make Rich squeeze tables to 80 columns
        env = dict(os.environ, NO_COLOR="1", COLUMNS="140", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(
            self.argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            with self._lock:
                if len(self.output) < MAX_OUTPUT:
                    self.output += redact(line)
        self.exit_code = self.process.wait()
        self.finished = time.time()

    def stop(self) -> None:
        if self.process and self.exit_code is None:
            self.process.terminate()

    def to_dict(self) -> dict:
        with self._lock:
            output = self.output
        return {
            "id": self.id,
            "label": self.label,
            "command": " ".join(self.argv[3:]),  # drop "python -m tranceplant.cli"
            "status": "running" if self.exit_code is None else ("ok" if self.exit_code == 0 else "failed"),
            "exit_code": self.exit_code,
            "seconds": round((self.finished or time.time()) - self.started, 1),
            "output": output,
        }


class App:
    def __init__(self, capsules_dir: Path, cwd: Path = PROJECT_DIR):
        self.capsules_dir = Path(capsules_dir).resolve()
        self.cwd = cwd
        self.jobs: dict[str, Job] = {}

    # --- building commands ---------------------------------------------------

    def build_argv(self, data: dict) -> tuple[list[str], str]:
        command = data.get("command")
        if command not in COMMANDS:
            raise BadRequest(f"command must be one of {sorted(COMMANDS)}")
        gemma = data.get("gemma") or "fake"
        if gemma not in GEMMA_MODES:
            raise BadRequest(f"gemma must be one of {sorted(GEMMA_MODES)}")
        argv = [sys.executable, "-m", "tranceplant.cli", "--gemma", gemma]

        def repo() -> str:
            path = Path(str(data.get("repo") or DEFAULT_REPO)).expanduser()
            if not path.is_dir():
                raise BadRequest(f"repo folder not found: {path}")
            return str(path.resolve())

        def dep() -> str:
            name = str(data.get("dep") or "").strip()
            if not DEP_RE.match(name):
                raise BadRequest("dependency must be a package name like jinja2")
            return name

        def backend() -> str:
            value = data.get("backend") or "auto"
            if value not in BACKENDS:
                raise BadRequest(f"backend must be one of {sorted(BACKENDS)}")
            return value

        if command == "scan":
            argv += ["scan", repo()]
            if data.get("explain"):
                argv.append("--explain")
            label = "scan"
        elif command == "xray":
            name = dep()
            argv += ["xray", repo(), name] + ([] if data.get("notes", True) else ["--no-notes"])
            label = f"xray {name}"
        elif command == "trial":
            name = dep()
            argv += ["trial", repo(), name, "--out", str(self.capsules_dir), "--backend", backend()]
            label = f"trial {name}"
        elif command == "replay":
            folder = self.capsule_folder(dep())
            argv += ["replay", str(folder)]
            if data.get("backend"):
                argv += ["--backend", backend()]
            label = f"replay {folder.name}"
        else:
            argv += ["gemma-check"]
            label = "gemma-check"
        return argv, label

    def capsule_folder(self, name: str) -> Path:
        folder = (self.capsules_dir / name).resolve()
        if folder.parent != self.capsules_dir or not (folder / "capsule.json").is_file():
            raise BadRequest(f"no capsule named {name!r}")
        return folder

    def start_job(self, data: dict) -> Job:
        argv, label = self.build_argv(data)
        job = Job(argv, label)
        self.jobs[job.id] = job
        job.start(self.cwd)
        return job

    # --- reading state -------------------------------------------------------

    def capsules(self) -> list[dict]:
        out = []
        if not self.capsules_dir.is_dir():
            return out
        for path in sorted(self.capsules_dir.glob("*/capsule.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            comparison = data.get("comparison", {})
            proposal = data.get("proposal", {})
            files = [n for n in ("capsule.html", "capsule.md", "capsule.json", "migration.patch", "replay.sh")
                     if (path.parent / n).is_file()]
            out.append({
                "name": path.parent.name,
                "verdict": data.get("verdict"),
                "reason": (data.get("verdict_reason") or {}).get("claim"),
                "action": proposal.get("action"),
                "replacement": proposal.get("replacement"),
                "created_at": data.get("created_at"),
                "newly_fixed": len(comparison.get("newly_fixed", [])),
                "regressions": len(comparison.get("regressions", [])),
                "open_gaps": sum(1 for g in data.get("gaps", []) if g.get("status") == "open"),
                "rounds": max(0, len(data.get("migration", [])) - 1),
                "backend": (data.get("control") or {}).get("backend"),
                "gemma": (data.get("gemma") or {}).get("models", []),
                "files": files,
            })
        return out

    def status(self) -> dict:
        from .sandbox import docker_available

        return {
            "gemma_key_present": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
            "docker": docker_available(),
            "default_repo": str(DEFAULT_REPO),
            "capsules_dir": str(self.capsules_dir),
        }

    def capsule_file(self, rel: str) -> Path | None:
        target = (self.capsules_dir / rel).resolve()
        if self.capsules_dir not in target.parents or not target.is_file():
            return None
        return target


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tranceplant-ui"

        def log_message(self, format, *args):  # keep the terminal quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _file(self, path: Path) -> None:
            ctype = mimetypes.guess_type(path.name)[0] or "text/plain"
            if path.suffix in (".md", ".patch", ".sh", ".log", ".xml"):
                ctype = "text/plain"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        def do_GET(self):  # noqa: N802
            path = unquote(urlparse(self.path).path)
            if path in ("/", "/index.html"):
                return self._file(STATIC_DIR / "index.html")
            if path.startswith("/static/"):
                target = (STATIC_DIR / path.removeprefix("/static/")).resolve()
                if STATIC_DIR in target.parents and target.is_file():
                    return self._file(target)
            elif path == "/api/status":
                return self._json(app.status())
            elif path == "/api/capsules":
                return self._json(app.capsules())
            elif path == "/api/jobs":
                return self._json([j.to_dict() | {"output": ""} for j in reversed(app.jobs.values())])
            elif path.startswith("/api/jobs/"):
                job = app.jobs.get(path.rsplit("/", 1)[-1])
                if job:
                    return self._json(job.to_dict())
            elif path.startswith("/capsules/"):
                target = app.capsule_file(path.removeprefix("/capsules/"))
                if target:
                    return self._file(target)
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            if length > 10_000:
                return self._json({"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise BadRequest("send a JSON object")
            except (json.JSONDecodeError, BadRequest) as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            if path == "/api/jobs":
                try:
                    job = app.start_job(data)
                except BadRequest as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return self._json(job.to_dict(), HTTPStatus.CREATED)
            if path.startswith("/api/jobs/") and path.endswith("/stop"):
                job = app.jobs.get(path.split("/")[3])
                if job:
                    job.stop()
                    return self._json(job.to_dict())
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def make_server(capsules_dir: Path, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    app = App(capsules_dir)
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.app = app  # type: ignore[attr-defined]
    return server
