import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from telegram import Chat, Message
from telegram.error import BadRequest

from cryptomathxbot.app import _card_error_text, _card_signer, _retire_message, callback_handler
from cryptomathxbot.card import CardAction, CardSigner, read_request
from cryptomathxbot.domain import Calculation, Chart, Coin, Quote
from cryptomathxbot.market import MarketUnavailable
from cryptomathxbot.session import ActorLocks, QueryRegistry
from cryptomathxbot.ui import home_keyboard, render_card, result_keyboard

KEY = "unit-test-card-key"


def value(expression: str = "BTC") -> Calculation:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    quote = Quote(coin, Decimal("100"), Decimal("1"), "Binance", "BTCUSDT", datetime.now(UTC))
    return Calculation(
        expression, {"BTC": Decimal(1)}, Decimal(0), {"BTC": quote}, Decimal(100), None, None
    )


def environment(
    *,
    action: str = "refresh",
    view: str = "text",
    actor: int = 42,
    chat_id: int = 42,
    topic: int | None = None,
    expression: str = "BTC",
    signed_actor: int = 42,
    signed_chat: int = 42,
    signed_topic: int | None = None,
) -> tuple[Any, Any, Any]:
    token = CardSigner(KEY).sign("BTC", signed_actor, signed_chat, signed_topic)
    chat = Chat(chat_id, "private" if chat_id > 0 else "supergroup")
    message = Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=chat,
        text=f"Запрос: {expression}\n\nПредыдущий результат: $100",
        reply_markup=result_keyboard(token, value(), persistent=True),
        message_thread_id=topic,
    )
    query = SimpleNamespace(
        id="callback-test",
        message=message,
        data=CardAction(token, action, view).encode(),
        answer=AsyncMock(),
    )
    overlay = Message(
        message_id=0,
        date=datetime.now(UTC),
        chat=chat,
        text="progress",
        message_thread_id=topic,
        api_kwargs={"ephemeral_message_id": 70},
    )
    btc = Coin("bitcoin", "BTC", "Bitcoin", 1)
    eth = Coin("ethereum", "ETH", "Ethereum", 2)
    quotes = {
        coin.symbol: Quote(
            coin, Decimal(100), None, "Binance", coin.symbol + "USDT", datetime.now(UTC)
        )
        for coin in (btc, eth)
    }

    async def chart(quote: Quote, timeframe: str, *, force_refresh: bool = False) -> Chart:
        return Chart(quote.coin.symbol, timeframe, ((1000, 100.0), (2000, 101.0)), "Binance")

    services = SimpleNamespace(
        settings=SimpleNamespace(token=KEY, max_symbols=8, max_favorites=8, query_timeout=1),
        registry=QueryRegistry(),
        actor_locks=ActorLocks(),
        preference_locks=ActorLocks(),
        limiter=SimpleNamespace(check=Mock(return_value=SimpleNamespace(allowed=True))),
        notice_limiter=SimpleNamespace(check=Mock(return_value=SimpleNamespace(allowed=True))),
        query_slots=asyncio.Semaphore(1),
        store=SimpleNamespace(favorites=AsyncMock(return_value=("BTC", "ETH"))),
        market=SimpleNamespace(
            resolve_many=AsyncMock(return_value={"BTC": btc, "ETH": eth}),
            quotes=AsyncMock(return_value=quotes),
            usd_rub=AsyncMock(return_value=(None, None)),
            chart=AsyncMock(side_effect=chart),
        ),
        charts=SimpleNamespace(render=AsyncMock(return_value=b"png")),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(
            send_message=AsyncMock(return_value=overlay), do_api_request=AsyncMock()
        ),
        application=SimpleNamespace(bot_data={"services": services}),
    )
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=actor),
        effective_chat=chat,
        effective_message=message,
    )
    return update, context, services


def legacy_source(update: Any, token: str) -> None:
    previous = update.callback_query.message
    message = Message(
        message_id=previous.message_id,
        date=previous.date,
        chat=previous.chat,
        text="Legacy price result",
        message_thread_id=previous.message_thread_id,
        reply_markup=result_keyboard(token, value()),
    )
    update.callback_query.message = message
    update.effective_message = message


