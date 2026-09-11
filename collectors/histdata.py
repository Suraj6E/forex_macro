"""HistData.com M1 bars — planning.md §4.5, §4.7, §5.2, §14 Q4.

The bulk backbone: one zip per instrument-month, 2000 onwards, roughly 1,600
files for seven pairs over nineteen years.

**This is an importer, not a downloader, and that is a measured decision.**
The site's download is a hidden HTML form posting to `get.php`.  Its token
field is empty in the markup and the form is submitted straight from a click
handler, so there is nothing to scrape; posting the form directly — with the
referer, with the page's cookies, with a browser user-agent — returns HTTP 200
and **zero bytes**.  The site declines to be automated.

§5.4 says exactly what to do about that: politeness here is self-interested,
and an HTML-shaped interface is permanently unstable.  §14 Q4 offers the
answer — accept manual file upload, because it is cheap and it saves fighting
a downloader.  So:

* **Upload** one or more monthly zips straight from the console, or
* point `import_dir` at a folder you filled with a download manager and let
  this collector sweep it.

Two facts about the data that are easy to get wrong and expensive to discover
late:

* **The clock is Eastern Standard Time with no DST adjustment**, stated in
  their own file spec: a fixed UTC−5 all year.  Using `America/New_York` would
  apply DST and shift half the history by an hour.
* **M1 bars are bid-only.**  Ask appears in their tick data, not their bar
  data, so this source can never give spread.  That is what Dukascopy is for.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from analytics.timeutils import HISTDATA_FIXED
from collectors.base import (
    CollectorError,
    DataPreview,
    FetchContext,
    FetchResult,
    PriceFrame,
    write_snapshot,
)

KEY = "histdata"
PARSER_VERSION = "histdata-m1-1"

PAGE_URL = (
    "https://www.histdata.com/download-free-forex-historical-data/"
    "?/ascii/1-minute-bar-quotes/{pair}/{year}/{month}"
)

#: `HISTDATA_COM_ASCII_EURUSD_M1_202409.zip`, and the DAT_ASCII_… form inside.
_NAME_RE = re.compile(r"([A-Z]{6})[_ ]M1[_ ](\d{4})(\d{2})", re.I)

DEFAULT_IMPORT_GLOB = "*.zip"


def parse_filename(name: str) -> tuple[str, date] | None:
    """Pull instrument and month out of a HistData archive or CSV name."""
    match = _NAME_RE.search(Path(name).name.upper())
    if not match:
        return None
    symbol, year, month = match.group(1), int(match.group(2)), int(match.group(3))
    if not 1 <= month <= 12:
        return None
    return symbol, date(year, month, 1)


def parse_csv(payload: bytes) -> pd.DataFrame:
    """`YYYYMMDD HHMMSS;open;high;low;close;volume`, stamped EST with no DST."""
    frame = pd.read_csv(
        io.BytesIO(payload),
        sep=";",
        header=None,
        names=["stamp", "open", "high", "low", "close", "volume"],
        dtype={"stamp": str},
    )
    naive = pd.to_datetime(frame["stamp"], format="%Y%m%d %H%M%S")
    frame["ts_utc"] = naive.dt.tz_localize(HISTDATA_FIXED).dt.tz_convert("UTC")
    frame["spread_mean"] = pd.NA  # bid-only bars: there is no ask to subtract
    frame["tick_count"] = pd.NA
    return frame[
        ["ts_utc", "open", "high", "low", "close", "volume", "spread_mean", "tick_count"]
    ]


def extract_csv(archive_bytes: bytes, label: str) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise CollectorError(f"{label}: archive contains no CSV")
            return archive.read(names[0])
    except zipfile.BadZipFile as exc:
        raise CollectorError(
            f"{label}: not a zip archive. If this came from the website's "
            f"download link, check it finished — a blocked download saves an "
            f"HTML error page with a .zip name."
        ) from exc


def _gather(ctx: FetchContext) -> list[tuple[str, bytes]]:
    """Uploaded files first, then the configured import directory."""
    if ctx.uploads:
        return list(ctx.uploads)

    import_dir = ctx.config.get("import_dir")
    if not import_dir:
        raise CollectorError(
            "Nothing to import. HistData declines automated downloads — its "
            "form posts an empty token and returns zero bytes to anything that "
            "is not a browser. Either upload the monthly zips here, or set "
            "`import_dir` in this source's configuration to a folder you filled "
            "yourself, and this collector will sweep it.\n\n"
            "Download pages look like: "
            + PAGE_URL.format(pair="eurusd", year=2024, month=9)
        )

    directory = Path(import_dir)
    if not directory.exists():
        raise CollectorError(f"Configured import_dir does not exist: {directory}")

    pattern = ctx.config.get("import_glob") or DEFAULT_IMPORT_GLOB
    files = sorted(directory.rglob(pattern))
    if not files:
        raise CollectorError(f"No files matching {pattern!r} under {directory}")
    return [(str(path), path.read_bytes()) for path in files]


def fetch(ctx: FetchContext) -> FetchResult:
    candidates = _gather(ctx)

    wanted_symbols = set(ctx.symbols)
    since, until = ctx.date_from, ctx.date_to

    snapshots = []
    frames: list[PriceFrame] = []
    preview_rows: list[list] = []
    total_bars = 0
    skipped: list[str] = []
    unnamed = 0

    for index, (name, payload) in enumerate(candidates, start=1):
        parsed = parse_filename(name)
        if parsed is None:
            unnamed += 1
            skipped.append(f"{Path(name).name} (unrecognised name)")
            continue
        symbol, month = parsed

        if wanted_symbols and symbol not in wanted_symbols:
            skipped.append(f"{symbol} {month:%Y-%m} (instrument not selected)")
            continue
        if since and month < date(since.year, since.month, 1):
            continue
        if until and month > date(until.year, until.month, 1):
            continue

        label = f"{symbol} {month:%Y-%m}"
        ctx.progress(index / len(candidates), f"importing {label}")

        snapshots.append(
            write_snapshot(
                ctx,
                payload,
                name=f"{symbol}-{month:%Y%m}-M1.zip",
                url=PAGE_URL.format(pair=symbol.lower(), year=month.year, month=month.month),
                content_type="application/zip",
            )
        )

        bars = parse_csv(extract_csv(payload, label))
        total_bars += len(bars)
        frames.append(PriceFrame(symbol=symbol, month=month, frame=bars))
        ctx.log(f"{label}: {len(bars):,} bars")

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
                    ]
                )

    if not frames:
        raise CollectorError(
            "Nothing imported. "
            + (
                f"{unnamed} file(s) had names this parser did not recognise — it "
                f"expects the site's own naming, e.g. "
                f"HISTDATA_COM_ASCII_EURUSD_M1_202409.zip."
                if unnamed
                else "Every candidate was filtered out by the instrument or date "
                "selection."
            )
        )

    notes = f"{total_bars:,} M1 bars from {len(frames)} instrument-month(s)."
    if skipped:
        notes += f" Skipped {len(skipped)}: {', '.join(skipped[:5])}"
        if len(skipped) > 5:
            notes += f" (+{len(skipped) - 5} more)"
    ctx.log(notes)

    return FetchResult(
        snapshots=snapshots,
        price_frames=frames,
        preview=DataPreview(
            columns=["symbol", "minute (UTC)", "open", "high", "low", "close", "volume"],
            rows=preview_rows,
            caption="Bid-only OHLC. Timestamps were stamped Eastern Standard with "
            "no daylight-saving adjustment and converted at a fixed UTC−5 — "
            "treating them as America/New_York would shift half the history by an "
            "hour. There is no spread column because these bars have no ask.",
            total=total_bars,
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )
