"""Server-rendered inline SVG charts.

No chart library, no build step, no CDN — the page is one HTML document and
the marks inherit the theme through CSS custom properties, so light and dark
are the same markup.

Design rules applied here:

* Bar charts are **one series**: identity comes from the row label and a
  direct value label, never from hue, so the monochrome palette costs nothing.
* The line chart carries at most two series — an actual and its forecast — and
  distinguishes them by weight and dash, not colour, so it survives both
  colour-vision deficiency and a black-and-white print.
* The surprise chart encodes polarity by **position about a zero baseline**.
  Position is the encoding; no diverging palette is needed, which is the only
  honest way to show polarity in monochrome.
* Data-ends are rounded, baselines square, adjacent marks keep a 2px gap.
* Grid and axes are recessive; values are labelled directly.
* Every mark carries a `<title>`, which is the native hover tooltip — no
  JavaScript, so a chart with 400 points still renders instantly.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from django.utils.safestring import mark_safe

__all__ = ["hbar", "vbar", "line", "surprise", "sparkline", "empty_chart", "compact"]

RADIUS = 4
PREVIEW_ROWS = 25


def _esc(value) -> str:
    return escape(str(value), quote=True)


def compact(value) -> str:
    """142000 -> 142K. Macro series span nine orders of magnitude; axis labels
    that spell every zero are unreadable."""
    if value is None:
        return "—"
    number = float(value)
    magnitude = abs(number)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if magnitude >= limit:
            scaled = number / limit
            text = f"{scaled:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    if magnitude >= 100:
        return f"{number:,.0f}"
    text = f"{number:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,}"


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round tick values covering [low, high]."""
    if high == low:
        pad = abs(high) * 0.1 or 1.0
        low, high = low - pad, high + pad
    span = high - low
    raw = span / max(count, 1)
    magnitude = 10 ** _floor_log10(raw)
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if step >= raw:
            break
    start = step * int(low / step)
    if start > low:
        start -= step
    ticks = []
    value = start
    while value <= high + step * 0.5:
        ticks.append(value)
        value += step
    return ticks


def _floor_log10(value: float) -> int:
    import math

    if value <= 0:
        return 0
    return int(math.floor(math.log10(value)))


def _rounded_end_h(x: float, y: float, w: float, h: float, r: float) -> str:
    r = max(0.0, min(r, h / 2, w))
    if w <= 0:
        return ""
    return (
        f"M{x:.1f},{y:.1f} H{x + w - r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} "
        f"V{y + h - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + w - r:.1f},{y + h:.1f} "
        f"H{x:.1f} Z"
    )


def _rounded_end_v(x: float, y: float, w: float, h: float, r: float) -> str:
    r = max(0.0, min(r, w / 2, h))
    if h <= 0:
        return ""
    return (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} "
        f"V{y + h:.1f} Z"
    )


def empty_chart(message: str = "Nothing to plot yet") -> str:
    return mark_safe(f'<div class="empty tiny">{_esc(message)}</div>')


# ----------------------------------------------------------------- bars ----


def hbar(
    rows,
    *,
    width: int = 560,
    row_height: int = 26,
    label_width: int = 168,
    value_width: int = 56,
    max_value: float | None = None,
    dim_predicate=None,
    empty: str = "Nothing to plot yet",
):
    rows = [tuple(r) if len(r) > 2 else (r[0], r[1], None) for r in (tuple(x) for x in rows)]
    if not rows:
        return empty_chart(empty)

    values = [float(r[1] or 0) for r in rows]
    scale_max = float(max_value) if max_value else max(values + [1.0])
    if scale_max <= 0:
        scale_max = 1.0

    bar_w = max(width - label_width - value_width, 40)
    height = len(rows) * row_height + 6
    gap = 2

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="xMinYMin meet">'
    ]
    for index, (label, value, tooltip) in enumerate(rows):
        numeric = float(value or 0)
        y = index * row_height + 3
        bh = row_height - gap * 2
        w = (numeric / scale_max) * bar_w
        dim = dim_predicate(label, numeric) if dim_predicate else numeric == 0
        cls = "bar-dim" if dim else "bar"
        title = tooltip if tooltip is not None else f"{label}: {_fmt(value)}"

        parts.append("<g>")
        parts.append(f"<title>{_esc(title)}</title>")
        parts.append(f'<text class="lbl" x="0" y="{y + bh / 2 + 4:.0f}">{_esc(label)}</text>')
        parts.append(
            f'<rect class="track" x="{label_width}" y="{y:.0f}" width="{bar_w}" '
            f'height="{bh:.0f}" rx="3"/>'
        )
        if w > 0:
            parts.append(
                f'<path class="{cls}" d="{_rounded_end_h(label_width, y, w, bh, RADIUS)}"/>'
            )
        parts.append(
            f'<text class="val" x="{width}" y="{y + bh / 2 + 4:.0f}" '
            f'text-anchor="end">{_esc(_fmt(value))}</text>'
        )
        parts.append("</g>")
    parts.append("</svg>")
    return mark_safe("".join(parts))


