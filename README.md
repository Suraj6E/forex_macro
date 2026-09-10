# forex_macro — Forex Macro Event Impact Explorer

A local, single-user testbed for measuring what FX majors do around
macroeconomic releases. Design and reasoning live in [`planning.md`](planning.md);
this file is just how to run it.

**Status: P0 (skeleton) complete.** Django project, the §9 data model, the
job runner, the full screen set, and one collector working end to end
(ForexFactory weekly feed → raw snapshot → normalise → merge → UI).
Nothing is measured yet — that is P1.

## Setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver
```

Then open <http://127.0.0.1:8000/>. **There is no login.** It is a local
single-user tool, so an account would protect nothing; every screen is open.
Django's admin — the generic table editor behind a username and password — is
off by default for the same reason. Set `FXMACRO_ADMIN=1` if you ever want it,
then create a login with `manage.py createsuperuser`.

## Screens

| Screen | What it is for |
|---|---|
| **Dashboard** | What the dataset holds, where it came from, how complete it is, and which capabilities exist yet. |
| **Data quality** | Timestamp confidence, forecast provenance, cross-source agreement, unmapped indicators, the forward-capture log. Deliberately prominent (§10). |
| **Calendar** | Every release held, filterable, with per-field provenance. |
| **Event detail** | One release: each field with its supplying source, what every source said verbatim, the revision log, co-timed releases. |
| **Indicators** | Assign canonical codes, importance and release groups — the §9 mapping surface. |
| **Instruments** | Price coverage ledger and known dislocations (§4.6). Empty until P1. |
| **Sources** | The register, and every collection button. |
| **Jobs & runs** | Every button press, with progress, log and result. |
| **Studies** | The §6.8 specification and the horizon ladder. Engine lands in P4. |

## Commands

Every one of these is also a button in the UI — the CLI exists for scripting
and for the Task Scheduler entry, not because you are expected to use it.

| Command | UI equivalent |
|---|---|
| `seed_reference` | Sources → **Re-seed reference data** |
| `capture_forward` | Sources → **Fetch** on the ForexFactory row |
| `reparse <source>` | Sources → **Re-parse latest** |
| `run_job <id> --force` | Job detail → **Run again** (the CLI variant prints the traceback inline instead of storing it) |
| `test tests` | — unit tests for the Django-free layer |

Set `FXMACRO_WORKER=0` to keep a command from spawning the background worker.

## Layout

```
config/          settings, urls
dashboard/       overview screen, sidebar counts, inline-SVG chart helpers
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
- Charts are server-rendered inline SVG — no chart library, no CDN, no build
  step. Every chart is a single series, so identity comes from the row label
  and a direct value label rather than from colour; the palette is monochrome
  and amber/red appear only for warning and failure, always beside a word.
