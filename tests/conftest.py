"""Tests must never touch the developer's real identity (~/.config/cckit/identity.json): a hook run by a test would find it and post test data,
signed with the real token, to whatever URL is configured (the shipped default is the live team app)."""
import pytest


@pytest.fixture(autouse=True)
def _private_identity(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("CCKIT_IDENTITY_FILE", str(tmp_path_factory.mktemp("identity") / "identity.json"))
    for k in ("CCKIT_TEAM_KEY", "CCKIT_EMAIL", "CCKIT_GITHUB", "CCKIT_TOKEN", "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)
