# forex_macro — Forex Macro Event Impact Explorer

A local, single-user testbed for measuring what FX majors do around
macroeconomic releases. Design and reasoning live in [`planning.md`](planning.md);
this file is just how to run it.

**Status: P0 complete, calendar loaded.** Django project, the §9 data model,
the job runner, the full screen set, six collectors, and the Parquet price
store. Nothing is *measured* yet — the event study is P1.

**The calendar is loaded: 86,450 releases, Jan 2007 → today, every one with a
timestamp.** 66,186 carry an actual, 56,111 a forecast, 26,090 a revised
previous. Source: the ForexFactory calendar pages. Verified against known
history — Nov 2008 payrolls read −533K, Apr 2020 read −20,537K, Aug 2024 read
142K — and the release times track US daylight saving correctly (08:30 New
York is 13:30 UTC in winter, 12:30 in summer, and only those two values
appear). No price data yet.

## Sources

| Source | How it collects | State |
|---|---|---|
| **ForexFactory pages** | HTTP, one request per month | **The calendar.** 86,450 events loaded, Jan 2007 → today. Nothing exists before Jan 2007 — months back to 2000 return a valid page with zero events. |
| **ForexFactory weekly** | HTTP, weekly cadence | Working. The *only* point-in-time forecast channel (§7.3) — the pages source cannot be, because it shows today's forecast. Rate-limits hard; cooldown enforced from the DB. |
| **DBnomics** | HTTP API, series list configurable per source | Working but **not loaded** — purged once ForexFactory covered the span. Available as a cross-check; it carries no release timestamps, so it can fill an `actual` but never anchor a study. |
| **MT5 calendar** | MQL5 script → UTF-8 CSV → upload or watched folder | Working. Run `mql5/CalendarExport.mq5` in the terminal. Handles the ×1,000,000 scaling and the `LONG_MIN` null sentinel. |
| **Dukascopy** | HTTP, one LZMA file per instrument-hour → M1 bars | Working; decode verified against live ticks. The feed throttles, so sweeps retry with backoff and record unavailable hours. Ticks are for event windows only — bulk would be ~800 GB. |
| **HistData** | Import: upload zips or point `import_dir` at a folder | Import path working. **Automated download is not possible** — the form posts an empty token and the site returns HTTP 200 with zero bytes to anything that is not a browser. §14 Q4 anticipated this. |
| ALFRED · agencies · Philly Fed SPF · FF pages · MT5 prices | — | Register rows with clocks and policies recorded, awaiting collectors. |

Every source has a **Preview** button: it fetches and parses exactly as a real
run would, shows a sample of the resulting rows, and writes nothing.

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
| **Overview** | Releases per year, most frequent and busiest high-impact indicators, currency split, column coverage, source contributions. |
| **Indicators** | Every series with a sparkline of its recent history. Search by name, currency or impact. |
| **Indicator detail** | **The chart page.** Value over time with the forecast overlaid, plus surprise (actual − forecast) about a zero line. 1Y/5Y/10Y/All ranges. Hover any point for its exact timestamp and value. |
| **Data quality** | Eleven checks graded blocking / warning / info, each stating what it means and offering the fix. Plus timestamp confidence, forecast provenance, cross-source agreement and the forward-capture log. Deliberately prominent (§10). |
| **Duplicates** | Merge indicators that different sources named differently; collapse duplicate releases; purge a source's contributions. Exact name matches can be merged in bulk; similar ones need a human. |
| **Calendar** | Every release held, filterable, with per-field provenance. |
| **Event detail** | One release: each field with its supplying source, what every source said verbatim, the revision log, co-timed releases. |
| **Indicators** | Assign canonical codes, importance and release groups — the §9 mapping surface. |
| **Instruments** | Price coverage ledger and known dislocations (§4.6). Empty until P1. |
| **Sources** | The register, and every collection button. Each source has a detail page with its editable configuration. |
| **Run detail** | A sample of the actual rows fetched, plus every raw payload — each one inspectable byte-for-byte or downloadable. |
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
| `prune_history` | Activity → **Clear history** (add `--dry-run` to see what would go) |
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
- **Every release is keyed provisionally.** ForexFactory carries no reporting
  period, so rows use a `release:<UTC minute>` key instead of the §4.4 identity.
  They cannot be joined to an agency figure by period until a source that does
  carry `period` — MT5 or an agency — supplies one and they are re-keyed.
- **No forecast in the dataset is point-in-time.** A historical scrape shows
  today's consensus, and calendar sites revise those (§4.3). Only the weekly
  forward capture earns that label; it currently holds 16 such forecasts and
  grows by one week each week. This is what blocks the leakage test (§3.4) and
  same-instant decomposition (§3.3) on historical data.
- **The dataset has one calendar source**, so `cross_source` is structurally
  always `single_source` and the conflict detection of §4.4 has nothing to
  compare. Loading MT5 as a second opinion is what switches it on.
- No event study yet. P0.5 (source audit) is next: export your MT5 calendar and
  import it to find out how far back it actually reaches, which decides where
  surprise-conditioned analysis can start.
- HistData cannot be downloaded automatically (see the sources table above).
- Dukascopy throttles sustained sweeps. Unavailable hours are counted and
  reported; re-running the same range is cheap because payloads are
  content-addressed and never downloaded twice.
- Charts are server-rendered inline SVG — no chart library, no CDN, no build
  step, no JavaScript. Every hue was run through a palette validator against
  the app's own surfaces in both themes: actual vs forecast separate at CVD
  ΔE 24.7 light / 26.8 dark, beat vs miss at 21.6 / 19.2, all far clear of the
  ΔE 8 floor. Colour is never the only channel — the forecast line is also
  dashed and thinner, surprise sign is also position about a zero line, and
  status chips always carry their word.
- Tables sort by clicking a column header. Sorting is done in SQL against a
  whitelist, so a hand-edited query string cannot inject an ordering.
- Series longer than ~420 points are thinned for display (endpoints always
  kept) — a 720px plot cannot resolve more.
- **Raw payloads have been pruned.** Fetch-run provenance is intact (what was
  fetched, when, by which parser, with what hash), but only the most recent run
  per source still has its bytes. Re-parsing older history would need a
  re-crawl — about 20 minutes for the full calendar.
