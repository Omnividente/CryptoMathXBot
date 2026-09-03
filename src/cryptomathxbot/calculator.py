from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal


class ExpressionError(ValueError):
    """A user-facing expression validation error."""


@dataclass(frozen=True, slots=True)
class Token:
    kind: Literal["number", "symbol", "operator"]
    value: str


@dataclass(frozen=True, slots=True)
class ParsedExpression:
    source: str
    python_expression: str
    variable_symbols: dict[str, str]
    coefficients: dict[str, Decimal]
    constant: Decimal

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.coefficients)

    def evaluate(self, prices: dict[str, Decimal]) -> Decimal:
        missing = self.coefficients.keys() - prices.keys()
        if missing:
            raise ExpressionError(f"Нет цены для: {', '.join(sorted(missing))}")
        variables = {
            variable: prices[symbol]
            for variable, symbol in self.variable_symbols.items()
            if symbol in prices
        }
        root = ast.parse(self.python_expression, mode="eval")
        with localcontext() as context:
            context.prec = 50
            total = _evaluate_numeric(root.body, self.python_expression, variables)
        return _bounded_decimal(total)


_RU_TO_EN = str.maketrans(
    {
        "й": "q",
        "ц": "w",
        "у": "e",
        "к": "r",
        "е": "t",
        "н": "y",
        "г": "u",
        "ш": "i",
        "щ": "o",
        "з": "p",
        "ф": "a",
        "ы": "s",
        "в": "d",
        "а": "f",
        "п": "g",
        "р": "h",
        "о": "j",
        "л": "k",
        "д": "l",
        "я": "z",
        "ч": "x",
        "с": "c",
        "м": "v",
        "и": "b",
        "т": "n",
        "ь": "m",
    }
)
_WORD = r"[A-Za-zА-Яа-яЁё0-9.,]+"
_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<symbol>\$?(?=[A-Za-zА-Яа-яЁё0-9]{1,11})(?=[A-Za-zА-Яа-яЁё0-9]*[A-Za-zА-Яа-яЁё])[A-Za-zА-Яа-яЁё0-9]{1,11})"
    r"|(?P<number>\d+(?:[.,]\d+)?)"
    r"|(?P<operator>\*\*|[()+\-*/])"
    r")"
)
_ALLOWED_BINARY = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_MAX_INPUT = 500
_MAX_TOKENS = 160
_MAX_NODES = 300


def fix_keyboard_layout(value: str) -> str:
    translated = value.translate(_RU_TO_EN)
    return translated if translated != value else value


def normalize_math_words(text: str) -> str:
    value = text.strip()
    substitutions = [
        (
            rf"(?i)\b(?:раздели(?:ть)?|подел(?:и|ить)|divide)\s+({_WORD})\s+(?:на|by)\s+({_WORD})\b",
            r"\1 / \2",
        ),
        (rf"(?i)\b(?:умножь|умножить|multiply)\s+({_WORD})\s+(?:на|by)\s+({_WORD})\b", r"\1 * \2"),
        (
            rf"(?i)\b(?:прибавь|прибавить|добавь|добавить|add)\s+({_WORD})\s+(?:к|to)\s+({_WORD})\b",
            r"\2 + \1",
        ),
        (rf"(?i)\b(?:вычти|вычесть|subtract)\s+({_WORD})\s+(?:из|from)\s+({_WORD})\b", r"\2 - \1"),
        (rf"(?i)\b({_WORD})\s+(?:плюс|plus)\s+({_WORD})\b", r"\1 + \2"),
        (rf"(?i)\b({_WORD})\s+(?:минус|minus)\s+({_WORD})\b", r"\1 - \2"),
        (rf"(?i)\b({_WORD})\s+(?:times|умножить\s+на|multiplied\s+by)\s+({_WORD})\b", r"\1 * \2"),
        (rf"(?i)\b({_WORD})\s+(?:divided\s+by|разделить\s+на|over)\s+({_WORD})\b", r"\1 / \2"),
        (rf"(?i)\b({_WORD})\s+(?:в\s+квадрат|squared)\b", r"\1 ** 2"),
        (rf"(?i)\b({_WORD})\s+(?:в\s+куб|cubed)\b", r"\1 ** 3"),
    ]
    for pattern, replacement in substitutions:
        value = re.sub(pattern, replacement, value)
    value = value.replace("×", "*").replace("÷", "/").replace("−", "-")
    value = re.sub(r"(?i)(?<=\d)\s*x\s*(?=\d)", " * ", value)
    return re.sub(r"\s+", " ", value).strip()