def test_production_signer_selects_configured_credential() -> None:
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "services": SimpleNamespace(settings=SimpleNamespace(token="configured-test-key")),
            }
        )
    )
    signer = _card_signer(context)
    token = signer.sign("BTC", 42, 42, None)
    assert CardSigner("configured-test-key").verify(token, "BTC", 42, 42, None)
    assert not CardSigner(KEY).verify(token, "BTC", 42, 42, None)


@pytest.mark.asyncio
async def test_refresh_does_not_consult_lost_ram_session(monkeypatch: pytest.MonkeyPatch) -> None:
    update, context, services = environment()
    services.registry.get = Mock(side_effect=AssertionError("RAM is not a card authority"))
    edited = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edited)

    await callback_handler(update, context)

    assert edited.await_count == 1
    assert "Запрос:" in edited.call_args.args[2]
    assert "Котировки:" in edited.call_args.args[2]
    services.market.quotes.assert_awaited_once()
    assert services.market.quotes.call_args.kwargs["force_refresh"] is True
    assert not services.actor_locks.get(42).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["refresh", "close"])
@pytest.mark.parametrize(
    "changes",
    [
        {"actor": 43},
        {"chat_id": 43},
        {"topic": 17},
        {"expression": "ETH"},
    ],
)
async def test_forged_or_foreign_card_has_no_side_effects(
    changes: dict[str, Any], action: str
) -> None:
    update, context, services = environment(action=action, **changes)

    await callback_handler(update, context)

    services.market.resolve_many.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["chart", "refresh"])
@pytest.mark.parametrize("period", ["1h", "24h", "7d"])
async def test_replayed_chart_preserves_selected_period(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    period: str,
) -> None:
    update, context, services = environment(action=action, view=period)
    edited = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_result_media", edited)

    await callback_handler(update, context)

    assert services.market.chart.call_args.args[1] == period
    assert edited.await_count == 1
    keyboard = edited.call_args.args[4]
    actions = [
        CardAction.parse(button.callback_data)
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("r|")
    ]
    assert any(item.action == "refresh" and item.view == period for item in actions)
    assert any(item.action == "text" for item in actions)


@pytest.mark.asyncio
async def test_group_topic_refresh_remains_receiver_scoped_after_ack_failure() -> None:
    update, context, services = environment(
        chat_id=-100, signed_chat=-100, topic=17, signed_topic=17
    )
    update.callback_query.answer.side_effect = BadRequest("query is too old")

    await callback_handler(update, context)

    sent = context.bot.send_message.call_args.kwargs
    assert sent["message_thread_id"] == 17
    assert sent["api_kwargs"]["ephemeral_message_parameters"]["receiver_user_id"] == 42
    (endpoint,) = context.bot.do_api_request.call_args.args
    assert endpoint == "edit_ephemeral_message_text"
    assert context.bot.do_api_request.call_args.kwargs["api_kwargs"]["ephemeral_message_id"] == 70
    assert not services.actor_locks.get(42).locked()


@pytest.mark.asyncio
async def test_next_coin_can_be_selected_without_start(monkeypatch: pytest.MonkeyPatch) -> None:
    update, context, services = environment()
    edited = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edited)
    update.callback_query.data = "menu|home"

    await callback_handler(update, context)

    keyboard = edited.call_args.args[3]
    assert "symbol|ETH" in [
        button.callback_data for row in keyboard.inline_keyboard for button in row
    ]
    update.callback_query.data = "symbol|ETH"
    await callback_handler(update, context)
    assert "Ethereum · ETH" in edited.call_args.args[2]
    assert "menu|home" in [
        button.callback_data for row in edited.call_args.args[3].inline_keyboard for button in row
    ]
    services.store.favorites.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_legacy_card_becomes_a_working_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    update, context, services = environment()
    legacy_source(update, "expired-token")
    update.callback_query.data = "q|expired-token|refresh"
    edited = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edited)

    await callback_handler(update, context)

    assert "Продолжим здесь" in edited.call_args.args[2]
    buttons = [
        button.callback_data for row in edited.call_args.args[3].inline_keyboard for button in row
    ]
    assert "symbol|BTC" in buttons
    assert not any(data and data.startswith("q|") for data in buttons)
    services.market.resolve_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_foreign_legacy_card_is_not_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    update, context, services = environment()
    session = services.registry.create(43, "BTC", value())
    legacy_source(update, session.token)
    update.callback_query.data = f"q|{session.token}|refresh"
    edited = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", edited)

    await callback_handler(update, context)

    edited.assert_not_awaited()
    assert "другого пользователя" in update.callback_query.answer.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["signed", "live_legacy"])
