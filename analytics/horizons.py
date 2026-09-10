"""The horizon ladder — planning.md §3.2 and §6.1.

Pure Python, no Django imports (§8): `analytics/` must stay importable from a
notebook.

Every horizon is a fixed offset from `t0` in seconds.  `+1d` is `t0 + 24h`,
**not** "the next daily bar" — daily bars inherit the 17:00 New York rollover
ambiguity of §4.1 and a fixed offset does not.
"""

from __future__ import annotations

from dataclasses import dataclass

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WEEK = 7 * DAY
MONTH = 30 * DAY  # calendar-month approximation, stated rather than implied


@dataclass(frozen=True)
class Horizon:
    label: str
    seconds: int

    @property
    def is_pre(self) -> bool:
        return self.seconds < 0

    def __str__(self) -> str:
        return self.label


PRE_HORIZONS: tuple[Horizon, ...] = (
    Horizon("-5d", -5 * DAY),
    Horizon("-1d", -1 * DAY),
    Horizon("-4h", -4 * HOUR),
    Horizon("-1h", -1 * HOUR),
    Horizon("-15m", -15 * MINUTE),
    Horizon("-5m", -5 * MINUTE),
)

POST_HORIZONS: tuple[Horizon, ...] = (
    Horizon("+1m", 1 * MINUTE),
    Horizon("+5m", 5 * MINUTE),
    Horizon("+15m", 15 * MINUTE),
    Horizon("+30m", 30 * MINUTE),
    Horizon("+1h", 1 * HOUR),
    Horizon("+4h", 4 * HOUR),
    Horizon("+1d", 1 * DAY),
    Horizon("+3d", 3 * DAY),
    Horizon("+1w", 1 * WEEK),
    Horizon("+2w", 2 * WEEK),
    Horizon("+1M", 1 * MONTH),
)

LADDER: tuple[Horizon, ...] = PRE_HORIZONS + POST_HORIZONS

BY_LABEL: dict[str, Horizon] = {h.label: h for h in LADDER}


def seconds_for(label: str) -> int:
    """Signed offset in seconds for a ladder label."""
    return BY_LABEL[label].seconds


class WindowScheme:
    """§3.2b.  Variable-length windows are selectable per study; every result
    from one is normalised by sqrt(window length) and reports that length,
    or long windows look more impactful purely for being longer."""

    FIXED = "fixed"
    UNTIL_NEXT_OWN = "until_next_own"
    UNTIL_NEXT_EITHER = "until_next_either"
    UNTIL_NEXT_ANY = "until_next_any"

    CHOICES = (
        (FIXED, "fixed ladder (§3.2) — comparability across events; the default"),
        (UNTIL_NEXT_OWN, "until the same currency's next tier-1 event"),
        (UNTIL_NEXT_EITHER, "until either currency's next tier-1 event"),
        (UNTIL_NEXT_ANY, "until the next event of any importance"),
    )
