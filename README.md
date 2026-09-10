# forex_macro — Forex Macro Event Impact Explorer

A local, single-user testbed for measuring what FX majors do around
macroeconomic releases. Design and reasoning live in [`planning.md`](planning.md);
this file is just how to run it.

**Status: P0 (skeleton) complete.** Django project, the §9 data model, the
job runner, the Sources console, and one collector working end to end
(ForexFactory weekly feed → raw snapshot → normalise → merge → console).
Nothing is measured yet — that is P1.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py seed_reference
.\.venv\Scripts\python.exe manage.py runserver
```

Then open <http://127.0.0.1:8000/sources/>.

`manage.py createsuperuser` if you want the admin, which is where the
canonical-code mapping happens (§8).

## Commands

| Command | What it does |
|---|---|
| `seed_reference` | Source register (§5), the 7 majors, release groups, known dislocations (§4.6). Idempotent. |
| `capture_forward` | The one scheduled task (§7.3). Runs in-process so Windows Task Scheduler can call it and exit. |
| `reparse <source>` | Re-run the current normaliser over a stored snapshot. No network call (§5.4). |
| `run_job <id> --force` | Execute a job in the foreground with the traceback inline, instead of swallowed into `job.error_text`. |
| `test tests` | Unit tests for the Django-free layer. |

Set `FXMACRO_WORKER=0` to keep a command from spawning the background worker.

## Layout

```
config/          settings, urls
sources/         Source, Job, FetchRun, RawSnapshot + the console + the worker
calendar_data/   Indicator, IndicatorAlias, ReleaseGroup, EventRelease,
                 SourceObservation, ValueRevision
prices/          Instrument, PriceCoverage, MarketEvent
studies/         StudySpec, StudyRun, EventImpact, CurrencyState, DecayCurve
quality/         the shared quality vocabulary (§5.3)
commentary/      Commentary, kept structurally separate from data

collectors/      one module per source — NO Django imports
normalisers/     per-source → CanonicalEvent, versioned — NO Django imports
analytics/       horizons, clocks, event study — NO Django imports
consolidation/   merge, field priority, conflict detection

data/raw/        immutable fetched payloads, content-hashed (gitignored)
data/parquet/    price bars (gitignored)
```

`collectors/`, `normalisers/` and `analytics/` import nothing from Django, so a
notebook can use the same engine the UI does (§0a, §8).

## Scheduling the forward capture (§7.3)

A missed week is permanently lost — a point-in-time forecast cannot be
re-fetched after the release. One Windows Task Scheduler entry, weekly:

```
Program:   D:\Work\projects\forex_macro\.venv\Scripts\python.exe
Arguments: manage.py capture_forward
Start in:  D:\Work\projects\forex_macro
Environment: FXMACRO_WORKER=0
```

Suggested cadence: Sunday before the week opens.

## Known limits at P0

- Only `forexfactory_weekly` has a collector. The other nine source rows exist
  in the register with their clocks and policies recorded, awaiting one.
- The FF feed carries no reporting period, so its rows use a provisional
  `release:<UTC minute>` identity instead of the §4.4 key. They are prefixed so
  a later job can re-key them once MT5 supplies `period`.
- The feed rate-limits hard (HTTP 429 observed). `min_interval_hours` is
  enforced from the DB; the console offers a `force` override.
- No price data, no event study, no studies. P0.5 (source audit) is next.
