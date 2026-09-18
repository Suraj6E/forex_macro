"""Fit and store the direction curve — the Django half of `analytics/direction`.

`analytics/direction.py` does the arithmetic and imports nothing from Django.
This module supplies it with two aligned series and stores the result.

**It re-measures nothing.** The signed abnormal return per event per horizon is
already in `EventImpact`, written by the Mode A study; the driver comes from
`EventRelease.actual_current` and `.previous`. So a direction curve is a query
and a regression, not another pass over nineteen years of bars — it runs in
seconds on a pairing the event study has already covered, and it is meaningless
on one it has not.
"""

from __future__ import annotations

import logging

from django.db import transaction

from analytics import direction
from analytics.eventstudy import benjamini_hochberg
from analytics.horizons import BY_LABEL
from calendar_data.models import EventRelease, Indicator
from prices.models import Instrument
from studies.engine import ENGINE_VERSION, _attribution
from studies.models import DecayCurve, EventImpact, Mode

logger = logging.getLogger(__name__)

#: Bumped when the fit changes, so a methodology change invalidates the cache
#: instead of mixing generations (§9). Tracks the Mode A engine version because
#: the inputs are its output.
DIRECTION_VERSION = f"{ENGINE_VERSION}+dir-1"


class DirectionResult:
    def __init__(self, indicator_id, instrument_id):
        self.indicator_id = indicator_id
        self.instrument_id = instrument_id
        self.releases_with_change = 0
        self.horizons_fitted = 0
        self.curve_points = 0
        self.notes = ""


def run_direction(
    indicator: Indicator,
    instrument: Instrument,
    *,
    log=lambda msg: None,
    progress=lambda frac, msg: None,
) -> DirectionResult:
    """Regress the signed abnormal move on the standardised change, per horizon."""
    result = DirectionResult(indicator.pk, instrument.pk)

    releases = list(
        EventRelease.objects.filter(
            indicator=indicator,
            release_time_utc__isnull=False,
            actual_current__isnull=False,
            previous__isnull=False,
        ).order_by("release_time_utc")
    )
    if len(releases) < direction.MIN_N:
        result.notes = (
            f"{len(releases)} release(s) carry both an actual and a previous — "
            f"below the {direction.MIN_N}-observation gate, so no coefficient is reported."
        )
        log(result.notes)
        return result

    # Chronological order matters: the standardising sigma is trailing, and
    # shuffling these rows would quietly let later releases scale earlier ones.
    changes = [float(r.actual_current) - float(r.previous) for r in releases]
    changes_z = direction.standardise_changes(changes)
    result.releases_with_change = sum(1 for z in changes_z if z is not None)

    log(
        f"{len(releases):,} release(s) with a previous print; "
        f"{result.releases_with_change:,} past the trailing-sigma warm-up."
    )

    impacts = EventImpact.objects.filter(
        event_release__in=releases,
        instrument=instrument,
        engine_version=ENGINE_VERSION,
    ).values_list("event_release_id", "horizon", "abnormal_ret")

    by_horizon: dict[str, dict[int, float]] = {}
    for release_id, horizon, abnormal in impacts.iterator():
        if abnormal is None:
            continue
        by_horizon.setdefault(horizon, {})[release_id] = abnormal

    if not by_horizon:
        result.notes = (
            f"No stored impacts for {instrument.symbol} at engine {ENGINE_VERSION}. "
            "Run the event study for this pairing first — the direction fit reads its "
            "output rather than re-measuring price."
        )
        log(result.notes)
        return result

    fits = []
    ordered = sorted(
        by_horizon, key=lambda label: BY_LABEL[label].seconds if label in BY_LABEL else 0
    )
    for index, horizon in enumerate(ordered, start=1):
        returns_by_release = by_horizon[horizon]
        aligned_x, aligned_y = [], []
        for release, change_z in zip(releases, changes_z):
            aligned_x.append(change_z)
            aligned_y.append(returns_by_release.get(release.pk))

        seconds = BY_LABEL[horizon].seconds if horizon in BY_LABEL else 0
        fit = direction.fit(horizon, seconds, aligned_x, aligned_y)
        fits.append(fit)
        progress(index / len(ordered), f"{index}/{len(ordered)} horizons")

    # FDR across the ladder before anything is called a finding, and before the
    # rows are written, so the stored curve and the sentence agree.
    for fit, corrected in zip(fits, benjamini_hochberg([f.p_value for f in fits])):
        fit.p_fdr = corrected

    result.horizons_fitted = len(fits)
    result.curve_points = _store(indicator, instrument, fits)

    # A pre-release window closes before the number exists, so a coefficient
    # there is not a response to it. Mixing the two would let the summary
    # announce a reaction that happened five days before the release.
    response = [
        f for f in fits if f.survives_fdr and not f.gated and not f.is_pre_release
    ]
    leakage = [f for f in fits if f.survives_fdr and not f.gated and f.is_pre_release]

    if response:
        headline = max(response, key=lambda f: abs(f.beta))
        way = "stronger" if headline.beta > 0 else "weaker"
        result.notes = (
            f"{len(fits)} horizon(s) fitted. Strongest response at {headline.horizon}: "
            f"a one-sigma rise in this indicator is followed by a {way} "
            f"{instrument.symbol[:3]} leg, beta {headline.beta:+.5f}, "
            f"R2 {headline.r_squared or 0:.3f}, n {headline.n}."
        )
        if headline.asymmetric:
            result.notes += (
                f" Rises and falls point opposite ways here "
                f"(up {headline.beta_up:+.5f}, down {headline.beta_down:+.5f}) — "
                f"worth a look before the pooled beta is believed."
            )
    else:
        floors = [
            f.detectability_floor
            for f in fits
            if f.detectability_floor and not f.is_pre_release
        ]
        smallest = f"{min(floors):.5f}" if floors else "unknown"
        result.notes = (
            f"{len(fits)} horizon(s) fitted, no post-release response distinguishable "
            f"from zero after FDR. The smallest beta this sample could have resolved "
            f"is {smallest}, so the honest reading is 'no effect larger than that', "
            f"not 'no effect'."
        )

    if leakage:
        worst = max(leakage, key=lambda f: abs(f.beta))
        result.notes += (
            f" Note a pre-release coefficient at {worst.horizon} "
            f"(beta {worst.beta:+.5f}, R2 {worst.r_squared or 0:.3f}): the move was "
            f"already underway before the number existed. That is §3.4's leakage "
            f"signature, and it is equally consistent with this release being "
            f"predictable from data published earlier."
        )

    log(result.notes)
    return result


