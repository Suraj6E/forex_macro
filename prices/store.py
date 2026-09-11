"""M1 bar storage — planning.md §4.5, §8.

Bars live in Parquet under `data/parquet/<symbol>/<source>/<YYYY-MM>.parquet`,
partitioned by instrument and month.  ~50M bars across 19 years is well under
a gigabyte compressed and DuckDB scans it comfortably; the same volume in
SQLite would be unpleasant for repeated analytical scans, so the split is by
workload rather than by preference.

SQLite keeps only the coverage ledger: which months exist, from which source,
and how complete each is.  Gaps are the thing that breaks long horizons — a
`-5d … +1M` window needs unbroken bars across weekends and holidays.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from django.conf import settings

from analytics.timeutils import expected_minutes
from prices.models import Instrument, PriceCoverage

#: One row per minute.  `spread_mean` and `tick_count` are null for bar-only
#: sources: HistData's M1 bars are bid-only and cannot give spread (§4.7).
BAR_COLUMNS = [
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "spread_mean",
    "tick_count",
]


def month_start(value: date | datetime) -> date:
    return date(value.year, value.month, 1)


def relative_path(symbol: str, source_key: str, month: date) -> str:
    return f"{symbol}/{source_key}/{month:%Y-%m}.parquet"


def absolute_path(symbol: str, source_key: str, month: date) -> Path:
    return settings.PARQUET_DIR / relative_path(symbol, source_key, month)


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="float64") for c in BAR_COLUMNS}).astype(
        {"ts_utc": "datetime64[ns, UTC]"}
    )


def normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce a collector's frame into the canonical bar schema.

    Timestamps must already be timezone-aware UTC — nothing downstream ever
    sees a naive datetime (§4.1), and silently localising here is how a source
    ends up an hour out for half its history.
    """
    out = frame.copy()
    for column in BAR_COLUMNS:
        if column not in out.columns:
            out[column] = pd.NA

    if out["ts_utc"].dt.tz is None:
        raise ValueError(
            "Bar timestamps are naive. The collector must convert with the "
            "source's own clock rule before handing frames over (§4.1)."
        )
    out["ts_utc"] = out["ts_utc"].dt.tz_convert("UTC")
    out = out[BAR_COLUMNS].sort_values("ts_utc").drop_duplicates("ts_utc", keep="last")
    return out.reset_index(drop=True)


def write_month(
    instrument: Instrument,
    source,
    frame: pd.DataFrame,
    month: date,
    *,
    fetch_run=None,
    gap_count: int | None = None,
) -> PriceCoverage:
    """Write (or merge into) one instrument-month and update the ledger.

    Re-fetching a month is idempotent: existing bars are merged with the new
    ones on timestamp, last write winning, so a partial day fetched twice does
    not duplicate and a re-fetch that corrects a bar corrects it.
    """
    month = month_start(month)
    frame = normalise_frame(frame)

    path = absolute_path(instrument.symbol, source.key, month)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = pd.read_parquet(path)
        frame = normalise_frame(pd.concat([existing, frame], ignore_index=True))

    frame.to_parquet(path, index=False, compression="zstd")

    expected = expected_minutes(month)
    bar_count = len(frame)
    if gap_count is None:
        gap_count = max(expected - bar_count, 0)

    coverage, _ = PriceCoverage.objects.update_or_create(
        instrument=instrument,
        source=source,
        month=month,
        defaults={
            "bar_count": bar_count,
            "expected_bar_count": expected,
            "gap_count": gap_count,
            "first_ts_utc": _as_utc(frame["ts_utc"].iloc[0]) if bar_count else None,
            "last_ts_utc": _as_utc(frame["ts_utc"].iloc[-1]) if bar_count else None,
            "parquet_path": relative_path(instrument.symbol, source.key, month),
            "fetch_run": fetch_run,
        },
    )
    return coverage


def read_month(symbol: str, source_key: str, month: date) -> pd.DataFrame:
    path = absolute_path(symbol, source_key, month_start(month))
    if not path.exists():
        return empty_frame()
    return pd.read_parquet(path)


def read_range(symbol: str, source_key: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Bars between two instants, stitched across month files."""
    frames = []
    cursor = month_start(start)
    last = month_start(end)
    while cursor <= last:
        frames.append(read_month(symbol, source_key, cursor))
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    if not frames:
        return empty_frame()
    out = pd.concat(frames, ignore_index=True)
    return out[(out["ts_utc"] >= start) & (out["ts_utc"] <= end)].reset_index(drop=True)


def _as_utc(value) -> datetime:
    stamp = pd.Timestamp(value)
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.to_pydatetime().astimezone(timezone.utc)


def frame_preview_rows(frame: pd.DataFrame, limit: int) -> list[list]:
    """Display-ready rows for the console preview."""
    rows = []
    for record in frame.head(limit).itertuples(index=False):
        rows.append(
            [
                pd.Timestamp(record.ts_utc).strftime("%Y-%m-%d %H:%M"),
                _round(record.open),
                _round(record.high),
                _round(record.low),
                _round(record.close),
                _round(record.volume, 2),
                _round(record.spread_mean, 6),
                None if pd.isna(record.tick_count) else int(record.tick_count),
            ]
        )
    return rows


def _round(value, digits: int = 5):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)