def vbar(rows, *, width: int = 640, height: int = 148, axis_every: int = 0,
         empty: str = "Nothing to plot yet"):
    rows = [(r[0], float(r[1] or 0)) for r in (tuple(x) for x in rows)]
    if not rows:
        return empty_chart(empty)

    axis_h = 18
    plot_h = height - axis_h - 14
    scale_max = max([v for _, v in rows] + [1.0])
    slot = width / len(rows)
    bar_w = max(slot - 3, 2)
    if not axis_every:
        axis_every = max(1, len(rows) // 8)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="xMinYMin meet">'
    ]
    baseline = height - axis_h
    parts.append(f'<line class="rule" x1="0" y1="{baseline}" x2="{width}" y2="{baseline}"/>')
    for index, (label, value) in enumerate(rows):
        h = (value / scale_max) * plot_h
        x = index * slot + (slot - bar_w) / 2
        y = baseline - h
        parts.append("<g>")
        parts.append(f"<title>{_esc(label)}: {_esc(_fmt(int(value)))}</title>")
        parts.append(
            f'<rect x="{x:.1f}" y="0" width="{bar_w:.1f}" height="{baseline:.0f}" fill="transparent"/>'
        )
        if h > 0:
            parts.append(
                f'<path class="bar" d="{_rounded_end_v(x, y, bar_w, h, min(RADIUS, bar_w / 2))}"/>'
            )
        if index % axis_every == 0:
            parts.append(
                f'<text class="axis" x="{index * slot + slot / 2:.1f}" y="{height - 4}" '
                f'text-anchor="middle">{_esc(label)}</text>'
            )
        parts.append("</g>")
    parts.append(f'<text class="axis" x="0" y="10">max {_esc(_fmt(int(scale_max)))}</text>')
    parts.append("</svg>")
    return mark_safe("".join(parts))


# ----------------------------------------------------------- time series ----


def _x_ticks(stamps: list[datetime], slots: int = 6) -> list[tuple[int, str]]:
    """Year labels at evenly spaced indices, deduplicated."""
    if not stamps:
        return []
    count = min(slots, len(stamps))
    step = max(len(stamps) // count, 1)
    ticks: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index in range(0, len(stamps), step):
        label = f"{stamps[index]:%Y}"
        if label not in seen:
            ticks.append((index, label))
            seen.add(label)
    return ticks


def line(
    points,
    *,
    secondary=None,
    width: int = 760,
    height: int = 300,
    label: str = "actual",
    secondary_label: str = "forecast",
    empty: str = "No values to plot.",
):
    """Value against time.

    `points` is [(datetime, value)]; `secondary` the same, drawn thinner and
    dashed. Two series are distinguished by weight and dash rather than hue —
    the legend names them and the marks differ without colour.
    """
    points = [(w, float(v)) for w, v in points if v is not None]
    if not points:
        return empty_chart(empty)

    secondary = [(w, float(v)) for w, v in (secondary or []) if v is not None]

    pad_left, pad_right, pad_top, pad_bottom = 62, 14, 18, 26
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    values = [v for _, v in points] + [v for _, v in secondary]
    ticks = _nice_ticks(min(values), max(values))
    low, high = min(ticks + values), max(ticks + values)
    span = (high - low) or 1.0

    stamps = [w for w, _ in points]
    index_of = {w: i for i, w in enumerate(stamps)}
    last = max(len(points) - 1, 1)

    def x_at(i: int) -> float:
        return pad_left + (i / last) * plot_w

    def y_at(value: float) -> float:
        return pad_top + plot_h - ((value - low) / span) * plot_h

    parts = [
        f'<svg class="chart chart--line" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="xMinYMin meet">'
    ]

    for tick in ticks:
        y = y_at(tick)
        if not (pad_top - 1 <= y <= pad_top + plot_h + 1):
            continue
        parts.append(
            f'<line class="grid" x1="{pad_left}" y1="{y:.1f}" x2="{width - pad_right}" y2="{y:.1f}"/>'
        )
        parts.append(
            f'<text class="axis" x="{pad_left - 8}" y="{y + 3.5:.1f}" text-anchor="end">'
            f"{_esc(compact(tick))}</text>"
        )

    zero_y = y_at(0.0)
    if low < 0 < high:
        parts.append(
            f'<line class="zero" x1="{pad_left}" y1="{zero_y:.1f}" '
            f'x2="{width - pad_right}" y2="{zero_y:.1f}"/>'
        )

    if secondary:
        path = " ".join(
            f"{'M' if n == 0 else 'L'}{x_at(index_of.get(w, 0)):.1f},{y_at(v):.1f}"
            for n, (w, v) in enumerate(secondary)
            if w in index_of
        )
        if path:
            parts.append(f'<path class="series series--secondary" d="{path}"/>')

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x_at(i):.1f},{y_at(v):.1f}"
        for i, (_, v) in enumerate(points)
    )
    parts.append(f'<path class="series" d="{path}"/>')

    # Invisible hit bands give every point a tooltip without drawing 400 dots.
    band = max(plot_w / max(len(points), 1), 1.0)
    for i, (when, value) in enumerate(points):
        parts.append(
            f'<rect x="{x_at(i) - band / 2:.1f}" y="{pad_top}" width="{band:.2f}" '
            f'height="{plot_h}" fill="transparent">'
            f"<title>{_esc(f'{when:%Y-%m-%d %H:%M}Z')}  {_esc(label)} "
            f"{_esc(compact(value))}</title></rect>"
        )

    extremes = {
        points.index(max(points, key=lambda p: p[1])),
        points.index(min(points, key=lambda p: p[1])),
        len(points) - 1,
    }
    for i in sorted(extremes):
        when, value = points[i]
        parts.append(f'<circle class="dot" cx="{x_at(i):.1f}" cy="{y_at(value):.1f}" r="3"/>')
        anchor = "end" if i == len(points) - 1 else "middle"
        parts.append(
            f'<text class="val" x="{x_at(i):.1f}" y="{y_at(value) - 8:.1f}" '
            f'text-anchor="{anchor}">{_esc(compact(value))}</text>'
        )

    baseline = pad_top + plot_h
    parts.append(
        f'<line class="rule" x1="{pad_left}" y1="{baseline}" x2="{width - pad_right}" y2="{baseline}"/>'
    )
    for index, text in _x_ticks(stamps):
        parts.append(
            f'<text class="axis" x="{x_at(index):.1f}" y="{height - 8}" '
            f'text-anchor="middle">{_esc(text)}</text>'
        )
    parts.append("</svg>")
    return mark_safe("".join(parts))


