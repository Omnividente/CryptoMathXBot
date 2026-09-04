import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from cryptomathxbot.storage import PreferencesStore


@pytest.mark.asyncio
async def test_legacy_user_and_chat_scopes_are_migrated(tmp_path: Path) -> None:
    legacy = tmp_path / "favorites.json"
    legacy.write_text(
        json.dumps({"123": ["btc", "eth"], "-456": ["xmr"]}),
        encoding="utf-8",
    )
    store = PreferencesStore(
        tmp_path / "data" / "state.sqlite3",
        default_favorites=("BTC",),
        max_favorites=8,
    )

    await store.initialize(legacy)

    assert await store.favorites(123) == ("BTC", "ETH")
    assert await store.favorites(999, -456) == ("XMR",)
    assert await store.favorites(999) == ("BTC",)


@pytest.mark.asyncio
async def test_repeated_legacy_migration_never_overwrites_user_preferences(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "favorites.json"
    database = tmp_path / "state.sqlite3"
    legacy.write_text(json.dumps({"123": ["BTC"]}), encoding="utf-8")
    store = PreferencesStore(database, default_favorites=("BTC",), max_favorites=8)
    await store.initialize(legacy)
    await store.set_favorites(123, ("SOL",))

    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DELETE FROM metadata WHERE key = 'legacy_favorites_migrated'")
    await store.initialize(legacy)

    assert await store.favorites(123) == ("SOL",)


@pytest.mark.asyncio
async def test_user_favorites_override_legacy_group_defaults(tmp_path: Path) -> None:
    store = PreferencesStore(
        tmp_path / "state.sqlite3",
        default_favorites=("BTC", "ETH"),
        max_favorites=3,
    )
    await store.initialize()

    await store.set_favorites(1, ("SOL", "XMR"))

    assert await store.favorites(1, -100) == ("SOL", "XMR")
    assert await store.favorites(2, -100) == ("BTC", "ETH")


@pytest.mark.asyncio
async def test_empty_and_oversized_favorites_are_rejected(tmp_path: Path) -> None:
    store = PreferencesStore(
        tmp_path / "state.sqlite3",
        default_favorites=("BTC",),
        max_favorites=2,
    )
    await store.initialize()

    with pytest.raises(ValueError):
        await store.set_favorites(1, ())
    with pytest.raises(ValueError):
        await store.set_favorites(1, ("BTC", "ETH", "XMR"))
