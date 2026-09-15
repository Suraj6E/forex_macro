"""Timestamp validation against price — planning.md §4.1, §5.3, §12, P2.

    Premise 1. Every measurement in this project is anchored to a release
    timestamp we were given, not one we observed.
    Premise 2. A timestamp wrong by an hour still produces a decay curve, a
    significance level and a confident sentence. It is wrong *and plausible*,
    which is the worst failure mode in §12's table.
    Premise 3. A release that matters moves price when it lands, and FX
    volatility is otherwise strongly patterned by weekday and hour.
    Inference. Comparing the move in the release's own bar against what that
    pair normally does in the same weekday-and-hour slot tells us whether the
    market agrees with the stored clock.

**What this module can and cannot say at hourly resolution.** §5.3's original
grade was "spike within ±2 minutes", which needs M1 bars. With H1 bars the
question becomes "did the spike land in the hour the timestamp claims, or in a
neighbouring one" — so the affirmative grade here is `confirmed_hour`, a
deliberately weaker word, and `confirmed` stays reserved for the minute-
resolution check that arrives with the M1 event windows. The weaker grade is
not a lesser check for the failure that actually threatens this dataset: a
source-clock or DST bug displaces a release by a whole hour or more, and that
is exactly what an hourly scan resolves.

**An offset is not by itself a clock bug**, and the first version of this check
proved it: run across the whole calendar, it named thirty-nine indicators as
systematically offset, and every single one had a heavier release sitting in the
hour the spike landed in — European morning prints resolving to +3h, which is
the 13:30 UTC US cluster, and 10:00 New York prints resolving to −2h, which is
the 08:30 one. The scan had found the market's dominant news hour, not our
clocks. So a bar cannot win the scan if a release of equal or greater importance
occupies it: the grade becomes `confounded`, §3.3's collinearity refusal applied
to timestamps. That is deliberately conservative — a genuine offset whose bar
happens to hold a big release is missed rather than reported — because a harness
whose job is finding our own bugs is worthless if it cries wolf. The histogram
keeps showing the raw picture either way, and
`quality.validation.co_timed_at_offset` names the neighbour.

**A single release's grade is noisy and is not meant to be read alone.** One
hour of FX can be quiet for reasons unrelated to the release, and can jump for
reasons unrelated to it too. The signal is in the pooled shape — `summarise()`
below — where a systematically wrong clock shows up as most of an indicator's
releases agreeing on the *same* non-zero offset. Half a dozen scattered
offsets mean the releases were weak, not that the clock is broken.

No Django imports (§8).
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from analytics.timeutils import NEW_YORK

#: Matched weekday-and-hour slots drawn from the preceding weeks, same reasoning
#: as the event study's baseline: FX has a strong intraday and intraweek shape,
#: so an all-hours average would call the London–New York overlap a spike.
BASELINE_WEEKS = 12

#: Keep walking back past weekends, holidays and the indicator's own previous
#: releases until enough clean slots are found, rather than giving up at 12.
MAX_LOOKBACK_WEEKS = 40

#: Fewer than this and the "normal" is itself noise, so the release is graded
#: uncheckable rather than compared against two observations.
MIN_BASELINE = 5

#: How many times the normal range counts as a spike. 1.5 is deliberately low:
#: this is a clock test, not an impact test, and a weak release that moves price
#: half again as much as usual still locates itself in time.
SPIKE_RATIO = 1.5

#: A neighbouring hour beating the release's own hour by less than this margin
#: is not evidence the timestamp is wrong — two adjacent hours of one news event
#: are routinely within a few percent of each other.
TIE_MARGIN = 0.9

#: Bars either side of the release to scan. Three hours covers every clock bug
#: this project can plausibly have: DST (±1), a source stamping local time for a
#: European or US zone (±1…3), and an off-by-one in our own conversion.
NEIGHBOURHOOD = 3

# Per-release grades. These strings intentionally equal the values of
# quality.enums.VolCheck, but this module may not import it (§8) — the Django
# layer owns the mapping and asserts it.
CONFIRMED_HOUR = "confirmed_hour"
OFFSET = "offset"
NO_SPIKE = "no_spike"
CONFOUNDED = "confounded"
UNCHECKABLE = "uncheckable"

# Pooled verdicts, one per indicator.
ALIGNED = "aligned"
OFFSET_CLOCK = "offset"
SCATTERED = "scattered"
SILENT = "silent"
INSUFFICIENT = "insufficient"

#: Below this many graded releases, no verdict is offered at all. §6.6's
#: minimum-n gate applied to the clock rather than to an effect size.
MIN_VERDICT_N = 10

#: Floor on the modal bin's share. The significance test below does the real
#: work; this stops a bin that is statistically ahead but substantively tiny —
#: 16% of a thousand spikes against a 14% null — from deciding anything.
MODAL_SHARE = 0.20

#: How unlikely the modal bin must be under the null that a spike is equally
#: likely in any bar of the neighbourhood. A bare share threshold cannot do
#: this job: with seven bars in the scan, chance alone puts 14% in each, so
#: "48% land on time" is overwhelming evidence and "30% land an hour late" out
#: of ten spikes is nothing at all. One number cannot separate those; a tail
#: probability can.
DECISIVE_P = 0.01

#: Below this share of releases showing any spike, the indicator is too quiet to
#: locate in time — which is a statement about the release, not about the clock.
SILENT_SHARE = 0.2


class BarIndex:
    """Bars keyed by bar-start, so a lookup is a dict hit rather than a scan.

    The check reads up to forty baseline slots for each of seven offsets for
    each release; a searchsorted per read costs more than the arithmetic does.
    Bar starts are exact multiples of the bar size from the epoch, so flooring
    an instant to its bar is a modulo rather than a search.

    Volatility is measured as the Parkinson range, `log(high / low)`. The
    close-to-open return is the wrong instrument here: a release that whipsaws
    fifty pips each way and returns to where it started is a violent reaction
    with a close-to-open return of zero.
    """

    #: This check is defined at hourly resolution, so finer bars are folded up
    #: to it rather than scanned as they are. Without this the neighbourhood —
    #: counted in bars — would quietly become ±3 *minutes* the day an M1
    #: backbone lands, and a scan that narrow cannot see a clock bug at all.
    #: The ±2-minute check of §5.3 is a different measurement, not this one
    #: pointed at smaller bars.
    RESOLUTION_SECONDS = 3600

    def __init__(self, bars: pd.DataFrame):
        frame = bars.copy()
        frame["ts_utc"] = pd.to_datetime(frame["ts_utc"], utc=True)
        frame = frame.sort_values("ts_utc").reset_index(drop=True)
        frame = self._to_resolution(frame)

        # The Parquet store writes microsecond-resolution stamps, not the
        # nanoseconds pandas uses by default. Assuming either one silently
        # rescales every timestamp — the symptom is a "bar size" of 4 seconds —
        # so the unit is read off the column rather than guessed.
        stamps = frame["ts_utc"]
        divisor = _EPOCH_DIVISOR[getattr(stamps.dt, "unit", "ns")]
        epochs = stamps.astype("int64").to_numpy() // divisor
        self.bar_seconds = _bar_seconds(epochs)

        high = frame["high"].to_numpy(dtype=float)
        low = frame["low"].to_numpy(dtype=float)
        with np.errstate(divide="ignore", invalid="ignore"):
            ranges = np.log(np.where(low > 0, high / np.where(low > 0, low, 1.0), np.nan))
        ranges[~np.isfinite(ranges)] = np.nan
        ranges[ranges < 0] = np.nan

        self._ranges = ranges
        self._rows = {int(epoch): index for index, epoch in enumerate(epochs)}
        self.n_bars = len(frame)

    @classmethod
    def _to_resolution(cls, frame: pd.DataFrame) -> pd.DataFrame:
        """Fold bars finer than an hour into hourly high/low, leave the rest."""
        if len(frame) < 2:
            return frame
        spacing = _bar_seconds(
            frame["ts_utc"].astype("int64").to_numpy()
            // _EPOCH_DIVISOR[getattr(frame["ts_utc"].dt, "unit", "ns")]
        )
        if spacing >= cls.RESOLUTION_SECONDS:
            return frame

        hourly = (
            frame.set_index("ts_utc")
            .resample(f"{cls.RESOLUTION_SECONDS}s")
            .agg({"high": "max", "low": "min"})
            .dropna()
            .reset_index()
        )
        return hourly

    def __len__(self) -> int:
        return self.n_bars

    def bar_start(self, moment: datetime) -> int:
        epoch = int(moment.timestamp())
        return epoch - epoch % self.bar_seconds

    def range_at(self, moment: datetime) -> float | None:
        """Parkinson range of the bar containing `moment`, if we hold it."""
        row = self._rows.get(self.bar_start(moment))
        if row is None:
            return None
        value = self._ranges[row]
        return None if np.isnan(value) else float(value)


_EPOCH_DIVISOR = {"s": 1, "ms": 1_000, "us": 1_000_000, "ns": 1_000_000_000}


def _bar_seconds(epochs: np.ndarray) -> int:
    if epochs.size < 2:
        return 3600
    deltas = np.diff(epochs)
    deltas = deltas[deltas > 0]
    if not deltas.size:
        return 3600
    values, counts = np.unique(deltas, return_counts=True)
    return int(values[counts.argmax()])


@dataclass
class OffsetReading:
    """One bar of the neighbourhood, against its own matched normal."""

    offset_seconds: int
    observed: float | None = None
    normal: float | None = None
    baseline_n: int = 0

    @property
    def ratio(self) -> float | None:
        if self.observed is None or not self.normal:
            return None
        return self.observed / self.normal


@dataclass
class ReleaseCheck:
    """What price says about one stored timestamp."""

    status: str
    ratio: float | None = None
    best_ratio: float | None = None
    offset_seconds: int | None = None
    readings: list[OffsetReading] = field(default_factory=list)

    @property
    def offset_minutes(self) -> int | None:
        if self.offset_seconds is None:
            return None
        return int(round(self.offset_seconds / 60))

    @property
    def graded(self) -> bool:
        return self.status != UNCHECKABLE


def exclusion_epochs(times) -> list[int]:
    """Release times as sorted epoch seconds, ready for the baseline filter.

    Computed once per indicator by the caller rather than per release: a weekly
    series has a thousand release times and the baseline consults them a few
    hundred times per release.
    """
    return sorted(int(t.timestamp()) for t in times)


def normal_range(
    index: BarIndex,
    moment: datetime,
    *,
    weeks: int = BASELINE_WEEKS,
    exclude: list[int] | None = None,
    max_lookback_weeks: int = MAX_LOOKBACK_WEEKS,
) -> tuple[float | None, int]:
    """Median range of this weekday-and-hour slot over the preceding weeks.

    Median rather than mean: FX ranges are fat-tailed, and one crisis afternoon
    in the look-back would raise the "normal" enough to hide the release we are
    trying to find.

    `exclude` carries the indicator's own release times as epoch seconds (see
    `exclusion_epochs`). Without it a weekly
    release is compared against previous instances of itself — the same
    circularity the event study's baseline had to fix, with the same
    consequence: the ratio collapses towards 1 and a perfectly good timestamp
    reads as `no_spike`.
    """
    excluded = exclude or []
    samples: list[float] = []
    week = 1
    while len(samples) < weeks and week <= max_lookback_weeks:
        slot = moment - timedelta(weeks=week)
        week += 1
        if _bar_contains(excluded, index.bar_start(slot), index.bar_seconds):
            continue
        value = index.range_at(slot)
        if value is not None:
            samples.append(value)

    if len(samples) < MIN_BASELINE:
        return None, len(samples)
    return float(np.median(samples)), len(samples)


def _bar_contains(excluded: list[int], bar_start: int, bar_seconds: int) -> bool:
    """Does the bar starting at `bar_start` hold an excluded release?"""
    if not excluded:
        return False
    position = bisect_left(excluded, bar_start)
    return position < len(excluded) and excluded[position] < bar_start + bar_seconds


def check_release(
    index: BarIndex,
    t0: datetime,
    *,
    neighbourhood: int = NEIGHBOURHOOD,
    weeks: int = BASELINE_WEEKS,
    exclude: list[int] | None = None,
    blocked: set[int] | frozenset = frozenset(),
    spike_ratio: float = SPIKE_RATIO,
) -> ReleaseCheck:
    """Grade one stored timestamp against the price series.

    `blocked` holds offsets (in seconds) whose bar is occupied by another
    release of equal or greater importance. Those bars may not win the scan —
    see the module docstring — and a win there is reported as `confounded`
    rather than as an offset.

    Returns `uncheckable` when the release's own bar is missing or its normal
    cannot be established — which is a statement about our price coverage, not
    about the timestamp, and is counted separately everywhere downstream.
    """
    readings = []
    for step in range(-neighbourhood, neighbourhood + 1):
        offset_seconds = step * index.bar_seconds
        moment = t0 + timedelta(seconds=offset_seconds)
        observed = index.range_at(moment)
        normal, baseline_n = normal_range(index, moment, weeks=weeks, exclude=exclude)
        readings.append(
            OffsetReading(
                offset_seconds=offset_seconds,
                observed=observed,
                normal=normal,
                baseline_n=baseline_n,
            )
        )

    zero = next(r for r in readings if r.offset_seconds == 0)
    if zero.ratio is None:
        return ReleaseCheck(status=UNCHECKABLE, readings=readings)

    rated = [r for r in readings if r.ratio is not None]
    best = max(rated, key=lambda r: r.ratio)

    if best.ratio < spike_ratio:
        # Nothing measurable happened anywhere nearby. For a low-impact release
        # this is the expected answer, not a fault.
        return ReleaseCheck(
            status=NO_SPIKE, ratio=zero.ratio, best_ratio=best.ratio, readings=readings
        )

    if zero.ratio >= spike_ratio and zero.ratio >= best.ratio * TIE_MARGIN:
        return ReleaseCheck(
            status=CONFIRMED_HOUR,
            ratio=zero.ratio,
            best_ratio=best.ratio,
            offset_seconds=0,
            readings=readings,
        )

    if best.offset_seconds in blocked:
        # The local maximum belongs to a heavier release. Nothing here can be
        # attributed to this one, in either direction — this is not evidence
        # that the clock is wrong, and it is not evidence that it is right.
        return ReleaseCheck(
            status=CONFOUNDED,
            ratio=zero.ratio,
            best_ratio=best.ratio,
            offset_seconds=best.offset_seconds,
            readings=readings,
        )

    return ReleaseCheck(
        status=OFFSET,
        ratio=zero.ratio,
        best_ratio=best.ratio,
        offset_seconds=best.offset_seconds,
        readings=readings,
    )


# ------------------------------------------------------------------ pooling --


@dataclass
class Verdict:
    """The pooled answer for one indicator — what the market says about its clock."""

    checked: int = 0
    confirmed: int = 0
    offset: int = 0
    no_spike: int = 0
    confounded: int = 0
    modal_offset_minutes: int | None = None
    modal_share: float = 0.0
    modal_p: float | None = None
    #: The offset claim's own test: how unlikely the *best non-zero* bin is
    #: under the uniform null. Every checked indicator has one, including the
    #: ones whose spike sits on time — which is what makes them a family the
    #: false-discovery correction can be applied across.
    offset_p: float | None = None
    offset_p_fdr: float | None = None
    median_ratio: float | None = None
    status: str = INSUFFICIENT

    @property
    def spiked(self) -> int:
        return self.confirmed + self.offset

    @property
    def attributable(self) -> int:
        """Releases whose nearby price action is this release's to read.

        Confounded ones are excluded: with a heavier release in the winning
        hour, neither a spike nor its absence says anything about this clock.
        """
        return self.spiked + self.no_spike

    @property
    def spiked_share(self) -> float:
        return self.spiked / self.attributable if self.attributable else 0.0

    @property
    def confirmed_share(self) -> float:
        return self.confirmed / self.attributable if self.attributable else 0.0

    @property
    def modal_offset_hours(self) -> float | None:
        if self.modal_offset_minutes is None:
            return None
        return self.modal_offset_minutes / 60

    @property
    def wrong(self) -> bool:
        """Is this a clock we should not be measuring against?"""
        return self.status == OFFSET_CLOCK

    @property
    def message(self) -> str:
        return VERDICT_MESSAGES[self.status].format(
            hours=abs(self.modal_offset_hours or 0),
            direction="later" if (self.modal_offset_minutes or 0) > 0 else "earlier",
            share=round(self.modal_share * 100),
            spiked=round(self.spiked_share * 100),
            chance=round(100 / (2 * NEIGHBOURHOOD + 1)),
            n=self.attributable,
            confounded=self.confounded,
            min_n=MIN_VERDICT_N,
        )


VERDICT_MESSAGES = {
    ALIGNED: "{share}% of the spikes land in the stored hour, against {chance}% "
    "if they fell anywhere in the window by chance. This clock agrees with the "
    "market.",
    OFFSET_CLOCK: "{share}% of the spikes land {hours:.0f}h {direction} than "
    "the stored timestamp, against {chance}% by chance. Either the clock is "
    "wrong by that much — in which case every measurement anchored to it "
    "inherits the error — or something bigger reliably follows this release, "
    "the way a press conference follows a rate decision. The histogram and the "
    "individual releases below tell you which.",
    SCATTERED: "Releases spike, but no offset stands out from chance: the "
    "biggest bin holds {share}% against {chance}% by luck. Usually this means "
    "the moves are too weak to locate rather than that the clock varies — "
    "check the median move before reading anything into it.",
    SILENT: "Only {spiked}% of these releases move price measurably at all, so "
    "price cannot say where they landed. The clock is unverified rather than "
    "wrong — and an indicator this quiet is itself worth knowing about.",
    INSUFFICIENT: "{n} release(s) whose price action is this one's to read "
    "({confounded} more sat in the shadow of a bigger release); {min_n} are "
    "needed before a pooled verdict means anything.",
}


def summarise(rows) -> Verdict:
    """Pool per-release grades into one indicator's verdict.

    `rows` is an iterable of `(status, offset_minutes, ratio)` — fed from the
    stored columns, so the screen never recomputes anything.
    """
    offsets: Counter = Counter()
    ratios: list[float] = []
    no_spike = confounded = 0

    for status, offset_minutes, ratio in rows:
        if status == CONFOUNDED:
            confounded += 1
            continue
        if status == CONFIRMED_HOUR:
            offsets[0] += 1
        elif status == OFFSET:
            if offset_minutes is None:
                # An offset grade carrying no offset predates this column and
                # cannot vote on where the spike was; counting it as spiked
                # without a location would bias the modal share downwards.
                continue
            offsets[int(offset_minutes)] += 1
        elif status == NO_SPIKE:
            no_spike += 1
        else:
            continue
        if ratio is not None:
            ratios.append(float(ratio))

    return summarise_counts(
        offsets=offsets,
        no_spike=no_spike,
        confounded=confounded,
        median_ratio=float(np.median(ratios)) if ratios else None,
    )


def summarise_counts(
    *,
    offsets: dict[int, int],
    no_spike: int,
    confounded: int = 0,
    median_ratio: float | None = None,
    bins: int = 2 * NEIGHBOURHOOD + 1,
) -> Verdict:
    """The same verdict from aggregate counts rather than individual rows.

    The screen has the rows; the Data Quality page has only a GROUP BY, and
    both must apply identical thresholds or the two pages disagree about the
    same dataset. So the thresholds live here, once.
    """
    spiked = sum(offsets.values())
    verdict = Verdict(
        checked=spiked + no_spike + confounded,
        confirmed=offsets.get(0, 0),
        offset=spiked - offsets.get(0, 0),
        no_spike=no_spike,
        confounded=confounded,
        median_ratio=median_ratio,
    )

    if spiked:
        # Ties resolve towards the stored timestamp and then towards the nearer
        # hour, so the verdict does not depend on dictionary ordering — an equal
        # split is not evidence that the clock is wrong.
        modal, count = min(
            offsets.items(), key=lambda item: (-item[1], abs(item[0]), item[0])
        )
        verdict.modal_offset_minutes = int(modal)
        verdict.modal_share = count / spiked
        verdict.modal_p = _binomial_tail(count, spiked, 1 / bins)

        elsewhere = {k: v for k, v in offsets.items() if k != 0}
        if elsewhere:
            best_off = max(elsewhere.values())
            verdict.offset_p = _binomial_tail(best_off, spiked, 1 / bins)

    # The gate counts attributable releases, not graded ones: an indicator that
    # spends its life in the shadow of a heavier release has plenty of grades
    # and no evidence.
    if verdict.attributable < MIN_VERDICT_N:
        verdict.status = INSUFFICIENT
    elif verdict.spiked_share < SILENT_SHARE:
        verdict.status = SILENT
    elif verdict.modal_share < MODAL_SHARE or not _decisive(verdict):
        verdict.status = SCATTERED
    elif verdict.modal_offset_minutes == 0:
        verdict.status = ALIGNED
    else:
        verdict.status = OFFSET_CLOCK
    return verdict


def apply_fdr(verdicts: list[Verdict], alpha: float = DECISIVE_P) -> list[Verdict]:
    """Control false discoveries across a whole sweep — §6.9, §12's top risk.

    Each indicator's offset is one hypothesis test, and a sweep tests several
    hundred of them. At p < 0.01 chance alone hands back three or four
    "systematically offset" clocks per sweep, which is precisely the kind of
    confident wrongness this harness exists to prevent — a false alarm here
    sends you hunting a timezone bug that was never there.

    **The family is every indicator checked, not the ones that came out
    significant.** Each checked indicator tested the same claim — "the spike
    lands somewhere other than the stored hour" — via `offset_p`, and adjusting
    only the winners would pretend the other three hundred tests never happened,
    which is the mistake the correction exists to prevent.

    Only offset verdicts can change. A verdict of *aligned* asserts that the
    spike is where the timestamp says, which is this test's null rather than a
    discovery against it.

    Mutates and returns the same objects, so the caller keeps one set of
    verdicts rather than two that can disagree.
    """
    from analytics.eventstudy import benjamini_hochberg

    tested = [v for v in verdicts if v.offset_p is not None]
    if not tested:
        return verdicts

    adjusted = benjamini_hochberg([v.offset_p for v in tested])
    for verdict, value in zip(tested, adjusted):
        verdict.offset_p_fdr = value
        if verdict.status == OFFSET_CLOCK and (value is None or value >= alpha):
            # Stands up on its own p and not against the sweep. Not evidence.
            verdict.status = SCATTERED
    return verdicts


def _decisive(verdict: Verdict) -> bool:
    """Does the modal bin stand out from chance?

    With scipy absent the tail probability cannot be computed, and the honest
    fallback is the blunt one it replaced — a bare majority — rather than
    treating "unknown" as "significant".
    """
    if verdict.modal_p is None:
        return verdict.modal_share >= 0.5
    return verdict.modal_p < DECISIVE_P


def _binomial_tail(hits: int, trials: int, rate: float) -> float | None:
    """P(at least `hits` of `trials` land in one bin) when every bin is equal."""
    if trials <= 0:
        return None
    try:
        from scipy import stats
    except ImportError:  # pragma: no cover - scipy is a declared dependency
        return None
    return float(stats.binom.sf(hits - 1, trials, rate))


def dst_window(moment: datetime) -> str:
    """Which side of the US daylight-saving boundary an instant falls on.

    §12 lists a DST shift as a High-likelihood, High-impact risk, and it has a
    signature no other bug has: the same indicator resolves to one offset in
    summer and an offset exactly one hour different in winter. Splitting the
    verdict on this is how that signature becomes visible instead of averaging
    into "scattered".
    """
    local = moment.astimezone(NEW_YORK)
    return "EDT" if local.dst() else "EST"
