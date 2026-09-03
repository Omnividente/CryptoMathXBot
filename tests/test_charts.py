from cryptomathxbot.charts import ChartRenderer, _format_axis_price
from cryptomathxbot.domain import Chart


async def test_chart_renderer_outputs_named_png() -> None:
    chart = Chart(
        symbol="BTC",
        timeframe="24h",
        points=((1_700_000_000_000, 100.0), (1_700_003_600_000, 110.0)),
        source="test",
    )

    image = await ChartRenderer(dpi=72, concurrency=1).render(chart)

    assert image.name == "btc-24h.png"
    assert image.read(8) == b"\x89PNG\r\n\x1a\n"


def test_chart_axis_price_uses_readable_units() -> None:
    assert _format_axis_price(1_500_000_000) == "$1.50B"
    assert _format_axis_price(1_500_000) == "$1.50M"
    assert _format_axis_price(1_500) == "$1.50K"
    assert _format_axis_price(15) == "$15.00"
    assert _format_axis_price(0.00125) == "$0.00125"
