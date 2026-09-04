import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from telegram import Chat, InaccessibleMessage, Message
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler, InlineQueryHandler, MessageHandler

from cryptomathxbot.app import (
    _calculate,
    _chart_callback,
    _edit_error_message,
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
    main,
)
from cryptomathxbot.calculator import ExpressionError, parse_expression
from cryptomathxbot.config import Settings
from cryptomathxbot.domain import Calculation, Chart, Coin, Quote
from cryptomathxbot.market import MarketUnavailable
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
        query_timeout=5,
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


def test_group_chatter_is_ignored_without_mention() -> None:
    context = SimpleNamespace(bot=SimpleNamespace(username="CryptoMathXBot", id=99))

    assert _group_expression(context, "обычный разговор") == ""
    assert _group_expression(context, "BTC") == ""
    assert _group_expression(context, "спасибо") == ""


def test_group_query_requires_explicit_mention() -> None:
    context = SimpleNamespace(bot=SimpleNamespace(username="CryptoMathXBot", id=99))

    assert _group_expression(context, "@CryptoMathXBot 0.5 BTC") == "0.5 BTC"
    assert _group_expression(context, "BTC") == ""




@pytest.mark.asyncio
async def test_ephemeral_group_response_refuses_public_fallback() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise BadRequest("ephemeral messages unavailable")

    update = update_with_text("/settings")
    update.effective_message.api_kwargs = {"ephemeral_message_id": 70}
    context = SimpleNamespace(bot=Bot())

    with pytest.raises(BadRequest):
        await _send_html(update, context, "settings", ephemeral=True)

    assert calls[0]["api_kwargs"]["ephemeral_message_parameters"]["receiver_user_id"] == 42
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_public_group_message_cannot_build_invalid_ephemeral_request() -> None:
    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            raise AssertionError("Telegram must not be called for an ineligible trigger")

    update = update_with_text("@CryptoMathXBot BTC")

    with pytest.raises(RuntimeError, match="eligible Telegram trigger"):
        await _send_html(update, SimpleNamespace(bot=Bot()), "result", ephemeral=True)


@pytest.mark.asyncio
async def test_ephemeral_send_rejects_response_without_identifier() -> None:
    calls: list[dict[str, Any]] = []
    deleted = False

    class Response:
        api_kwargs: ClassVar[dict[str, Any]] = {}

        async def delete(self) -> None:
            nonlocal deleted
            deleted = True

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return Response()

    update = update_with_text("/settings")
    update.effective_message.api_kwargs = {"ephemeral_message_id": 70}

    with pytest.raises(RuntimeError, match="incomplete ephemeral message"):
        await _send_html(update, SimpleNamespace(bot=Bot()), "settings", ephemeral=True)

    assert len(calls) == 1
    assert deleted


@pytest.mark.asyncio
async def test_public_group_query_error_is_delivered_publicly() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_chat_action(self, **kwargs: Any) -> None:
            return None

        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(message_id=8)

    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(1),
        settings=SimpleNamespace(max_symbols=8, query_timeout=1),
    )
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = update_with_text("@CryptoMathXBot BTC +")

    await _handle_expression(update, context, "BTC +")

    assert len(calls) == 1
    assert calls[0]["api_kwargs"] is None
    assert "Проверьте скобки и операторы" in calls[0]["text"]


@pytest.mark.asyncio
async def test_ephemeral_interactive_response_never_publishes_personal_screen() -> None:
    calls: list[dict[str, Any]] = []

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise BadRequest("ephemeral messages unavailable")

    update = update_with_text("/favorites")
    update.effective_message.api_kwargs = {"ephemeral_message_id": 71}
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


