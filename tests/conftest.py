"""Shared synthetic credential for handler tests with minimal Services doubles."""

import pytest

from cryptomathxbot.card import CardSigner


@pytest.fixture(autouse=True)
def card_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep real signing/verification, but never require a live bot credential.
    # Production key selection is tested separately through its original callable.
    monkeypatch.setattr(
        "cryptomathxbot.app._card_signer",
        lambda context: CardSigner("unit-test-card-key"),
    )
