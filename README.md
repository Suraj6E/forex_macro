# forex_macro — Forex Macro Event Impact Explorer

A local, single-user testbed for measuring what FX majors do around
macroeconomic releases. Design and reasoning live in [`planning.md`](planning.md);
this file is just how to run it.

**Status: P1 complete, P2 validation harness in.** Django project, the §9 data
model, the job runner, the full screen set, six collectors, the Parquet price
store, the event study across the horizon ladder, and — new — the check that
asks price whether our release timestamps are where we think they are.

**The calendar is loaded: 86,450 releases, Jan 2007 → today.** 66,186 carry an
exact release time and an actual; 56,111 a forecast, 26,090 a revised previous.
The other 20,264 are bank holidays, bond auctions, speeches and other all-day or
tentative entries that carry a scheduled time but no instant — they can be read,
but nothing can be anchored to them. Source: the ForexFactory calendar pages.
Verified against known
history — Nov 2008 payrolls read −533K, Apr 2020 read −20,537K, Aug 2024 read
142K — and the release times track US daylight saving correctly (08:30 New
York is 13:30 UTC in winter, 12:30 in summer, and only those two values
appear).

**Prices: hourly bars for all seven majors, Jan 2007 → Aug 2026**, from
Dukascopy, in the Parquet store.

**The timestamps have now been checked against price (§4.1, P2).** All 359
indicators, 65,495 releases: each one's hour compared against what its pair
normally does in the same weekday-and-hour slot, scanning ±3 hours around the
stored instant.

| Verdict | Indicators | |
|---|---:|---|
| **aligned** | 259 | the spike lands in the stored hour far more often than chance |
| **scattered** | 97 | releases spike, but no offset stands out — almost all are weak series |
| **offset** | 2 | EUR Retail Sales (+1h) and Belgian NBB Business Climate (−3h) |
| insufficient | 1 | too few readable releases to say anything |

**And the agencies agree.** Twenty-one releases — payrolls, CPI and FOMC
decisions between 2008 and 2024 — have been checked by hand against the
publisher's own release page, and all twenty-one match on value, reporting
month and release time ([`docs/agency_verification.md`](docs/agency_verification.md)).
March 2008 is the one worth knowing about: BLS published payrolls on the 7th
under an **EST** embargo and CPI on the 14th under an **EDT** one, same 8:30
local time, and our two stored instants differ by exactly the hour the agency's
own wording says they should. That is the daylight-saving question answered
from the publisher's side rather than from price.

**The clocks are sound.** Federal Funds Rate puts 90% of its spikes in the
stored hour at 5.0× the normal hourly range; NZD Official Cash Rate 92% at 4.5×;
payrolls 83% at 2.5× — against the ~14% each hour would get if the release had
nothing to do with when price moved. The two remaining offsets are low-impact
series and are worth a human look; everything a study is likely to be anchored
to reads clean.

Getting there took two corrections to the check itself, both of which the data
forced:

- A flat "a majority must agree" rule called 48%-on-time-out-of-seven-bins
  *scattered*. Replaced with a binomial tail against the uniform null.
- The check then named **39** indicators as systematically offset — and every
  one had a heavier release sitting in the hour the spike landed in. European
  morning prints resolved to +3h, which is the 13:30 UTC US cluster; 10:00 New
  York prints to −2h, which is the 08:30 one. It had found the market's
  dominant news hour, not our clocks. A bar can no longer win the scan if a
  release at least as important occupies it, and the sweep is
  false-discovery-controlled across all 359 tests.

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

## The three modes

Every study is stamped with the mode that produced it, and the three are never
merged into one column.

| Mode | The question | What it needs | State |
|---|---|---|---|
| **A** | Does this release move price more than usual? | timestamps + price | working |
| **direction** | Does the currency strengthen when this indicator *rises*? | `actual` and `previous` | working |
| **B** | Does it strengthen when the indicator beats *expectations*? | a point-in-time forecast | blocked |

**A** reports an unsigned magnitude. It cannot say which way price went, and
for a symmetric event the mean signed move is ~0 by construction — that is the
expected result, not a failure.

**direction** regresses the signed abnormal return on the change against the
indicator's own last print, standardised by its own trailing sigma. 99.9% of
timestamped releases carry both numbers, so it runs over the whole history now,
without a forecast. The signs it recovers are mutually consistent across
independent economies: CAD Employment Change up puts CAD up and CAD
Unemployment Rate up puts CAD down; the same mirrored pair holds for NZD.

**It is not Mode B**, and the distinction is not pedantry. It measures *higher
than last time*, not *higher than expected*. Part of any change was already
priced, which pulls the coefficient toward zero; and when the market expected a
rise and got a smaller one, the change is positive while the surprise is
negative — the two can carry opposite signs on the same release. Different
quantity, own mode, own label, exactly as §6.7 requires of modelled
expectations.

**B** stays blocked on §4.3: a historical calendar scrape shows today's
consensus, so only the weekly forward capture earns the label, and it currently
holds 16 forecasts.

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
| **Data quality** | Twelve checks graded blocking / warning / info, each stating what it means and offering the fix. Plus timestamp confidence, forecast provenance, cross-source agreement and the forward-capture log. Deliberately prominent (§10). |
| **Timestamps** | **The validation harness.** Does a volatility spike land where each stored timestamp claims? Per-indicator verdict, the histogram of where spikes actually landed, a US summer/winter split for daylight-saving bugs, and the individual offset releases with what else shared that hour. |
| **Duplicates** | Merge indicators that different sources named differently; collapse duplicate releases; purge a source's contributions. Exact name matches can be merged in bulk; similar ones need a human. |
| **Calendar** | Every release held, filterable, with per-field provenance. |
| **Event detail** | One release: each field with its supplying source, what every source said verbatim, the revision log, co-timed releases. |
| **Event ranking** | **Which kinds of news actually move FX**, ranked on how many times its own normal move the pair made — and where that disagrees with the calendar's own high/medium/low. Pools across economies on the concept; counts a co-timed report once. |
| **Indicators** | Assign canonical codes, importance and release groups — the §9 mapping surface. |
| **Prices** | Coverage ledger per instrument, bar size and month, plus known dislocations (§4.6). |
| **Price chart** | **Candlesticks with calendar releases marked on them** (TradingView `lightweight-charts`, vendored). Overlay up to 4 indicators, filtered by impact and currency. Scroll left to load earlier history. Jump straight from any release to the chart centred on it. |
| **Sources** | The register, and every collection button. Each source has a detail page with its editable configuration. |
| **Run detail** | A sample of the actual rows fetched, plus every raw payload — each one inspectable byte-for-byte or downloadable. |
| **Jobs & runs** | Every button press, with progress, log and result. |
| **Event study** | One indicator against one pair across the horizon ladder: the decay curve, the per-release scatter behind it, and how long the effect stays distinguishable. Measured against the pair's own matched weekday-and-hour behaviour. |
| **Studies** | The §6.8 specification and the horizon ladder. The saved-specification engine lands in P4. |

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
| `vol_check` | Timestamps → **Check these timestamps against price** |
| `group_releases --apply` | — derive release groups from co-timed releases (§3.3) |
| `classify_indicators --apply` | — derive the concept of each indicator from its name (§6.4) |
| `run_direction` | — fit the signed response to the change vs previous |
| `mt5_audit` | — P0.5 Q1 and Q3, once the MT5 CSV is imported |
| `price_compare --symbol <pair>` | — P0.5 Q4, once HistData zips are imported |
| `test tests` | — unit tests for the Django-free layer |

`mt5_audit` and `price_compare` each wait on one manual step and say exactly
which. Run either now and it will tell you what to go and fetch; run it after
the import and it answers its question.

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
quality/         the shared quality vocabulary (§5.3) + the validation harness
commentary/      Commentary, kept structurally separate from data

collectors/      one module per source — NO Django imports
normalisers/     per-source → CanonicalEvent, versioned — NO Django imports
analytics/       horizons, clocks, event study, vol-check — NO Django imports
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

## Known limits at P2

- **The vol-check resolves the hour, not the minute.** The price backbone is
  hourly, so the affirmative grade is `confirmed_hour` — the spike landed in the
  release's own hour. §5.3's ±2-minute grade (`confirmed`) needs M1 windows
  around each event and stays reserved for that check, so the two are never
  conflated in the database. The weaker grade still catches every failure that
  displaces a release by a whole hour or more, which is what a daylight-saving
  or source-clock bug does.
- **An offset verdict has two readings and the check cannot separate them.**
  Either the clock is wrong, or something bigger reliably happens that much
  later. A bar occupied by a release at least as important is refused outright
  (`confounded` — 10,592 releases), but the rule only sees what the calendar
  knows about: a market open, a fixing, or an unlisted press conference is
  invisible to it. The screen lists what else occupies the winning hour, which
  is the evidence that settles it; §3.3's confound problem, arriving in the
  validation harness.
- **The refusal is deliberately conservative.** A genuine offset whose hour
  happens to hold a big release is missed rather than reported. A check whose
  job is finding our own bugs is worthless if it cries wolf, and the histogram
  still shows the raw picture either way.
- **A quiet indicator's clock cannot be validated this way at all.** If price
  does not move when a release lands, price cannot say where it landed. Those
  read `no_spike` and the verdict says *unverified*, never *correct*.
- **A per-release `vol_check` grade is not a verdict about that release.** Its
  label reads *"offset — spike found, but not where the timestamp says"*, which
  on a single event page asserts something the check cannot support: three of
  the twenty-one hand-verified releases carry `offset` against an agency page
  that confirms the stored timestamp exactly, and 87 of CPI m/m's 235 releases
  are graded `offset` while the series as a whole is comfortably aligned. For
  one release, the strongest bar landing elsewhere in a ±3 hour window is noise.
  Only the indicator-level binomial test decides a clock. The wording should
  change and the event page should show the series verdict beside the release's
  own grade; neither is done.
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
- **Cross-source comparison, the other half of P2, cannot run yet** — with one
  calendar source there is nothing to compare against. Loading the MT5 export is
  what switches it on, and is also P0.5's remaining question: how far back does
  your MT5 calendar actually reach, which decides where surprise-conditioned
  analysis (Mode B) can start.
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
- **Chart markers identify their indicator by a printed code, not by colour.**
  The candles already use the only two well-separated hues; a marker palette
  beside them fails validation (orange vs the red down-candle measures ΔE 7.1
  in normal vision, violet vs the blue up-candle ΔE 1.9 under protanopia). The
  shape carries beat/missed and the label carries which series, so neither
  depends on hue.
- Series longer than ~420 points are thinned for display (endpoints always
  kept) — a 720px plot cannot resolve more.
- **Raw payloads have been pruned.** Fetch-run provenance is intact (what was
  fetched, when, by which parser, with what hash), but only the most recent run
  per source still has its bytes. Re-parsing older history would need a
  re-crawl — about 20 minutes for the full calendar.
