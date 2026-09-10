"""Which collector serves which source row — planning.md §5.

"Each of these is a row in a `source` table with its own fetch button, policy
and health — not a hardcoded script."  The row is the configuration; this
module is the only place that knows which callable implements it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from collectors import forexfactory as ff
from collectors.base import FetchContext, FetchResult


@dataclass(frozen=True)
class RegisteredCollector:
    key: str
    fetch: Callable[[FetchContext], FetchResult]
    parser_version: str
    #: Re-run the normaliser over stored bytes, given their original capture
    #: instant.  None means this source cannot be re-parsed offline yet.
    reparse: Callable[..., list] | None = None


COLLECTORS: dict[str, RegisteredCollector] = {
    ff.KEY: RegisteredCollector(ff.KEY, ff.fetch, ff.PARSER_VERSION, ff.reparse),
}


def get(source_key: str) -> RegisteredCollector | None:
    return COLLECTORS.get(source_key)


def implemented_keys() -> set[str]:
    return set(COLLECTORS)
