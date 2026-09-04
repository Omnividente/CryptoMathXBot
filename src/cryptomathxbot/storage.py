from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    scope TEXT PRIMARY KEY,
    favorites_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class PreferencesStore:
    def __init__(
        self,
        database: Path,
        *,
        default_favorites: tuple[str, ...],
        max_favorites: int,
    ) -> None:
        self._database = database
        self._default_favorites = default_favorites
        self._max_favorites = max_favorites
        self._write_lock = asyncio.Lock()

    async def initialize(self, legacy_file: Path | None = None) -> None:
        self._database.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)
        if legacy_file is not None:
            await self._migrate_legacy(legacy_file)

    async def favorites(self, user_id: int, chat_id: int | None = None) -> tuple[str, ...]:
        scopes = [self.user_scope(user_id)]
        if chat_id is not None:
            scopes.append(self.chat_scope(chat_id))
        result = await asyncio.to_thread(self._read_favorites_sync, scopes)
        return result or self._default_favorites

    async def set_favorites(self, user_id: int, symbols: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(symbol.upper() for symbol in symbols if symbol))
        if not cleaned:
            raise ValueError("favorites cannot be empty")
        if len(cleaned) > self._max_favorites:
            raise ValueError(f"at most {self._max_favorites} favorites are allowed")
        async with self._write_lock:
            await asyncio.to_thread(
                self._write_preferences_sync,
                self.user_scope(user_id),
                cleaned,
            )
        return cleaned


    @staticmethod
    def user_scope(user_id: int) -> str:
        return f"user:{user_id}"

    @staticmethod
    def chat_scope(chat_id: int) -> str:
        return f"chat:{chat_id}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_sync(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def _read_favorites_sync(self, scopes: list[str]) -> tuple[str, ...] | None:
        placeholders = ",".join("?" for _ in scopes)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"SELECT scope, favorites_json FROM preferences WHERE scope IN ({placeholders})",
                scopes,
            ).fetchall()
        by_scope = {str(scope): str(payload) for scope, payload in rows}
        for scope in scopes:
            payload = by_scope.get(scope)
            if payload is None:
                continue
            try:
                values = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(values, list):
                cleaned = tuple(str(value).upper() for value in values if str(value).strip())
                if cleaned:
                    return cleaned[: self._max_favorites]
        return None


    def _write_preferences_sync(
        self,
        scope: str,
        favorites: tuple[str, ...],
    ) -> None:
        payload = json.dumps(favorites, ensure_ascii=False, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO preferences(scope, favorites_json)
                VALUES (?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                    favorites_json = excluded.favorites_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (scope, payload),
            )

    async def _migrate_legacy(self, legacy_file: Path) -> None:
        if not await asyncio.to_thread(legacy_file.is_file):
            return
        already_done = await asyncio.to_thread(self._metadata_sync, "legacy_favorites_migrated")
        if already_done:
            return
        try:
            raw = await asyncio.to_thread(legacy_file.read_text, encoding="utf-8-sig")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            _LOGGER.warning("legacy favorites migration skipped: %s", type(exc).__name__)
            return
        if not isinstance(data, dict):
            return

        rows: list[tuple[str, tuple[str, ...]]] = []
        for raw_scope, raw_symbols in data.items():
            if not isinstance(raw_symbols, list):
                continue
            try:
                numeric_id = int(raw_scope)
            except (TypeError, ValueError):
                continue
            symbols = tuple(
                dict.fromkeys(
                    str(value).strip().upper() for value in raw_symbols if str(value).strip()
                )
            )[: self._max_favorites]
            if not symbols:
                continue
            scope = self.user_scope(numeric_id) if numeric_id > 0 else self.chat_scope(numeric_id)
            rows.append((scope, symbols))

        async with self._write_lock:
            migrated = await asyncio.to_thread(self._migrate_legacy_sync, rows)
        _LOGGER.info("legacy favorites migrated count=%d", migrated)

    def _metadata_sync(self, key: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row[0]) if row else None

    def _migrate_legacy_sync(
        self,
        rows: list[tuple[str, tuple[str, ...]]],
    ) -> int:
        migrated = 0
        with closing(self._connect()) as connection, connection:
            for scope, favorites in rows:
                payload = json.dumps(favorites, ensure_ascii=False, separators=(",", ":"))
                cursor = connection.execute(
                    """
                    INSERT INTO preferences(scope, favorites_json)
                    VALUES (?, ?)
                    ON CONFLICT(scope) DO NOTHING
                    """,
                    (scope, payload),
                )
                migrated += cursor.rowcount
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                ("legacy_favorites_migrated", str(migrated)),
            )
        return migrated
