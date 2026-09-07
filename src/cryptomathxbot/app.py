from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, cast

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import (
    BadRequest,
    Conflict,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from . import __version__
from .calculator import ExpressionError, ParsedExpression, parse_expression
from .card import CardAction, CardSigner, read_request
from .charts import ChartRenderer
from .config import ConfigurationError, Settings
from .domain import Calculation, Chart, Coin
from .logging_setup import configure_logging
from .market import MarketService, MarketUnavailable
from .rate_limit import SlidingWindowLimiter
from .session import ActorLocks, QueryRegistry, QuerySession
from .single_instance import AlreadyRunningError, InstanceLockError, SingleInstanceLock
from .storage import PreferencesStore
from .ui import (
    chart_caption,
    format_decimal,
    help_text,
    home_keyboard,
    navigation_keyboard,
    render_calculation,
    render_card,
    request_header,
    result_keyboard,
    settings_keyboard,
    settings_text,
    start_text,
)

_LOGGER = logging.getLogger(__name__)
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,11}$")
_GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


@dataclass(slots=True)
class Services:
    settings: Settings
    store: PreferencesStore
    market: MarketService
    charts: ChartRenderer
    limiter: SlidingWindowLimiter
    notice_limiter: SlidingWindowLimiter
    registry: QueryRegistry
    actor_locks: ActorLocks
    preference_locks: ActorLocks
    query_slots: asyncio.Semaphore
    started_at: float


