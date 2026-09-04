from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

from .domain import Chart


class ChartRenderer:
    def __init__(self, dpi: int, concurrency: int = 1) -> None:
        self._dpi = dpi
        self._semaphore = asyncio.Semaphore(concurrency)

    async def render(self, chart: Chart) -> io.BytesIO:
        async with self._semaphore:
            return await asyncio.to_thread(_render_chart, chart, self._dpi)


def _render_chart(chart: Chart, dpi: int) -> io.BytesIO:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.dates import AutoDateLocator, ConciseDateFormatter, date2num
    from matplotlib.figure import Figure
    from matplotlib.ticker import FuncFormatter
    timestamps = [
        date2num(datetime.fromtimestamp(point[0] / 1000, tz=UTC))  # type: ignore[no-untyped-call]
        for point in chart.points
    ]
    prices = [point[1] for point in chart.points]
    change = ((prices[-1] / prices[0]) - 1) * 100 if prices[0] else 0.0
    line_color = "#16A34A" if change >= 0 else "#DC2626"

    figure = Figure(figsize=(7.2, 3.8), dpi=dpi, facecolor="#0E1621")
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    axis.set_facecolor("#0E1621")
    axis.plot(timestamps, prices, color=line_color, linewidth=2.2)
    axis.fill_between(timestamps, prices, min(prices), color=line_color, alpha=0.12)
    axis.scatter(timestamps[-1], prices[-1], color=line_color, s=32, zorder=3)

    title = f"{chart.symbol} · {chart.timeframe} · {change:+.2f}%"
    axis.set_title(title, color="#F4F4F5", fontsize=14, fontweight="bold", loc="left", pad=14)
    axis.text(
        1.0,
        1.04,
        f"{chart.source} · UTC",
        color="#94A3B8",
        fontsize=8,
        ha="right",
        transform=axis.transAxes,
    )
    axis.grid(axis="y", color="#334155", linewidth=0.7, alpha=0.55)
    axis.grid(axis="x", visible=False)
    axis.tick_params(colors="#94A3B8", labelsize=8)
    for spine in axis.spines.values():
        spine.set_visible(False)

    locator = AutoDateLocator(minticks=3, maxticks=6)  # type: ignore[no-untyped-call]
    axis.xaxis.set_major_locator(locator)
    axis.xaxis.set_major_formatter(ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]
    axis.yaxis.set_major_formatter(FuncFormatter(lambda value, _: _format_axis_price(value)))
    axis.margins(x=0.01, y=0.12)
    figure.tight_layout(pad=1.2)

    output = io.BytesIO()
    figure.savefig(
        output,
        format="png",
        facecolor=figure.get_facecolor(),
        bbox_inches="tight",
        pad_inches=0.12,
        metadata={"Software": "CryptoMathXBot"},
    )
    output.seek(0)
    output.name = f"{chart.symbol.lower()}-{chart.timeframe}.png"
    return output


def _format_axis_price(value: float) -> str:
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:.2f}K"
    if absolute >= 1:
        return f"${value:,.2f}"
    return f"${value:.8f}".rstrip("0").rstrip(".")