def tokenize(text: str) -> tuple[Token, ...]:
    normalized = normalize_math_words(text)
    if not normalized:
        raise ExpressionError("Введите выражение")
    if len(normalized) > _MAX_INPUT:
        raise ExpressionError(f"Выражение длиннее {_MAX_INPUT} символов")

    tokens: list[Token] = []
    position = 0
    while position < len(normalized):
        match = _TOKEN_RE.match(normalized, position)
        if match is None:
            if normalized[position:].strip() == "":
                break
            fragment = normalized[position : position + 12]
            raise ExpressionError(f"Недопустимый фрагмент: {fragment!r}")
        if match.end() == position:
            raise ExpressionError("Не удалось разобрать выражение")
        if match.lastgroup == "number":
            tokens.append(Token("number", match.group("number").replace(",", ".")))
        elif match.lastgroup == "symbol":
            raw = match.group("symbol").lstrip("$")
            symbol = fix_keyboard_layout(raw).upper()
            tokens.append(Token("symbol", symbol))
        else:
            tokens.append(Token("operator", match.group("operator")))
        position = match.end()
        if len(tokens) > _MAX_TOKENS:
            raise ExpressionError("Слишком много элементов в выражении")
    if not tokens:
        raise ExpressionError("Введите выражение")
    return tuple(tokens)