def test_main_reports_invalid_configuration_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CRYPTOMATHX_BOT_TOKEN", "redacted-test-token")
    monkeypatch.setenv("CRYPTOMATHX_MAX_SYMBOLS", "999")

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "Ошибка конфигурации" in captured.err
    assert "Traceback" not in captured.err



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

    async def blocked_refresh(
        update: Any,
        context: Any,
        session: Any,
        *,
        edit_message: Message | None = None,
    ) -> None:
        nonlocal refresh_calls
        assert edit_message is not None
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
        date=datetime.now(UTC),
        chat=Chat(42, "private"),
        text="result",
    )
    first_query = Query(message)
    second_query = Query(message)
    first_update = SimpleNamespace(
        callback_query=first_query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=message.chat,
        effective_message=message,
    )
    second_update = SimpleNamespace(
        callback_query=second_query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=message.chat,
        effective_message=message,
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
            return SimpleNamespace(api_kwargs={"ephemeral_message_id": 93})

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
    assert calls[0][1]["reply_parameters"] == {"ephemeral_message_id": 92}
    assert calls[1][0] == "delete_ephemeral_message"
    assert calls[1][1]["ephemeral_message_id"] == 92


@pytest.mark.asyncio
async def test_incomplete_ephemeral_media_replacement_keeps_original() -> None:
    delete_calls = 0

    class MessageLike:
        photo = (object(),)
        api_kwargs: ClassVar[dict[str, Any]] = {"ephemeral_message_id": 92}
        chat_id = -100
        message_thread_id = None

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            return SimpleNamespace(api_kwargs={})

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            nonlocal delete_calls
            delete_calls += 1
            return True

    with pytest.raises(RuntimeError, match="incomplete ephemeral message"):
        await _edit_result_message(
            Bot(),
            MessageLike(),
            "Настройки",
            object(),
            receiver_user_id=42,
            callback_query_id="callback-19",
        )

    assert delete_calls == 0


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
            return SimpleNamespace(
                message_id=0,
                api_kwargs={"ephemeral_message_id": 93},
            )

    message = Message(
        message_id=7,
        date=datetime.now(UTC),
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
        "ephemeral_message_parameters": {
            "receiver_user_id": 42,
            "callback_query_id": "callback-2",
            "replace_callback_query_message": True,
        }
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


def test_application_registers_supported_handler_contracts(tmp_path: Path) -> None:
    application = build_application(settings(tmp_path))
    handlers = [handler for group in application.handlers.values() for handler in group]
    command_names = {
        command
        for handler in handlers
        if isinstance(handler, CommandHandler)
        for command in handler.commands
    }

    assert command_names == {"start", "help", "price", "favorites", "settings", "ping"}
    assert sum(isinstance(handler, CallbackQueryHandler) for handler in handlers) == 1
    assert sum(isinstance(handler, InlineQueryHandler) for handler in handlers) == 1
    assert sum(isinstance(handler, MessageHandler) for handler in handlers) == 1


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
        username = "CryptoMathXBot"

        async def set_my_commands(self, commands: Any, **kwargs: Any) -> None:
            events.append(("commands", (list(commands), kwargs)))

        async def set_my_description(self, description: str) -> None:
            events.append(("description", description))

        async def set_my_short_description(self, description: str) -> None:
            events.append(("short-description", description))


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
    command_events = [event[1] for event in events if event[0] == "commands"]
    group_commands = next(commands for commands, _ in command_events if len(commands) == 5)
    assert {command.command for command in group_commands} == {
        "price",
        "favorites",
        "settings",
        "help",
        "ping",
    }
    assert all(command.api_kwargs.get("is_ephemeral") is True for command in group_commands)
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
        datetime.now(UTC),
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
            return SimpleNamespace(
                message_id=0,
                api_kwargs={"ephemeral_message_id": 732},
            )

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
async def test_public_group_callback_anchors_delayed_error_before_market_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    progress_sent = asyncio.Event()

    class Query:
        def __init__(self, message: Message) -> None:
            self.id = "callback-18"
            self.data = "q|token|refresh"
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            calls.append(("send_message", kwargs))
            progress_sent.set()
            return Message(
                message_id=0,
                date=datetime.now(UTC),
                chat=Chat(-100, "supergroup"),
                text=kwargs["text"],
                api_kwargs={"ephemeral_message_id": 95},
            )

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    async def failed_refresh(
        update: Any,
        context: Any,
        session: Any,
        *,
        edit_message: Message | None = None,
    ) -> str:
        assert progress_sent.is_set()
        assert edit_message is not None
        return "⚠️ Не удалось обновить цены. Попробуйте позже."

    monkeypatch.setattr("cryptomathxbot.app._refresh_callback", failed_refresh)
    recovery_markup = object()
    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        text="result",
        reply_markup=recovery_markup,
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
    assert calls[0][0] == "send_message"
    assert calls[0][1]["text"] == "⏳ Обновляю…"
    assert calls[1][0] == "edit_ephemeral_message_text"
    assert calls[1][1]["ephemeral_message_id"] == 95
    assert calls[1][1]["reply_markup"] is recovery_markup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback_data", "expected_edit", "answer_text"),
    [
        ("q|token|refresh", "text", "Обновляю…"),
        ("q|token|chart|1h", "media", "Готовлю график…"),
    ],
)
async def test_public_group_callback_renders_success_in_ephemeral_overlay(
    monkeypatch: pytest.MonkeyPatch,
    callback_data: str,
    expected_edit: str,
    answer_text: str,
) -> None:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    quote = Quote(
        coin,
        Decimal("100"),
        Decimal("1"),
        "Binance",
        "BTCUSDT",
        datetime.now(UTC),
    )
    calculation = Calculation(
        expression="BTC",
        coefficients={"BTC": Decimal(1)},
        constant_usd=Decimal(0),
        quotes={"BTC": quote},
        total_usd=Decimal("100"),
        usd_rub=Decimal("80"),
        cbr_date="04.09.2026",
    )
    session = SimpleNamespace(
        token="token",
        expression="BTC",
        calculation=calculation,
        active_timeframe=None,
    )
    overlay = Message(
        message_id=0,
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        text="⏳",
        api_kwargs={"ephemeral_message_id": 96},
    )
    edits: list[tuple[str, Message]] = []
    send_calls: list[dict[str, Any]] = []

    class Query:
        id = "callback-20"

        def __init__(self, message: Message) -> None:
            self.data = callback_data
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    class Registry:
        def get(self, token: str, user_id: int) -> Any:
            assert (token, user_id) == ("token", 42)
            return session

        def update(self, current: Any, result: Calculation) -> None:
            assert current is session
            assert result is calculation

        def set_active_timeframe(self, current: Any, timeframe: str) -> Any:
            assert current is session
            assert timeframe == "1h"
            return session

    class Market:
        async def chart(self, current_quote: Quote, timeframe: str) -> Chart:
            assert current_quote is quote
            assert timeframe == "1h"
            return Chart("BTC", "1h", ((1_000, 100.0), (2_000, 101.0)), "Binance")

    class Charts:
        async def render(self, chart: Chart) -> bytes:
            assert chart.symbol == "BTC"
            return b"png"

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            send_calls.append(kwargs)
            return overlay

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            raise AssertionError(f"unexpected raw API call: {endpoint} {api_kwargs}")

    async def refresh_market_data(
        current: Any,
        current_services: Any,
        *,
        include_chart: bool,
    ) -> tuple[Calculation, None]:
        assert current is session
        assert not include_chart
        return calculation, None

    async def edit_text(
        bot: Any,
        message: Message,
        text: str,
        reply_markup: Any,
        *,
        receiver_user_id: int,
        callback_query_id: str | None = None,
    ) -> None:
        assert receiver_user_id == 42
        edits.append(("text", message))

    async def edit_media(
        bot: Any,
        message: Message,
        image: Any,
        caption: str,
        reply_markup: Any,
        *,
        receiver_user_id: int,
    ) -> None:
        assert receiver_user_id == 42
        edits.append(("media", message))

    monkeypatch.setattr("cryptomathxbot.app._refresh_market_data", refresh_market_data)
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edit_text)
    monkeypatch.setattr("cryptomathxbot.app._edit_result_media", edit_media)
    source_message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        text="public result",
    )
    query = Query(source_message)
    services = SimpleNamespace(
        registry=Registry(),
        actor_locks=ActorLocks(),
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        query_slots=asyncio.Semaphore(1),
        settings=SimpleNamespace(query_timeout=1, max_symbols=8),
        market=Market(),
        charts=Charts(),
    )
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=source_message.chat,
        effective_message=source_message,
    )

    await callback_handler(update, context)

    assert query.answers == [(answer_text, False)]
    assert edits == [(expected_edit, overlay)]
    assert edits[0][1] is not source_message
    assert len(send_calls) == 1
    assert send_calls[0]["api_kwargs"]["ephemeral_message_parameters"] == {
        "receiver_user_id": 42,
        "callback_query_id": "callback-20",
        "replace_callback_query_message": True,
    }


