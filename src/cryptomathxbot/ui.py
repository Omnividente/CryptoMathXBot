from __future__ import annotations

import math
from decimal import Decimal
from html import escape

from telegram import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup

from .domain import Calculation, Chart

_TIMEFRAME_LABELS = {"1h": "1 ч", "24h": "24 ч", "7d": "7 д"}
_COMMON_SYMBOLS = ("BTC", "ETH", "TON", "SOL", "XMR", "BNB", "XRP", "DOGE")


def format_decimal(value: Decimal, *, max_decimals: int = 12) -> str:
    if not value.is_finite():
        raise ValueError("value must be finite")
    if value == 0:
        return "0"
    absolute = abs(value)
    adjusted = absolute.adjusted()
    if adjusted >= 18 or adjusted <= -13:
        mantissa, exponent = f"{value:.8E}".split("E", maxsplit=1)
        mantissa = mantissa.rstrip("0").rstrip(".")
        sign = exponent[0]
        digits = exponent[1:].lstrip("0") or "0"
        return f"{mantissa}E{sign}{digits}"
    if absolute >= 1:
        decimals = 2 if absolute >= 100 else 4
    else:
        decimals = min(max_decimals, max(4, -adjusted + 3))
    rendered = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
    rendered = rendered.replace(",", "\u202f")
    if rendered in {"0", "-0"}:
        return f"{value:.4E}"
    return rendered


def render_calculation(calculation: Calculation) -> str:
    lines: list[str] = ["<b>CryptoMathX · результат</b>"]
    stale = False
    sources: list[str] = []

    for symbol, coefficient in calculation.coefficients.items():
        quote = calculation.quotes[symbol]
        subtotal = coefficient * quote.usd
        change = _format_change(quote.change_24h)
        lines.extend(
            [
                "",
                f"<b>{escape(quote.coin.name)} · {escape(symbol)}</b>",
                f"<code>{format_decimal(coefficient)} × ${format_decimal(quote.usd)}</code>",
                f"24ч: {change} · сумма <code>${format_decimal(subtotal)}</code>",
            ]
        )
        stale = stale or quote.stale
        if quote.source not in sources:
            sources.append(quote.source)

    if calculation.constant_usd:
        lines.extend(
            [
                "",
                f"Денежная часть: <code>${format_decimal(calculation.constant_usd)}</code>",
            ]
        )

    total_line = f"<b>Итого: ${format_decimal(calculation.total_usd)}</b>"
    if calculation.total_rub is not None:
        total_line += f" · <b>{format_decimal(calculation.total_rub)} ₽</b>"
    lines.extend(["", total_line])

    if calculation.usd_rub is not None:
        cbr_date = escape(calculation.cbr_date or "—")
        lines.append(f"<i>USD/RUB ЦБ: {format_decimal(calculation.usd_rub)} · {cbr_date}</i>")
    else:
        lines.append("<i>Курс USD/RUB временно недоступен; итог в рублях не рассчитан.</i>")
    if stale:
        lines.append(
            "<i>⚠️ Использована последняя сохранённая цена: провайдер временно недоступен.</i>"
        )
    lines.append(f"<i>Источники: {escape(', '.join(sources))}</i>")
    return "\n".join(lines)


def chart_caption(calculation: Calculation, chart: Chart) -> str:
    first_price = chart.points[0][1]
    last_price = chart.points[-1][1]
    if first_price > 0 and math.isfinite(first_price) and math.isfinite(last_price):
        change = (last_price / first_price - 1) * 100
        change_text = f"{change:+.2f}%"
    else:
        change_text = "—"
    timeframe = _TIMEFRAME_LABELS.get(chart.timeframe, chart.timeframe)
    return (
        f"{render_calculation(calculation)}\n\n"
        f"<i>График {escape(chart.symbol)} · {escape(timeframe)} · "
        f"изменение {change_text}</i>"
    )


