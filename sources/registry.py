"""Which collector serves which source row — planning.md §5.

"Each of these is a row in a `source` table with its own fetch button, policy
and health — not a hardcoded script."  The row is the configuration; this
module is the only place that knows which callable implements it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from collectors import dbnomics as dbn
from collectors import dukascopy as duka
from collectors import forexfactory as ff
from collectors import forexfactory_pages as ffp
from collectors import histdata as hd
from collectors import mt5_calendar as mt5
from collectors.base import FetchContext, FetchResult


@dataclass(frozen=True)
class RegisteredCollector:
    key: str
    fetch: Callable[[FetchContext], FetchResult]
    parser_version: str

    #: Re-run the normaliser over stored bytes, given their original capture
    #: instant. None means this source cannot be re-parsed offline — true of
    #: the price feeds, whose snapshots go to the Parquet store rather than
    #: through the calendar merge.
    reparse: Callable[..., list] | None = None

    #: Accepts a file uploaded from the console instead of a network fetch.
    accepts_upload: bool = False

    #: Accepts many files at once — HistData is ~1,600 monthly zips.
    multi_upload: bool = False

    #: File types the upload control should offer.
    upload_accept: str = ".csv,.zip"

    #: Needs an instrument selection on the fetch form.
    needs_symbols: bool = False

    #: Needs an explicit date range; the form marks it required.
    requires_date_range: bool = False

    #: Bar sizes this collector can fetch, as (value, label). Empty means the
    #: source has only one resolution and the form omits the control.
    timeframes: tuple = ()


COLLECTORS: dict[str, RegisteredCollector] = {
    ff.KEY: RegisteredCollector(ff.KEY, ff.fetch, ff.PARSER_VERSION, ff.reparse),
    mt5.KEY: RegisteredCollector(
        mt5.KEY,
        mt5.fetch,
        mt5.PARSER_VERSION,
        mt5.reparse,
        accepts_upload=True,
        upload_accept=".csv",
    ),
    ffp.KEY: RegisteredCollector(
        ffp.KEY,
        ffp.fetch,
        ffp.PARSER_VERSION,
        ffp.reparse,
        requires_date_range=True,
    ),
    dbn.KEY: RegisteredCollector(dbn.KEY, dbn.fetch, dbn.PARSER_VERSION, dbn.reparse),
    duka.KEY: RegisteredCollector(
        duka.KEY,
        duka.fetch,
        duka.PARSER_VERSION,
        needs_symbols=True,
        requires_date_range=True,
        timeframes=(
            ("h1", "1 hour — one file per month, practical for decades"),
            ("d1", "1 day — one file per year"),
            ("m1", "1 minute — built from ticks, one request per hour"),
        ),
    ),
    # HistData declines to be automated (see its module docstring), so it is an
    # importer: upload the monthly zips or point it at a folder.
    hd.KEY: RegisteredCollector(
        hd.KEY,
        hd.fetch,
        hd.PARSER_VERSION,
        accepts_upload=True,
        multi_upload=True,
    ),
}


def get(source_key: str) -> RegisteredCollector | None:
    return COLLECTORS.get(source_key)


def implemented_keys() -> set[str]:
    return set(COLLECTORS)