@transaction.atomic
def _store(indicator, instrument, fits) -> int:
    DecayCurve.objects.filter(
        indicator=indicator,
        instrument=instrument,
        mode=Mode.DIRECTION,
        engine_version=DIRECTION_VERSION,
    ).delete()

    rows = []
    for fit in fits:
        # §6.6's minimum-n gate: below twenty observations no coefficient is
        # reported at all, whatever the fit came back with.
        gated = fit.gated
        rows.append(
            DecayCurve(
                indicator=indicator,
                instrument=instrument,
                mode=Mode.DIRECTION,
                horizon=fit.horizon,
                effect_size=None if gated else fit.beta,
                std_error=None if gated else fit.std_error,
                r_squared=None if gated else fit.r_squared,
                p_raw=None if gated else fit.p_value,
                p_fdr=None if gated else fit.p_fdr,
                detectability_floor=fit.detectability_floor,
                n=fit.n,
                attribution_confidence=_attribution(fit.seconds),
                engine_version=DIRECTION_VERSION,
            )
        )
    DecayCurve.objects.bulk_create(rows)
    return len(rows)


def curve_for(indicator_id: int, instrument_id: int) -> list[DecayCurve]:
    """The stored direction curve for a pairing, oldest horizon first."""
    rows = list(
        DecayCurve.objects.filter(
            indicator_id=indicator_id,
            instrument_id=instrument_id,
            mode=Mode.DIRECTION,
            engine_version=DIRECTION_VERSION,
        )
    )
    return sorted(
        rows, key=lambda r: BY_LABEL[r.horizon].seconds if r.horizon in BY_LABEL else 0
    )
