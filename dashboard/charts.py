"""Server-rendered inline SVG charts.

No chart library, no build step, no CDN — the page is one HTML document and
the marks inherit the theme through CSS custom properties, so light and dark
are the same markup.

Design rules applied here:

* Every chart is **one series** — a magnitude comparison. Identity comes from
  the row label and a direct value label, never from hue, so the monochrome
  palette costs nothing and colour-vision deficiency is a non-issue.
* Data-ends are rounded, baselines are square, and adjacent marks keep a 2px
  surface gap.
* Grid and axes are recessive; values are labelled directly rather than read
  off a ruler.
* Each mark carries a `<title>`, which is the native hover tooltip.
"""

from __future__ import annotations

from html import escape

from django.utils.safestring import mark_safe

__all__ = ["hbar", "vbar", "empty_chart"]

RADIUS = 4


def _esc(value) -> str:
    return escape(str(value), quote=True)


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,}"


def _rounded_end_h(x: float, y: float, w: float, h: float, r: float) -> str:
    """Horizontal bar: square at the baseline (left), rounded at the data end."""
    r = max(0.0, min(r, h / 2, w))
    if w <= 0:
        return ""
    return (
        f"M{x:.1f},{y:.1f} H{x + w - r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} "
        f"V{y + h - r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + w - r:.1f},{y + h:.1f} "
        f"H{x:.1f} Z"
    )


def _rounded_end_v(x: float, y: float, w: float, h: float, r: float) -> str:
    """Vertical bar: square at the baseline (bottom), rounded at the top."""
    r = max(0.0, min(r, w / 2, h))
    if h <= 0:
        return ""
    return (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} "
        f"A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} "
        f"V{y + h:.1f} Z"
    )


def empty_chart(message: str = "Nothing to plot yet") -> str:
    return mark_safe(f'<div class="empty tiny">{_esc(message)}</div>')


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
    """Horizontal bars, one row per label.

    `rows` is an iterable of `(label, value)` or `(label, value, tooltip)`.
    `dim_predicate(label, value)` may return True to render a row in the
    recessive ink — used for "this is zero / not collected yet" rows so the
    absence reads as absence rather than as a small quantity.
    """
    rows = [tuple(r) if len(r) > 2 else (r[0], r[1], None) for r in (tuple(x) for x in rows)]
    if not rows:
        return empty_chart(empty)

    values = [float(r[1] or 0) for r in rows]
    scale_max = float(max_value) if max_value else max(values + [1.0])
    if scale_max <= 0:
        scale_max = 1.0

    bar_w = max(width - label_width - value_width, 40)
    height = len(rows) * row_height + 6
    gap = 2  # surface gap between adjacent marks

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
        parts.append(
            f'<text class="lbl" x="0" y="{y + bh / 2 + 4:.0f}">{_esc(label)}</text>'
        )
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


def vbar(
    rows,
    *,
    width: int = 640,
    height: int = 148,
    axis_every: int = 0,
    empty: str = "Nothing to plot yet",
):
    """Vertical bars over an ordered axis — counts per day, per month.

    `rows` is an iterable of `(label, value)`.  Labels are thinned rather than
    rotated: an unreadable axis is worse than a sparse one.
    """
    rows = [(r[0], float(r[1] or 0)) for r in (tuple(x) for x in rows)]
    if not rows:
        return empty_chart(empty)

    axis_h = 18
    plot_h = height - axis_h - 14
    scale_max = max([v for _, v in rows] + [1.0])
    slot = width / len(rows)
    bar_w = max(slot - 3, 2)  # the 3 is the inter-mark surface gap
    if not axis_every:
        axis_every = max(1, len(rows) // 8)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'preserveAspectRatio="xMinYMin meet">'
    ]
    baseline = height - axis_h
    parts.append(
        f'<line class="rule" x1="0" y1="{baseline}" x2="{width}" y2="{baseline}"/>'
    )
    for index, (label, value) in enumerate(rows):
        h = (value / scale_max) * plot_h
        x = index * slot + (slot - bar_w) / 2
        y = baseline - h
        parts.append("<g>")
        parts.append(f"<title>{_esc(label)}: {_esc(_fmt(int(value)))}</title>")
        parts.append(
            f'<rect x="{x:.1f}" y="0" width="{bar_w:.1f}" height="{baseline:.0f}" '
            f'fill="transparent"/>'
        )
        if h > 0:
            parts.append(
                f'<path class="bar" d="{_rounded_end_v(x, y, bar_w, h, min(RADIUS, bar_w / 2))}"/>'
            )
        if index % axis_every == 0:
            parts.append(
                f'<text class="axis" x="{index * slot + slot / 2:.1f}" '
                f'y="{height - 4}" text-anchor="middle">{_esc(label)}</text>'
            )
        parts.append("</g>")
    parts.append(
        f'<text class="axis" x="0" y="10">max {_esc(_fmt(int(scale_max)))}</text>'
    )
    parts.append("</svg>")
    return mark_safe("".join(parts))