@pytest.mark.asyncio
async def test_cancelled_public_group_callback_removes_ephemeral_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    started = asyncio.Event()

    class Query:
        id = "callback-cancel"
        data = "q|token|refresh"

        def __init__(self, message: Message) -> None:
            self.message = message

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            return None

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            return Message(
                message_id=0,
                date=datetime.now(UTC),
                chat=Chat(-100, "supergroup"),
                text=kwargs["text"],
                api_kwargs={"ephemeral_message_id": 97},
            )

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    async def blocked_refresh(
        update: Any,
        context: Any,
        session: Any,
        *,
        edit_message: Message | None = None,
    ) -> None:
        assert edit_message is not None
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("cryptomathxbot.app._refresh_callback", blocked_refresh)
    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        text="result",
    )
    actor_locks = ActorLocks()
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(
            bot_data={
                "services": SimpleNamespace(
                    registry=SimpleNamespace(get=lambda token, user_id: object()),
                    actor_locks=actor_locks,
                    limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
                )
            }
        ),
    )
    update = SimpleNamespace(
        callback_query=Query(message),
        effective_user=SimpleNamespace(id=42),
        effective_chat=message.chat,
        effective_message=message,
    )

    task = asyncio.create_task(callback_handler(update, context))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [
        (
            "delete_ephemeral_message",
            {"chat_id": -100, "receiver_user_id": 42, "ephemeral_message_id": 97},
        )
    ]
    assert not actor_locks.get(42).locked()




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
        settings=SimpleNamespace(max_symbols=8, query_timeout=1),
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
        callback_query=None,
        update_id=100,
    )

    await _handle_expression(update, context, "2 + 2", edit_message=message)
    assert draft_calls == 0
    assert edited == ["<code>2 + 2</code> = <b>4</b>"]


