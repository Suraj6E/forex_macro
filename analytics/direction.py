"""Signed response to the change in an indicator — the third mode.

planning.md draws two modes: **A**, which needs only timestamps and price and
measures how much *further* price moved than usual, and **B**, which needs a
consensus forecast and measures the response to the *surprise*.  Mode A cannot
answer "what does rising inflation do to the currency", because the mean signed
move of a symmetric event is ~0 by construction.  Mode B can, and is blocked:
no forecast in the dataset is point-in-time (§4.3).

There is a third quantity sitting in data already held.  `actual` and
`previous` are present on 99.9% of timestamped releases, so the *change in the
indicator* is computable for the whole history without any forecast at all:

    change   = actual - previous
    change_z = change / rolling_std(change, last 20 releases)

and then, per horizon,

    abnormal_ret = alpha + beta * change_z + epsilon

`beta` is the answer to the question as asked: a positive beta on CPI against
EURUSD means euro-area inflation coming in above last month's print is followed
by a stronger euro.

**This is not Mode B, and conflating the two would be a real error.** It
measures the response to *higher than last time*, not to *higher than
expected*.  Some of any change was anticipated and already priced, which
attenuates beta toward zero; and where the market expected a rise and got a
smaller one, the change is positive while the surprise is negative, so the two
can carry opposite signs on the same release.  It is a different quantity, it
gets its own mode, and it never merges with a Mode B column — the same rule
§6.7 applies to modelled expectations.

Standardising by the indicator's own rolling sigma is what makes different
indicators commensurate (§3.5, and Balduzzi, Elton & Green 2001 for the
surprise analogue).  Trailing rather than full-sample, so the scaling at each
release uses only what was knowable by then.

Nothing here imports Django (§8).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: §6.5's window for the standardising sigma.
SIGMA_WINDOW = 20

#: Below this, a trailing sigma is too noisy to divide by. The expanding window
#: covers the start of a series rather than discarding it.
SIGMA_MIN_PERIODS = 8

#: §6.6's minimum-n gate, applied to the regression rather than to a mean.
MIN_N = 20


@dataclass
class DirectionFit:
    """One horizon's fit.  `beta` is in units of abnormal return per 1 sigma of
    indicator change."""

    horizon: str
    seconds: int
    n: int = 0

    beta: float | None = None
    std_error: float | None = None
    t_stat: float | None = None
    p_value: float | None = None
    #: Benjamini-Hochberg across the ladder. A curve runs eleven tests, so the
    #: raw p is not the one a claim should rest on (§6.6).
    p_fdr: float | None = None
    r_squared: float | None = None
    intercept: float | None = None

    #: §6.5's asymmetry: the same fit run on rises and falls separately. A
    #: currency that sells off hard on a miss and shrugs at a beat shows it
    #: here and nowhere else.
    beta_up: float | None = None
    beta_down: float | None = None
    n_up: int = 0
    n_down: int = 0

    #: §6.6 — the smallest beta this many observations could have resolved, so
    #: a null reads as "no effect larger than X" rather than "no effect".
    detectability_floor: float | None = None

    samples: list = field(default_factory=list)

    @property
    def gated(self) -> bool:
        return self.n < MIN_N

    @property
    def significant(self) -> bool:
        """Raw significance. Use `survives_fdr` for anything a person reads as
        a finding."""
        return self.p_value is not None and self.p_value < 0.05

    @property
    def survives_fdr(self) -> bool:
        return self.p_fdr is not None and self.p_fdr < 0.05

    @property
    def is_pre_release(self) -> bool:
        """A window that closes before the release. A beta here is not a
        response to the news — nothing had been published yet. It is §3.4's
        leakage test: a coefficient means the move was already underway in the
        direction the data would later confirm."""
        return self.seconds < 0

    @property
    def asymmetric(self) -> bool:
        """Do the two halves disagree about the sign?  A weak test — it says
        the halves point different ways, not that the difference is
        significant — so it flags a question, never an answer."""
        if self.beta_up is None or self.beta_down is None:
            return False
        return (self.beta_up > 0) != (self.beta_down > 0)


def standardise_changes(values: list[float | None]) -> list[float | None]:
    """`change_z` for a chronologically ordered series of raw changes.

    The sigma at each point is trailing: it uses the `SIGMA_WINDOW` changes
    before this one, never this one and never anything after it. A full-sample
    sigma would leak the future into the scaling of every early release, which
    is §4.3's failure mode wearing a different hat.
    """
    out: list[float | None] = []
    history: list[float] = []
    for value in values:
        if value is None or not math.isfinite(value):
            out.append(None)
        elif len(history) < SIGMA_MIN_PERIODS:
            out.append(None)
        else:
            window = history[-SIGMA_WINDOW:]
            sigma = float(np.std(window, ddof=1)) if len(window) > 1 else 0.0
            out.append(value / sigma if sigma > 0 else None)
        if value is not None and math.isfinite(value):
            history.append(float(value))
    return out


def fit(
    horizon: str,
    seconds: int,
    changes_z: list[float | None],
    returns: list[float | None],
) -> DirectionFit:
    """Ordinary least squares of abnormal return on standardised change."""
    pairs = [
        (x, y)
        for x, y in zip(changes_z, returns)
        if x is not None and y is not None and math.isfinite(x) and math.isfinite(y)
    ]
    result = DirectionFit(horizon=horizon, seconds=seconds, n=len(pairs))
    if len(pairs) < 3:
        return result

    x = np.array([p[0] for p in pairs], dtype=float)
    y = np.array([p[1] for p in pairs], dtype=float)
    result.samples = [[float(a), float(b)] for a, b in pairs]

    estimate = _ols(x, y)
    if estimate is None:
        return result
    result.beta, result.intercept, result.std_error, result.r_squared = estimate

    if result.std_error:
        result.t_stat = result.beta / result.std_error
        result.p_value = _two_sided_p(result.t_stat, len(pairs) - 2)
        # 80% power at 5% two-sided needs roughly 2.8 standard errors.
        result.detectability_floor = 2.8 * result.std_error

    up = [(a, b) for a, b in pairs if a > 0]
    down = [(a, b) for a, b in pairs if a < 0]
    result.n_up, result.n_down = len(up), len(down)
    for half, attribute in ((up, "beta_up"), (down, "beta_down")):
        if len(half) >= 3:
            half_fit = _ols(
                np.array([p[0] for p in half], dtype=float),
                np.array([p[1] for p in half], dtype=float),
            )
            if half_fit is not None:
                setattr(result, attribute, half_fit[0])
    return result


def _ols(x: np.ndarray, y: np.ndarray):
    """Returns (beta, intercept, standard error of beta, R squared)."""
    n = x.size
    if n < 3:
        return None
    x_mean, y_mean = float(x.mean()), float(y.mean())
    sxx = float(((x - x_mean) ** 2).sum())
    if sxx <= 0:
        return None

    beta = float(((x - x_mean) * (y - y_mean)).sum() / sxx)
    intercept = y_mean - beta * x_mean

    residuals = y - (intercept + beta * x)
    sse = float((residuals**2).sum())
    sst = float(((y - y_mean) ** 2).sum())

    degrees = n - 2
    if degrees < 1:
        return beta, intercept, None, None
    std_error = math.sqrt(sse / degrees / sxx) if sse > 0 else None
    r_squared = 1.0 - sse / sst if sst > 0 else None
    return beta, intercept, std_error, r_squared


def _two_sided_p(t_stat: float, degrees: int) -> float | None:
    if degrees < 1:
        return None
    try:
        from scipy import stats
    except ImportError:
        return None
    return float(2 * (1 - stats.t.cdf(abs(t_stat), degrees)))
