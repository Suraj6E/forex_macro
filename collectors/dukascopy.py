"""Dukascopy tick feed — planning.md §4.7, §5.2.

Gives bid **and** ask with millisecond timestamps, which is the only free way
to measure spread; HistData's M1 bars are bid-only.  It is also the
independent second opinion that makes cross-validation possible — the GBP and
JPY flash crashes show different extremes on different feeds because there was
no consolidated price (§4.6).

Bulk tick is off the table: a single month of EURUSD ticks can exceed 500 MB,
so nineteen years across seven pairs would be roughly 800 GB (§4.5).  This
collector therefore takes an explicit date range and refuses an unbounded one.

Wire format, for the next person to read this: one LZMA-compressed file per
instrument-hour, each record 20 bytes big-endian — millisecond offset into the
hour, ask, bid, ask volume, bid volume.  Prices are integers scaled by the
instrument's point size.  A zero-length body means the market was shut, which
is data, not an error.
"""

from __future__ import annotations

import io
import lzma
import struct
import time
import zipfile
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from collectors.base import (
    CollectorError,
    DataPreview,
    FetchContext,
    FetchResult,
    MissingResource,
    PriceFrame,
    TransientError,
    http_request,
    write_snapshot,
)

KEY = "dukascopy"
PARSER_VERSION = "dukascopy-ticks-1"

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
RECORD = struct.Struct(">3I2f")
RECORD_SIZE = RECORD.size

#: Requests are one per instrument-hour. A month of one pair is ~530 of them,
#: so the cap is a guard against an accidental "all pairs, all years" click.
MAX_REQUESTS = 2400
DEFAULT_DELAY_SECONDS = 0.05

#: The feed answers 503 for hours it does not want to serve right now — a
#: throttle, not a gap. Aborting a 500-request sweep on one of them wastes the
#: other 499, so back off and try again; only give up after that.
DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


def point_scale(symbol: str) -> float:
    """JPY-quoted pairs are quoted to three decimals, the rest to five."""
    return 1_000.0 if symbol.upper().endswith("JPY") else 100_000.0


def hour_url(symbol: str, when: datetime) -> str:
    # Dukascopy months are zero-based in the path. Getting this wrong returns
    # a valid file for the wrong month, which is worse than a 404.
    return (
        f"{BASE_URL}/{symbol.upper()}/{when.year:04d}/{when.month - 1:02d}/"
        f"{when.day:02d}/{when.hour:02d}h_ticks.bi5"
    )


def decode_ticks(payload: bytes, hour_start: datetime, scale: float) -> list[tuple]:
    """Decompress and unpack one hour of ticks."""
    if not payload:
        return []
    try:
        raw = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(payload)
    except lzma.LZMAError:
        try:
            raw = lzma.decompress(payload, format=lzma.FORMAT_AUTO)
        except lzma.LZMAError as exc:
            raise CollectorError(f"Undecodable tick payload: {exc}") from exc

    out = []
    for offset in range(0, len(raw) - RECORD_SIZE + 1, RECORD_SIZE):
        ms, ask_i, bid_i, ask_vol, bid_vol = RECORD.unpack_from(raw, offset)
        out.append(
            (
                hour_start + timedelta(milliseconds=ms),
                ask_i / scale,
                bid_i / scale,
                float(ask_vol),
                float(bid_vol),
            )
        )
    return out


def ticks_to_m1(ticks: list[tuple]) -> pd.DataFrame:
    """Aggregate ticks to one-minute bars on the bid, carrying mean spread."""
    if not ticks:
        return pd.DataFrame(
            columns=["ts_utc", "open", "high", "low", "close", "volume", "spread_mean", "tick_count"]
        )

    frame = pd.DataFrame(ticks, columns=["ts", "ask", "bid", "ask_vol", "bid_vol"])
    frame["spread"] = frame["ask"] - frame["bid"]
    frame["vol"] = frame["ask_vol"] + frame["bid_vol"]

    grouped = frame.set_index("ts").resample("1min")
    bars = pd.DataFrame(
        {
            "open": grouped["bid"].first(),
            "high": grouped["bid"].max(),
            "low": grouped["bid"].min(),
            "close": grouped["bid"].last(),
            "volume": grouped["vol"].sum(),
            "spread_mean": grouped["spread"].mean(),
            "tick_count": grouped["bid"].count(),
        }
    )
    bars = bars.dropna(subset=["open"]).reset_index().rename(columns={"ts": "ts_utc"})
    return bars


def _get_hour(ctx: FetchContext, url: str, retries: int) -> bytes | None:
    """Bytes for one hour, or None if the feed will not serve it.

    404 means the hour does not exist.  503 means "not now" — retried with
    backoff, and only treated as unavailable once the retries are spent.
    """
    for attempt in range(retries + 1):
        try:
            return http_request(ctx, url, missing_statuses=(404,)).content
        except MissingResource:
            return None
        except TransientError:
            if attempt == retries:
                ctx.log(f"unavailable after {retries + 1} attempts: {url}")
                return None
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None


