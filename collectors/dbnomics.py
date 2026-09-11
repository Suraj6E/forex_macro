"""DBnomics — the actuals backbone (planning.md §4.2 Route D).

    Premise: we need actuals for 8 economies, and writing 13 separate agency
    scrapers is a large, permanently-maintained surface.
    Premise: DBnomics already aggregates those agencies behind one API,
    without altering values.
    Inference: one integration replaces most of that scraper surface.
    Conclusion: include as the actuals backbone; keep direct agency
    integrations for the tier-1 US releases where release timestamps matter.

Which series to pull is **configuration, not code** — it lives in the source's
config on the console, so adding a country or an indicator is an edit, not a
deployment.  Series that do not resolve are reported by name rather than
silently dropped.
"""

from __future__ import annotations

from datetime import date

from collectors.base import (
    CollectorError,
    FetchContext,
    FetchResult,
    http_request,
    preview_from_events,
    write_snapshot,
)
from normalisers import dbnomics as normaliser

KEY = "dbnomics"
PARSER_VERSION = normaliser.PARSER_VERSION

API_URL = "https://api.db.nomics.world/v22/series"

#: The dataset starts in 2007 (locked decision 3), so that is the default
#: floor. Anything earlier is noise for this project.
DEFAULT_SINCE = date(2007, 1, 1)

#: A starting set, editable on the source's config screen. These are examples
#: of use, not the definition of the product — the point is that the list is a
#: parameter.
DEFAULT_SERIES = [
    {"id": "BLS/cu/CUSR0000SA0", "currency": "USD", "name": "US CPI-U, all items (SA)", "importance": 3},
    {"id": "BLS/ln/LNS14000000", "currency": "USD", "name": "US unemployment rate", "importance": 3},
    {"id": "Eurostat/prc_hicp_manr/M.RCH_A.CP00.EA", "currency": "EUR", "name": "Euro area HICP, annual rate", "importance": 3},
    {"id": "Eurostat/une_rt_m/M.SA.TOTAL.PC_ACT.T.EA20", "currency": "EUR", "name": "Euro area unemployment rate", "importance": 2},
]

MAX_SERIES_PER_REQUEST = 20


def series_config(ctx: FetchContext) -> dict[str, dict]:
    configured = ctx.config.get("series") or DEFAULT_SERIES
    out: dict[str, dict] = {}
    for entry in configured:
        if isinstance(entry, str):
            entry = {"id": entry}
        series_id = (entry.get("id") or "").strip()
        if series_id:
            out[series_id] = entry
    return out


def fetch(ctx: FetchContext) -> FetchResult:
    config = series_config(ctx)
    if not config:
        raise CollectorError(
            "No series configured. Add them on the source's configuration "
            "screen — which series to pull is a setting, not code."
        )

    since = ctx.date_from or DEFAULT_SINCE
    until = ctx.date_to

    ids = list(config)
    snapshots = []
    rows = []
    found: list[str] = []
    missing: list[str] = []

    batches = [
        ids[i : i + MAX_SERIES_PER_REQUEST]
        for i in range(0, len(ids), MAX_SERIES_PER_REQUEST)
    ]
    for index, batch in enumerate(batches, start=1):
        ctx.progress(
            (index - 1) / len(batches), f"requesting {len(batch)} series ({index}/{len(batches)})"
        )
        response = http_request(
            ctx,
            API_URL,
            params={"series_ids": ",".join(batch), "observations": "1"},
        )
        snapshots.append(
            write_snapshot(
                ctx,
                response.content,
                name=f"series-batch-{index}.json",
                url=response.url,
                http_status=response.status,
                content_type=response.content_type,
                duration_ms=response.duration_ms,
            )
        )

        batch_config = {sid: config[sid] for sid in batch}
        try:
            batch_rows, batch_found, batch_missing = normaliser.parse(
                response.content, series_config=batch_config, since=since, until=until
            )
        except ValueError as exc:
            raise CollectorError(f"{exc} (payload kept at {snapshots[-1].path})") from exc

        rows.extend(batch_rows)
        found.extend(batch_found)
        missing.extend(batch_missing)

    notes = (
        f"{len(rows):,} observations from {len(found)}/{len(ids)} series, "
        f"reference periods from {since:%Y-%m-%d}"
        + (f" to {until:%Y-%m-%d}" if until else "")
        + ". Values are current, not first prints — no release timestamps."
    )
    if missing:
        notes += f" Unresolved series: {', '.join(missing)}"
        ctx.log(f"unresolved: {', '.join(missing)}")
    ctx.log(notes)

    return FetchResult(
        snapshots=snapshots,
        rows=rows,
        preview=preview_from_events(
            rows,
            caption="Actuals indexed by reference period. There is deliberately "
            "no scheduled time: this source knows what the number was, not when "
            "it was published, so it validates and fills `actual` and can never "
            "anchor an event study by itself.",
        ),
        notes=notes,
        parser_version=PARSER_VERSION,
    )


def reparse(content: bytes, *, captured_at=None, config=None, **_):
    rows, _found, _missing = normaliser.parse(
        content, series_config=_config_map(config), since=None, until=None
    )
    return rows


def _config_map(config) -> dict[str, dict]:
    entries = (config or {}).get("series") or DEFAULT_SERIES
    out = {}
    for entry in entries:
        if isinstance(entry, str):
            entry = {"id": entry}
        if entry.get("id"):
            out[entry["id"]] = entry
    return out
