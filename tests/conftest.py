import pytest

from app import accounts


@pytest.fixture(autouse=True)
def ignore_local_ai_setting(monkeypatch):
    """Tests must never use the real AI choice from someone's .env file."""
    monkeypatch.setenv("AI_BACKEND", "")


@pytest.fixture(autouse=True)
def forget_wrong_tries():
    accounts.tries.reset()
    yield
    accounts.tries.reset()
