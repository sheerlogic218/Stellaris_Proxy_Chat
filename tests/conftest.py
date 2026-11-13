"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def no_real_token(monkeypatch):
    """No test may see the developer's real Discord token, so none can ever print it.

    Covers the environment variable and the Windows user variable both modes read.
    """
    import proxychat_bot.config as local_config
    import proxychat_server.config as server_config
    monkeypatch.delenv("PROXYCHAT_DISCORD_TOKEN", raising=False)
    monkeypatch.setattr(local_config, "_token_from_env", lambda: None)
    monkeypatch.setattr(server_config, "_windows_user_token", lambda: "")