@pytest.mark.asyncio
async def test_public_group_symbol_rate_limit_does_not_leave_eager_progress() -> None:
    calls: list[dict[str, Any]] = []
    recovery_markup = object()

    class Query:
        id = "callback-rate-limit"
        data = "symbol|BTC"

        def __init__(self, message: Message) -> None:
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    class Bot:
        async def send_message(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(api_kwargs={"ephemeral_message_id": 100})

    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        text="home",
        reply_markup=recovery_markup,
    )
    query = Query(message)
    services = SimpleNamespace(
        limiter=SimpleNamespace(
            check=lambda key: SimpleNamespace(allowed=False, notify=True, retry_after=2)
        ),
    )
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=message.chat,
        effective_message=message,
    )

    await callback_handler(update, context)

    assert query.answers == [("Получаю цену…", False)]
    assert len(calls) == 1
    assert "Слишком часто" in calls[0]["text"]
    assert "Получаю цену" not in calls[0]["text"]
    assert calls[0]["reply_markup"] is recovery_markup


@pytest.mark.asyncio
async def test_symbol_callback_failure_keeps_home_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edits: list[tuple[str, Any]] = []
    recovery_markup = object()

    class Query:
        data = "symbol|BTC"

        def __init__(self, message: Message) -> None:
            self.message = message
            self.answers: list[tuple[str | None, bool]] = []

        async def answer(self, text: str | None = None, *, show_alert: bool = False) -> None:
            self.answers.append((text, show_alert))

    async def unavailable_calculation(*args: Any, **kwargs: Any) -> Any:
        raise MarketUnavailable("offline")

    async def edit_result(*args: Any, **kwargs: Any) -> None:
        edits.append((args[2], args[3]))

    monkeypatch.setattr("cryptomathxbot.app._calculate", unavailable_calculation)
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edit_result)
    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(42, "private"),
        text="home",
        reply_markup=recovery_markup,
    )
    query = Query(message)
    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        notice_limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(1),
        settings=SimpleNamespace(max_symbols=8, query_timeout=1),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
        effective_chat=message.chat,
        effective_message=message,
    )

    await callback_handler(update, context)

    assert query.answers == [("Получаю цену…", False)]
    assert "временно недоступны" in edits[0][0]
    assert edits[0][1] is recovery_markup