def build_application(settings: Settings) -> Application[Any, Any, Any, Any, Any, Any]:
    services = Services(
        settings=settings,
        store=PreferencesStore(
            settings.data_dir / "state.sqlite3",
            default_favorites=settings.default_favorites,
            max_favorites=settings.max_favorites,
        ),
        market=MarketService(settings),
        charts=ChartRenderer(settings.chart_dpi),
        limiter=SlidingWindowLimiter(
            settings.rate_limit_requests,
            settings.rate_limit_window,
        ),
        notice_limiter=SlidingWindowLimiter(1, 10),
        registry=QueryRegistry(),
        actor_locks=ActorLocks(),
        preference_locks=ActorLocks(),
        query_slots=asyncio.Semaphore(settings.query_concurrency),
        started_at=time.monotonic(),
    )

    pool_size = max(16, settings.concurrent_updates * 2)
    builder = (
        ApplicationBuilder()
        .token(settings.token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .rate_limiter(AIORateLimiter(max_retries=2))
        .concurrent_updates(settings.concurrent_updates)
        .connection_pool_size(pool_size)
        .pool_timeout(10.0)
        .connect_timeout(10.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .media_write_timeout(60.0)
        .http_version("1.1")
        .get_updates_connection_pool_size(2)
        .get_updates_connect_timeout(10.0)
        .get_updates_read_timeout(45.0)
        .get_updates_write_timeout(15.0)
        .get_updates_pool_timeout(10.0)
        .get_updates_http_version("1.1")
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
    )
    application = builder.build()
    application.bot_data["services"] = services

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("favorites", favorites_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    application.add_error_handler(error_handler)
    return application


async def _post_init(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    services = _services_from_application(application)
    await services.store.initialize(services.settings.legacy_favorites_file)
    await services.market.start()

    private_commands = [
        BotCommand("start", "Открыть калькулятор"),
        BotCommand("price", "Рассчитать цену или выражение"),
        BotCommand("favorites", "Настроить избранные монеты"),
        BotCommand("settings", "Настройки"),
        BotCommand("help", "Примеры и помощь"),
        BotCommand("ping", "Проверить доступность"),
    ]
    group_commands = [
        BotCommand(
            "price",
            "Рассчитать цену или выражение",
            api_kwargs={"is_ephemeral": True},
        ),
        BotCommand("favorites", "Личные быстрые кнопки", api_kwargs={"is_ephemeral": True}),
        BotCommand("settings", "Личные настройки", api_kwargs={"is_ephemeral": True}),
        BotCommand("help", "Помощь", api_kwargs={"is_ephemeral": True}),
        BotCommand("ping", "Проверить доступность", api_kwargs={"is_ephemeral": True}),
    ]
    setup_results = await asyncio.gather(
        application.bot.set_my_commands(
            private_commands,
            scope=BotCommandScopeAllPrivateChats(),
        ),
        application.bot.set_my_commands(
            group_commands,
            scope=BotCommandScopeAllGroupChats(),
        ),
        application.bot.set_my_description(
            "Криптовалютный калькулятор: цены в USD и RUB, выражения и графики."
        ),
        application.bot.set_my_short_description("Цены криптовалют, расчёты и графики"),
        return_exceptions=True,
    )
    for result in setup_results:
        if isinstance(result, Exception):
            _LOGGER.warning("Telegram profile setup failed error=%s", type(result).__name__)
    _LOGGER.info(
        "READY username=%s version=%s pid=%d",
        application.bot.username,
        __version__,
        os.getpid(),
    )
    if services.settings.owner_chat_id is not None:
        try:
            await application.bot.send_message(
                services.settings.owner_chat_id,
                f"CryptoMathXBot v{__version__} запущен и готов к работе.",
            )
        except TelegramError as exc:
            _LOGGER.warning("owner startup notification failed error=%s", type(exc).__name__)


async def _post_shutdown(application: Application[Any, Any, Any, Any, Any, Any]) -> None:
    await _services_from_application(application).market.close()
    _LOGGER.info("shutdown complete")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    if not await _ensure_personal_response(update, context):
        return
    favorites = await _services(context).store.favorites(user.id, _chat_id(update))
    await _send_html(
        update,
        context,
        start_text(),
        reply_markup=home_keyboard(favorites),
        ephemeral=_is_group(update),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    if not await _ensure_personal_response(update, context):
        return
    favorites = await _services(context).store.favorites(user.id, _chat_id(update))
    await _send_html(
        update,
        context,
        help_text(),
        reply_markup=home_keyboard(favorites),
        ephemeral=_is_group(update),
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show_settings(update, context, edit=False)


async def favorites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        return
    if not await _ensure_personal_response(update, context):
        return
    if not context.args:
        await _show_settings(update, context, edit=False)
        return

    services = _services(context)
    decision = services.limiter.check(("query", user.id))
    if not decision.allowed:
        if decision.notify:
            await _send_html(
                update,
                context,
                f"⏳ Слишком часто. Повторите через {max(1, round(decision.retry_after))} с.",
                ephemeral=_ephemeral_response_available(update),
            )
        return
    raw_symbols = tuple(
        dict.fromkeys(
            item.upper().lstrip("$")
            for item in re.split(r"[\s,;]+", " ".join(context.args).strip())
            if item
        )
    )
    if not raw_symbols or any(_SYMBOL_RE.fullmatch(symbol) is None for symbol in raw_symbols):
        await _send_html(
            update,
            context,
            "Используйте тикеры: <code>/favorites BTC ETH XMR</code>",
            ephemeral=_ephemeral_response_available(update),
        )
        return
    if len(raw_symbols) > services.settings.max_favorites:
        await _send_html(
            update,
            context,
            f"Можно выбрать не больше {services.settings.max_favorites} монет.",
            ephemeral=_ephemeral_response_available(update),
        )
        return

    progress: Message | None = None
    if _ephemeral_response_available(update):
        progress = await _try_send_ephemeral_progress(update, context, "⏳ Проверяю монеты…")
    elif not _is_group(update):
        try:
            await context.bot.send_chat_action(
                chat_id=_chat_id(update),
                action=ChatAction.TYPING,
                message_thread_id=_thread_id(update),
            )
        except TelegramError:
            pass

    try:
        try:
            resolved = await services.market.resolve_many(raw_symbols)
            unknown = [symbol for symbol in raw_symbols if symbol not in resolved]
            if unknown:
                text = "Не нашёл: <code>" + ", ".join(map(_escape, unknown)) + "</code>"
                keyboard = navigation_keyboard()
            else:
                canonical = tuple(dict.fromkeys(resolved[symbol].symbol for symbol in raw_symbols))
                async with services.preference_locks.get(user.id):
                    favorites = await services.store.set_favorites(user.id, canonical)
                text = settings_text(favorites, services.settings.max_favorites)
                keyboard = settings_keyboard(favorites)
        except MarketUnavailable:
            text = "⚠️ Рыночные источники временно недоступны. Повторите через минуту."
            keyboard = navigation_keyboard()

        if progress is not None:
            await _edit_ephemeral_text(context.bot, progress, user.id, text, keyboard)
            progress = None
        else:
            await _send_html(
                update,
                context,
                text,
                reply_markup=keyboard,
                ephemeral=_ephemeral_response_available(update),
            )
    finally:
        if progress is not None:
            await _delete_ephemeral_message(context.bot, progress, user.id)


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    expression = " ".join(context.args or ()).strip()
    if not expression:
        await _send_html(
            update,
            context,
            "Пример: <code>/price 0.5 BTC + 2 ETH</code>",
            reply_markup=navigation_keyboard(),
            ephemeral=_ephemeral_response_available(update),
        )
        return
    await _handle_expression(update, context, expression)


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = _services(context)
    uptime = max(0, int(time.monotonic() - services.started_at))
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    await _send_html(
        update,
        context,
        f"🟢 <b>Работаю</b> · v{__version__} · {hours:02d}:{minutes:02d}:{seconds:02d}",
        ephemeral=_ephemeral_response_available(update),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    expression = message.text.strip()
    if _is_group(update):
        expression = _group_expression(context, expression)
        if not expression:
            return
    await _handle_expression(update, context, expression)


async def _handle_expression(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    expression: str,
    *,
    edit_message: Message | None = None,
) -> None:
    user = update.effective_user
    if user is None:
        return
    recovery_markup = getattr(getattr(update.callback_query, "message", None), "reply_markup", None)
    services = _services(context)
    decision = services.limiter.check(("query", user.id))
    if not decision.allowed:
        if decision.notify:
            await _send_html(
                update,
                context,
                f"⏳ Слишком часто. Повторите через {max(1, round(decision.retry_after))} с.",
                reply_markup=recovery_markup,
                ephemeral=_ephemeral_response_available(update),
            )
        return

    actor_lock = services.actor_locks.get(user.id)
    if actor_lock.locked():
        notice = services.notice_limiter.check(("busy", user.id))
        if notice.allowed:
            await _send_html(
                update,
                context,
                "⏳ Предыдущий запрос ещё обрабатывается.",
                reply_markup=recovery_markup,
                ephemeral=_ephemeral_response_available(update),
            )
        return

    async with actor_lock:
        started_at = time.monotonic()
        draft_id = None
        progress_message: Message | None = None
        target_message = edit_message
        if edit_message is None:
            if _is_group(update) and _ephemeral_response_available(update):
                progress_message = await _try_send_ephemeral_progress(
                    update,
                    context,
                    "⏳ Обрабатываю запрос…",
                    reply_markup=recovery_markup,
                )
                target_message = progress_message
            else:
                draft_id = await _show_draft(update, context, "Ищу монеты…")
        try:
            parsed = parse_expression(expression, max_symbols=services.settings.max_symbols)
            if not parsed.coefficients:
                value = parsed.constant
                arithmetic_text = (
                    f"<code>{_escape(parsed.source)}</code> = <b>{format_decimal(value)}</b>"
                )
                if target_message is not None:
                    await _edit_result_message(
                        context.bot,
                        target_message,
                        arithmetic_text,
                        navigation_keyboard(),
                        receiver_user_id=user.id,
                    )
                    progress_message = None
                else:
                    await _send_html(
                        update,
                        context,
                        arithmetic_text,
                        reply_markup=navigation_keyboard(),
                        ephemeral=_ephemeral_response_available(update),
                    )
                return
            await _update_draft(update, context, draft_id, "Получаю актуальные цены…")
            calculation = await asyncio.wait_for(
                _with_query_slot(services, lambda: _calculate(parsed, services)),
                timeout=services.settings.query_timeout,
            )
            token = _card_signer(context).sign(
                calculation.expression, user.id, _chat_id(update), _thread_id(update),
            )
            text = render_card(calculation)
            keyboard = result_keyboard(token, calculation, persistent=True)
            if target_message is not None:
                await _edit_result_message(
                    context.bot,
                    target_message,
                    text,
                    keyboard,
                    receiver_user_id=user.id,
                )
                progress_message = None
            else:
                await _send_html(
                    update,
                    context,
                    text,
                    reply_markup=keyboard,
                    ephemeral=_ephemeral_response_available(update),
                )
            _LOGGER.info(
                "query complete symbols=%d duration_ms=%d",
                len(calculation.coefficients),
                round((time.monotonic() - started_at) * 1000),
            )
        except TimeoutError:
            if await _deliver_error(
                update,
                context,
                target_message,
                "⚠️ Запрос занял слишком много времени. Попробуйте ещё раз.",
            ):
                progress_message = None
        except ExpressionError as exc:
            if await _deliver_error(update, context, target_message, f"⚠️ {_escape(str(exc))}"):
                progress_message = None
        except MarketUnavailable:
            if await _deliver_error(
                update,
                context,
                target_message,
                "⚠️ Рыночные источники временно недоступны. Повторите через минуту.",
            ):
                progress_message = None
        except Exception:
            _LOGGER.exception("query failed")
            if await _deliver_error(
                update,
                context,
                target_message,
                "⚠️ Не удалось выполнить расчёт. Попробуйте ещё раз.",
            ):
                progress_message = None
        finally:
            if progress_message is not None:
                await _delete_ephemeral_message(context.bot, progress_message, user.id)


async def _calculate(
    parsed: ParsedExpression,
    services: Services,
    *,
    force_refresh: bool = False,
) -> Calculation:
    resolved = await services.market.resolve_many(parsed.symbols)
    unknown = [symbol for symbol in parsed.symbols if symbol not in resolved]
    if unknown:
        raise ExpressionError("Не нашёл монеты: " + ", ".join(unknown))

    coefficients: dict[str, Decimal] = {}
    unique_coins: dict[str, Coin] = {}
    coins_by_symbol: dict[str, Coin] = {}
    for entered_symbol, coefficient in parsed.coefficients.items():
        coin = resolved[entered_symbol]
        existing = coins_by_symbol.get(coin.symbol)
        if existing is not None and existing.id != coin.id:
            raise ExpressionError(
                f"Тикер {coin.symbol} относится к нескольким разным монетам в одном запросе"
            )
        coins_by_symbol[coin.symbol] = coin
        coefficients[coin.symbol] = coefficients.get(coin.symbol, Decimal(0)) + coefficient
        unique_coins[coin.id] = coin

    coins = tuple(unique_coins.values())
    quotes_task = (
        services.market.quotes(coins, force_refresh=True)
        if force_refresh
        else services.market.quotes(coins)
    )
    cbr_task = services.market.usd_rub()
    quotes, (usd_rub, cbr_date) = await asyncio.gather(quotes_task, cbr_task)
    missing = [symbol for symbol in coefficients if symbol not in quotes]
    if missing:
        raise MarketUnavailable("missing quotes")
    total = parsed.constant
    for symbol, coefficient in coefficients.items():
        total += coefficient * quotes[symbol].usd
    if not total.is_finite():
        raise ExpressionError("Результат не является конечным числом")
    return Calculation(
        expression=parsed.source,
        coefficients=coefficients,
        constant_usd=parsed.constant,
        quotes=quotes,
        total_usd=total,
        usd_rub=usd_rub,
        cbr_date=cbr_date,
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    if not isinstance(query.message, Message):
        await query.answer(
            "Сообщение с кнопкой больше недоступно. Откройте /start.",
            show_alert=True,
        )
        return
    message = query.message
    data = query.data if isinstance(query.data, str) else ""
    parts = data.split("|")

    if len(parts) == 2 and parts[0] == "menu":
        await _menu_callback(update, context, parts[1])
        return
    if len(parts) == 2 and parts[0] == "symbol":
        if _SYMBOL_RE.fullmatch(parts[1]) is None:
            await query.answer("Кнопка устарела.", show_alert=True)
            return
        await query.answer("Получаю цену…")
        target_message: Message | None = message
        if _is_group(update) and _ephemeral_message_id(message) is None:
            target_message = None
        await _handle_expression(
            update,
            context,
            parts[1],
            edit_message=target_message,
        )
        return

    if parts and parts[0] == "fav":
        await _favorite_callback(update, context, parts)
        return

    if len(parts) >= 3 and parts[0] in {"q", "r"}:
        services = _services(context)
        replay = parts[0] == "r"
        session: QuerySession | None
        if replay:
            try:
                card_action = CardAction.parse(data)
            except ValueError:
                await _answer_callback(query, "Неизвестная кнопка. Выберите «Монеты».", alert=True)
                return
            expression = read_request(message.text or message.caption)
            if expression is None or not _card_signer(context).verify(
                card_action.token, expression, user.id, message.chat_id, message.message_thread_id,
            ):
                await _answer_callback(
                    query, "Не могу подтвердить карточку для вас. Нажмите «Монеты» и откройте свою.",
                    alert=True,
                )
                return
            session = QuerySession(
                token=card_action.token, owner_user_id=user.id, expression=expression,
                calculation=None, expires_at=float("inf"),
                active_timeframe=None if card_action.view == "text" else card_action.view,
            )
            action = "refresh" if card_action.action == "text" else card_action.action
            is_chart = action == "chart"
            timeframe = card_action.view
        else:
            session = services.registry.get(parts[1], user.id)
            if session is None:
                await _recover_legacy_card(update, context, parts[1])
                return
            action = parts[2]
            is_chart = len(parts) == 4 and action == "chart"
            timeframe = parts[3] if is_chart else "text"
            if (action != "refresh" or len(parts) != 3) and not is_chart:
                await query.answer("Кнопка устарела.", show_alert=True)
                return
        if is_chart and timeframe not in {"1h", "24h", "7d"}:
            await query.answer("Неизвестный период.", show_alert=True)
            return
        if is_chart and session.calculation is not None and len(session.calculation.coefficients) != 1:
            await query.answer("График доступен для одной монеты.", show_alert=True)
            return
        if action != "close":
            decision = services.limiter.check(("query", user.id))
            if not decision.allowed:
                await query.answer(
                    f"Слишком часто. Повторите через {max(1, round(decision.retry_after))} с.",
                    show_alert=True,
                )
                return

        actor_lock = services.actor_locks.get(user.id)
        if actor_lock.locked():
            await query.answer("Предыдущее действие ещё выполняется.", show_alert=True)
            return
        await actor_lock.acquire()
        progress_message: Message | None = None
        try:
            if action == "close":
                await _answer_callback(query)
                await _retire_message(context.bot, message, user.id)
                return
            progress_text = "Обновляю…" if action == "refresh" else "Готовлю график…"
            if _is_group(update) and _ephemeral_message_id(message) is None:
                progress_message = await _try_send_ephemeral_progress(
                    update,
                    context,
                    f"⏳ {progress_text}",
                )
                if progress_message is None:
                    await query.answer(
                        "Не удалось открыть личный ответ. Повторите запрос.",
                        show_alert=True,
                    )
                    return
            await _answer_callback(query, progress_text)
            target_message = progress_message or message
            if action == "refresh":
                error_text = await _refresh_callback(
                    update,
                    context,
                    session,
                    edit_message=target_message,
                )
            else:
                error_text = await _chart_callback(
                    update,
                    context,
                    session,
                    timeframe,
                    edit_message=target_message,
                )
            if error_text is not None:
                if replay:
                    error_text = _card_error_text(session.expression, message, error_text, photo=bool(target_message.photo))
                try:
                    await _edit_error_message(
                        context.bot,
                        target_message,
                        error_text,
                        getattr(message, "reply_markup", None),
                        receiver_user_id=user.id,
                    )
                except (TelegramError, RuntimeError) as exc:
                    _LOGGER.warning("callback error delivery failed error=%s", type(exc).__name__)
                    return
            progress_message = None
        finally:
            actor_lock.release()
            if progress_message is not None:
                await _delete_ephemeral_message(context.bot, progress_message, user.id)
        return

    await query.answer("Кнопка устарела.", show_alert=True)


def _card_signer(context: ContextTypes.DEFAULT_TYPE) -> CardSigner:
    return CardSigner(_services(context).settings.token)


async def _answer_callback(query: Any, text: str | None = None, *, alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=alert)
    except TelegramError as exc:
        _LOGGER.debug("callback acknowledgement failed error=%s", type(exc).__name__)


async def _menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not isinstance(query.message, Message):
        return
    if action not in {"home", "help", "settings", "input", "close"}:
        await _answer_callback(query, "Неизвестное действие. Выберите «Монеты».", alert=True)
        return
    services = _services(context)
    lock = services.actor_locks.get(user.id)
    if lock.locked():
        await _answer_callback(query, "Предыдущее действие ещё выполняется.", alert=True)
        return
    async with lock:
        message = query.message
        if action == "close":
            if _is_group(update) and _ephemeral_message_id(message) is None:
                await _answer_callback(
                    query, "Это общая карточка. Откройте личный экран через «Монеты».", alert=True,
                )
                return
            await _answer_callback(query)
            await _retire_message(context.bot, message, user.id)
            return
        await _answer_callback(query)
        if action == "settings":
            await _show_settings(update, context, edit=not _is_group(update))
            return
        favorites = await services.store.favorites(user.id, _chat_id(update))
        if action == "help":
            text = help_text()
        elif action == "input":
            text = (
                "<b>Другая монета или выражение</b>\n\n"
                "В личном чате просто отправьте <code>ETH</code> или "
                "<code>0.5 BTC + 2 ETH</code>.\n"
                "В группе используйте <code>/price ETH</code>.\n\n"
                "Либо выберите быструю кнопку ниже. /start не нужен."
            )
        else:
            text = start_text()
        if _is_group(update) and _ephemeral_message_id(message) is None:
            await _send_html(update, context, text, reply_markup=home_keyboard(favorites), ephemeral=True)
        else:
            await _edit_result_message(
                context.bot, message, text, home_keyboard(favorites),
                receiver_user_id=user.id, callback_query_id=query.id,
            )


async def _recover_legacy_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE, token: str,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None or not isinstance(query.message, Message):
        return
    markup = query.message.reply_markup
    if markup is None or not any(
        isinstance(button.callback_data, str)
        and button.callback_data == query.data
        and button.callback_data.startswith(f"q|{token}|")
        for row in markup.inline_keyboard for button in row
    ):
        await _answer_callback(query, "Эта кнопка не относится к карточке. Откройте «Монеты».", alert=True)
        return
    services = _services(context)
    if services.registry.contains(token):
        await _answer_callback(
            query, "Это карточка другого пользователя. Откройте «Монеты» для своего запроса.",
            alert=True,
        )
        return
    lock = services.actor_locks.get(user.id)
    if lock.locked():
        await _answer_callback(query, "Предыдущее действие ещё выполняется.", alert=True)
        return
    async with lock:
        await _answer_callback(query, "Открываю выбор монеты…")
        favorites = await services.store.favorites(user.id, _chat_id(update))
        text = (
            "<b>Продолжим здесь</b>\n"
            "У старой карточки больше нет исходного запроса. "
            "Не буду восстанавливать количество по округлённым ценам.\n\n"
            "Выберите монету ниже или отправьте новое выражение. /start не нужен."
        )
        message = query.message
        if _is_group(update) and _ephemeral_message_id(message) is None:
            await _set_message_keyboard(context.bot, message, user.id, navigation_keyboard())
            await _send_html(update, context, text, reply_markup=home_keyboard(favorites), ephemeral=True)
        else:
            await _edit_result_message(
                context.bot, message, text, home_keyboard(favorites),
                receiver_user_id=user.id, callback_query_id=query.id,
            )


def _card_error_text(expression: str, source: Message, error: str, *, photo: bool) -> str:
    base = source.text_html or source.caption_html or request_header(expression)
    base = base.split("\n\n<b>Статус:</b>", 1)[0]
    notice = f"\n\n<b>Статус:</b> {_escape(error)} Нажмите «Обновить» или «Монеты»."
    limit = 1024 if photo else 4096
    if len((base + notice).encode("utf-16-le")) // 2 > limit:
        base = request_header(expression) + "Предыдущий результат не обновлён."
    return base + notice


async def _set_message_keyboard(
    bot: Any, message: Message, receiver_user_id: int, keyboard: Any,
) -> None:
    try:
        ephemeral_id = _ephemeral_message_id(message)
        if ephemeral_id is None:
            await message.edit_reply_markup(reply_markup=keyboard)
        else:
            photo = bool(message.photo)
            content = message.caption_html if photo else message.text_html
            field = "caption" if photo else "text"
            endpoint = "edit_ephemeral_message_caption" if photo else "edit_ephemeral_message_text"
            await bot.do_api_request(endpoint, api_kwargs={
                "chat_id": message.chat_id, "receiver_user_id": receiver_user_id,
                "ephemeral_message_id": ephemeral_id, field: content or "Карточка закрыта.",
                "parse_mode": ParseMode.HTML, "reply_markup": keyboard,
            })
    except (TelegramError, RuntimeError) as exc:
        _LOGGER.debug("keyboard cleanup failed error=%s", type(exc).__name__)


async def _retire_message(bot: Any, message: Message, receiver_user_id: int) -> None:
    try:
        ephemeral_id = _ephemeral_message_id(message)
        if ephemeral_id is None:
            await message.delete()
        else:
            await bot.do_api_request("delete_ephemeral_message", api_kwargs={
                "chat_id": message.chat_id, "receiver_user_id": receiver_user_id,
                "ephemeral_message_id": ephemeral_id,
            })
        return
    except TelegramError as exc:
        _LOGGER.debug("message cleanup failed error=%s", type(exc).__name__)
    # Old messages may exceed Telegram's deletion window. Leave passive history,
    # not a duplicate set of live controls; never delete before a new send succeeds.
    await _set_message_keyboard(bot, message, receiver_user_id, None)


async def _refresh_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: QuerySession,
    *,
    edit_message: Message | None = None,
) -> str | None:
    query = update.callback_query
    user = update.effective_user
    source_message = (
        query.message if query is not None and isinstance(query.message, Message) else None
    )
    if source_message is None or user is None:
        return "⚠️ Сообщение с кнопкой больше недоступно."
    target_message = edit_message or source_message
    services = _services(context)
    replay = session.calculation is None
    try:
        calculation, chart = await asyncio.wait_for(
            _with_query_slot(
                services,
                lambda: _refresh_market_data(
                    session,
                    services,
                    include_chart=bool(session.active_timeframe and (replay or source_message.photo)),
                ),
            ),
            timeout=services.settings.query_timeout,
        )
        keyboard = result_keyboard(
            session.token,
            calculation,
            active_timeframe=session.active_timeframe,
            persistent=replay,
        )
        if chart is not None:
            image = await services.charts.render(chart)
            await _edit_result_media(
                context.bot,
                target_message,
                image,
                render_card(calculation, chart) if replay else chart_caption(calculation, chart),
                keyboard,
                receiver_user_id=user.id,
            )
        else:
            await _edit_result_message(
                context.bot,
                target_message,
                render_card(calculation) if replay else render_calculation(calculation),
                keyboard,
                receiver_user_id=user.id,
            )
        if not replay:
            services.registry.update(session, calculation)
    except TimeoutError:
        return "⚠️ Обновление заняло слишком много времени. Попробуйте позже."
    except ExpressionError, MarketUnavailable, ValueError:
        return "⚠️ Не удалось обновить цены. Попробуйте позже."
    except (TelegramError, RuntimeError) as exc:
        _LOGGER.warning("refresh result delivery failed error=%s", type(exc).__name__)
        return "⚠️ Не удалось обновить сообщение. Повторите или выберите «Монеты»."
    return None


async def _refresh_market_data(
    session: QuerySession,
    services: Services,
    *,
    include_chart: bool,
) -> tuple[Calculation, Chart | None]:
    parsed = parse_expression(
        session.expression,
        max_symbols=services.settings.max_symbols,
    )
    calculation = await _calculate(parsed, services, force_refresh=True)
    chart: Chart | None = None
    if include_chart and session.active_timeframe is not None:
        if len(calculation.coefficients) != 1:
            raise ExpressionError("График доступен для одной монеты")
        symbol = next(iter(calculation.coefficients))
        chart = await services.market.chart(
            calculation.quotes[symbol],
            session.active_timeframe,
            force_refresh=True,
        )
    return calculation, chart


async def _chart_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: QuerySession,
    timeframe: str,
    *,
    edit_message: Message | None = None,
) -> str | None:
    query = update.callback_query
    user = update.effective_user
    source_message = (
        query.message if query is not None and isinstance(query.message, Message) else None
    )
    if source_message is None or user is None:
        return "⚠️ Сообщение с кнопкой больше недоступно."
    target_message = edit_message or source_message
    if timeframe not in {"1h", "24h", "7d"}:
        return "⚠️ Неизвестный период."
    calculation = session.calculation
    services = _services(context)
    replay = calculation is None
    try:
        if calculation is None:
            calculation, replay_chart = await asyncio.wait_for(
                _with_query_slot(
                    services,
                    lambda: _refresh_market_data(
                        replace(session, active_timeframe=timeframe), services, include_chart=True,
                    ),
                ),
                timeout=services.settings.query_timeout,
            )
            if replay_chart is None:
                raise MarketUnavailable("chart is absent")
            chart = replay_chart
        else:
            if len(calculation.coefficients) != 1:
                return "⚠️ График доступен для одной монеты."
            symbol = next(iter(calculation.coefficients))
            quote = calculation.quotes[symbol]
            chart = await asyncio.wait_for(
                _with_query_slot(services, lambda: services.market.chart(quote, timeframe)),
                timeout=services.settings.query_timeout,
            )
        image = await services.charts.render(chart)
        caption = render_card(calculation, chart) if replay else chart_caption(calculation, chart)
        keyboard = result_keyboard(session.token, calculation, active_timeframe=timeframe, persistent=replay)
        await _edit_result_media(
            context.bot,
            target_message,
            image,
            caption,
            keyboard,
            receiver_user_id=user.id,
        )
        if not replay:
            services.registry.set_active_timeframe(session, timeframe)
    except TimeoutError:
        return "⚠️ График не успел загрузиться. Попробуйте позже."
    except ExpressionError, MarketUnavailable, ValueError:
        return "⚠️ График сейчас недоступен."
    except (TelegramError, RuntimeError) as exc:
        _LOGGER.warning("chart result delivery failed error=%s", type(exc).__name__)
        return "⚠️ Не удалось показать график. Повторите или выберите «Монеты»."
    return None


async def _favorite_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[str],
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    try:
        await query.answer("Сохраняю…")
    except TelegramError:
        return

    services = _services(context)
    error_text: str | None = None
    favorites: tuple[str, ...] | None = None
    async with services.preference_locks.get(user.id):
        current = list(await services.store.favorites(user.id, _chat_id(update)))
        if parts[1:] == ["reset"]:
            current = list(services.settings.default_favorites)
        elif len(parts) == 3 and parts[1] == "toggle" and _SYMBOL_RE.fullmatch(parts[2]):
            symbol = parts[2]
            if symbol in current:
                if len(current) == 1:
                    error_text = "Нужна хотя бы одна избранная монета."
                else:
                    current.remove(symbol)
            elif len(current) >= services.settings.max_favorites:
                error_text = f"Максимум {services.settings.max_favorites} монет."
            else:
                current.append(symbol)
        else:
            error_text = "Кнопка устарела."
        if error_text is None:
            try:
                favorites = await services.store.set_favorites(user.id, tuple(current))
            except ValueError:
                error_text = "Не удалось сохранить настройки."

    message = query.message if isinstance(query.message, Message) else None
    if error_text is not None:
        if message is not None and (
            not _is_group(update) or _ephemeral_message_id(message) is not None
        ):
            current_favorites = tuple(current)
            await _edit_result_message(
                context.bot,
                message,
                settings_text(current_favorites, services.settings.max_favorites)
                + f"\n\n⚠️ {_escape(error_text)}",
                settings_keyboard(current_favorites),
                receiver_user_id=user.id,
                callback_query_id=query.id,
            )
        else:
            await _send_html(
                update,
                context,
                f"⚠️ {_escape(error_text)}",
                ephemeral=_ephemeral_response_available(update),
            )
        return

    if favorites is None:
        raise RuntimeError("favorite update produced no result")
    text = settings_text(favorites, services.settings.max_favorites)
    keyboard = settings_keyboard(favorites)
    if message is not None and _is_group(update) and _ephemeral_message_id(message) is None:
        await _send_html(
            update,
            context,
            text,
            reply_markup=keyboard,
            ephemeral=True,
        )
    elif message is not None:
        await _edit_result_message(
            context.bot,
            message,
            text,
            keyboard,
            receiver_user_id=user.id,
            callback_query_id=query.id,
        )


async def _show_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit: bool,
) -> None:
    user = update.effective_user
    if user is None:
        return
    if not await _ensure_personal_response(update, context):
        return
    services = _services(context)
    favorites = await services.store.favorites(user.id, _chat_id(update))
    text = settings_text(favorites, services.settings.max_favorites)
    keyboard = settings_keyboard(favorites)
    query = update.callback_query
    message = query.message if query is not None and isinstance(query.message, Message) else None
    if message is not None and (edit or _ephemeral_message_id(message) is not None):
        await _edit_result_message(
            context.bot,
            message,
            text,
            keyboard,
            receiver_user_id=user.id,
            callback_query_id=query.id if query is not None else None,
        )
    else:
        await _send_html(
            update,
            context,
            text,
            reply_markup=keyboard,
            ephemeral=_ephemeral_response_available(update),
        )


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    user = update.effective_user
    if inline_query is None or user is None:
        return
    expression = inline_query.query.strip()
    if not expression:
        await inline_query.answer(
            [],
            cache_time=5,
            is_personal=True,
            button=None,
        )
        return
    decision = _services(context).limiter.check(("inline", user.id))
    if not decision.allowed:
        result = _inline_notice(
            expression,
            "Слишком много запросов",
            "Подождите несколько секунд и повторите.",
        )
        await inline_query.answer([result], cache_time=2, is_personal=True)
        return
    try:
        parsed = parse_expression(
            expression,
            max_symbols=_services(context).settings.max_symbols,
        )
        if parsed.coefficients:
            calculation = await asyncio.wait_for(
                _calculate(parsed, _services(context)), timeout=9.0
            )
            message = render_calculation(calculation)
            description = f"Итого ${format_decimal(calculation.total_usd)}"
        else:
            value = parsed.constant
            formatted_value = format_decimal(value)
            message = f"<code>{_escape(parsed.source)}</code> = <b>{formatted_value}</b>"
            description = f"Результат: {formatted_value}"
        result_id = hashlib.sha256(expression.encode("utf-8")).hexdigest()[:32]
        result = InlineQueryResultArticle(
            id=result_id,
            title=f"Рассчитать: {expression[:48]}",
            description=description,
            input_message_content=InputTextMessageContent(
                message,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            ),
        )
        await inline_query.answer([result], cache_time=15, is_personal=True)
    except ExpressionError as exc:
        result = _inline_notice(expression, "Проверьте выражение", str(exc))
        await inline_query.answer([result], cache_time=2, is_personal=True)
    except TimeoutError, MarketUnavailable:
        result = _inline_notice(
            expression,
            "Цены временно недоступны",
            "Повторите запрос позже.",
        )
        await inline_query.answer([result], cache_time=2, is_personal=True)


def _inline_notice(expression: str, title: str, description: str) -> InlineQueryResultArticle:
    result_id = hashlib.sha256(f"notice:{title}:{expression}".encode()).hexdigest()[:32]
    safe_description = description[:240]
    return InlineQueryResultArticle(
        id=result_id,
        title=title[:64],
        description=safe_description,
        input_message_content=InputTextMessageContent(
            f"<b>{_escape(title)}</b>\n{_escape(safe_description)}",
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        ),
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, Conflict):
        _LOGGER.critical("another Telegram getUpdates consumer detected; stopping")
        context.application.stop_running()
        return
    if isinstance(error, (NetworkError, RetryAfter)) and not isinstance(update, Update):
        _LOGGER.warning("Telegram polling transient error=%s", type(error).__name__)
        return
    _LOGGER.error(
        "unhandled update error=%s",
        type(error).__name__,
        exc_info=(type(error), error, error.__traceback__) if error else None,
    )
    if isinstance(update, Update) and update.effective_chat is not None:
        try:
            await _send_html(
                update,
                context,
                "⚠️ Непредвиденная ошибка. Повторите запрос позже.",
                ephemeral=_ephemeral_response_available(update),
            )
        except TelegramError, RuntimeError:
            _LOGGER.warning("could not deliver error response")


async def _show_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> int | None:
    if _is_group(update) or update.effective_chat is None:
        try:
            await context.bot.send_chat_action(
                chat_id=_chat_id(update),
                action=ChatAction.TYPING,
                message_thread_id=_thread_id(update),
            )
        except TelegramError:
            pass
        return None
    draft_id = (update.update_id % 2_000_000_000) + 1
    try:
        await context.bot.send_message_draft(
            chat_id=update.effective_chat.id,
            message_thread_id=_thread_id(update),
            draft_id=draft_id,
            text=text,
        )
        return draft_id
    except TelegramError:
        return None


async def _update_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int | None,
    text: str,
) -> None:
    if draft_id is None or update.effective_chat is None:
        return
    try:
        await context.bot.send_message_draft(
            chat_id=update.effective_chat.id,
            message_thread_id=_thread_id(update),
            draft_id=draft_id,
            text=text,
        )
    except TelegramError:
        pass


async def _try_send_ephemeral_progress(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: Any = None,
) -> Message | None:
    try:
        return await _send_html(
            update,
            context,
            text,
            reply_markup=reply_markup,
            ephemeral=True,
        )
    except (TelegramError, RuntimeError) as exc:
        _LOGGER.warning("ephemeral progress unavailable error=%s", type(exc).__name__)
        return None


async def _send_html(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: Any = None,
    ephemeral: bool = False,
) -> Message:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        raise RuntimeError("update has no effective chat")
    message = update.effective_message
    query = update.callback_query
    if ephemeral and not _ephemeral_response_available(update):
        raise RuntimeError("ephemeral response requires an eligible Telegram trigger")
    ephemeral_message_id = _ephemeral_message_id(message)
    reply_parameters: Any = None
    if message is not None and _is_group(update) and query is None:
        if ephemeral_message_id is not None:
            reply_parameters = {"ephemeral_message_id": ephemeral_message_id}
        elif message.message_id:
            reply_parameters = ReplyParameters(
                message_id=message.message_id,
                allow_sending_without_reply=True,
            )

    api_kwargs = None
    if ephemeral and user is not None and _is_group(update):
        parameters: dict[str, Any] = {"receiver_user_id": user.id}
        if query is not None:
            parameters["callback_query_id"] = query.id
            if ephemeral_message_id is None:
                parameters["replace_callback_query_message"] = True
        api_kwargs = {"ephemeral_message_parameters": parameters}

    kwargs = {
        "chat_id": chat.id,
        "message_thread_id": _thread_id(update),
        "text": text,
        "parse_mode": ParseMode.HTML,
        "link_preview_options": LinkPreviewOptions(is_disabled=True),
        "reply_parameters": reply_parameters,
        "reply_markup": reply_markup,
        "api_kwargs": api_kwargs,
    }
    try:
        sent = await context.bot.send_message(**kwargs)
    except BadRequest:
        if api_kwargs is None:
            raise
        _LOGGER.info("ephemeral message unavailable; refusing public fallback")
        raise
    if ephemeral and _is_group(update) and _ephemeral_message_id(sent) is None:
        try:
            await sent.delete()
        except TelegramError:
            _LOGGER.error("could not remove invalid non-ephemeral response")
        raise RuntimeError("Telegram returned incomplete ephemeral message")
    return sent


async def _edit_ephemeral_text(
    bot: Any,
    message: Message,
    receiver_user_id: int,
    text: str,
    reply_markup: Any = None,
) -> None:
    ephemeral_message_id = _ephemeral_message_id(message)
    if ephemeral_message_id is None:
        raise RuntimeError("Telegram returned incomplete ephemeral message")
    await bot.do_api_request(
        "edit_ephemeral_message_text",
        api_kwargs={
            "chat_id": message.chat_id,
            "receiver_user_id": receiver_user_id,
            "ephemeral_message_id": ephemeral_message_id,
            "text": text,
            "parse_mode": ParseMode.HTML,
            "reply_markup": reply_markup,
            "link_preview_options": LinkPreviewOptions(is_disabled=True).to_dict(),
        },
    )


async def _delete_ephemeral_message(
    bot: Any,
    message: Message,
    receiver_user_id: int,
) -> None:
    ephemeral_message_id = _ephemeral_message_id(message)
    if ephemeral_message_id is None:
        _LOGGER.warning("Telegram returned an ephemeral message without its identifier")
        return
    try:
        await bot.do_api_request(
            "delete_ephemeral_message",
            api_kwargs={
                "chat_id": message.chat_id,
                "receiver_user_id": receiver_user_id,
                "ephemeral_message_id": ephemeral_message_id,
            },
        )
    except TelegramError:
        _LOGGER.debug("could not remove ephemeral progress message")


async def _replace_media_with_text(
    bot: Any,
    message: Message,
    text: str,
    reply_markup: Any,
    *,
    receiver_user_id: int,
    callback_query_id: str | None = None,
) -> None:
    ephemeral_message_id = _ephemeral_message_id(message)
    kwargs: dict[str, Any] = {
        "chat_id": message.chat_id,
        "message_thread_id": message.message_thread_id,
        "text": text,
        "parse_mode": ParseMode.HTML,
        "link_preview_options": LinkPreviewOptions(is_disabled=True),
        "reply_markup": reply_markup,
    }
    if ephemeral_message_id is not None:
        parameters: dict[str, Any] = {"receiver_user_id": receiver_user_id}
        kwargs["reply_parameters"] = {"ephemeral_message_id": ephemeral_message_id}
        if callback_query_id is not None:
            parameters["callback_query_id"] = callback_query_id
        kwargs["api_kwargs"] = {"ephemeral_message_parameters": parameters}
    sent = await bot.send_message(**kwargs)
    if sent is None:
        raise RuntimeError("Telegram returned no message")
    if ephemeral_message_id is not None and _ephemeral_message_id(sent) is None:
        try:
            await sent.delete()
        except TelegramError:
            _LOGGER.error("could not remove invalid non-ephemeral media replacement")
        raise RuntimeError("Telegram returned incomplete ephemeral message")
    await _retire_message(bot, message, receiver_user_id)


async def _edit_result_message(
    bot: Any,
    message: Message,
    text: str,
    reply_markup: Any,
    *,
    receiver_user_id: int,
    callback_query_id: str | None = None,
) -> None:
    if message.photo:
        await _replace_media_with_text(
            bot,
            message,
            text,
            reply_markup,
            receiver_user_id=receiver_user_id,
            callback_query_id=callback_query_id,
        )
        return
    ephemeral_message_id = _ephemeral_message_id(message)
    try:
        if ephemeral_message_id is not None:
            payload = {
                "chat_id": message.chat_id,
                "receiver_user_id": receiver_user_id,
                "ephemeral_message_id": ephemeral_message_id,
                "text": text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": reply_markup,
                "link_preview_options": LinkPreviewOptions(is_disabled=True).to_dict(),
            }
            await bot.do_api_request("edit_ephemeral_message_text", api_kwargs=payload)
        else:
            await message.edit_text(
                text=text,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_markup=reply_markup,
            )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise


async def _edit_result_media(
    bot: Any,
    message: Message,
    image: Any,
    caption: str,
    reply_markup: Any,
    *,
    receiver_user_id: int,
) -> None:
    ephemeral_message_id = _ephemeral_message_id(message)
    if ephemeral_message_id is not None:
        media = InputMediaPhoto(image, caption=caption, parse_mode=ParseMode.HTML)
        await bot.do_api_request(
            "edit_ephemeral_message_media",
            api_kwargs={
                "chat_id": message.chat_id,
                "receiver_user_id": receiver_user_id,
                "ephemeral_message_id": ephemeral_message_id,
                "media": media,
                "reply_markup": reply_markup,
            },
        )
        return
    if message.photo:
        media = InputMediaPhoto(image, caption=caption, parse_mode=ParseMode.HTML)
        await message.edit_media(media, reply_markup=reply_markup)
        return
    sent = await bot.send_photo(
        chat_id=message.chat_id,
        message_thread_id=message.message_thread_id,
        photo=image,
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    if sent is None:
        raise RuntimeError("Telegram returned no message")
    await _retire_message(bot, message, receiver_user_id)


async def _with_query_slot[T](
    services: Services,
    operation: Callable[[], Awaitable[T]],
) -> T:
    async with services.query_slots:
        return await operation()


async def _edit_error_message(
    bot: Any,
    message: Message,
    text: str,
    reply_markup: Any,
    *,
    receiver_user_id: int,
) -> None:
    ephemeral_message_id = _ephemeral_message_id(message)
    if not message.photo:
        await _edit_result_message(
            bot,
            message,
            text,
            reply_markup,
            receiver_user_id=receiver_user_id,
        )
        return
    try:
        if ephemeral_message_id is None:
            await message.edit_caption(caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            await bot.do_api_request(
                "edit_ephemeral_message_caption",
                api_kwargs={
                    "chat_id": message.chat_id,
                    "receiver_user_id": receiver_user_id,
                    "ephemeral_message_id": ephemeral_message_id,
                    "caption": text,
                    "parse_mode": ParseMode.HTML,
                    "reply_markup": reply_markup,
                },
            )
    except BadRequest as exc:
        if "message is not modified" not in str(exc).casefold():
            raise


async def _deliver_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit_message: Message | None,
    text: str,
) -> bool:
    user = update.effective_user
    try:
        if edit_message is not None and user is not None:
            await _edit_error_message(
                context.bot,
                edit_message,
                text,
                getattr(edit_message, "reply_markup", None) or navigation_keyboard(),
                receiver_user_id=user.id,
            )
        else:
            await _send_html(
                update,
                context,
                text,
                reply_markup=navigation_keyboard(),
                ephemeral=_ephemeral_response_available(update),
            )
    except (TelegramError, RuntimeError) as exc:
        _LOGGER.warning("could not deliver query error response error=%s", type(exc).__name__)
        return False
    return True


def _group_expression(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> str:
    bot_username = context.bot.username or "CryptoMathXBot"
    mention_pattern = re.compile(rf"@{re.escape(bot_username)}\b", re.IGNORECASE)
    if mention_pattern.search(text) is None:
        return ""
    return mention_pattern.sub("", text).strip()


def _services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return cast(Services, context.application.bot_data["services"])


def _services_from_application(
    application: Application[Any, Any, Any, Any, Any, Any],
) -> Services:
    return cast(Services, application.bot_data["services"])


def _is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in _GROUP_TYPES)


def _ephemeral_response_available(update: Update) -> bool:
    if not _is_group(update) or update.effective_user is None:
        return False
    if update.callback_query is not None:
        return True
    return _ephemeral_message_id(update.effective_message) is not None


async def _ensure_personal_response(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    if not _is_group(update) or _ephemeral_response_available(update):
        return True
    username = context.bot.username or "CryptoMathXBot"
    await _send_html(
        update,
        context,
        f"🔒 Личные настройки доступны в чате с <code>@{_escape(username)}</code>.",
    )
    return False


def _chat_id(update: Update) -> int:
    if update.effective_chat is None:
        raise RuntimeError("update has no effective chat")
    return update.effective_chat.id


def _thread_id(update: Update) -> int | None:
    message = update.effective_message
    return message.message_thread_id if isinstance(message, Message) else None


def _ephemeral_message_id(message: object | None) -> int | None:
    api_kwargs = getattr(message, "api_kwargs", None)
    if not isinstance(api_kwargs, Mapping):
        return None
    value = api_kwargs.get("ephemeral_message_id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _escape(value: object) -> str:
    from html import escape

    return escape(str(value))


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except OSError, UnicodeError:
        print("Ошибка конфигурации: не удалось прочитать файлы настройки.", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        configure_logging(settings.log_dir, settings.log_level, secrets=(settings.token,))
    except OSError:
        print("Ошибка запуска: не удалось подготовить каталог журналов.", file=sys.stderr)
        raise SystemExit(2) from None

    try:
        application = build_application(settings)
        with SingleInstanceLock(settings.data_dir / "cryptomathxbot.lock"):
            application.run_polling(
                allowed_updates=["message", "callback_query", "inline_query"],
                drop_pending_updates=False,
                close_loop=True,
            )
    except InvalidToken:
        _LOGGER.error("startup refused: Telegram rejected the bot token")
        raise SystemExit(2) from None
    except AlreadyRunningError:
        _LOGGER.error("startup refused: another instance is already running")
        raise SystemExit(2) from None
    except InstanceLockError:
        _LOGGER.error("startup refused: instance lock is unavailable")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
