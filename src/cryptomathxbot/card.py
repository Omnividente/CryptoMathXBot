"""Authenticate replayable result cards without storing a query history.

A card keeps the exact expression in its first visible line, not rounded prices.
The MAC binds it to its owner, chat and topic. Restarting the process does not
invalidate it; rotating the bot credential intentionally does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

REQUEST_PREFIX = "Запрос: "
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
_VIEWS = {"text", "1h", "24h", "7d"}


@dataclass(frozen=True, slots=True)
class CardAction:
    token: str
    action: str
    view: str

    @classmethod
    def parse(cls, data: str) -> CardAction:
        parts = data.split("|")
        if len(data.encode("utf-8")) > 64 or len(parts) != 4 or parts[0] != "r":
            raise ValueError("invalid card action")
        _, token, action, view = parts
        if _TOKEN_RE.fullmatch(token) is None or view not in _VIEWS:
            raise ValueError("invalid card action")
        if action not in {"refresh", "chart", "text", "close"}:
            raise ValueError("invalid card action")
        if action == "chart" and view == "text":
            raise ValueError("invalid chart period")
        if action in {"text", "close"} and view != "text":
            raise ValueError("invalid card view")
        return cls(token, action, view)

    def encode(self) -> str:
        value = f"r|{self.token}|{self.action}|{self.view}"
        self.parse(value)
        return value


def read_request(text: str | None) -> str | None:
    if not text:
        return None
    first_line, separator, _ = text.partition("\n")
    if not separator or not first_line.startswith(REQUEST_PREFIX):
        return None
    expression = first_line[len(REQUEST_PREFIX):]
    if not 1 <= len(expression) <= 500 or any(ord(char) < 32 for char in expression):
        return None
    return expression


class CardSigner:
    def __init__(self, credential: str) -> None:
        if not credential:
            raise ValueError("card signing credential must not be empty")
        self._key = hmac.digest(
            credential.encode("utf-8"), b"CryptoMathXBot/result-card/v1", hashlib.sha256,
        )

    def sign(
        self, expression: str, owner_user_id: int, chat_id: int, thread_id: int | None,
    ) -> str:
        if read_request(REQUEST_PREFIX + expression + "\n") != expression:
            raise ValueError("invalid card request")
        payload = json.dumps(
            [expression, owner_user_id, chat_id, thread_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        digest = hmac.digest(self._key, payload, hashlib.sha256)[:16]
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify(
        self, token: str, expression: str, owner_user_id: int, chat_id: int,
        thread_id: int | None,
    ) -> bool:
        if _TOKEN_RE.fullmatch(token) is None:
            return False
        try:
            expected = self.sign(expression, owner_user_id, chat_id, thread_id)
        except ValueError:
            return False
        return hmac.compare_digest(token, expected)
