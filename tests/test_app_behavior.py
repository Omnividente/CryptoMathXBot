import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from telegram import Chat, InaccessibleMessage, Message
from telegram.error import BadRequest

from cryptomathxbot.app import (
    _calculate,
    _edit_result_media,
    _edit_result_message,
    _favorite_callback,
    _group_expression,
    _handle_expression,
    _post_init,
    _post_shutdown,
    _refresh_callback,
    _send_html,
    build_application,
    callback_handler,
    favorites_command,
    inline_query_handler,
)
from cryptomathxbot.calculator import ExpressionError, parse_expression
from cryptomathxbot.config import Settings
from cryptomathxbot.domain import Calculation, Chart, Coin, Quote
from cryptomathxbot.session import ActorLocks, QueryRegistry


def settings(tmp_path: Path, *, owner_chat_id: int | None = None) -> Settings:
    return Settings(
        token="123:TEST",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        legacy_favorites_file=tmp_path / "favorites.json",
        owner_chat_id=owner_chat_id,
        log_level="INFO",
        default_favorites=("BTC", "ETH", "XMR"),
        max_favorites=8,
        max_symbols=8,
        concurrent_updates=4,
        query_concurrency=4,
        rate_limit_requests=8,
        rate_limit_window=30,
        http_timeout=2,
        http_retries=1,
        chart_dpi=100,
    )


def update_with_text(text: str, *, reply_to_bot: bool = False) -> Any:
    replied_user = SimpleNamespace(id=99) if reply_to_bot else SimpleNamespace(id=123)
    message = SimpleNamespace(
        text=text,
        message_id=7,
        message_thread_id=None,
        reply_to_message=SimpleNamespace(from_user=replied_user),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_user=SimpleNamespace(id=42),
        callback_query=None,
    )


def test_group_chatter_is_ignored_without_mention_or_reply() -> None:
    context = SimpleNamespace(bot=SimpleNamespace(username="CryptoMathXBot", id=99))

    assert (
        _group_expression(update_with_text("обычный разговор"), context, "обычный разговор") == ""
    )
    assert _group_expression(update_with_text("BTC"), context, "BTC") == ""


def test_group_query_requires_explicit_invocation() -> None:
    context = SimpleNamespace(bot=SimpleNamespace(username="CryptoMathXBot", id=99))

    assert (
        _group_expression(
            update_with_text("@CryptoMathXBot 0.5 BTC"),
            context,
            "@CryptoMathXBot 0.5 BTC",
        )
        == "0.5 BTC"
    )
    assert _group_expression(update_with_text("BTC", reply_to_bot=True), context, "BTC") == "BTC"


