from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
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
from .charts import ChartRenderer
from .config import Settings
from .domain import Calculation, Coin
from .logging_setup import configure_logging
from .market import MarketService, MarketUnavailable
from .rate_limit import SlidingWindowLimiter
from .session import ActorLocks, QueryRegistry, QuerySession
from .single_instance import AlreadyRunningError, SingleInstanceLock
from .storage import PreferencesStore
from .ui import (
    chart_caption,
    format_decimal,
    help_text,
    home_keyboard,
    render_calculation,
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
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(InlineQueryHandler(inline_query_handler))
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
        BotCommand("price", "Рассчитать цену или выражение"),
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
    identity = await application.bot.get_me()
    _LOGGER.info("READY username=%s version=%s pid=%d", identity.username, __version__, os.getpid())
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
                ephemeral=_is_group(update),
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
            ephemeral=_is_group(update),
        )
        return
    if len(raw_symbols) > services.settings.max_favorites:
        await _send_html(
            update,
            context,
            f"Можно выбрать не больше {services.settings.max_favorites} монет.",
            ephemeral=_is_group(update),
        )
        return

    progress: Message | None = None
    if _is_group(update) and _ephemeral_message_id(update.effective_message) is not None:
        progress = await _send_html(update, context, "⏳ Проверяю монеты…", ephemeral=True)

    try:
        resolved = await services.market.resolve_many(raw_symbols)
        unknown = [symbol for symbol in raw_symbols if symbol not in resolved]
        if unknown:
            text = "Не нашёл: <code>" + ", ".join(map(_escape, unknown)) + "</code>"
            keyboard = None
        else:
            canonical = tuple(dict.fromkeys(resolved[symbol].symbol for symbol in raw_symbols))
            async with services.preference_locks.get(user.id):
                favorites = await services.store.set_favorites(user.id, canonical)
            text = settings_text(favorites, services.settings.max_favorites)
            keyboard = settings_keyboard(favorites)
    except MarketUnavailable:
        text = "⚠️ Рыночные источники временно недоступны. Повторите через минуту."
        keyboard = None

    if progress is not None:
        await _edit_ephemeral_text(context.bot, progress, user.id, text, keyboard)
    else:
        await _send_html(
            update,
            context,
            text,
            reply_markup=keyboard,
            ephemeral=_is_group(update),
        )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    expression = " ".join(context.args or ()).strip()
    if not expression:
        await _send_html(
            update,
            context,
            "Пример: <code>/price 0.5 BTC + 2 ETH</code>",
            ephemeral=_is_group(update),
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
        ephemeral=_is_group(update),
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    expression = message.text.strip()
    if _is_group(update):
        expression = _group_expression(update, context, expression)
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
    services = _services(context)
    decision = services.limiter.check(("query", user.id))
    if not decision.allowed:
        if decision.notify:
            await _send_html(
                update,
                context,
                f"⏳ Слишком часто. Повторите через {max(1, round(decision.retry_after))} с.",
                ephemeral=_is_group(update),
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
                ephemeral=_is_group(update),
            )
        return

    async with actor_lock, services.query_slots:
        started_at = time.monotonic()
        draft_id = None
        if edit_message is None:
            draft_id = await _show_draft(update, context, "Ищу монеты…")
        try:
            parsed = parse_expression(expression, max_symbols=services.settings.max_symbols)
            if not parsed.coefficients:
                value = parsed.evaluate({})
                arithmetic_text = (
                    f"<code>{_escape(parsed.source)}</code> = <b>{format_decimal(value)}</b>"
                )
                if edit_message is not None:
                    await _edit_result_message(
                        context.bot,
                        edit_message,
                        arithmetic_text,
                        None,
                        receiver_user_id=user.id,
                    )
                else:
                    await _send_html(update, context, arithmetic_text)
                return
            await _update_draft(update, context, draft_id, "Получаю актуальные цены…")
            calculation = await _calculate(parsed, services)
            session = services.registry.create(user.id, expression, calculation)
            text = render_calculation(calculation)
            keyboard = result_keyboard(session.token, calculation)
            if edit_message is not None:
                await _edit_result_message(
                    context.bot,
                    edit_message,
                    text,
                    keyboard,
                    receiver_user_id=user.id,
                )
            else:
                await _send_html(update, context, text, reply_markup=keyboard)
            _LOGGER.info(
                "query complete symbols=%d duration_ms=%d",
                len(calculation.coefficients),
                round((time.monotonic() - started_at) * 1000),
            )
        except ExpressionError as exc:
            await _deliver_error(update, context, edit_message, f"⚠️ {_escape(str(exc))}")
        except MarketUnavailable:
            await _deliver_error(
                update,
                context,
                edit_message,
                "⚠️ Рыночные источники временно недоступны. Повторите через минуту.",
            )
        except Exception:
            _LOGGER.exception("query failed")
            await _deliver_error(
                update,
                context,
                edit_message,
                "⚠️ Не удалось выполнить расчёт. Попробуйте ещё раз.",
            )


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
    data = query.data or ""
    parts = data.split("|")

    if parts[:2] == ["menu", "home"]:
        await query.answer()
        favorites = await _services(context).store.favorites(user.id, _chat_id(update))
        if _is_group(update) and _ephemeral_message_id(message) is None:
            await _send_html(
                update,
                context,
                start_text(),
                reply_markup=home_keyboard(favorites),
                ephemeral=True,
            )
        else:
            await _edit_result_message(
                context.bot,
                message,
                start_text(),
                home_keyboard(favorites),
                receiver_user_id=user.id,
                callback_query_id=query.id,
            )
        return

    if parts[:2] == ["menu", "help"]:
        await query.answer()
        favorites = await _services(context).store.favorites(user.id, _chat_id(update))
        if _is_group(update) and _ephemeral_message_id(message) is None:
            await _send_html(
                update,
                context,
                help_text(),
                reply_markup=home_keyboard(favorites),
                ephemeral=True,
            )
        else:
            await _edit_result_message(
                context.bot,
                message,
                help_text(),
                home_keyboard(favorites),
                receiver_user_id=user.id,
                callback_query_id=query.id,
            )
        return

    if parts[:2] == ["menu", "settings"]:
        await query.answer()
        await _show_settings(update, context, edit=not _is_group(update))
        return
    if len(parts) == 2 and parts[0] == "symbol":
        if _SYMBOL_RE.fullmatch(parts[1]) is None:
            await query.answer("Кнопка устарела.", show_alert=True)
            return
        await query.answer("Получаю цену…")
        await _handle_expression(
            update,
            context,
            parts[1],
            edit_message=message,
        )
        return

    if parts and parts[0] == "fav":
        await _favorite_callback(update, context, parts)
        return

    if len(parts) >= 3 and parts[0] == "q":
        services = _services(context)
        session = services.registry.get(parts[1], user.id)
        if session is None:
            await query.answer(
                "Кнопка устарела или принадлежит другому пользователю.", show_alert=True
            )
            return
        action = parts[2]
        is_chart = len(parts) == 4 and action == "chart"
        if action != "refresh" and not is_chart:
            await query.answer("Кнопка устарела.", show_alert=True)
            return
        if is_chart and parts[3] not in {"1h", "24h", "7d"}:
            await query.answer("Неизвестный период.", show_alert=True)
            return
        if is_chart and len(session.calculation.coefficients) != 1:
            await query.answer("График доступен для одной монеты.", show_alert=True)
            return
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
        try:
            await query.answer("Обновляю…" if action == "refresh" else "Готовлю график…")
            async with services.query_slots:
                if action == "refresh":
                    error_text = await _refresh_callback(update, context, session)
                else:
                    error_text = await _chart_callback(update, context, session, parts[3])
            if error_text is not None:
                await _send_html(
                    update,
                    context,
                    error_text,
                    ephemeral=_is_group(update),
                )
        finally:
            actor_lock.release()
        return

    await query.answer("Кнопка устарела.", show_alert=True)


async def _refresh_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: QuerySession,
) -> str | None:
    query = update.callback_query
    user = update.effective_user
    message = query.message if query is not None and isinstance(query.message, Message) else None
    if message is None or user is None:
        return "⚠️ Сообщение с кнопкой больше недоступно."
    services = _services(context)
    try:
        parsed = parse_expression(
            session.expression,
            max_symbols=services.settings.max_symbols,
        )
        calculation = await _calculate(parsed, services, force_refresh=True)
        session = services.registry.update(session, calculation)
        text = render_calculation(calculation)
        keyboard = result_keyboard(
            session.token,
            calculation,
            active_timeframe=session.active_timeframe,
        )
        if message.photo and session.active_timeframe is not None:
            symbol = next(iter(calculation.coefficients))
            chart = await services.market.chart(
                calculation.quotes[symbol],
                session.active_timeframe,
                force_refresh=True,
            )
            image = await services.charts.render(chart)
            await _edit_result_media(
                context.bot,
                message,
                image,
                chart_caption(calculation, chart),
                keyboard,
                receiver_user_id=user.id,
            )
        else:
            await _edit_result_message(
                context.bot,
                message,
                text,
                keyboard,
                receiver_user_id=user.id,
            )
    except (ExpressionError, MarketUnavailable, ValueError):
        return "⚠️ Не удалось обновить цены. Попробуйте позже."
    except TelegramError as exc:
        _LOGGER.warning("refresh result delivery failed error=%s", type(exc).__name__)
        return "⚠️ Не удалось обновить сообщение. Откройте /start и повторите."
    return None


