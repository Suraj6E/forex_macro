"""ForexFactory weekly calendar feed — planning.md §4.2 Route B, §7.3.

This is the forward-capture channel.  A missed week is permanently lost: the
point-in-time forecast can only be captured *before* the release happens, and
no amount of later crawling recovers it.

Fetch cadence is weekly and cached.  §4.2/§5.4, verified: polling this feed
hard gets you blocked quickly.
"""

from __future__ import annotations

from datetime import datetime, timezone

from collectors.base import (
    CollectorError,
    FetchContext,
    FetchResult,
    http_get,
    write_snapshot,
)
from normalisers import forexfactory as ff_normaliser

KEY = "forexfactory_weekly"
FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
PARSER_VERSION = ff_normaliser.PARSER_VERSION


def reparse(content: bytes, *, captured_at: datetime):
    """Re-run the normaliser over a stored snapshot — §5.4.

    `captured_at` must be the instant the bytes were *originally* fetched, not
    now.  It is what decides whether a forecast is point-in-time (§4.3), so
    re-parsing with the current clock would silently downgrade every forecast
    in the archive to `vendor_stored`.
    """
    return ff_normaliser.parse(content, captured_at=captured_at)


def fetch(ctx: FetchContext) -> FetchResult:
    ctx.progress(0.05, "requesting weekly feed")
    ctx.log(f"GET {FEED_URL}")

    content, status, content_type, duration_ms = http_get(ctx, FEED_URL)
    captured_at = datetime.now(timezone.utc)
    ctx.log(f"HTTP {status}, {len(content)} bytes in {duration_ms} ms")

    snapshot = write_snapshot(
        ctx,
        content,
        name="ff_calendar_thisweek.json",
        url=FEED_URL,
        http_status=status,
        content_type=content_type,
        duration_ms=duration_ms,
    )
    ctx.progress(0.5, "parsing")

    try:
        rows = ff_normaliser.parse(content, captured_at=captured_at)
    except ValueError as exc:
        # The snapshot is already on disk, so this is recoverable by fixing
        # the parser and re-parsing — no re-crawl needed (§5.4).
        raise CollectorError(f"{exc} (snapshot kept at {snapshot.path})") from exc

    point_in_time = sum(1 for r in rows if r.forecast_target == "forecast_point_in_time")
    ctx.progress(0.9, f"{len(rows)} in-scope rows")
    notes = (
        f"{len(rows)} in-scope rows; {point_in_time} captured before their "
        f"scheduled release (point-in-time forecast), "
        f"{len(rows) - point_in_time} after. Feed carries no actuals."
    )
    ctx.log(notes)

    return FetchResult(
        snapshots=[snapshot],
        rows=rows,
        notes=notes,
        parser_version=PARSER_VERSION,
    )
