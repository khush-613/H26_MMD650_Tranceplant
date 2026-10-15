import shutil

import pytest

from helpers import copy_repo


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """No test may hit the real API or share a cache by accident."""
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "TRANCEPLANT_GEMMA_MODE", "TRANCEPLANT_FIXTURES_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRANCEPLANT_CACHE_DIR", str(tmp_path / "gemma-cache"))
    monkeypatch.setenv("TRANCEPLANT_GEMMA_RPM", "0")
    monkeypatch.setenv("TRANCEPLANT_DOTENV", str(tmp_path / "no-such.env"))  # never read the developer's real key


@pytest.fixture
def linkwatch(tmp_path):
    return copy_repo(tmp_path / "linkwatch")


@pytest.fixture
def needs_uv():
    if not shutil.which("uv"):
        pytest.skip("uv is not installed")
