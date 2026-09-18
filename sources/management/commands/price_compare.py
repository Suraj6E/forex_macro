"""P0.5 question 4: do HistData and Dukascopy agree?

planning.md §11 asks for the comparison on two kinds of hour, and the pairing
is the whole point:

* **A normal hour** establishes the baseline offset.  Two retail feeds will
  never be bar-identical — they are different liquidity pools — but on a quiet
  hour they should differ by well under a pip, and a systematic gap here means
  a clock or a bid/ask convention is wrong, not that the market disagreed.
* **A dislocation** is where feeds genuinely diverge.  §5.2's note on the 2016
  GBP flash crash says it plainly: there was no consolidated price, so
  different providers show different extremes.  The disagreement is real and
  the point is to measure how large it gets, because that is the error bar on
  any study whose window contains one.

Two conventions make this comparison a trap if they are not held in mind, and
both are recorded in `collectors/histdata.py`:

* **HistData's clock is Eastern Standard Time all year, with no DST.**  A fixed
  UTC-5.  The collector converts on import; if a whole summer looks shifted by
  an hour, that conversion is what to suspect first.
* **HistData M1 bars are bid-only.**  Dukascopy's are too in this store, so the
  comparison is like for like — but neither feed can supply spread from bars,
  and a mid-price source compared against either will read as a systematic
  offset of about half a spread.

This reads the Parquet store only; it writes nothing and fetches nothing.

    manage.py price_compare --symbol EURUSD
"""

from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from prices.models import MarketEvent
from prices.store import read_range

HISTDATA = "histdata"
DUKASCOPY = "dukascopy"

#: A deliberately dull hour: mid-week, mid-London-session, no scheduled tier-1
#: release, well clear of a month or quarter end.
DEFAULT_NORMAL_HOUR = datetime(2019, 6, 12, 10, 0, tzinfo=timezone.utc)

NO_HISTDATA = """Nothing to compare: the Parquet store holds no HistData bars for {symbol}.

HistData declines automated downloads — its form posts an empty token and
returns zero bytes to anything that is not a browser, which §14 Q4 anticipated.
So the zips have to be fetched by hand, one per month this comparison needs:

{urls}

Then either upload them from the Sources console, or set `import_dir` in the
histdata source's configuration to the folder you put them in and press Fetch."""


class Command(BaseCommand):
    help = "P0.5 Q4: compare HistData against Dukascopy on a normal hour and a dislocation."

    def add_arguments(self, parser):
        parser.add_argument("--symbol", default="EURUSD", help="Instrument to compare.")
        parser.add_argument(
            "--timeframe", default="m1", help="Bar size held in the store. Default m1."
        )
        parser.add_argument(
            "--normal-hour",
            default=DEFAULT_NORMAL_HOUR.strftime("%Y-%m-%dT%H"),
            help="The quiet hour, as YYYY-MM-DDTHH in UTC.",
        )

    def handle(self, *args, **options):
        symbol = options["symbol"].upper()
        timeframe = options["timeframe"]

        normal = datetime.strptime(options["normal_hour"], "%Y-%m-%dT%H").replace(
            tzinfo=timezone.utc
        )
        windows = [("normal hour", normal)]
        for event in MarketEvent.objects.filter(kind="dislocation").order_by("ts_utc"):
            if _relevant(symbol, event.label):
                windows.append((event.label, event.ts_utc))

        urls = "\n".join(
            f"  https://www.histdata.com/download-free-forex-historical-data/"
            f"?/ascii/1-minute-bar-quotes/{symbol.lower()}/{year}/{month}"
            for year, month in sorted({(w.year, w.month) for _label, w in windows})
        )
        probe = read_range(
            symbol, HISTDATA, windows[0][1], windows[0][1] + timedelta(hours=1), timeframe
        )
        if probe.empty and not any(
            not read_range(symbol, HISTDATA, when, when + timedelta(hours=1), timeframe).empty
            for _label, when in windows[1:]
        ):
            self.stdout.write(
                NO_HISTDATA.format(symbol=symbol, urls=urls)
            )
            return

        if len(windows) == 1:
            self.stdout.write(
                self.style.WARNING(
                    f"No dislocation on record touches {symbol}, so only the normal hour is "
                    "compared. That half establishes the baseline offset but says nothing "
                    "about where the feeds diverge, which is the half worth having."
                )
            )

        for label, when in windows:
            self._compare(symbol, timeframe, label, when)

    def _compare(self, symbol, timeframe, label, when):
        start = when.replace(minute=0, second=0, microsecond=0)
        end = start + timedelta(hours=1)

        theirs = read_range(symbol, HISTDATA, start, end, timeframe)
        ours = read_range(symbol, DUKASCOPY, start, end, timeframe)

        self.stdout.write("")
        self.stdout.write(
            self.style.MIGRATE_HEADING(f"{label} — {symbol} {start:%Y-%m-%d %H:%M} UTC")
        )

        if theirs.empty or ours.empty:
            missing = " and ".join(
                name
                for name, frame in ((HISTDATA, theirs), (DUKASCOPY, ours))
                if frame.empty
            )
            self.stdout.write(self.style.WARNING(f"No bars stored from {missing}."))
            return

        merged = theirs.merge(ours, on="ts_utc", suffixes=("_hist", "_duka"))
        if merged.empty:
            self.stdout.write(
                self.style.WARNING(
                    f"Both feeds have bars in this hour but none share a timestamp "
                    f"({len(theirs)} HistData, {len(ours)} Dukascopy). That is a clock "
                    "problem, not a price one — suspect HistData's fixed UTC-5 first."
                )
            )
            return

        pip = 0.01 if symbol.endswith("JPY") else 0.0001
        delta = (merged["close_hist"] - merged["close_duka"]) / pip

        self.stdout.write(
            f"{len(merged)} shared bars of {len(theirs)} HistData / {len(ours)} Dukascopy."
        )
        self.stdout.write(
            f"  close difference: mean {delta.mean():+.2f} pips, "
            f"median {delta.median():+.2f}, sd {delta.std():.2f}, "
            f"max |{delta.abs().max():.2f}|"
        )
        self.stdout.write(
            f"  range this hour: HistData "
            f"{(merged['high_hist'].max() - merged['low_hist'].min()) / pip:.1f} pips, "
            f"Dukascopy "
            f"{(merged['high_duka'].max() - merged['low_duka'].min()) / pip:.1f} pips"
        )

        if delta.abs().max() <= 1.0:
            self.stdout.write(
                self.style.SUCCESS("  Agreement within a pip — nothing to explain.")
            )
        elif abs(delta.median()) > 1.0:
            self.stdout.write(
                self.style.WARNING(
                    "  The median is offset, not just the extremes. A constant gap is a "
                    "convention difference (bid vs mid, or a clock), not disagreement "
                    "about the price."
                )
            )
        else:
            self.stdout.write(
                "  Centred but wide: the feeds agree on where price is and disagree on "
                "how far it reached. Expected in a dislocation; this spread is the error "
                "bar on any study whose window contains this hour."
            )


def _relevant(symbol: str, label: str) -> bool:
    """Does a recorded dislocation touch this pair?  Matched on the currency
    named in the label, so a new MarketEvent row needs no code change."""
    return any(
        code in label.upper() and code in symbol
        for code in ("EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "USD")
    )
