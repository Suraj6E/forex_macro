"""Collector contract — planning.md §5, §7.1.

Nothing in `collectors/` imports Django (§8).  A collector fetches bytes,
writes them to the immutable raw tree, and hands back both the snapshot
records and the normalised rows.  Persisting those is the web layer's job.

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


class CollectorError(RuntimeError):
    """Raised for anything the operator needs to see on the console."""


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
class FetchContext:
    """Everything a collector is allowed to know about its environment."""

    raw_root: Path
    source_key: str
    params: dict[str, Any] = field(default_factory=dict)
    user_agent: str = DEFAULT_USER_AGENT
    timeout: int = DEFAULT_TIMEOUT
    log: Callable[[str], None] = lambda msg: None
    progress: Callable[[float, str], None] = lambda frac, msg: None

    @property
    def date_from(self) -> date | None:
        return _as_date(self.params.get("date_from"))

    @property
    def date_to(self) -> date | None:
        return _as_date(self.params.get("date_to"))


@dataclass
class FetchResult:
    snapshots: list[Snapshot] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)  # normalisers.base.CanonicalEvent
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


def http_get(ctx: FetchContext, url: str) -> tuple[bytes, int, str, int]:
    """GET with an honest User-Agent.

    §5.4 point 2: politeness here is self-interested, not ethical — people who
    poll hard get locked out fast, and a locked-out tool is a broken tool.
    """
    import requests

    started = time.monotonic()
    try:
        response = requests.get(
            url,
            headers={"User-Agent": ctx.user_agent, "Accept": "*/*"},
            timeout=ctx.timeout,
        )
    except requests.RequestException as exc:
        raise CollectorError(f"GET {url} failed: {exc}") from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    if response.status_code != 200:
        raise CollectorError(
            f"GET {url} returned HTTP {response.status_code} "
            f"({len(response.content)} bytes)"
        )
    return (
        response.content,
        response.status_code,
        response.headers.get("Content-Type", ""),
        duration_ms,
    )