@pytest.mark.asyncio
async def test_ephemeral_group_response_refuses_public_fallback() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise BadRequest("ephemeral messages unavailable")

    update = update_with_text("/settings")
    context = SimpleNamespace(bot=Bot())

    with pytest.raises(BadRequest):
        await _send_html(update, context, "settings", ephemeral=True)

    assert calls[0]["api_kwargs"]["ephemeral_message_parameters"]["receiver_user_id"] == 42
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ephemeral_interactive_response_never_publishes_personal_screen() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise BadRequest("ephemeral messages unavailable")

    update = update_with_text("/favorites")
    context = SimpleNamespace(bot=Bot())

    with pytest.raises(BadRequest):
        await _send_html(
            update,
            context,
            "личный список BTC",
            reply_markup=object(),
            ephemeral=True,
        )

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_session_callbacks_reject_parallel_action(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    refresh_calls = 0

    class Query:
        def __init__(self, message: Message) -> None:
            self.data = "q|token|refresh"
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    async def blocked_refresh(update: Any, context: Any, session: Any) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        entered.set()
        await release.wait()

    monkeypatch.setattr("cryptomathxbot.app._refresh_callback", blocked_refresh)
    services = SimpleNamespace(
        registry=SimpleNamespace(get=lambda token, user_id: object()),
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(2),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
    message = Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(42, "private"),
        text="result",
    )
    first_query = Query(message)
    second_query = Query(message)
    first_update = SimpleNamespace(
        callback_query=first_query,
        effective_user=SimpleNamespace(id=42),
    )
    second_update = SimpleNamespace(
        callback_query=second_query,
        effective_user=SimpleNamespace(id=42),
    )

    first_task = asyncio.create_task(callback_handler(first_update, context))
    await entered.wait()
    await callback_handler(second_update, context)
    release.set()
    await first_task

    assert refresh_calls == 1
    assert second_query.answers == [("Предыдущее действие ещё выполняется.", True)]


@pytest.mark.asyncio
async def test_edit_result_message_replaces_photo_with_text_and_removes_old_media() -> None:
    send_calls: list[dict[str, Any]] = []
    deleted = False

    class MessageLike:
        photo = (object(),)
        api_kwargs: ClassVar[dict[str, Any]] = {}
        chat_id = 42
        message_thread_id = None

        async def delete(self) -> None:
            nonlocal deleted
            deleted = True

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            send_calls.append(kwargs)
            return SimpleNamespace(message_id=12)

    await _edit_result_message(Bot(), MessageLike(), "Настройки", object(), receiver_user_id=42)

    assert send_calls[0]["text"] == "Настройки"
    assert deleted


@pytest.mark.asyncio
async def test_edit_ephemeral_photo_uses_new_text_message_then_deletes_old_photo() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class MessageLike:
        photo = (object(),)
        api_kwargs: ClassVar[dict[str, Any]] = {"ephemeral_message_id": 92}
        chat_id = -100
        message_thread_id = 17

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(("send", kwargs))
            return SimpleNamespace(message_id=13)

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    await _edit_result_message(
        Bot(),
        MessageLike(),
        "Настройки",
        object(),
        receiver_user_id=42,
        callback_query_id="callback-19",
    )

    assert calls[0][0] == "send"
    assert calls[0][1]["api_kwargs"] == {
        "ephemeral_message_parameters": {
            "receiver_user_id": 42,
            "callback_query_id": "callback-19",
        }
    }
    assert calls[1][0] == "delete_ephemeral_message"
    assert calls[1][1]["ephemeral_message_id"] == 92


@pytest.mark.asyncio
async def test_favorite_callback_acknowledges_before_waiting_for_lock() -> None:
    locks = ActorLocks()
    held = locks.get(42)
    await held.acquire()

    class Query:
        message = None
        id = "callback-1"

        def __init__(self) -> None:
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    query = Query()

    class Store:
        async def favorites(self, user_id: int, chat_id: int) -> tuple[str, ...]:
            return ("BTC",)

        async def set_favorites(self, user_id: int, favorites: tuple[str, ...]) -> tuple[str, ...]:
            return favorites

    services = SimpleNamespace(
        preference_locks=locks,
        store=Store(),
        settings=SimpleNamespace(default_favorites=("BTC",), max_favorites=8),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"services": services}),
        bot=SimpleNamespace(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_message=None,
    )

    task = asyncio.create_task(_favorite_callback(update, context, ["fav", "reset"]))
    await asyncio.sleep(0)
    assert query.answers == [("Сохраняю…", False)]

    held.release()
    await task


@pytest.mark.asyncio
async def test_group_favorite_callback_does_not_edit_public_settings_message() -> None:
    sent: list[dict[str, Any]] = []

    class Query:
        id = "callback-2"

        def __init__(self, message: Message) -> None:
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    class Store:
        async def favorites(self, user_id: int, chat_id: int) -> tuple[str, ...]:
            return ("BTC",)

        async def set_favorites(self, user_id: int, favorites: tuple[str, ...]) -> tuple[str, ...]:
            return favorites

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            sent.append(kwargs)
            return SimpleNamespace(message_id=14)

    message = Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(-100, "supergroup"),
        text="Настройки",
    )
    query = Query(message)
    services = SimpleNamespace(
        preference_locks=ActorLocks(),
        store=Store(),
        settings=SimpleNamespace(default_favorites=("BTC",), max_favorites=8),
    )
    context = SimpleNamespace(
        application=SimpleNamespace(bot_data={"services": services}),
        bot=Bot(),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_message=message,
    )

    await _favorite_callback(update, context, ["fav", "toggle", "ETH"])

    assert len(sent) == 1
    assert sent[0]["api_kwargs"] == {
        "ephemeral_message_parameters": {"receiver_user_id": 42}
    }
    assert sent[0]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_calculation_rejects_two_assets_with_same_symbol() -> None:
    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            assert symbols == ("SIDESHIFT", "XAI")
            return {
                "SIDESHIFT": Coin("sideshift-token", "XAI", "SideShift"),
                "XAI": Coin("xai-blockchain", "XAI", "Xai"),
            }

    parsed = parse_expression("SIDESHIFT + XAI")
    services = SimpleNamespace(market=Market())

    with pytest.raises(ExpressionError, match="нескольким разным монетам"):
        await _calculate(parsed, services)


