"""Dukascopy — planning.md §4.7, §5.2.

Gives bid **and** ask with millisecond timestamps, which is the only free way
to measure spread; HistData's M1 bars are bid-only. It is also the independent
second opinion that makes cross-validation possible — the GBP and JPY flash
crashes show different extremes on different feeds because there was no
consolidated price (§4.6).

**Two endpoints, and choosing the right one is the whole design.**

* *Ticks* — one LZMA file per instrument-**hour**. Twenty years of seven pairs
  is ~1.2 million requests and roughly 800 GB (§4.5), so ticks are for event
  windows only, never for bulk.
* *Candles* — pre-aggregated, one file per instrument-**month** at hourly
  resolution. The same twenty years is ~1,600 requests. That is what makes an
  hourly backbone practical at all, and it is what `timeframe="h1"` uses.

Candle wire format, decoded empirically rather than assumed: 24 bytes per
record, big-endian, `>5if` — seconds from the start of the file's period, then
**open, close, low, high** as integers scaled by the instrument's point size,
then volume as a float. The field order matters and is easy to get backwards:
reading it as open/high/low/close puts the high below the close on most bars,
which is how you know it is wrong.

Closed hours are padded with a flat zero-volume record carrying the last known
price. Those are not bars and are dropped.
"""

from __future__ import annotations

import io
import lzma
import struct
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
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
PARSER_VERSION = "dukascopy-2"

BASE_URL = "https://datafeed.dukascopy.com/datafeed"

TICK_RECORD = struct.Struct(">3I2f")
CANDLE_RECORD = struct.Struct(">5if")

#: Bar length in seconds for each supported timeframe, and the path fragment
#: Dukascopy uses for it.
CANDLE_ENDPOINTS = {
    "h1": ("candles_hour_1", 3600),
    "d1": ("candles_day_1", 86400),
}

MAX_MONTHS = 1200

#: Measured, not guessed: a candle file takes ~7s to arrive and pacing the
#: requests further apart made the total *worse*, so the constraint is latency
#: rather than a rate limit. Hence a small delay and a few connections at once.
DEFAULT_DELAY_SECONDS = 0.2
DEFAULT_WORKERS = 6
DEFAULT_RETRIES = 4
RETRY_BACKOFF_SECONDS = 3.0

#: Ticks stay capped hard — the endpoint is per-hour and the volume is the
#: reason §4.5 rules out bulk tick entirely.
MAX_TICK_REQUESTS = 2400


def point_scale(symbol: str) -> float:
    """JPY-quoted pairs are quoted to three decimals, the rest to five."""
    return 1_000.0 if symbol.upper().endswith("JPY") else 100_000.0


def candle_url(symbol: str, month: date, timeframe: str, side: str = "BID") -> str:
    fragment, _seconds = CANDLE_ENDPOINTS[timeframe]
    if timeframe == "d1":
        return f"{BASE_URL}/{symbol.upper()}/{month.year:04d}/{side}_{fragment}.bi5"
    # Dukascopy months are zero-based in the path. Getting this wrong returns a
    # valid file for the wrong month, which is worse than a 404.
    return (
        f"{BASE_URL}/{symbol.upper()}/{month.year:04d}/{month.month - 1:02d}/"
        f"{side}_{fragment}.bi5"
    )


def hour_url(symbol: str, when: datetime) -> str:
    return (
        f"{BASE_URL}/{symbol.upper()}/{when.year:04d}/{when.month - 1:02d}/"
        f"{when.day:02d}/{when.hour:02d}h_ticks.bi5"
    )


def _decompress(payload: bytes) -> bytes:
    try:
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(payload)
    except lzma.LZMAError:
        try:
            return lzma.decompress(payload, format=lzma.FORMAT_AUTO)
        except lzma.LZMAError as exc:
            raise CollectorError(f"Undecodable payload: {exc}") from exc


