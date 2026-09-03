from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class _RedactingFormatter(logging.Formatter):
    def __init__(
        self,
        fmt: str,
        *,
        datefmt: str,
        secrets: tuple[str, ...],
    ) -> None:
        super().__init__(fmt, datefmt=datefmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "<redacted>")
        return rendered


def configure_logging(log_dir: Path, level: str, *, secrets: tuple[str, ...] = ()) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = _RedactingFormatter(_FORMAT, datefmt=_DATE_FORMAT, secrets=secrets)

    file_handler = RotatingFileHandler(
        log_dir / "bot.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