def test_application_registers_all_supported_update_handlers(tmp_path: Path) -> None:
    application = build_application(settings(tmp_path))

    assert sum(len(group) for group in application.handlers.values()) == 9
    assert application.bot_data["services"].settings.max_symbols == 8


@pytest.mark.asyncio
async def test_post_init_configures_profile_and_notifies_owner(tmp_path: Path) -> None:
    events: list[tuple[str, Any]] = []

    class Store:
        async def initialize(self, legacy_file: Path) -> None:
            events.append(("store", legacy_file))

    class Market:
        async def start(self) -> None:
            events.append(("market-start", None))

        async def close(self) -> None:
            events.append(("market-close", None))

    class Bot:
        async def set_my_commands(self, commands: Any, **kwargs: Any) -> None:
            events.append(("commands", ([command.command for command in commands], kwargs)))

        async def set_my_description(self, description: str) -> None:
            events.append(("description", description))

        async def set_my_short_description(self, description: str) -> None:
            events.append(("short-description", description))

        async def get_me(self) -> Any:
            return SimpleNamespace(username="CryptoMathXBot")

        async def send_message(self, chat_id: int, text: str) -> None:
            events.append(("owner", (chat_id, text)))

    services = SimpleNamespace(
        settings=settings(tmp_path, owner_chat_id=42),
        store=Store(),
        market=Market(),
    )
    application = SimpleNamespace(bot=Bot(), bot_data={"services": services})

    await _post_init(application)
    await _post_shutdown(application)

    assert events[0][0] == "store"
    assert events[1] == ("market-start", None)
    assert [event[0] for event in events].count("commands") == 2
    command_events = [event[1] for event in events if event[0] == "commands"]
    assert any("favorites" in names and len(names) == 5 for names, _ in command_events)
    assert ("owner", (42, "CryptoMathXBot v2.0.0 запущен и готов к работе.")) in events
    assert events[-1] == ("market-close", None)


@pytest.mark.asyncio
async def test_calculation_combines_market_quotes_with_constant() -> None:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    quote = Quote(
        coin,
        Decimal("10"),
        Decimal("1.5"),
        "test",
        None,
        datetime.now(timezone.utc),
    )

    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            return {"BTC": coin}

        async def quotes(self, coins: tuple[Coin, ...]) -> dict[str, Quote]:
            assert coins == (coin,)
            return {"BTC": quote}

        async def usd_rub(self) -> tuple[Decimal, str]:
            return Decimal("80"), "02.09.2026"

    result = await _calculate(parse_expression("2 BTC + 5"), SimpleNamespace(market=Market()))

    assert result.total_usd == Decimal("25")
    assert result.total_rub == Decimal("2000")


@pytest.mark.asyncio
async def test_ephemeral_command_replies_to_ephemeral_message_id() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(message_id=0)

    update = update_with_text("/settings")
    update.effective_message.message_id = 0
    update.effective_message.api_kwargs = {"ephemeral_message_id": 731}

    await _send_html(update, SimpleNamespace(bot=Bot()), "settings", ephemeral=True)

    assert calls[0]["reply_parameters"] == {"ephemeral_message_id": 731}
    assert calls[0]["api_kwargs"]["ephemeral_message_parameters"] == {"receiver_user_id": 42}