def result_keyboard(
    token: str,
    calculation: Calculation,
    *,
    active_timeframe: str | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "↻ Обновить",
                callback_data=f"q|{token}|refresh",
                style="primary",
            ),
            InlineKeyboardButton(
                "Скопировать итог",
                copy_text=CopyTextButton(text=f"${format_decimal(calculation.total_usd)}"),
            ),
        ]
    ]
    if len(calculation.coefficients) == 1:
        chart_row: list[InlineKeyboardButton] = []
        for timeframe in ("1h", "24h", "7d"):
            label = _TIMEFRAME_LABELS[timeframe]
            if active_timeframe == timeframe:
                label = f"✓ {label}"
            chart_row.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"q|{token}|chart|{timeframe}",
                    style="success" if active_timeframe == timeframe else None,
                )
            )
        rows.append(chart_row)
    rows.append([InlineKeyboardButton("⚙️ Настройки", callback_data="menu|settings")])
    return InlineKeyboardMarkup(rows)


def home_keyboard(favorites: tuple[str, ...]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                symbol,
                callback_data=f"symbol|{symbol}",
                style="primary" if index == 0 else None,
            )
            for index, symbol in enumerate(favorites[start : start + 3])
        ]
        for start in range(0, len(favorites), 3)
    ]
    rows.append(
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="menu|settings"),
            InlineKeyboardButton("Помощь", callback_data="menu|help"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def settings_keyboard(favorites: tuple[str, ...]) -> InlineKeyboardMarkup:
    available = tuple(dict.fromkeys((*favorites, *_COMMON_SYMBOLS)))
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(available), 3):
        row: list[InlineKeyboardButton] = []
        for symbol in available[start : start + 3]:
            selected = symbol in favorites
            row.append(
                InlineKeyboardButton(
                    f"✓ {symbol}" if selected else symbol,
                    callback_data=f"fav|toggle|{symbol}",
                    style="success" if selected else None,
                )
            )
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("Сбросить", callback_data="fav|reset", style="danger"),
            InlineKeyboardButton("← Назад", callback_data="menu|home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def settings_text(favorites: tuple[str, ...], max_favorites: int) -> str:
    return (
        "<b>Настройки</b>\n"
        f"Избранное: <code>{escape(', '.join(favorites))}</code>\n"
        f"Выберите от 1 до {max_favorites} монет. "
        "Для другого тикера: <code>/favorites BTC TON XMR</code>."
    )


def help_text() -> str:
    return (
        "<b>Как пользоваться</b>\n\n"
        "Отправьте тикер или выражение:\n"
        "• <code>BTC</code>\n"
        "• <code>0.5 BTC</code>\n"
        "• <code>(1 BTC + 2 ETH) / 3</code>\n"
        "• <code>раздели 3 на 2</code>\n\n"
        "В группе упомяните <code>@CryptoMathXBot</code> или используйте приватную "
        "команду <code>/price</code>. Обычная переписка не отправляется рыночным API.\n\n"
        "<b>Команды</b>\n"
        "/price — рассчитать выражение\n"
        "/favorites — личные быстрые кнопки (в группе команда приватная)\n"
        "/settings — открыть настройки\n"
        "/ping — проверить доступность"
        "\n\nБиржевые цены в USDT, USDC или FDUSD используются как эквивалент USD "
        "и могут отклоняться при потере привязки stablecoin."
    )


def start_text() -> str:
    return (
        "<b>CryptoMathXBot</b>\n"
        "Криптовалютный калькулятор с актуальными ценами, курсом ЦБ и графиками.\n\n"
        "Нажмите монету ниже или введите, например: <code>0.25 BTC + 2 SOL</code>."
    )


def _format_change(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if value > 0:
        return f"🟢 <b>+{format_decimal(value)}%</b>"
    if value < 0:
        return f"🔴 <b>{format_decimal(value)}%</b>"
    return "⚪ <b>0%</b>"