def decode_candles(
    payload: bytes, period_start: datetime, scale: float, *, drop_padding: bool = True
) -> list[tuple]:
    """(timestamp, open, high, low, close, volume) from one candle file."""
    if not payload:
        return []
    raw = _decompress(payload)

    out = []
    for offset in range(0, len(raw) - CANDLE_RECORD.size + 1, CANDLE_RECORD.size):
        seconds, open_i, close_i, low_i, high_i, volume = CANDLE_RECORD.unpack_from(raw, offset)
        # A flat bar with no volume is Dukascopy carrying the last price across
        # a closed hour. It is padding, not a quote.
        if drop_padding and volume == 0 and open_i == close_i == low_i == high_i:
            continue
        out.append(
            (
                period_start + timedelta(seconds=seconds),
                open_i / scale,
                high_i / scale,
                low_i / scale,
                close_i / scale,
                float(volume),
            )
        )
    return out


def decode_ticks(payload: bytes, hour_start: datetime, scale: float) -> list[tuple]:
    if not payload:
        return []
    raw = _decompress(payload)
    out = []
    for offset in range(0, len(raw) - TICK_RECORD.size + 1, TICK_RECORD.size):
        ms, ask_i, bid_i, ask_vol, bid_vol = TICK_RECORD.unpack_from(raw, offset)
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
    columns = ["ts_utc", "open", "high", "low", "close", "volume", "spread_mean", "tick_count"]
    if not ticks:
        return pd.DataFrame(columns=columns)

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
    return bars.dropna(subset=["open"]).reset_index().rename(columns={"ts": "ts_utc"})


def candles_to_frame(rows: list[tuple]) -> pd.DataFrame:
    columns = ["ts_utc", "open", "high", "low", "close", "volume", "spread_mean", "tick_count"]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(
        rows, columns=["ts_utc", "open", "high", "low", "close", "volume"]
    )
    # Bid candles carry no ask, so there is no spread to record. HistData has
    # the same limit (§4.7); only the tick endpoint can give spread.
    frame["spread_mean"] = pd.NA
    frame["tick_count"] = pd.NA
    return frame[columns]


def _months(start: date, end: date) -> list[date]:
    out = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    while cursor <= last:
        out.append(cursor)
        cursor = (
            date(cursor.year + 1, 1, 1)
            if cursor.month == 12
            else date(cursor.year, cursor.month + 1, 1)
        )
    return out


#: Why the feed would not serve a period. The two are not interchangeable and
#: collapsing them is how a caller ends up advising a retry that can never
#: succeed: MISSING is permanent until Dukascopy publishes the file — the
#: current month has no monthly candle file while it is still running —
#: whereas THROTTLED is the feed saying "not now" and is worth re-running.
MISSING = "missing"
THROTTLED = "throttled"


def _get(
    ctx: FetchContext, url: str, retries: int, delay: float
) -> tuple[bytes | None, str | None]:
    """`(payload, None)`, or `(None, reason)` when the feed will not serve it.

    404 means it does not exist. 503 means "not now" — this feed throttles
    bursts aggressively, so that is retried with growing backoff rather than
    treated as a gap, and only counted as unavailable once the retries run out.
    The reason is returned rather than swallowed so the caller can say which
    happened instead of guessing.
    """
    for attempt in range(retries + 1):
        try:
            return http_request(ctx, url, missing_statuses=(404,)).content, None
        except MissingResource:
            return None, MISSING
        except TransientError:
            if attempt == retries:
                ctx.log(f"unavailable after {retries + 1} attempts: {url}")
                return None, THROTTLED
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1) + delay)
    return None, THROTTLED


def fetch(ctx: FetchContext) -> FetchResult:
    timeframe = (ctx.params.get("timeframe") or "h1").lower()
    if timeframe == "m1":
        return _fetch_ticks(ctx)
    if timeframe not in CANDLE_ENDPOINTS:
        raise CollectorError(
            f"Unsupported timeframe {timeframe!r}. Available: "
            f"{', '.join(sorted(CANDLE_ENDPOINTS))}, or m1 for tick-derived minutes."
        )
    return _fetch_candles(ctx, timeframe)