@pytest.mark.asyncio
async def test_callback_ephemeral_response_uses_callback_identity_without_public_fallback() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise BadRequest("ephemeral unavailable")

    update = update_with_text("button")
    update.callback_query = SimpleNamespace(id="callback-17")

    with pytest.raises(BadRequest):
        await _send_html(update, SimpleNamespace(bot=Bot()), "private", ephemeral=True)

    parameters = calls[0]["api_kwargs"]["ephemeral_message_parameters"]
    assert parameters == {
        "receiver_user_id": 42,
        "callback_query_id": "callback-17",
        "replace_callback_query_message": True,
    }
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ephemeral_message_is_edited_through_raw_bot_api() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class Bot:
        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    message = SimpleNamespace(
        api_kwargs={"ephemeral_message_id": 91},
        chat_id=-100,
        photo=(),
    )

    await _edit_result_message(Bot(), message, "<b>result</b>", None, receiver_user_id=42)

    assert calls == [
        (
            "edit_ephemeral_message_text",
            {
                "chat_id": -100,
                "receiver_user_id": 42,
                "ephemeral_message_id": 91,
                "text": "<b>result</b>",
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
                "reply_markup": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_inaccessible_callback_gets_one_explicit_answer() -> None:
    class Query:
        def __init__(self) -> None:
            self.data = "menu|home"
            self.message = InaccessibleMessage(Chat(-100, "supergroup"), 7)
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    query = Query()
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))

    await callback_handler(update, SimpleNamespace())

    assert query.answers == [("Сообщение с кнопкой больше недоступно. Откройте /start.", True)]


@pytest.mark.asyncio
async def test_callback_failure_answers_once_and_delivers_visible_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Query:
        def __init__(self, message: Message) -> None:
            self.id = "callback-18"
            self.data = "q|token|refresh"
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            self.kwargs = kwargs
            return SimpleNamespace(message_id=0)

    async def failed_refresh(update: Any, context: Any, session: Any) -> str:
        return "⚠️ Не удалось обновить цены. Попробуйте позже."

    monkeypatch.setattr("cryptomathxbot.app._refresh_callback", failed_refresh)
    message = Message(
        message_id=7,
        date=datetime.now(timezone.utc),
        chat=Chat(-100, "supergroup"),
        text="result",
    )
    query = Query(message)
    bot = Bot()
    services = SimpleNamespace(
        registry=SimpleNamespace(get=lambda token, user_id: object()),
        actor_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(1),
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
    )
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=message.chat,
        effective_message=message,
    )

    await callback_handler(update, context)

    assert query.answers == [("Обновляю…", False)]
    assert bot.kwargs["text"] == "⚠️ Не удалось обновить цены. Попробуйте позже."


@pytest.mark.asyncio
async def test_callback_calculation_does_not_leave_a_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_calls = 0
    edited: list[str] = []

    async def show_draft(*args: Any, **kwargs: Any) -> int:
        nonlocal draft_calls
        draft_calls += 1
        return 1

    async def edit_result(*args: Any, **kwargs: Any) -> None:
        edited.append(args[2])

    monkeypatch.setattr("cryptomathxbot.app._show_draft", show_draft)
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edit_result)
    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(1),
        settings=SimpleNamespace(max_symbols=8),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    message = SimpleNamespace(api_kwargs={}, photo=())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_message=SimpleNamespace(message_thread_id=None),
        update_id=100,
    )

    await _handle_expression(update, context, "2 + 2", edit_message=message)

    assert draft_calls == 0
    assert edited == ["<code>2 + 2</code> = <b>4</b>"]


