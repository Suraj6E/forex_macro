"""Collector contract — planning.md §5, §7.1.

Nothing in `collectors/` imports Django (§8).  A collector fetches bytes,
writes them to the immutable raw tree, and hands back the snapshot records,
the normalised rows, any price frames, and a **preview of the actual data** so
the console can show what arrived rather than only how much of it.

The raw snapshot is not an optimisation.  §5.4: HTML scrapers are permanently
unstable and usually emit plausible-looking wrong data rather than an error.
Keeping the payload means a parser fix is a re-parse, not a re-crawl.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

DEFAULT_USER_AGENT = "fxmacro/0.1 (local research tool; single user)"
DEFAULT_TIMEOUT = 30

#: How many rows of real data a preview carries.  Enough to see the shape and
#: spot a decimal-point disaster; not so many that the page becomes the data.
PREVIEW_ROWS = 25


class CollectorError(RuntimeError):
    """Raised for anything the operator needs to see on the console."""


class MissingResource(CollectorError):
    """The source has no data here — a gap, not a failure."""


class TransientError(CollectorError):
    """The source could not answer right now: a timeout, a dropped connection,
    a 503 or a 429.  Worth retrying; not worth abandoning a 500-request sweep
    over.  Distinguished by type rather than by matching on message text."""


@dataclass
class Snapshot:
    path: str  # relative to the raw root
    sha256: str
    size_bytes: int
    url: str = ""
    http_status: int | None = None
    content_type: str = ""
    duration_ms: int | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DataPreview:
    """A sample of what was actually fetched.

    Quality counters tell you how much arrived; this tells you *what*.  Both
    are needed — a column that is 100% populated with the wrong number looks
    perfect on a coverage chart.
    """

    columns: list[str]
    rows: list[list[Any]]
    caption: str = ""
    total: int = 0

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)

    def as_dict(self) -> dict:
        return {
            "columns": self.columns,
            "rows": [[_jsonable(v) for v in row] for row in self.rows],
            "caption": self.caption,
            "total": self.total,
        }


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass
class PriceFrame:
    """One instrument-month of M1 bars, ready for the Parquet store.

    The DataFrame stays out of Django's way: collectors build it, `prices.store`
    writes it and updates the coverage ledger.
    """

    symbol: str
    month: date
    frame: Any  # pandas.DataFrame
    gap_count: int = 0
    note: str = ""


@dataclass
class FetchContext:
    """Everything a collector is allowed to know about its environment."""

    raw_root: Path
    source_key: str
    params: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = DEFAULT_TIMEOUT
    log: Callable[[str], None] = lambda msg: None
    progress: Callable[[float, str], None] = lambda frac, msg: None
    #: Preview mode: fetch and parse, show the data, write nothing to the
    #: dataset.  Snapshots are still kept — the bytes cost nothing twice.
    preview_only: bool = False
    #: Files handed in from the console instead of fetched over the network,
    #: as (filename, bytes).  §14 Q4: cheap, and it saves fighting a downloader
    #: on a bad day — which for HistData is every day.
    uploads: list[tuple[str, bytes]] = field(default_factory=list)

    @property
    def upload(self) -> bytes | None:
        return self.uploads[0][1] if self.uploads else None

    @property
    def upload_name(self) -> str:
        return self.uploads[0][0] if self.uploads else ""

    @property
    def date_from(self) -> date | None:
        return _as_date(self.params.get("date_from"))

    @property
    def date_to(self) -> date | None:
        return _as_date(self.params.get("date_to"))

    @property
    def symbols(self) -> list[str]:
        raw = self.params.get("symbols") or []
        if isinstance(raw, str):
            raw = [s for s in raw.replace(",", " ").split() if s]
        return [s.upper() for s in raw]


@dataclass
class FetchResult:
    snapshots: list[Snapshot] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)  # normalisers.base.CanonicalEvent
    price_frames: list[PriceFrame] = field(default_factory=list)
    preview: DataPreview | None = None
    notes: str = ""
    parser_version: str = ""


class Collector(Protocol):
    key: str
    parser_version: str

    def fetch(self, ctx: FetchContext) -> FetchResult: ...


def _as_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def write_snapshot(
    ctx: FetchContext,
    content: bytes,
    *,
    name: str,
    url: str = "",
    http_status: int | None = None,
    content_type: str = "",
    duration_ms: int | None = None,
) -> Snapshot:
    """Write bytes into `raw/<source>/<YYYY>/<MM>/<sha12>-<name>`.

    Content-addressed, so re-fetching identical bytes rewrites the same file
    rather than accumulating near-duplicates.
    """
    digest = hashlib.sha256(content).hexdigest()
    now = datetime.now(timezone.utc)
    rel_dir = Path(ctx.source_key) / f"{now:%Y}" / f"{now:%m}"
    rel_path = rel_dir / f"{digest[:12]}-{name}"

    abs_path = ctx.raw_root / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    if not abs_path.exists():
        abs_path.write_bytes(content)

    return Snapshot(
        path=str(rel_path).replace("\\", "/"),
        sha256=digest,
        size_bytes=len(content),
        url=url,
        http_status=http_status,
        content_type=content_type,
        duration_ms=duration_ms,
        fetched_at=now,
    )


@dataclass
class HttpResponse:
    content: bytes
    status: int
    content_type: str
    duration_ms: int
    url: str


def http_request(
    ctx: FetchContext,
    url: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    data: dict | None = None,
    headers: dict | None = None,
    missing_statuses: tuple[int, ...] = (),
) -> HttpResponse:
    """One HTTP call with an honest User-Agent.

    §5.4 point 2: politeness here is self-interested, not ethical — people who
    poll hard get locked out fast, and a locked-out tool is a broken tool.

    `missing_statuses` are treated as "no data here", raising `MissingResource`
    so a caller sweeping a date range can count a gap instead of aborting.
    """
    import requests

    merged = {"User-Agent": ctx.user_agent, "Accept": "*/*"}
    merged.update(headers or {})

    started = time.monotonic()
    try:
        response = requests.request(
            method, url, params=params, data=data, headers=merged, timeout=ctx.timeout
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientError(f"{method} {url} did not answer: {exc}") from exc
    except requests.RequestException as exc:
        raise CollectorError(f"{method} {url} failed: {exc}") from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    if response.status_code in missing_statuses:
        raise MissingResource(f"{method} {url} -> HTTP {response.status_code}")
    if response.status_code in (429, 500, 502, 503, 504):
        raise TransientError(f"{method} {url} -> HTTP {response.status_code}")
    if response.status_code != 200:
        raise CollectorError(
            f"{method} {url} returned HTTP {response.status_code} "
            f"({len(response.content)} bytes)"
        )
    return HttpResponse(
        content=response.content,
        status=response.status_code,
        content_type=response.headers.get("Content-Type", ""),
        duration_ms=duration_ms,
        url=response.url,
    )


def http_get(ctx: FetchContext, url: str) -> tuple[bytes, int, str, int]:
    """Back-compat shim for the simple case."""
    r = http_request(ctx, url)
    return r.content, r.status, r.content_type, r.duration_ms


def preview_from_events(rows, *, caption: str = "") -> DataPreview:
    """Standard preview for calendar-style rows (`CanonicalEvent`)."""
    columns = [
        "currency",
        "event",
        "period",
        "scheduled (UTC)",
        "actual",
        "forecast",
        "previous",
        "revised prev.",
        "importance",
    ]
    sample = []
    for row in rows[:PREVIEW_ROWS]:
        when = row.release_time_utc or row.scheduled_time_utc
        sample.append(
            [
                row.currency,
                row.source_event_name,
                row.reference_period,
                when.strftime("%Y-%m-%d %H:%M") if when else None,
                row.actual,
                row.forecast,
                row.previous,
                row.revised_previous,
                row.importance_source,
            ]
        )
    return DataPreview(columns=columns, rows=sample, caption=caption, total=len(rows))
