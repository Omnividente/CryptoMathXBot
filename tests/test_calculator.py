from decimal import Decimal

import pytest

from cryptomathxbot.calculator import (
    ExpressionError,
    fix_keyboard_layout,
    normalize_math_words,
    parse_expression,
)


def test_plain_arithmetic_obeys_precedence() -> None:
    assert parse_expression("2 + 3 * 4").evaluate({}) == Decimal(14)
    assert parse_expression("(2 + 3) * 4").evaluate({}) == Decimal(20)
    assert parse_expression("раздели 3 на 2").evaluate({}) == Decimal("1.5")


def test_crypto_expression_is_reduced_to_linear_coefficients() -> None:
    parsed = parse_expression("(1 BTC + 2 ETH) / 3")

    assert set(parsed.coefficients) == {"BTC", "ETH"}
    result = parsed.evaluate({"BTC": Decimal(30), "ETH": Decimal(15)})
    assert result == Decimal(20)


def test_repeated_symbols_are_aggregated() -> None:
    parsed = parse_expression("BTC + 2 BTC - 0.5 BTC")

    assert parsed.coefficients == {"BTC": Decimal("2.5")}


def test_common_compact_notation_is_supported() -> None:
    assert parse_expression("2x3").evaluate({}) == Decimal(6)
    assert parse_expression("0.5BTC").coefficients == {"BTC": Decimal("0.5")}
    assert parse_expression("1INCH").coefficients == {"1INCH": Decimal(1)}
    assert parse_expression("BTC-ETH").coefficients == {
        "BTC": Decimal(1),
        "ETH": Decimal(-1),
    }


def test_keyboard_layout_and_words_are_normalized() -> None:
    assert fix_keyboard_layout("иеc") == "btc"
    assert normalize_math_words("умножь 3 на 2") == "3 * 2"


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "(1).__class__",
        "BTC * ETH",
        "1 / BTC",
        "BTC ** 2",
        "2 ** 101",
        "1 / 0",
        "BTC !!!",
    ],
)
def test_unsafe_or_nonlinear_expressions_are_rejected(expression: str) -> None:
    with pytest.raises(ExpressionError):
        parse_expression(expression)


def test_symbol_limit_is_enforced_before_network_work() -> None:
    with pytest.raises(ExpressionError, match="максимум 2"):
        parse_expression("BTC + ETH + XMR", max_symbols=2)