async def test_forged_legacy_callback_cannot_replace_foreign_public_keyboard(
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
) -> None:
    update, context, services = environment(chat_id=-100, signed_chat=-100, signed_actor=43)
    if source_kind == "live_legacy":
        session = services.registry.create(43, "BTC", value())
        legacy_source(update, session.token)
    update.callback_query.data = "q|missing-token|refresh"
    changed = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._set_message_keyboard", changed)

    await callback_handler(update, context)

    changed.assert_not_awaited()
    services.store.favorites.assert_not_awaited()
    context.bot.send_message.assert_not_awaited()
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_close_failure_strips_keyboard_instead_of_claiming_deletion() -> None:
    message = SimpleNamespace(
        api_kwargs={},
        delete=AsyncMock(side_effect=BadRequest("message can't be deleted")),
        edit_reply_markup=AsyncMock(),
    )

    await _retire_message(SimpleNamespace(), message, 42)

    message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_market_error_preserves_request_and_replay_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update, context, services = environment()
    services.market.resolve_many.side_effect = MarketUnavailable("offline")
    edited = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_error_message", edited)

    await callback_handler(update, context)

    text = edited.call_args.args[2]
    assert read_request(text) == "BTC"
    assert "Предыдущий результат: $100" in text
    assert "Статус:" in text
    keyboard = edited.call_args.args[3]
    retry = next(
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
        and button.callback_data.startswith("r|")
        and CardAction.parse(button.callback_data).action == "refresh"
    )
    previous = update.callback_query.message
    recovered = Message(
        message_id=previous.message_id,
        date=previous.date,
        chat=previous.chat,
        text=text,
        reply_markup=keyboard,
        message_thread_id=previous.message_thread_id,
    )
    update.callback_query.message = recovered
    update.effective_message = recovered
    update.callback_query.data = retry
    services.market.resolve_many.side_effect = None
    refreshed = AsyncMock()
    monkeypatch.setattr("cryptomathxbot.app._edit_result_message", refreshed)

    await callback_handler(update, context)

    assert any(
        button.copy_text and button.copy_text.text == "$100"
        for row in refreshed.call_args.args[3].inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_cancelled_replay_releases_lock_slot_and_ephemeral_progress() -> None:
    update, context, services = environment(chat_id=-100, signed_chat=-100)
    entered = asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    services.market.resolve_many.side_effect = blocked
    task = asyncio.create_task(callback_handler(update, context))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not services.actor_locks.get(42).locked()
    await asyncio.wait_for(services.query_slots.acquire(), timeout=0.1)
    services.query_slots.release()
    assert context.bot.do_api_request.call_args.args == ("delete_ephemeral_message",)


@pytest.mark.asyncio
async def test_navigation_does_not_race_an_active_calculation() -> None:
    update, context, services = environment()
    update.callback_query.data = "menu|home"
    lock = services.actor_locks.get(42)
    await lock.acquire()
    try:
        await callback_handler(update, context)
    finally:
        lock.release()
    services.store.favorites.assert_not_awaited()
    assert update.callback_query.answer.call_args.kwargs["show_alert"] is True


def test_long_photo_caption_keeps_exact_request_and_budget() -> None:
    expression = "0." + "0" * 490 + "1 BTC"
    calculation = value(expression)
    chart = Chart("BTC", "7d", ((1000, 100.0), (2000, 101.0)), "Binance")
    rendered = render_card(calculation, chart)
    assert expression in rendered
    assert len(rendered.encode("utf-16-le")) // 2 <= 1024
    assert "USD/RUB" in rendered


def test_repeated_error_notice_does_not_grow_card_indefinitely() -> None:
    source = Message(
        message_id=7, date=datetime.now(UTC), chat=Chat(42, "private"), text="Запрос: BTC\n\nresult"
    )
    text = _card_error_text("BTC", source, "offline", photo=False)
    repeated = _card_error_text(
        "BTC",
        SimpleNamespace(text_html=text, caption_html=None),
        "offline",
        photo=False,
    )
    assert repeated == text
    assert read_request(repeated) == "BTC"


def test_every_home_screen_has_an_input_and_close_path() -> None:
    buttons = [
        button.callback_data for row in home_keyboard(("BTC",)).inline_keyboard for button in row
    ]
    assert {"menu|input", "menu|close"} <= set(buttons)