async def _chart_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: QuerySession,
    timeframe: str,
) -> str | None:
    query = update.callback_query
    user = update.effective_user
    message = query.message if query is not None and isinstance(query.message, Message) else None
    if message is None or user is None:
        return "⚠️ Сообщение с кнопкой больше недоступно."
    if timeframe not in {"1h", "24h", "7d"}:
        return "⚠️ Неизвестный период."
    calculation = session.calculation
    if len(calculation.coefficients) != 1:
        return "⚠️ График доступен для одной монеты."
    symbol = next(iter(calculation.coefficients))
    quote = calculation.quotes[symbol]
    services = _services(context)
    try:
        chart = await services.market.chart(quote, timeframe)
        image = await services.charts.render(chart)
        caption = chart_caption(calculation, chart)
        keyboard = result_keyboard(session.token, calculation, active_timeframe=timeframe)
        await _edit_result_media(
            context.bot,
            message,
            image,
            caption,
            keyboard,
            receiver_user_id=user.id,
        )
        session = services.registry.set_active_timeframe(session, timeframe)
    except (MarketUnavailable, ValueError):
        return "⚠️ График сейчас недоступен."
    except TelegramError as exc:
        _LOGGER.warning("chart result delivery failed error=%s", type(exc).__name__)
        return "⚠️ Не удалось показать график. Откройте /start и повторите."
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
                ephemeral=_is_group(update),
                callback_query_response=False,
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
            callback_query_response=False,
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
            ephemeral=_is_group(update),
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
            value = parsed.evaluate({})
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
    except (MarketUnavailable, asyncio.TimeoutError):
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
                ephemeral=_is_group(update),
            )
        except TelegramError:
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