def _fetch_candles(ctx: FetchContext, timeframe: str) -> FetchResult:
    symbols = ctx.symbols
    if not symbols:
        raise CollectorError("Select at least one instrument.")
    start, end = ctx.date_from, ctx.date_to
    if not start or not end:
        raise CollectorError("Pick a date range.")
    if end < start:
        raise CollectorError("The end date is before the start date.")

    months = _months(start, end)
    planned = len(months) * len(symbols)
    if planned > MAX_MONTHS:
        raise CollectorError(
            f"That is {planned} monthly files, over the {MAX_MONTHS} cap. "
            f"Fetch fewer instruments or a shorter span."
        )

    delay = float(ctx.config.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    retries = int(ctx.config.get("max_retries", DEFAULT_RETRIES))
    already = set(ctx.params.get("skip_months") or [])

    workers = max(1, int(ctx.config.get("workers", DEFAULT_WORKERS)))

    targets = [
        (symbol, month)
        for symbol in symbols
        for month in months
        if f"{symbol}:{month:%Y-%m}" not in already
    ]
    skipped = planned - len(targets)

    snapshots = []
    frames: list[PriceFrame] = []
    preview_rows: list[list] = []
    total_bars = 0
    unavailable: list[str] = []
    reasons: dict[str, int] = {MISSING: 0, THROTTLED: 0}
    done = 0

    # These downloads take seconds each and are entirely independent, so the
    # wall clock is latency, not bandwidth or CPU. A handful of connections in
    # flight turns hours into minutes; the cap keeps it modest, because §5.4's
    # politeness is self-interested and a blocked feed is a broken tool.
    def download(target):
        symbol, month = target
        url = candle_url(symbol, month, timeframe)
        payload, reason = _get(ctx, url, retries, delay)
        return target, url, payload, reason

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (symbol, month), url, payload, reason in pool.map(download, targets):
            done += 1
            key = f"{symbol}:{month:%Y-%m}"

            if payload is None:
                unavailable.append(key)
                if reason in reasons:
                    reasons[reason] += 1
            elif payload:
                snapshots.append(
                    write_snapshot(
                        ctx,
                        payload,
                        name=f"{symbol}-{month:%Y%m}-{timeframe}.bi5",
                        url=url,
                        http_status=200,
                        content_type="application/octet-stream",
                    )
                )
                period_start = datetime(month.year, month.month, 1, tzinfo=timezone.utc)
                frame = candles_to_frame(
                    decode_candles(payload, period_start, point_scale(symbol))
                )
                if not frame.empty:
                    total_bars += len(frame)
                    frames.append(PriceFrame(symbol=symbol, month=month, frame=frame))
                    if len(preview_rows) < 25:
                        for record in frame.head(25 - len(preview_rows)).itertuples(index=False):
                            preview_rows.append(
                                [
                                    symbol,
                                    pd.Timestamp(record.ts_utc).strftime("%Y-%m-%d %H:%M"),
                                    round(float(record.open), 5),
                                    round(float(record.high), 5),
                                    round(float(record.low), 5),
                                    round(float(record.close), 5),
                                    round(float(record.volume), 2),
                                ]
                            )

            if done % 5 == 0 or done == len(targets):
                ctx.progress(done / max(len(targets), 1), f"{key} — {total_bars:,} bars")

    notes = (
        f"{total_bars:,} {timeframe} bars over {len(months)} month(s) × "
        f"{len(symbols)} instrument(s). {skipped} month(s) already stored and "
        f"skipped, {len(unavailable)} the feed would not serve."
    )
    if unavailable:
        notes += f" Unavailable: {', '.join(unavailable[:6])}"
        if len(unavailable) > 6:
            notes += f" (+{len(unavailable) - 6} more)"
    ctx.log(notes)

    if not frames and not skipped:
        # Which of the two it was decides whether re-running is sensible at
        # all, so say it rather than assuming the throttle.
        if reasons[THROTTLED] and not reasons[MISSING]:
            raise CollectorError(
                "Nothing was returned. This feed throttles bursts — every request "
                "came back 503 or timed out. Wait a few minutes and re-run: months "
                "already stored are skipped, so a resumed fetch picks up where this "
                "one stopped."
            )
        if reasons[MISSING] and not reasons[THROTTLED]:
            raise CollectorError(
                f"The feed has no {timeframe} file for any of these periods "
                f"({reasons[MISSING]} requested, all 404). Dukascopy publishes a "
                "monthly candle file once the month is over, so the month in "
                "progress is never available this way — re-running will not change "
                "that. Use m1 (tick-derived) for the current month, or wait until "
                "the month closes."
            )
        raise CollectorError(
            f"Nothing was returned: {reasons[MISSING]} period(s) the feed does not "
            f"have (404) and {reasons[THROTTLED]} it declined to serve (503 or "
            "timeout). Re-running can recover the second group but not the first."
        )

    return FetchResult(
        snapshots=snapshots,
        price_frames=frames,
        preview=DataPreview(
            columns=["symbol", "bar start (UTC)", "open", "high", "low", "close", "volume"],
            rows=preview_rows,
            caption="Bid-side OHLC from Dukascopy's pre-aggregated candles. "
            "Flat zero-volume bars marking closed hours have been dropped. "
            "There is no spread column: bid candles carry no ask — only the "
            "tick endpoint can give spread.",
            total=total_bars,
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )


def _fetch_ticks(ctx: FetchContext) -> FetchResult:
    """Minute bars built from ticks — one request per instrument-hour.

    Kept deliberately expensive to reach: §4.5 rules out bulk tick, so this is
    for event windows.
    """
    symbols = ctx.symbols
    start, end = ctx.date_from, ctx.date_to
    if not symbols:
        raise CollectorError("Select at least one instrument.")
    if not start or not end:
        raise CollectorError(
            "Minute bars come from the tick endpoint, which is one request per "
            "hour. An explicit date range is required."
        )

    days = (end - start).days + 1
    planned = days * 24 * len(symbols)
    if planned > MAX_TICK_REQUESTS:
        raise CollectorError(
            f"That range is {planned} hourly tick requests ({days} days × "
            f"{len(symbols)} instruments), over the {MAX_TICK_REQUESTS} cap. "
            f"Ticks are for event windows — use the hourly timeframe for a span "
            f"like this."
        )

    delay = float(ctx.config.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    retries = int(ctx.config.get("max_retries", DEFAULT_RETRIES))

    snapshots = []
    frames: list[PriceFrame] = []
    preview_rows: list[list] = []
    total_ticks = total_bars = empty_hours = missing_hours = done = 0

    for symbol in symbols:
        scale = point_scale(symbol)
        by_month: dict[date, list[pd.DataFrame]] = {}
        day = start
        while day <= end:
            hour_payloads: dict[str, bytes] = {}
            day_ticks: list[tuple] = []
            for hour in range(24):
                hour_start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
                payload = _get(ctx, hour_url(symbol, hour_start), retries, delay)
                if payload is None:
                    missing_hours += 1
                elif payload:
                    hour_payloads[f"{hour:02d}h_ticks.bi5"] = payload
                    day_ticks.extend(decode_ticks(payload, hour_start, scale))
                else:
                    empty_hours += 1
                done += 1
                if done % 12 == 0 or done == planned:
                    ctx.progress(done / planned, f"{symbol} {day:%Y-%m-%d}")
                if delay:
                    time.sleep(delay)

            if hour_payloads:
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
                    for name, payload in sorted(hour_payloads.items()):
                        archive.writestr(name, payload)
                snapshots.append(
                    write_snapshot(
                        ctx, buffer.getvalue(),
                        name=f"{symbol}-{day:%Y%m%d}-ticks.zip",
                        url=hour_url(symbol, datetime(day.year, day.month, day.day, tzinfo=timezone.utc)),
                        http_status=200, content_type="application/zip",
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
                                round(float(record.open), 5), round(float(record.high), 5),
                                round(float(record.low), 5), round(float(record.close), 5),
                                round(float(record.volume), 2),
                                round(float(record.spread_mean), 6),
                            ]
                        )
            day += timedelta(days=1)

        for month, chunks in by_month.items():
            frames.append(
                PriceFrame(symbol=symbol, month=month, frame=pd.concat(chunks, ignore_index=True))
            )

    notes = (
        f"{total_ticks:,} ticks → {total_bars:,} M1 bars. {empty_hours} hour(s) "
        f"empty (market shut), {missing_hours} the feed would not serve."
    )
    ctx.log(notes)

    return FetchResult(
        snapshots=snapshots,
        price_frames=frames,
        preview=DataPreview(
            columns=["symbol", "minute (UTC)", "open", "high", "low", "close", "volume", "mean spread"],
            rows=preview_rows,
            caption="Bid-side OHLC aggregated from ticks. Mean spread is the "
            "ask−bid average over the minute — the number that says how much of "
            "a measured move was actually capturable.",
            total=total_bars,
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )


# No offline re-parse: Dukascopy snapshots feed the price pipeline rather than
# the calendar merge, and re-fetching is cheap because payloads are
# content-addressed and months already stored are skipped.
