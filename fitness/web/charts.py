"""Простые SVG-графики без внешних библиотек. Цвета задаются CSS-переменными страницы."""

from datetime import date
from html import escape

WIDTH, HEIGHT = 720, 240
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 52, 16, 16, 30


def _fmt(value):
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _scale(values):
    low, high = min(values), max(values)
    if low == high:
        low, high = low - 1, high + 1
    margin = (high - low) * 0.08
    return low - margin, high + margin


def _frame(title, low, high, first_label, last_label, body):
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    grid = []
    for i in range(5):
        y = PAD_TOP + plot_h * i / 4
        value = high - (high - low) * i / 4
        grid.append(f'<line class="grid" x1="{PAD_LEFT}" y1="{y:.1f}" x2="{WIDTH - PAD_RIGHT}" y2="{y:.1f}"/>'
                    f'<text class="axis" x="{PAD_LEFT - 6}" y="{y + 4:.1f}" text-anchor="end">{_fmt(value)}</text>')
    return (
        f'<svg class="chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(title)}">'
        f'<title>{escape(title)}</title>{"".join(grid)}{body}'
        f'<text class="axis" x="{PAD_LEFT}" y="{HEIGHT - 8}">{escape(first_label)}</text>'
        f'<text class="axis" x="{WIDTH - PAD_RIGHT}" y="{HEIGHT - 8}" text-anchor="end">{escape(last_label)}</text>'
        "</svg>"
    )


def _x_positions(dates):
    """X по реальным датам, чтобы пропуски во времени были видны."""
    ordinals = [date.fromisoformat(d).toordinal() for d in dates]
    first, last = ordinals[0], ordinals[-1]
    span = max(last - first, 1)
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    if last == first:
        return [PAD_LEFT + plot_w / 2 for _ in ordinals]
    return [PAD_LEFT + plot_w * (o - first) / span for o in ordinals]


def line_chart(points, title, unit):
    """points: [(дата 'YYYY-MM-DD', число)] по возрастанию даты."""
    points = [(d, v) for d, v in points if v is not None]
    if not points:
        return ""
    dates = [d for d, _ in points]
    values = [v for _, v in points]
    low, high = _scale(values)
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    xs = _x_positions(dates)
    ys = [PAD_TOP + plot_h * (high - v) / (high - low) for v in values]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    dots = "".join(
        f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="3.5"><title>{escape(d)}: {_fmt(v)} {escape(unit)}</title></circle>'
        for x, y, d, v in zip(xs, ys, dates, values)
    )
    body = f'<polyline class="line" points="{path}"/>{dots}'
    return _frame(title, low, high, dates[0], dates[-1], body)


def bar_chart(points, title, unit):
    """points: [(дата, число или None)] — столбик на каждую дату."""
    points = [(d, v) for d, v in points if v is not None]
    if not points:
        return ""
    values = [v for _, v in points]
    low, high = 0, max(values) * 1.08 or 1
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    step = plot_w / len(points)
    bar_w = max(min(step * 0.7, 40), 2)
    bars = []
    for i, (d, v) in enumerate(points):
        h = plot_h * v / high
        x = PAD_LEFT + step * i + (step - bar_w) / 2
        bars.append(f'<rect class="bar" x="{x:.1f}" y="{PAD_TOP + plot_h - h:.1f}" width="{bar_w:.1f}" '
                    f'height="{h:.1f}"><title>{escape(d)}: {_fmt(v)} {escape(unit)}</title></rect>')
    return _frame(title, low, high, points[0][0], points[-1][0], "".join(bars))


def range_chart(points, title, unit):
    """points: [(дата, минимум, среднее, максимум)] — вертикальная линия минимум–максимум и линия среднего."""
    points = [p for p in points if None not in p]
    if not points:
        return ""
    low, high = _scale([v for _, lo, _, hi in points for v in (lo, hi)])
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    xs = _x_positions([d for d, *_ in points])

    def y(v):
        return PAD_TOP + plot_h * (high - v) / (high - low)

    ranges = "".join(
        f'<line class="range" x1="{x:.1f}" y1="{y(hi):.1f}" x2="{x:.1f}" y2="{y(lo):.1f}">'
        f'<title>{escape(d)}: {_fmt(lo)}–{_fmt(hi)} {escape(unit)}</title></line>'
        for x, (d, lo, _, hi) in zip(xs, points))
    path = " ".join(f"{x:.1f},{y(avg):.1f}" for x, (_, _, avg, _) in zip(xs, points))
    dots = "".join(
        f'<circle class="dot" cx="{x:.1f}" cy="{y(avg):.1f}" r="3.5"><title>{escape(d)}: среднее {_fmt(avg)} '
        f'{escape(unit)}, мин. {_fmt(lo)}, макс. {_fmt(hi)}</title></circle>'
        for x, (d, lo, avg, hi) in zip(xs, points))
    body = f'{ranges}<polyline class="line" points="{path}"/>{dots}'
    return _frame(title, low, high, points[0][0], points[-1][0], body)