async def _send_html(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    reply_markup: Any = None,
    ephemeral: bool = False,
    callback_query_response: bool = True,
) -> Message:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        raise RuntimeError("update has no effective chat")
    message = update.effective_message
    query = update.callback_query
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
        if query is not None and callback_query_response:
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
        return await context.bot.send_message(**kwargs)
    except BadRequest:
        if api_kwargs is None:
            raise
        _LOGGER.info("ephemeral message unavailable; refusing public fallback")
        raise


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
        if callback_query_id is not None:
            parameters["callback_query_id"] = callback_query_id
        kwargs["api_kwargs"] = {"ephemeral_message_parameters": parameters}
    sent = await bot.send_message(**kwargs)
    if sent is None:
        raise RuntimeError("Telegram returned no message")
    if ephemeral_message_id is not None:
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
            _LOGGER.debug("could not remove previous ephemeral media result")
        return
    try:
        await message.delete()
    except TelegramError:
        _LOGGER.debug("could not remove previous media result")


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
    media = InputMediaPhoto(image, caption=caption, parse_mode=ParseMode.HTML)
    ephemeral_message_id = _ephemeral_message_id(message)
    if ephemeral_message_id is not None:
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
    try:
        await message.delete()
    except TelegramError:
        _LOGGER.debug("could not remove text result after chart upload")


async def _deliver_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    edit_message: Message | None,
    text: str,
) -> None:
    user = update.effective_user
    if edit_message is not None and user is not None:
        await _edit_result_message(
            context.bot,
            edit_message,
            text,
            None,
            receiver_user_id=user.id,
        )
    else:
        await _send_html(update, context, text, ephemeral=_is_group(update))


def _group_expression(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> str:
    message = update.effective_message
    bot_username = context.bot.username or "CryptoMathXBot"
    mention_pattern = re.compile(rf"@{re.escape(bot_username)}\b", re.IGNORECASE)
    mentioned = mention_pattern.search(text) is not None
    replied_to_bot = bool(
        message
        and message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == context.bot.id
    )
    if not mentioned and not replied_to_bot:
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
    settings = Settings.from_env()
    configure_logging(settings.log_dir, settings.log_level, secrets=(settings.token,))
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


if __name__ == "__main__":
    main()