def surprise(points, *, width: int = 760, height: int = 190,
             empty: str = "No forecast to compare against."):
    """Actual minus forecast, as bars about a zero line.

    Polarity is encoded by position, not by a diverging palette — which is the
    only honest way to show sign in monochrome, and it survives printing.
    """
    points = [(w, float(v)) for w, v in points if v is not None]
    if not points:
        return empty_chart(empty)

    pad_left, pad_right, pad_top, pad_bottom = 62, 14, 14, 24
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    values = [v for _, v in points]
    extent = max(abs(min(values)), abs(max(values))) or 1.0
    zero_y = pad_top + plot_h / 2
    slot = plot_w / max(len(points), 1)
    bar_w = max(slot - 2, 1.0)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="xMinYMin meet">'
    ]
    for tick in (extent, -extent):
        y = zero_y - (tick / extent) * (plot_h / 2)
        parts.append(
            f'<text class="axis" x="{pad_left - 8}" y="{y + 3.5:.1f}" text-anchor="end">'
            f"{_esc(compact(tick))}</text>"
        )

    for index, (when, value) in enumerate(points):
        h = abs(value) / extent * (plot_h / 2)
        x = pad_left + index * slot + (slot - bar_w) / 2
        y = zero_y - h if value >= 0 else zero_y
        cls = "bar" if value >= 0 else "bar-dim"
        parts.append("<g>")
        parts.append(
            f"<title>{_esc(f'{when:%Y-%m-%d}')}  "
            f"{'beat' if value >= 0 else 'missed'} by {_esc(compact(abs(value)))}</title>"
        )
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.2f}" height="{h:.1f}" class="{cls}"/>')
        parts.append("</g>")

    parts.append(
        f'<line class="zero" x1="{pad_left}" y1="{zero_y:.1f}" x2="{width - pad_right}" y2="{zero_y:.1f}"/>'
    )
    parts.append(
        f'<text class="axis" x="{pad_left}" y="{height - 6}">'
        f"{_esc(f'{points[0][0]:%Y}')}</text>"
    )
    parts.append(
        f'<text class="axis" x="{width - pad_right}" y="{height - 6}" text-anchor="end">'
        f"{_esc(f'{points[-1][0]:%Y}')}</text>"
    )
    parts.append("</svg>")
    return mark_safe("".join(parts))


def sparkline(values, *, width: int = 104, height: int = 26):
    """Shape only — no axes, no labels. Used inside table rows."""
    values = [float(v) for v in values if v is not None]
    if len(values) < 2:
        return mark_safe('<span class="faint tiny">—</span>')

    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{i * step:.1f},"
        f"{height - 2 - ((v - low) / span) * (height - 4):.1f}"
        for i, v in enumerate(values)
    )
    last_y = height - 2 - ((values[-1] - low) / span) * (height - 4)
    return mark_safe(
        f'<svg class="chart spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img">'
        f'<title>{len(values)} recent values, {_esc(compact(low))} to {_esc(compact(high))}</title>'
        f'<path class="series" d="{path}"/>'
        f'<circle class="dot" cx="{width:.1f}" cy="{last_y:.1f}" r="2"/>'
        f"</svg>"
    )


def preview_columns_rows(preview: dict) -> tuple[list, list]:
    return preview.get("columns") or [], preview.get("rows") or []
