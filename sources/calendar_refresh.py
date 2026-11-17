"""Keep the calendar current — what `manage.py refresh_calendar` runs, and
what the replay simulator runs in the background while it is open.

Until this existed the calendar was collected only by pressing Fetch, and it
went stale: the last fetch was 22 Sep 2026, so nothing after 30 Sep was ever
in the database. This refresh runs the ordinary `fetch_source` job, so every
fetch is a FetchRun with its snapshots, exactly as if Fetch had been pressed:

* **The weekly feed** whenever its fetch policy allows (24 h): this week's
  schedule with the forecasts as they stand before the releases, the only
  point-in-time forecasts there are (§7.3).
* **The calendar pages**, the schedule ahead and the actuals:
  - a *full* refresh at most once a day: every month from two days before
    the last full fetch to next month, so a gap of any length is filled and
    next month's schedule is known;
  - in between, a *recent* refresh: the pages around today, folding in only
    the events from a day ago to eight days ahead, which is where actuals
    arrive and the schedule and forecasts move.

Both are kept small on purpose. A month page is a 1.7 MB snapshot and ~400
events, and each event folded in is an observation row kept as provenance:
refetching whole months every hour would add gigabytes a year. So a recent
refresh folds in only its window, and a page the refresh fetches again
replaces its own earlier snapshot of that page (the newest holds everything
the older one did). Snapshots from a fetch made by hand are never touched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone

#: Marks the fetches this refresh makes, in their FetchRun parameters.
BY = "refresh_calendar"
PAGES = "forexfactory_pages"
WEEKLY = "forexfactory_weekly"

FULL_EVERY = timedelta(hours=20)
FULL_OVERLAP = timedelta(days=2)
MONTHS_AHEAD = 1
RECENT_BACK = timedelta(days=1)
RECENT_AHEAD = timedelta(days=8)
#: Never fetch the pages more often than this, however often it is asked.
MIN_GAP = timedelta(minutes=10)


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def add_months(d: date, n: int) -> date:
    k = d.year * 12 + d.month - 1 + n
    return date(k // 12, k % 12 + 1, 1)


@dataclass
class PagesPlan:
    mode: str  # "full", "recent" or "skip"
    reason: str
    date_from: date | None = None
    date_to: date | None = None
    rows_from: datetime | None = None
    rows_to: datetime | None = None

    def params(self) -> dict:
        out = {"date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat(), "by": BY}
        if self.rows_from is not None:
            out["rows_from"] = self.rows_from.isoformat()
        if self.rows_to is not None:
            out["rows_to"] = self.rows_to.isoformat()
        return out


def plan_pages(now: datetime, last_full: datetime | None, last_any: datetime | None,
               force: bool = False) -> PagesPlan:
    """What to fetch from the calendar pages at `now`, given when they were
    last fetched in full (no event window) and last fetched at all."""
    if not force and last_any is not None and now - last_any < MIN_GAP:
        minutes = int((now - last_any).total_seconds() // 60)
        return PagesPlan("skip", f"fetched {minutes} min ago")
    ahead = add_months(month_start(now.date()), MONTHS_AHEAD)
    if last_full is None or now - last_full >= FULL_EVERY:
        start = last_full - FULL_OVERLAP if last_full is not None else None
        first = month_start(start.date()) if start is not None else month_start(now.date())
        reason = (f"last full refresh {last_full:%Y-%m-%d %H:%M} UTC" if last_full is not None
                  else "never fetched in full")
        return PagesPlan("full", reason, first, ahead, rows_from=start)
    a, b = now - RECENT_BACK, now + RECENT_AHEAD
    return PagesPlan("recent", f"last full refresh {last_full:%Y-%m-%d %H:%M} UTC",
                     month_start(a.date()), month_start(b.date()), rows_from=a, rows_to=b)


# -- running it (Django from here on) ------------------------------------------


class RefreshError(RuntimeError):
    pass


def _run_fetch(source_key: str, **params) -> dict:
    """One `fetch_source` job, run here and now, as `capture_forward` does."""
    from django.utils import timezone as dj_tz

    from sources import jobs
    from sources.models import Job, JobStatus

    job = jobs.enqueue("fetch_source", source_key=source_key, **params)
    Job.objects.filter(pk=job.pk).update(status=JobStatus.RUNNING, started_at=dj_tz.now())
    job.refresh_from_db()
    jobs.run_job(job)
    job.refresh_from_db()
    if job.status != JobStatus.SUCCESS:
        lines = [x for x in (job.error_text or "").strip().splitlines() if x.strip()]
        raise RefreshError(lines[-1] if lines else f"job #{job.pk} finished {job.status}")
    return dict(job.result_json or {})


def _page_times() -> tuple[datetime | None, datetime | None]:
    """When the pages were last fetched in full, and at all (successful,
    written runs only)."""
    from sources.models import FetchRun, JobStatus

    last_full = last_any = None
    runs = (FetchRun.objects.filter(source__key=PAGES, status=JobStatus.SUCCESS, preview_only=False)
            .order_by("-started_at").values("started_at", "params_json"))
    for run in runs.iterator():
        if last_any is None:
            last_any = run["started_at"]
        if "rows_to" not in (run["params_json"] or {}):
            last_full = run["started_at"]
            break
    return last_full, last_any


def drop_superseded_snapshots(fetch_run_id: int) -> tuple[int, int]:
    """Remove this refresh's earlier snapshots of the pages this run fetched
    again: files and rows. Returns (files removed, bytes freed)."""
    from django.conf import settings

    from sources.models import RawSnapshot

    removed = freed = 0
    for snap in RawSnapshot.objects.filter(fetch_run_id=fetch_run_id):
        older = (RawSnapshot.objects.filter(url=snap.url, fetched_at__lt=snap.fetched_at,
                                            fetch_run__source_id=snap.fetch_run.source_id)
                 .select_related("fetch_run"))
        for old in older:
            if (old.fetch_run.params_json or {}).get("by") != BY:
                continue  # fetched by hand: the operator's to prune
            target = settings.RAW_DIR / old.path
            try:
                if target.exists():
                    target.unlink()
                    removed += 1
                    freed += old.size_bytes or 0
            except OSError:
                continue
            old.delete()
    return removed, freed


@dataclass
class Outcome:
    weekly: str
    pages: str
    pages_mode: str
    errors: list
    twins: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def refresh(now: datetime | None = None, force: bool = False, log=lambda msg: None) -> Outcome:
    """Fetch whatever is due. A failure of one source does not stop the other.
    `force` skips only the pages' ten-minute gap; the weekly feed's own
    fetch policy always holds."""
    from sources.models import Source

    now = now or datetime.now(timezone.utc)
    errors = []

    weekly = Source.objects.filter(key=WEEKLY).first()
    if weekly is None or not weekly.enabled:
        weekly_note = "the weekly feed is not set up or is disabled"
    elif wait := weekly.cooldown_remaining():  # its policy holds even when forced
        weekly_note = f"weekly feed: next allowed in {wait.total_seconds() / 3600:.1f} h"
    else:
        try:
            result = _run_fetch(WEEKLY, by=BY)
            weekly_note = f"weekly feed: {result.get('summary', 'fetched')}"
        except Exception as exc:  # noqa: BLE001 — reported, the pages still run
            weekly_note = f"weekly feed failed: {exc}"
            errors.append(weekly_note)
    log(weekly_note)

    last_full, last_any = _page_times()
    plan = plan_pages(now, last_full, last_any, force=force)
    if plan.mode == "skip":
        pages_note = f"calendar pages: {plan.reason}"
    else:
        span = (f"{plan.date_from:%b %Y}" if plan.date_from == plan.date_to
                else f"{plan.date_from:%b %Y} to {plan.date_to:%b %Y}")
        try:
            result = _run_fetch(PAGES, **plan.params())
            files, freed = drop_superseded_snapshots(result["fetch_run_id"])
            pages_note = f"calendar pages ({plan.mode}, {span}): {result.get('summary', 'fetched')}"
            if files:
                pages_note += f"; {files} older snapshot(s) of the same pages replaced ({freed / 1e6:.0f} MB)"
        except Exception as exc:  # noqa: BLE001
            pages_note = f"calendar pages ({plan.mode}, {span}) failed: {exc}"
            errors.append(pages_note)
    log(pages_note)

    # The weekly feed and the pages name the same events; anything the feed
    # filed beside its pages series is folded into it (consolidation.merge).
    from consolidation.merge import merge_title_twins

    try:
        twins = merge_title_twins(log=log)["summary"]
    except Exception as exc:  # noqa: BLE001
        twins = f"folding duplicates failed: {exc}"
        errors.append(twins)
    return Outcome(weekly_note, pages_note, plan.mode, errors, twins)