@pytest.mark.asyncio
async def test_group_price_anchors_ephemeral_progress_before_market_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    progress_sent = asyncio.Event()
    release_market = asyncio.Event()
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    calculation = Calculation(
        expression="BTC",
        coefficients={"BTC": Decimal(1)},
        constant_usd=Decimal(0),
        quotes={
            "BTC": Quote(
                coin,
                Decimal("100"),
                Decimal("1"),
                "CoinGecko",
                None,
                datetime.now(UTC),
            )
        },
        total_usd=Decimal("100"),
        usd_rub=None,
        cbr_date=None,
    )

    async def delayed_calculation(*args: Any, **kwargs: Any) -> Calculation:
        assert progress_sent.is_set()
        await release_market.wait()
        return calculation

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            calls.append(("send_message", kwargs))
            progress_sent.set()
            return Message(
                message_id=0,
                date=datetime.now(UTC),
                chat=Chat(-100, "supergroup"),
                text=kwargs["text"],
                api_kwargs={"ephemeral_message_id": 98},
            )

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    monkeypatch.setattr("cryptomathxbot.app._calculate", delayed_calculation)
    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        notice_limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(1),
        settings=SimpleNamespace(max_symbols=8, query_timeout=1),
        registry=SimpleNamespace(create=lambda user_id, expression, result: SimpleNamespace(token="t")),
    )
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    incoming = SimpleNamespace(
        message_id=0,
        message_thread_id=None,
        api_kwargs={"ephemeral_message_id": 97},
    )
    update = SimpleNamespace(
        update_id=100,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_message=incoming,
        callback_query=None,
    )

    task = asyncio.create_task(_handle_expression(update, context, "BTC"))
    await progress_sent.wait()
    assert calls[0][0] == "send_message"
    assert calls[0][1]["text"] == "⏳ Обрабатываю запрос…"

    release_market.set()
    await task

    assert calls[1][0] == "edit_ephemeral_message_text"
    assert calls[1][1]["ephemeral_message_id"] == 98