@pytest.mark.asyncio
async def test_inline_parse_error_returns_explanatory_result() -> None:
    class InlineQuery:
        query = "BTC +"

        def __init__(self) -> None:
            self.answers: list[tuple[list[Any], dict[str, Any]]] = []

        async def answer(self, results: list[Any], **kwargs: Any) -> None:
            self.answers.append((results, kwargs))

    inline_query = InlineQuery()
    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        settings=SimpleNamespace(max_symbols=8),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
    update = SimpleNamespace(inline_query=inline_query, effective_user=SimpleNamespace(id=42))

    await inline_query_handler(update, context)

    results, kwargs = inline_query.answers[0]
    assert len(results) == 1
    assert results[0].title == "Проверьте выражение"
    assert kwargs == {"cache_time": 2, "is_personal": True}


@pytest.mark.asyncio
async def test_photo_refresh_rerenders_active_chart_and_keeps_selection() -> None:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    old_quote = Quote(
        coin,
        Decimal("100"),
        Decimal("1"),
        "Binance",
        "BTCUSDT",
        datetime.now(timezone.utc),
    )
    new_quote = Quote(
        coin,
        Decimal("101"),
        Decimal("2"),
        "Binance",
        "BTCUSDT",
        datetime.now(timezone.utc),
    )
    initial = Calculation(
        expression="BTC",
        coefficients={"BTC": Decimal(1)},
        constant_usd=Decimal(0),
        quotes={"BTC": old_quote},
        total_usd=Decimal("100"),
        usd_rub=Decimal("80"),
        cbr_date="03.09.2026",
    )
    registry = QueryRegistry()
    session = registry.set_active_timeframe(registry.create(42, "BTC", initial), "1h")
    calls: list[tuple[str, dict[str, Any]]] = []

    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            return {"BTC": coin}

        async def quotes(
            self, coins: tuple[Coin, ...], *, force_refresh: bool = False
        ) -> dict[str, Quote]:
            assert force_refresh
            return {"BTC": new_quote}

        async def usd_rub(self) -> tuple[Decimal, str]:
            return Decimal("80"), "03.09.2026"

        async def chart(
            self, quote: Quote, timeframe: str, *, force_refresh: bool = False
        ) -> Chart:
            assert quote is new_quote
            assert timeframe == "1h"
            assert force_refresh
            return Chart("BTC", "1h", ((1_000, 100.0), (2_000, 101.0)), "Binance")

    class Charts:
        async def render(self, chart: Chart) -> bytes:
            return b"png"

    class Bot:
        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    message = Message(
        message_id=0,
        date=datetime.now(timezone.utc),
        chat=Chat(-100, "supergroup"),
        photo=(SimpleNamespace(),),
        api_kwargs={"ephemeral_message_id": 92},
    )
    services = SimpleNamespace(
        settings=SimpleNamespace(max_symbols=8),
        market=Market(),
        charts=Charts(),
        registry=registry,
    )
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        callback_query=SimpleNamespace(message=message),
        effective_user=SimpleNamespace(id=42),
    )

    error = await _refresh_callback(update, context, session)

    assert error is None
    assert calls[0][0] == "edit_ephemeral_message_media"
    assert calls[0][1]["receiver_user_id"] == 42
    assert registry.get(session.token, 42).active_timeframe == "1h"
    assert "График BTC · 1 ч · изменение +1.00%" in calls[0][1]["media"].caption



@pytest.mark.asyncio
async def test_text_result_chart_upload_keeps_png_bytes_after_media_paths() -> None:
    uploaded: list[bytes] = []

    class Bot:
        async def send_photo(self, **kwargs: Any) -> Any:
            photo = kwargs["photo"]
            uploaded.append(photo.read())
            return SimpleNamespace(message_id=8)

    deleted = False

    async def delete() -> None:
        nonlocal deleted
        deleted = True

    message = SimpleNamespace(
        chat_id=123,
        message_thread_id=None,
        photo=None,
        delete=delete,
    )
    image = BytesIO(b"\x89PNG\r\n\x1a\nchart")
    image.name = "btc-1h.png"

    await _edit_result_media(
        Bot(),
        message,
        image,
        "<b>График</b>",
        None,
        receiver_user_id=42,
    )

    assert uploaded == [b"\x89PNG\r\n\x1a\nchart"]

