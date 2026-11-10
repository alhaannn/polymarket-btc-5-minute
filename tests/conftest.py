import os
import pytest

@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch):
    """Ensures test runs default to standard test conditions."""
    monkeypatch.setenv("DISABLE_DAILY_SHUTDOWN", "false")
    monkeypatch.setenv("SIZING_MODE", "canonical")
    monkeypatch.setenv("DAILY_LOSS_LIMIT_USD", "25.0")