def _resolve_window(ctx: FetchContext) -> tuple[date, date]:
    start, end = ctx.date_from, ctx.date_to
    if not start or not end:
        raise CollectorError(
            "Dukascopy needs an explicit date range. Bulk tick download is off "
            "the table — nineteen years across seven pairs is roughly 800 GB, "
            "so ticks are fetched for event windows only."
        )
    if end < start:
        raise CollectorError("The end date is before the start date.")
    return start, end


def fetch(ctx: FetchContext) -> FetchResult:
    start, end = _resolve_window(ctx)
    symbols = ctx.symbols
    if not symbols:
        raise CollectorError("Select at least one instrument.")

    days = (end - start).days + 1
    planned = days * 24 * len(symbols)
    if planned > MAX_REQUESTS:
        raise CollectorError(
            f"That range is {planned} hourly requests ({days} days × "
            f"{len(symbols)} instruments). The cap is {MAX_REQUESTS}. Narrow "
            f"the range or fetch one instrument at a time."
        )

    delay = float(ctx.config.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    retries = int(ctx.config.get("max_retries", DEFAULT_RETRIES))
    snapshots = []
    frames: list[PriceFrame] = []
    preview_rows: list[list] = []
    total_ticks = 0
    total_bars = 0
    empty_hours = 0
    missing_hours = 0
    done = 0

    for symbol in symbols:
        scale = point_scale(symbol)
        by_month: dict[date, list[pd.DataFrame]] = {}

        day = start
        while day <= end:
            hour_payloads: dict[str, bytes] = {}
            day_ticks: list[tuple] = []

            for hour in range(24):
                hour_start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
                url = hour_url(symbol, hour_start)
                payload = _get_hour(ctx, url, retries)

                if payload is None:
                    missing_hours += 1
                    payload = b""
                elif payload:
                    hour_payloads[f"{hour:02d}h_ticks.bi5"] = payload
                else:
                    empty_hours += 1

                if payload:
                    day_ticks.extend(decode_ticks(payload, hour_start, scale))

                done += 1
                if done % 12 == 0 or done == planned:
                    ctx.progress(
                        done / planned,
                        f"{symbol} {day:%Y-%m-%d} — {total_ticks + len(day_ticks):,} ticks",
                    )
                if delay:
                    time.sleep(delay)

            if hour_payloads:
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                    for name, payload in sorted(hour_payloads.items()):
                        archive.writestr(name, payload)
                snapshots.append(
                    write_snapshot(
                        ctx,
                        buffer.getvalue(),
                        name=f"{symbol}-{day:%Y%m%d}-ticks.zip",
                        url=hour_url(symbol, datetime(day.year, day.month, day.day, tzinfo=timezone.utc)),
                        http_status=200,
                        content_type="application/zip",
                    )
                )

            total_ticks += len(day_ticks)
            bars = ticks_to_m1(day_ticks)
            if not bars.empty:
                total_bars += len(bars)
                by_month.setdefault(date(day.year, day.month, 1), []).append(bars)
                if len(preview_rows) < 25:
                    for record in bars.head(25 - len(preview_rows)).itertuples(index=False):
                        preview_rows.append(
                            [
                                symbol,
                                pd.Timestamp(record.ts_utc).strftime("%Y-%m-%d %H:%M"),
                                round(float(record.open), 5),
                                round(float(record.high), 5),
                                round(float(record.low), 5),
                                round(float(record.close), 5),
                                round(float(record.volume), 2),
                                round(float(record.spread_mean), 6),
                                int(record.tick_count),
                            ]
                        )

            day += timedelta(days=1)

        for month, chunks in by_month.items():
            frames.append(
                PriceFrame(
                    symbol=symbol,
                    month=month,
                    frame=pd.concat(chunks, ignore_index=True),
                )
            )

    notes = (
        f"{total_ticks:,} ticks over {days} day(s) × {len(symbols)} instrument(s) "
        f"→ {total_bars:,} M1 bars. {empty_hours} hours empty (market shut), "
        f"{missing_hours} hours the feed would not serve."
    )
    if missing_hours > planned * 0.25:
        notes += (
            " That is a large share — the feed is likely throttling rather than "
            "missing data. Re-run the same range: hours already stored are "
            "content-addressed, so nothing is downloaded twice."
        )
    ctx.log(notes)

    return FetchResult(
        snapshots=snapshots,
        price_frames=frames,
        preview=DataPreview(
            columns=[
                "symbol", "minute (UTC)", "open", "high", "low", "close",
                "volume", "mean spread", "ticks",
            ],
            rows=preview_rows,
            caption="Bid-side OHLC aggregated from ticks. Mean spread is the "
            "ask−bid average over the minute — the number that says how much of "
            "a measured move was actually capturable.",
            total=total_bars,
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )


# No offline re-parse is registered for Dukascopy: its snapshots feed the price
# pipeline rather than the calendar merge, and re-fetching a range is already
# cheap because payloads are content-addressed and never written twice.
