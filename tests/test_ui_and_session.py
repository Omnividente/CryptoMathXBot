from datetime import UTC, datetime
from decimal import Decimal

from cryptomathxbot.domain import Calculation, Chart, Coin, Quote
from cryptomathxbot.session import QueryRegistry
from cryptomathxbot.ui import (
    chart_caption,
    format_decimal,
    help_text,
    render_calculation,
    result_keyboard,
    settings_keyboard,
)


def calculation() -> Calculation:
    coin = Coin("bitcoin", "BTC", "Bitcoin", 1)
    quote = Quote(
        coin=coin,
        usd=Decimal("12345.67"),
        change_24h=Decimal("2.5"),
        source="Binance",
        pair="BTCUSDT",
        fetched_at=datetime.now(UTC),
    )
    return Calculation(
        expression="0.5 BTC",
        coefficients={"BTC": Decimal("0.5")},
        constant_usd=Decimal(0),
        quotes={"BTC": quote},
        total_usd=Decimal("6172.835"),
        usd_rub=Decimal("80.5"),
        cbr_date="02.09.2026",
    )


def test_result_is_compact_and_has_modern_actions() -> None:
    value = calculation()
    text = render_calculation(value)
    keyboard = result_keyboard("abc123", value)
    callback_data = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert "Bitcoin · BTC" in text
    assert "6\u202f172" in text
    assert len(text) <= 1024
    assert {
        "q|abc123|refresh",
        "q|abc123|chart|1h",
        "q|abc123|chart|24h",
        "q|abc123|chart|7d",
    } <= set(callback_data)
    assert all(len(value.encode("utf-8")) <= 64 for value in callback_data)


def test_result_keyboard_marks_selected_timeframe_and_caption_explains_change() -> None:
    value = calculation()
    keyboard = result_keyboard("abc123", value, active_timeframe="24h")
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    caption = chart_caption(
        value,
        Chart("BTC", "24h", ((1, 100.0), (2, 110.0)), "Binance"),
    )

    assert "✓ 24 ч" in labels
    assert "1 ч" in labels
    assert "График BTC · 24 ч · изменение +10.00%" in caption


def test_missing_cbr_rate_is_explained_and_chart_caption_fits() -> None:
    value = calculation()
    value = Calculation(
        expression=value.expression,
        coefficients=value.coefficients,
        constant_usd=value.constant_usd,
        quotes=value.quotes,
        total_usd=value.total_usd,
        usd_rub=None,
        cbr_date=None,
    )

    rendered = render_calculation(value)
    caption = chart_caption(
        value,
        Chart("BTC", "24h", ((1, 100.0), (2, 110.0)), "Binance"),
    )

    assert "Курс USD/RUB временно недоступен" in rendered
    assert len(caption) <= 1024



def test_help_discloses_stablecoin_usd_assumption() -> None:
    assert "USDT, USDC или FDUSD" in help_text()
    assert "потере привязки" in help_text()

def test_settings_buttons_make_selected_state_visible() -> None:
    keyboard = settings_keyboard(("BTC", "XMR"))
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "✓ BTC" in labels
    assert "✓ XMR" in labels
    assert "ETH" in labels


def test_decimal_format_avoids_scientific_notation_for_regular_totals() -> None:
    assert format_decimal(Decimal("1000")) == "1\u202f000"


def test_decimal_format_bounds_extreme_values_for_telegram_buttons() -> None:
    rendered = format_decimal(Decimal("9" * 500))

    assert "E+" in rendered
    assert len(f"${rendered}") <= 256


def test_query_buttons_are_bound_to_initiating_user() -> None:
    registry = QueryRegistry(ttl=60)
    session = registry.create(100, "BTC", calculation())

    assert registry.get(session.token, 100) is not None
    assert registry.get(session.token, 200) is None


def test_query_session_keeps_active_chart_across_price_refresh() -> None:
    registry = QueryRegistry(ttl=60)
    session = registry.create(100, "BTC", calculation())
    chart_session = registry.set_active_timeframe(session, "1h")
    refreshed = registry.update(chart_session, calculation())

    assert refreshed.active_timeframe == "1h"


def test_rendered_market_data_stays_within_telegram_message_limit() -> None:
    coefficients: dict[str, Decimal] = {}
    quotes: dict[str, Quote] = {}
    for index in range(8):
        symbol = f"X{index}"
        coin = Coin(f"coin-{index}", symbol, "<unsafe>" + "X" * 72, index + 1)
        coefficients[symbol] = Decimal("9E+100")
        quotes[symbol] = Quote(
            coin=coin,
            usd=Decimal("9E+100"),
            change_24h=Decimal("9E+100"),
            source="CoinGecko",
            pair=None,
            fetched_at=datetime.now(UTC),
        )
    value = Calculation(
        expression="large",
        coefficients=coefficients,
        constant_usd=Decimal(0),
        quotes=quotes,
        total_usd=Decimal("6.48E+202"),
        usd_rub=Decimal("80"),
        cbr_date="03.09.2026",
    )

    rendered = render_calculation(value)

    assert "&lt;unsafe&gt;" in rendered
    assert len(rendered) <= 4096