@pytest.mark.asyncio
async def test_cancelled_group_query_removes_ephemeral_progress_and_releases_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    started = asyncio.Event()

    async def blocked_calculation(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            return Message(
                message_id=0,
                date=datetime.now(UTC),
                chat=Chat(-100, "supergroup"),
                text=kwargs["text"],
                api_kwargs={"ephemeral_message_id": 99},
            )

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    monkeypatch.setattr("cryptomathxbot.app._calculate", blocked_calculation)
    actor_locks = ActorLocks()
    query_slots = asyncio.Semaphore(1)
    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        notice_limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=actor_locks,
        query_slots=query_slots,
        settings=SimpleNamespace(max_symbols=8, query_timeout=10),
    )
    context = SimpleNamespace(
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    incoming = SimpleNamespace(
        message_id=0,
        message_thread_id=None,
        api_kwargs={"ephemeral_message_id": 98},
    )
    update = SimpleNamespace(
        update_id=100,
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_message=incoming,
        callback_query=None,
    )

    task = asyncio.create_task(_handle_expression(update, context, "BTC"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [
        (
            "delete_ephemeral_message",
            {"chat_id": -100, "receiver_user_id": 42, "ephemeral_message_id": 99},
        )
    ]
    assert not actor_locks.get(42).locked()
    await asyncio.wait_for(query_slots.acquire(), timeout=0.1)
    query_slots.release()


@pytest.mark.asyncio
async def test_text_query_timeout_releases_actor_and_query_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[tuple[str, Any]] = []
    started = asyncio.Event()
    recovery_markup = object()

    async def blocked_calculation(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await asyncio.Event().wait()

    async def edit_result(*args: Any, **kwargs: Any) -> None:
        delivered.append((args[2], args[3]))

    monkeypatch.setattr("cryptomathxbot.app._calculate", blocked_calculation)
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edit_result)
    actor_locks = ActorLocks()
    query_slots = asyncio.Semaphore(1)
    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        actor_locks=actor_locks,
        query_slots=query_slots,
        settings=SimpleNamespace(max_symbols=8, query_timeout=0.01),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_message=SimpleNamespace(message_thread_id=None),
        callback_query=None,
        update_id=100,
    )

    await _handle_expression(
        update,
        context,
        "BTC",
        edit_message=SimpleNamespace(photo=(), reply_markup=recovery_markup),
    )

    assert started.is_set()
    assert "слишком много времени" in delivered[0][0]
    assert delivered[0][1] is recovery_markup
    assert not actor_locks.get(42).locked()
    await asyncio.wait_for(query_slots.acquire(), timeout=0.1)
    query_slots.release()


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
async def test_delayed_ephemeral_photo_error_edits_caption_in_place() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    recovery_markup = object()

    class Bot:
        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    message = Message(
        message_id=0,
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        photo=(SimpleNamespace(),),
        api_kwargs={"ephemeral_message_id": 92},
    )

    await _edit_error_message(
        Bot(),
        message,
        "Источники цен временно недоступны.",
        recovery_markup,
        receiver_user_id=42,
    )

    assert calls == [
        (
            "edit_ephemeral_message_caption",
            {
                "chat_id": -100,
                "receiver_user_id": 42,
                "ephemeral_message_id": 92,
                "caption": "Источники цен временно недоступны.",
                "parse_mode": "HTML",
                "reply_markup": recovery_markup,
            },
        )
    ]


@pytest.mark.asyncio
async def test_photo_refresh_rerenders_active_chart_and_keeps_selection() -> None:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    old_quote = Quote(
        coin,
        Decimal("100"),
        Decimal("1"),
        "Binance",
        "BTCUSDT",
        datetime.now(UTC),
    )
    new_quote = Quote(
        coin,
        Decimal("101"),
        Decimal("2"),
        "Binance",
        "BTCUSDT",
        datetime.now(UTC),
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
        date=datetime.now(UTC),
        chat=Chat(-100, "supergroup"),
        photo=(SimpleNamespace(),),
        api_kwargs={"ephemeral_message_id": 92},
    )
    services = SimpleNamespace(
        query_slots=asyncio.Semaphore(1),
        settings=SimpleNamespace(max_symbols=8, query_timeout=5),
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
async def test_chart_timeout_releases_query_slot() -> None:
    started = asyncio.Event()
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    quote = Quote(
        coin,
        Decimal("100"),
        None,
        "CoinGecko",
        None,
        datetime.now(UTC),
    )
    calculation = Calculation(
        expression="BTC",
        coefficients={"BTC": Decimal(1)},
        constant_usd=Decimal(0),
        quotes={"BTC": quote},
        total_usd=Decimal("100"),
        usd_rub=None,
        cbr_date=None,
    )
    session = QueryRegistry().create(42, "BTC", calculation)

    class Market:
        async def chart(self, quote: Quote, timeframe: str) -> Chart:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    query_slots = asyncio.Semaphore(1)
    services = SimpleNamespace(
        market=Market(),
        query_slots=query_slots,
        settings=SimpleNamespace(query_timeout=0.01),
        registry=SimpleNamespace(
            set_active_timeframe=lambda current, timeframe: current,
        ),
    )
    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(42, "private"),
        text="result",
    )
    update = SimpleNamespace(
        callback_query=SimpleNamespace(message=message),
        effective_user=SimpleNamespace(id=42),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(),
        application=SimpleNamespace(bot_data={"services": services}),
    )

    error = await _chart_callback(update, context, session, "24h")

    assert started.is_set()
    assert error == "⚠️ График не успел загрузиться. Попробуйте позже."
    await asyncio.wait_for(query_slots.acquire(), timeout=0.1)
    query_slots.release()



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
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
    )

    await favorites_command(update, context)



@pytest.mark.asyncio
async def test_private_favorites_shows_typing_before_market_lookup() -> None:
    typing_sent = asyncio.Event()
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)

    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            assert typing_sent.is_set()
            return {"BTC": coin}

    class Store:
        async def set_favorites(self, user_id: int, symbols: tuple[str, ...]) -> tuple[str, ...]:
            return symbols

    class Bot:
        async def send_chat_action(self, **kwargs: Any) -> None:
            typing_sent.set()

        async def send_message(self, **kwargs: Any) -> Any:
            return SimpleNamespace(message_id=8)

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
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=42, type="private"),
        effective_message=SimpleNamespace(message_thread_id=None),
        callback_query=None,
    )

    await favorites_command(update, context)

    assert typing_sent.is_set()


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
                date=datetime.now(UTC),
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
async def test_cancelled_group_favorites_removes_ephemeral_progress() -> None:
    progress_sent = asyncio.Event()
    calls: list[tuple[str, Any]] = []

    class Market:
        async def resolve_many(self, symbols: tuple[str, ...]) -> dict[str, Coin]:
            progress_sent.set()
            await asyncio.Event().wait()

    class Bot:
        async def send_message(self, **kwargs: Any) -> Message:
            return Message(
                message_id=0,
                date=datetime.now(UTC),
                chat=Chat(-100, "supergroup"),
                text=kwargs["text"],
                api_kwargs={"ephemeral_message_id": 95},
            )

        async def do_api_request(self, endpoint: str, api_kwargs: dict[str, Any]) -> bool:
            calls.append((endpoint, api_kwargs))
            return True

    services = SimpleNamespace(
        limiter=SimpleNamespace(check=lambda key: SimpleNamespace(allowed=True)),
        market=Market(),
        preference_locks=ActorLocks(),
        settings=SimpleNamespace(max_favorites=8),
    )
    context = SimpleNamespace(
        args=["BTC"],
        bot=Bot(),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_chat=SimpleNamespace(id=-100, type="supergroup"),
        effective_message=SimpleNamespace(
            message_id=0,
            message_thread_id=None,
            api_kwargs={"ephemeral_message_id": 94},
        ),
        callback_query=None,
    )

    task = asyncio.create_task(favorites_command(update, context))
    await progress_sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [
        (
            "delete_ephemeral_message",
            {"chat_id": -100, "receiver_user_id": 42, "ephemeral_message_id": 95},
        )
    ]

@pytest.mark.asyncio
async def test_result_callback_is_rate_limited_before_market_work() -> None:
    answers: list[tuple[str | None, bool]] = []

    class Query:
        data = "q|token|refresh"
        message = Message(
            message_id=7,
            date=datetime.now(UTC),
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
