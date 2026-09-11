"""MT5 calendar CSV import — planning.md §4.2 Route A.

This is the primary calendar source: it ships with MetaTrader 5, covers 900+
indicators across the 18 largest economies, and is the only free source
carrying forecast, actual, previous *and* revised-previous at depth.

There is no HTTP request here.  The Python `MetaTrader5` package exposes
prices, symbols, orders, positions and history — and **no calendar function at
all**.  The bridge is therefore an MQL5 script running inside the terminal
that writes UTF-8 CSV to `MQL5\\Files`, which this collector reads.

Two ways in, both from the console:

* **Upload** the exported CSV directly, which is the path that works when the
  terminal lives on another machine or a different user profile.
* **Point at the export directory** in the source's configuration and let it
  pick the newest matching file.

How far back your terminal's calendar reaches is genuinely unknown and
terminal-dependent — one user reports ~90,000 events back to January 2007,
another in the same thread saw only 2017 onward.  That is P0.5 question 1, and
importing the file is how you answer it: the run reports the earliest event it
found.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from collectors.base import (
    CollectorError,
    FetchContext,
    FetchResult,
    preview_from_events,
    write_snapshot,
)
from normalisers import mt5_calendar as normaliser

KEY = "mt5_calendar"
PARSER_VERSION = normaliser.PARSER_VERSION

DEFAULT_FILENAME_GLOB = "fxmacro_calendar*.csv"


def default_export_dirs() -> list[Path]:
    """Where MetaTrader keeps `MQL5\\Files` on a standard Windows install."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    terminals = Path(appdata) / "MetaQuotes" / "Terminal"
    if not terminals.exists():
        return []
    return sorted(terminals.glob("*/MQL5/Files"))


def _locate(ctx: FetchContext) -> Path:
    configured = ctx.config.get("export_dir")
    pattern = ctx.config.get("filename_glob") or DEFAULT_FILENAME_GLOB

    candidates: list[Path] = []
    if configured:
        directory = Path(configured)
        if not directory.exists():
            raise CollectorError(f"Configured export directory does not exist: {directory}")
        candidates = sorted(directory.glob(pattern))
    else:
        for directory in default_export_dirs():
            candidates.extend(directory.glob(pattern))

    if not candidates:
        searched = configured or ", ".join(str(d) for d in default_export_dirs()) or "(none found)"
        raise CollectorError(
            f"No calendar export matching {pattern!r} found in: {searched}. "
            f"Run mql5/CalendarExport.mq5 inside the terminal, or upload the "
            f"CSV from the console."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def fetch(ctx: FetchContext) -> FetchResult:
    if ctx.upload is not None:
        content = ctx.upload
        origin = f"upload:{ctx.upload_name or 'calendar.csv'}"
        ctx.log(f"reading uploaded file {ctx.upload_name} ({len(content):,} bytes)")
    else:
        path = _locate(ctx)
        content = path.read_bytes()
        origin = str(path)
        ctx.log(f"reading {path} ({len(content):,} bytes)")

    ctx.progress(0.3, "parsing export")
    snapshot = write_snapshot(
        ctx,
        content,
        name="mt5-calendar.csv",
        url=origin,
        http_status=None,
        content_type="text/csv",
    )

    try:
        rows = normaliser.parse(content)
    except ValueError as exc:
        raise CollectorError(f"{exc} (file kept at {snapshot.path})") from exc

    since = ctx.date_from
    until = ctx.date_to
    if since or until:
        before = len(rows)
        rows = [
            r
            for r in rows
            if r.release_time_utc
            and (not since or r.release_time_utc.date() >= since)
            and (not until or r.release_time_utc.date() <= until)
        ]
        ctx.log(f"date filter kept {len(rows):,} of {before:,} rows")

    ctx.progress(0.9, f"{len(rows):,} in-scope rows")

    stamps = [r.release_time_utc for r in rows if r.release_time_utc]
    earliest = min(stamps) if stamps else None
    latest = max(stamps) if stamps else None
    with_forecast = sum(1 for r in rows if r.forecast is not None)
    with_actual = sum(1 for r in rows if r.actual is not None)

    notes = (
        f"{len(rows):,} in-scope rows"
        + (f", {earliest:%Y-%m-%d} → {latest:%Y-%m-%d}" if earliest else "")
        + f". {with_actual:,} carry an actual, {with_forecast:,} carry a forecast. "
        f"Timestamps are trade-server time converted with the offset recorded at "
        f"export, so every row is marked 'inferred' until the volatility "
        f"cross-check confirms it."
    )
    ctx.log(notes)

    return FetchResult(
        snapshots=[snapshot],
        rows=rows,
        preview=preview_from_events(
            rows,
            caption="Values have been divided by 1,000,000 and LONG_MIN has been "
            "read as null. If a number here looks six orders of magnitude wrong, "
            "or you see −9.2×10¹⁸, the scaling is the first thing to check.",
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )


def reparse(content: bytes, *, captured_at: datetime | None = None, **_):
    return normaliser.parse(content)
