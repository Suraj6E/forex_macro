"""ForexFactory historical calendar — planning.md §4.2 Route B, §5.4.

One request per calendar month, which is 228 requests for 2007→today. That is
a job that runs for a while, deliberately paced: §5.4's politeness is
self-interested, and this site has already demonstrated it will return HTTP
429 when pushed.

Every page is snapshotted before parsing. The data is embedded as a JavaScript
object rather than only rendered into a table, so the parser is far sturdier
than a DOM scrape — but it is still an unofficial interface, and when it
changes the fix must be a re-parse, not a re-crawl of nineteen years.
"""

from __future__ import annotations

from datetime import date

from collectors.base import (
    CollectorError,
    FetchContext,
    FetchResult,
    TransientError,
    http_request,
    preview_from_events,
    write_snapshot,
)
from normalisers import forexfactory_pages as normaliser

KEY = "forexfactory_pages"
PARSER_VERSION = normaliser.PARSER_VERSION

PAGE_URL = "https://www.forexfactory.com/calendar?month={month}"

#: Verified present: January 2007 returns 326 events, 273 with actuals.
EARLIEST = date(2007, 1, 1)

DEFAULT_DELAY_SECONDS = 2.5
DEFAULT_RETRIES = 2
MAX_MONTHS = 240


def month_token(month: date) -> str:
    return f"{month:%b}".lower() + f".{month.year}"


def month_range(start: date, end: date) -> list[date]:
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


def _get_month(ctx: FetchContext, month: date, retries: int):
    url = PAGE_URL.format(month=month_token(month))
    for attempt in range(retries + 1):
        try:
            return http_request(ctx, url, headers={"Accept": "text/html"})
        except TransientError as exc:
            if attempt == retries:
                raise CollectorError(
                    f"{month:%Y-%m}: {exc}. The site rate-limits; raise "
                    f"`delay_seconds` in this source's configuration or fetch a "
                    f"narrower range."
                ) from exc
            import time

            time.sleep(DEFAULT_DELAY_SECONDS * (attempt + 2))
    raise CollectorError(f"{month:%Y-%m}: unreachable")


def fetch(ctx: FetchContext) -> FetchResult:
    import time

    start = ctx.date_from or EARLIEST
    end = ctx.date_to or date.today()
    if end < start:
        raise CollectorError("The end date is before the start date.")
    if start < EARLIEST:
        ctx.log(f"clamping start to {EARLIEST} — nothing earlier is published")
        start = EARLIEST

    months = month_range(start, end)
    if len(months) > MAX_MONTHS:
        raise CollectorError(
            f"That range is {len(months)} monthly pages. The cap is {MAX_MONTHS} "
            f"per run — fetch it a few years at a time."
        )

    delay = float(ctx.config.get("delay_seconds", DEFAULT_DELAY_SECONDS))
    retries = int(ctx.config.get("max_retries", DEFAULT_RETRIES))

    snapshots = []
    rows = []
    empty_months: list[str] = []

    for index, month in enumerate(months, start=1):
        ctx.progress((index - 1) / len(months), f"{month:%Y-%m} ({index}/{len(months)})")
        response = _get_month(ctx, month, retries)

        snapshot = write_snapshot(
            ctx,
            response.content,
            name=f"calendar-{month:%Y-%m}.html",
            url=response.url,
            http_status=response.status,
            content_type=response.content_type,
            duration_ms=response.duration_ms,
        )
        snapshots.append(snapshot)

        try:
            month_rows = normaliser.parse(response.content)
        except ValueError as exc:
            raise CollectorError(f"{month:%Y-%m}: {exc} (page kept at {snapshot.path})") from exc

        if not month_rows:
            empty_months.append(f"{month:%Y-%m}")
        rows.extend(month_rows)
        ctx.log(f"{month:%Y-%m}: {len(month_rows)} in-scope events")

        if delay and index < len(months):
            time.sleep(delay)

    stats = normaliser.summarise(rows)
    stamps = [r.release_time_utc or r.scheduled_time_utc for r in rows]
    stamps = [s for s in stamps if s]

    notes = (
        f"{stats['rows']:,} in-scope events across {len(months)} month(s)"
        + (f", {min(stamps):%Y-%m-%d} → {max(stamps):%Y-%m-%d}" if stamps else "")
        + f". {stats['with_actual']:,} carry an actual, "
        f"{stats['with_forecast']:,} a forecast, "
        f"{stats['with_revision']:,} a revised previous. "
        f"{stats['time_masked']:,} have a masked release time and are graded "
        f"date-only. Forecasts are vendor-stored, never point-in-time: this is "
        f"a historical scrape, and calendar sites revise displayed forecasts."
    )
    if empty_months:
        notes += f" No events parsed for: {', '.join(empty_months[:6])}"
    ctx.log(notes)

    return FetchResult(
        snapshots=snapshots,
        rows=rows,
        preview=preview_from_events(
            rows,
            caption="Timestamps are Unix seconds from the site and track US "
            "daylight saving correctly — 08:30 New York reads 13:30 UTC in "
            "winter and 12:30 in summer. The revised-previous column is the "
            "site's restatement of the prior release, not of this one.",
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )


def reparse(content: bytes, *, captured_at=None, **_):
    return normaliser.parse(content)
