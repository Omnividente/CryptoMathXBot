from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,11}$")


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is missing or invalid."""


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _symbols(raw: str) -> tuple[str, ...]:
    values: list[str] = []
    for item in raw.replace(",", " ").split():
        symbol = item.strip().upper()
        if _SYMBOL_RE.fullmatch(symbol) is None:
            raise ConfigurationError("CRYPTOMATHX_DEFAULT_FAVORITES contains an invalid ticker")
        if symbol not in values:
            values.append(symbol)
    return tuple(values) or ("BTC", "ETH", "XMR")


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    data_dir: Path
    log_dir: Path
    legacy_favorites_file: Path
    owner_chat_id: int | None
    log_level: str
    default_favorites: tuple[str, ...]
    max_favorites: int
    max_symbols: int
    concurrent_updates: int
    query_concurrency: int
    rate_limit_requests: int
    rate_limit_window: int
    http_timeout: float
    http_retries: int
    chart_dpi: int

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> Settings:
        token = os.getenv("CRYPTOMATHX_BOT_TOKEN", "").strip()
        token_file = Path(os.getenv("CRYPTOMATHX_TOKEN_FILE", "BOT_TOKEN.txt"))
        if not token and token_file.is_file():
            token = token_file.read_text(encoding="utf-8-sig").strip()
        if require_token and not token:
            raise ConfigurationError(
                "Set CRYPTOMATHX_BOT_TOKEN or create BOT_TOKEN.txt in the working directory"
            )

        owner_raw = os.getenv("CRYPTOMATHX_OWNER_CHAT_ID", "").strip()
        try:
            owner_chat_id = int(owner_raw) if owner_raw else None
        except ValueError as exc:
            raise ConfigurationError("CRYPTOMATHX_OWNER_CHAT_ID must be an integer") from exc

        data_dir = Path(os.getenv("CRYPTOMATHX_DATA_DIR", "data"))
        log_dir = Path(os.getenv("CRYPTOMATHX_LOG_DIR", "logs"))
        legacy_file = Path(os.getenv("CRYPTOMATHX_LEGACY_FAVORITES_FILE", "favorites.json"))
        log_level = os.getenv("CRYPTOMATHX_LOG_LEVEL", "INFO").upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("CRYPTOMATHX_LOG_LEVEL is invalid")
        max_favorites = _bounded_int("CRYPTOMATHX_MAX_FAVORITES", 8, 1, 20)
        default_favorites = _symbols(os.getenv("CRYPTOMATHX_DEFAULT_FAVORITES", "BTC ETH XMR"))
        if len(default_favorites) > max_favorites:
            raise ConfigurationError(
                "CRYPTOMATHX_DEFAULT_FAVORITES exceeds CRYPTOMATHX_MAX_FAVORITES"
            )

        return cls(
            token=token,
            data_dir=data_dir,
            log_dir=log_dir,
            legacy_favorites_file=legacy_file,
            owner_chat_id=owner_chat_id,
            log_level=log_level,
            default_favorites=default_favorites,
            max_favorites=max_favorites,
            max_symbols=_bounded_int("CRYPTOMATHX_MAX_SYMBOLS", 8, 1, 20),
            concurrent_updates=_bounded_int("CRYPTOMATHX_CONCURRENT_UPDATES", 8, 1, 64),
            query_concurrency=_bounded_int("CRYPTOMATHX_QUERY_CONCURRENCY", 6, 1, 32),
            rate_limit_requests=_bounded_int("CRYPTOMATHX_RATE_LIMIT_REQUESTS", 8, 1, 100),
            rate_limit_window=_bounded_int("CRYPTOMATHX_RATE_LIMIT_WINDOW", 30, 1, 3600),
            http_timeout=_bounded_float("CRYPTOMATHX_HTTP_TIMEOUT", 10.0, 1.0, 60.0),
            http_retries=_bounded_int("CRYPTOMATHX_HTTP_RETRIES", 2, 0, 5),
            chart_dpi=_bounded_int("CRYPTOMATHX_CHART_DPI", 140, 72, 240),
        )