@pytest.mark.asyncio
async def test_favorites_command_does_not_bypass_query_rate_limit() -> None:
    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            raise AssertionError("market must not be called")

    services = SimpleNamespace(
        limiter=SimpleNamespace(
            check=lambda key: SimpleNamespace(allowed=False, notify=False, retry_after=10.0)
        ),
        market=Market(),
        settings=SimpleNamespace(max_favorites=8),
    )
    context = SimpleNamespace(
        args=["BTC"],
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))

    await favorites_command(update, context)



@pytest.mark.asyncio
async def test_slow_group_favorites_sends_ephemeral_progress_before_market_lookup() -> None:
    progress_sent = asyncio.Event()
    release_market = asyncio.Event()
    calls: list[tuple[str, Any]] = []

    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            assert progress_sent.is_set()
            await release_market.wait()
            coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
            return {"BTC": coin}

    class Store:
        async def set_favorites(self, user_id: int, symbols: tuple[str, ...]) -> tuple[str, ...]:
            return symbols

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            calls.append(("send", kwargs))
            progress_sent.set()
            return Message(
                message_id=0,
                date=datetime.now(timezone.utc),
                chat=Chat(-100, "supergroup"),
                text=kwargs["text"],
                api_kwargs={"ephemeral_message_id": 94},
            )

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        market=Market(),
        store=Store(),
        preference_locks=ActorLocks(),
        settings=SimpleNamespace(max_favorites=8),
    )
    context = SimpleNamespace(
        args=["BTC"],
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    incoming = SimpleNamespace(
        message_id=0,
        message_thread_id=None,
        api_kwargs={"ephemeral_message_id": 93},
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_message=incoming,
        callback_query=None,
    )

    task = asyncio.create_task(favorites_command(update, context))
    await progress_sent.wait()
    assert calls[0][0] == "send"
    assert calls[0][1]["text"] == "⏳ Проверяю монеты…"

    release_market.set()
    await task
    assert calls[1][0] == "edit_ephemeral_message_text"
    assert calls[1][1]["ephemeral_message_id"] == 94

@pytest.mark.asyncio
async def test_result_callback_is_rate_limited_before_market_work() -> None:
    answers: list[tuple[str | None, bool]] = []

    class Query:
        data = "q|token|refresh"
        message = Message(
            message_id=7,
            date=datetime.now(timezone.utc),
            chat=Chat(42, "private"),
            text="result",
        )

        async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
            answers.append((text, show_alert))

    session = SimpleNamespace(calculation=SimpleNamespace(coefficients={"BTC": 1}))
    services = SimpleNamespace(
        registry=SimpleNamespace(get=lambda token, user_id: session),
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=False, retry_after=10.0)),
    )
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"services": services}))
    update = SimpleNamespace(callback_query=Query(), effective_user=SimpleNamespace(id=42))

    await callback_handler(update, context)

    assert answers == [("Слишком часто. Повторите через 10 с.", True)]


@pytest.mark.asyncio
async def test_concurrent_favorite_toggles_preserve_both_changes() -> None:
    class Store:
        value = ("BTC",)

        async def favorites(self, user_id: int, chat_id: int) -> tuple[str, ...]:
            snapshot = self.value
            await asyncio.sleep(0)
            return snapshot

        async def set_favorites(self, user_id: int, symbols: tuple[str, ...]) -> tuple[str, ...]:
            await asyncio.sleep(0)
            self.value = symbols
            return symbols

    class Query:
        message = None

        async def answer(self, text: str, show_alert: bool = False) -> None:
            return None

    store = Store()
    services = SimpleNamespace(
        store=store,
        preference_locks=ActorLocks(),
        settings=SimpleNamespace(default_favorites=("BTC",), max_favorites=8),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        application=SimpleNamespace(bot_data={"services": services}),
    )

    def update() -> Any:
        return SimpleNamespace(
            callback_query=Query(),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=42),
        )

    await asyncio.gather(
        _favorite_callback(update(), context, ["fav", "toggle", "ETH"]),
        _favorite_callback(update(), context, ["fav", "toggle", "SOL"]),
    )

    assert store.value == ("BTC", "ETH", "SOL")
