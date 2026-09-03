import pytest

from cryptomathxbot.config import ConfigurationError, Settings


def test_default_favorites_cannot_exceed_configured_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTOMATHX_DEFAULT_FAVORITES", "BTC ETH XMR SOL")
    monkeypatch.setenv("CRYPTOMATHX_MAX_FAVORITES", "3")

    with pytest.raises(ConfigurationError, match="exceeds"):
        Settings.from_env(require_token=False)


def test_default_favorites_at_limit_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTOMATHX_DEFAULT_FAVORITES", "BTC ETH XMR")
    monkeypatch.setenv("CRYPTOMATHX_MAX_FAVORITES", "3")

    settings = Settings.from_env(require_token=False)

    assert settings.default_favorites == ("BTC", "ETH", "XMR")
    assert settings.max_favorites == 3


def test_invalid_default_favorite_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRYPTOMATHX_DEFAULT_FAVORITES", "BTC INVALID-TICKER")

    with pytest.raises(ConfigurationError, match="invalid ticker"):
        Settings.from_env(require_token=False)
