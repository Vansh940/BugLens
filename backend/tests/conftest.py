"""Test bootstrap: put the backend on sys.path and pin a hermetic test env.

Nothing here touches the network. LLM calls are monkeypatched per-test; the
engine store uses a throwaway SQLite file; Redis is left unconfigured (budget
degrades to a no-op, which is the documented behaviour).
"""
import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("GITHUB_TOKEN", "ghp_test_token")
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")  # unroutable on purpose
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ["BUGLENS_DB"] = str(Path(tempfile.gettempdir()) / "buglens_test_state.sqlite")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///" + str(Path(tempfile.gettempdir()) / "buglens_test_app.db"))

import pytest  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A fresh engine-store database per test."""
    path = str(tmp_path / "state.sqlite")
    monkeypatch.setenv("BUGLENS_DB", path)
    return path


@pytest.fixture(autouse=True)
def _close_store_pool():
    """The store pools SQLite connections and aiosqlite worker threads are non-daemon,
    so an unclosed pool would hang the interpreter at exit."""
    yield
    from buglens import store

    import asyncio
    asyncio.run(store.close_pool())