def parse_expression(text: str, *, max_symbols: int = 8) -> ParsedExpression:
    tokens = tokenize(text)
    variable_for_symbol: dict[str, str] = {}
    variable_symbols: dict[str, str] = {}
    output: list[str] = []
    previous: Token | None = None

    for token in tokens:
        begins_atom = token.kind in {"number", "symbol"} or token.value == "("
        ends_previous_atom = previous is not None and (
            previous.kind in {"number", "symbol"} or previous.value == ")"
        )
        if begins_atom and ends_previous_atom:
            output.append("*")

        if token.kind == "symbol":
            variable = variable_for_symbol.get(token.value)
            if variable is None:
                if len(variable_for_symbol) >= max_symbols:
                    raise ExpressionError(f"В одном запросе максимум {max_symbols} монет")
                variable = f"S{len(variable_for_symbol)}"
                variable_for_symbol[token.value] = variable
                variable_symbols[variable] = token.value
            output.append(variable)
        else:
            output.append(token.value)
        previous = token

    python_expression = " ".join(output)
    try:
        root = ast.parse(python_expression, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError("Проверьте скобки и операторы") from exc
    if sum(1 for _ in ast.walk(root)) > _MAX_NODES:
        raise ExpressionError("Выражение слишком сложное")

    with localcontext() as context:
        context.prec = 50
        constant, variable_coefficients = _as_affine(root.body, python_expression)
    coefficients = {
        variable_symbols[variable]: coefficient
        for variable, coefficient in variable_coefficients.items()
        if coefficient != 0
    }
    if not coefficients and variable_for_symbol:
        raise ExpressionError("Монеты сократились до нулевого количества")
    return ParsedExpression(
        source=normalize_math_words(text),
        python_expression=python_expression,
        variable_symbols=variable_symbols,
        coefficients=coefficients,
        constant=constant,
    )


def _as_affine(node: ast.AST, expression: str) -> tuple[Decimal, dict[str, Decimal]]:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        source = ast.get_source_segment(expression, node)
        try:
            value = Decimal(source if source is not None else str(node.value))
        except InvalidOperation as exc:
            raise ExpressionError("Некорректное число") from exc
        return value, {}

    if isinstance(node, ast.Name) and re.fullmatch(r"S\d+", node.id):
        return Decimal(0), {node.id: Decimal(1)}

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        constant, coefficients = _as_affine(node.operand, expression)
        factor = Decimal(-1) if isinstance(node.op, ast.USub) else Decimal(1)
        return constant * factor, _scale(coefficients, factor)

    if not isinstance(node, ast.BinOp) or not isinstance(node.op, _ALLOWED_BINARY):
        raise ExpressionError("Поддерживаются только числа, тикеры и + − × ÷ степени")

    left_constant, left_coefficients = _as_affine(node.left, expression)
    right_constant, right_coefficients = _as_affine(node.right, expression)

    if isinstance(node.op, (ast.Add, ast.Sub)):
        factor = Decimal(-1) if isinstance(node.op, ast.Sub) else Decimal(1)
        return (
            left_constant + factor * right_constant,
            _merge(left_coefficients, _scale(right_coefficients, factor)),
        )

    if isinstance(node.op, ast.Mult):
        if left_coefficients and right_coefficients:
            raise ExpressionError("Умножать монету на монету нельзя")
        if left_coefficients:
            return left_constant * right_constant, _scale(left_coefficients, right_constant)
        if right_coefficients:
            return left_constant * right_constant, _scale(right_coefficients, left_constant)
        return _bounded_decimal(left_constant * right_constant), {}

    if isinstance(node.op, ast.Div):
        if right_coefficients:
            raise ExpressionError("Делить на монету нельзя")
        if right_constant == 0:
            raise ExpressionError("Деление на ноль")
        return (
            _bounded_decimal(left_constant / right_constant),
            _scale(left_coefficients, Decimal(1) / right_constant),
        )

    if left_coefficients or right_coefficients:
        raise ExpressionError("Возводить выражение с монетой в степень нельзя")
    if right_constant != right_constant.to_integral_value() or abs(right_constant) > 100:
        raise ExpressionError("Степень должна быть целой от −100 до 100")
    try:
        result = left_constant ** int(right_constant)
    except (InvalidOperation, OverflowError, ZeroDivisionError) as exc:
        raise ExpressionError("Не удалось вычислить степень") from exc
    return _bounded_decimal(result), {}


def _evaluate_numeric(
    node: ast.AST,
    expression: str,
    variables: dict[str, Decimal],
) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        source = ast.get_source_segment(expression, node)
        try:
            return Decimal(source if source is not None else str(node.value))
        except InvalidOperation as exc:
            raise ExpressionError("Некорректное число") from exc
    if isinstance(node, ast.Name) and node.id in variables:
        return variables[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate_numeric(node.operand, expression, variables)
        return -value if isinstance(node.op, ast.USub) else value
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, _ALLOWED_BINARY):
        raise ExpressionError("Недопустимая операция")
    left = _evaluate_numeric(node.left, expression, variables)
    right = _evaluate_numeric(node.right, expression, variables)
    if isinstance(node.op, ast.Add):
        return _bounded_decimal(left + right)
    if isinstance(node.op, ast.Sub):
        return _bounded_decimal(left - right)
    if isinstance(node.op, ast.Mult):
        return _bounded_decimal(left * right)
    if isinstance(node.op, ast.Div):
        if right == 0:
            raise ExpressionError("Деление на ноль")
        return _bounded_decimal(left / right)
    if right != right.to_integral_value() or abs(right) > 100:
        raise ExpressionError("Степень должна быть целой от −100 до 100")
    try:
        return _bounded_decimal(left ** int(right))
    except (InvalidOperation, OverflowError, ZeroDivisionError) as exc:
        raise ExpressionError("Не удалось вычислить степень") from exc


def _scale(values: dict[str, Decimal], factor: Decimal) -> dict[str, Decimal]:
    return {key: value * factor for key, value in values.items()}


def _merge(left: dict[str, Decimal], right: dict[str, Decimal]) -> dict[str, Decimal]:
    merged = dict(left)
    for key, value in right.items():
        merged[key] = merged.get(key, Decimal(0)) + value
    return merged


def _bounded_decimal(value: Decimal) -> Decimal:
    if not value.is_finite() or abs(value) > Decimal("1e100"):
        raise ExpressionError("Результат слишком велик")
    return value


def looks_like_private_query(text: str) -> bool:
    value = text.strip()
    if not value or len(value) > _MAX_INPUT:
        return False
    return bool(
        re.search(r"[0-9$()+*/×÷]", value)
        or re.fullmatch(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9]{1,10}", value)
        or re.search(r"(?i)\b(плюс|минус|раздел|умнож|plus|minus|divide|multiply)\b", value)
    )
