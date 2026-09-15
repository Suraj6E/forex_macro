"""Price side — planning.md §9.

The bars themselves are not here.  ~50M M1 bars (§4.5) live in Parquet under
`data/parquet/`; SQLite holds only the *coverage ledger* that says which
months exist, from which source, and how complete they are.  §8: split by
workload.
"""

from django.db import models

from quality.enums import Regime

#: The feed every measurement currently reads. One name, so adding a second
#: backbone later is a change in one place rather than a grep.
DEFAULT_PRICE_SOURCE = "dukascopy"

MAJORS = [
    ("EURUSD", "EUR", "USD", "0.0001"),
    ("GBPUSD", "GBP", "USD", "0.0001"),
    ("USDJPY", "USD", "JPY", "0.01"),
    ("USDCHF", "USD", "CHF", "0.0001"),
    ("AUDUSD", "AUD", "USD", "0.0001"),
    ("USDCAD", "USD", "CAD", "0.0001"),
    ("NZDUSD", "NZD", "USD", "0.0001"),
]


class Instrument(models.Model):
    symbol = models.CharField(max_length=12, unique=True)
    base_ccy = models.CharField(max_length=3)
    quote_ccy = models.CharField(max_length=3)
    pip_size = models.DecimalField(max_digits=12, decimal_places=8)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["symbol"]

    def __str__(self):
        return self.symbol

    def involves(self, currency: str) -> bool:
        return currency in (self.base_ccy, self.quote_ccy)


class Timeframe(models.TextChoices):
    """Bar size. §7 locks 1-minute as the working resolution, but coarser bars
    are a legitimate first pass: an hourly series is ~60× smaller and still
    resolves everything past the `+1h` rung of the horizon ladder."""

    M1 = "m1", "1 minute"
    M15 = "m15", "15 minutes"
    H1 = "h1", "1 hour"
    D1 = "d1", "1 day"


#: Bar length in seconds, for expected-count arithmetic.
TIMEFRAME_SECONDS = {
    Timeframe.M1: 60,
    Timeframe.M15: 15 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
}


class PriceCoverage(models.Model):
    """One row per instrument × source × timeframe × month.

    Gaps are the thing that breaks long horizons: a `-5d … +1M` window needs
    unbroken bars across weekends and holidays, which is a different failure
    mode from a short window (§15).
    """

    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="coverage")
    source = models.ForeignKey("sources.Source", on_delete=models.CASCADE)
    timeframe = models.CharField(
        max_length=4, choices=Timeframe.choices, default=Timeframe.M1, db_index=True
    )
    month = models.DateField(help_text="First day of the covered month.")

    bar_count = models.IntegerField(default=0)
    expected_bar_count = models.IntegerField(default=0)
    gap_count = models.IntegerField(default=0)
    first_ts_utc = models.DateTimeField(null=True, blank=True)
    last_ts_utc = models.DateTimeField(null=True, blank=True)
    parquet_path = models.TextField(blank=True, help_text="Relative to settings.PARQUET_DIR.")

    fetch_run = models.ForeignKey(
        "sources.FetchRun", on_delete=models.SET_NULL, null=True, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["instrument", "source", "timeframe", "month"]
        constraints = [
            models.UniqueConstraint(
                fields=["instrument", "source", "timeframe", "month"],
                name="uniq_coverage_month",
            )
        ]

    def __str__(self):
        return f"{self.instrument} {self.source_id} {self.timeframe} {self.month:%Y-%m}"

    @property
    def is_complete(self) -> bool:
        """Enough of the month present to skip on a resumed fetch.

        Not 100%: Dukascopy pads closed hours and public holidays differ by
        venue, so an exact match would never happen and every resume would
        re-download everything.
        """
        if not self.expected_bar_count:
            return False
        return self.bar_count >= self.expected_bar_count * 0.9

    @property
    def completeness(self) -> float | None:
        if not self.expected_bar_count:
            return None
        return self.bar_count / self.expected_bar_count


class MarketEventKind(models.TextChoices):
    DISLOCATION = "dislocation", "dislocation / flash crash"
    POLICY_SHOCK = "policy_shock", "unscheduled policy shock"
    CRISIS = "crisis", "crisis period"
    HOLIDAY = "holiday", "market holiday / thin liquidity"
    OTHER = "other", "other"


class MarketEvent(models.Model):
    """Known dislocations — §4.6.

    So the UI can annotate them rather than have you rediscover them as
    inexplicable dots on a scatter plot.

    `end_ts_utc` is not in §9; added because COVID and the 2022 inflation
    shock are periods, not instants, while the SNB floor removal is an instant.
    """

    ts_utc = models.DateTimeField(db_index=True)
    end_ts_utc = models.DateTimeField(null=True, blank=True)
    label = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=20, choices=MarketEventKind.choices, default=MarketEventKind.OTHER
    )
    regime = models.CharField(max_length=20, choices=Regime.choices, blank=True)
    instruments = models.ManyToManyField(Instrument, blank=True, related_name="market_events")
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["ts_utc"]

    def __str__(self):
        return f"{self.ts_utc:%Y-%m-%d} {self.label}"
