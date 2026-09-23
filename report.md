# forex_macro — Project Review

*Review date: 2026-09-18. Reviewed at commit `743902a` ("phase 5"). Every number below was
read from `db.sqlite3` and `data/parquet/` on this machine or recomputed with the project's
own `analytics/` functions. Where I ran a check the project does not run itself, I say so.*

---

## Interactive version

**[What Moves FX →](https://claude.ai/artifact/JAbC6hkifjwduLep15L3hW)**

The findings below as a page you can operate rather than scroll. It carries the four
charts this document can only tabulate:

| On the page | What it lets you do that the text cannot |
|---|---|
| **Concept ranking** | Toggle between the *median* release in a family and its *heaviest* member. The two disagree sharply — inflation sits last on the median and third on the ceiling — and the toggle is the fastest way to see that the median is describing family composition, not impact. |
| **Horizon curves** | Switch between the FOMC, the Employment Situation, the ECB and CPI on one scale, so the shapes are comparable: the FOMC's two-week tail against payrolls' three days, and the negative pre-release bars that are the liquidity-withdrawal signature. |
| **Regime split** | Step through §4.4's seven regimes per event. CPI going from 2 pips to 36 is the single most striking thing in this review and it is close to invisible as a table row. |
| **Direction survivors** | The count surviving FDR at each horizon, with the pre-release window shaded, so the +1h spike and the leakage test read as one picture. |

Every figure is transcribed from the sections below — the page is a reading surface, not a
second analysis, and it recomputes nothing. Two things found after this review was written
(the 70 fused releases, and Dukascopy's current-month limit) are carried there as a dated
addendum rather than folded into the original findings.

The empirical null of 0.70 (§3.3a) is drawn on the ranking chart as a reference line,
because that correction is the one most likely to be lost when a number is read out of
context.

---

## 0. The short version

**What this is.** A local, single-user Django application that holds 86,450 ForexFactory
calendar entries (Jan 2007 → Sep 2026), hourly Dukascopy bars for the seven USD majors over
the same span, and an event-study engine that measures what each pair does around each
release relative to what it normally does in the same weekday-and-hour slot. It has run that
measurement for 231 indicator × pair combinations (608,344 stored per-event rows, 2,536
decay-curve points), fitted a signed "direction" regression for 229 of them, and validated
every release timestamp against the price series.

**What it gets right.** The data foundation is unusually careful for a hobby project: UTC
everywhere, DST verified from the publisher's own embargo text, a validation harness that
found and fixed two of its own bugs, provenance tables, engine versioning, a pure-Python
analytics layer with 222 passing unit tests, and documentation that states what the tool
cannot do as prominently as what it can. The core measurement, mean excess absolute move
against a matched baseline, is sound, and its p-values are trustworthy: I checked the
statistic on 400 random non-event hours and it is centred on zero.

**What it gets wrong.** Four things matter, in order:

1. **The ratio shown everywhere reads about 0.70–0.88 for "nothing happened", not 1.0.**
   It is the median of per-event ratios whose denominator is a mean, and FX returns are
   fat-tailed. Every "how many times its normal move" number on the ranking page and the
   event-study page is biased low by 15–30%, more at short horizons. The p-values are
   unaffected. The reading of the ranking is affected.
2. **Weekly series lose most of their baseline.** The fix that stopped weekly releases being
   compared against themselves walks back until it finds a window free of the indicator, and
   for a weekly series at horizons of a week or longer there is no such window. Unemployment
   Claims keeps 469 of 1,020 releases at +1h and 41 at +1w. Natural Gas Storage keeps 0 at
   +1w. The surviving subset is holiday-shifted weeks, not a random sample. Any weekly-series
   long-horizon result in the database is an artefact.
3. **The outlier policy is not implemented.** `OutlierPolicy.FLAG` is the documented default
   and `is_outlier` is `False` on all 608,344 rows. The Swiss Libor Rate "finding" (R² 0.35,
   the strongest direction result in the database) is one day: 15 January 2015, when the SNB
   removed the floor and USD/CHF moved 1,133 pips against the baseline in the hour. Ten
   non-zero rate changes in 55 releases, one of them 25× the size of the others.
4. **The platform the plan promised is not built.** planning.md §0a makes "studies are
   configured, not coded" the governing requirement. Today `StudySpec`, `StudyRun`,
   `Hypothesis` and `CurrencyState` are all empty tables. A study is one indicator against
   one pair over the fixed ladder, launched from a dropdown. That is P1's vertical slice,
   well executed, not the §6.8 engine.

**What the data says about macroeconomics.** Central bank rate decisions move FX far more
than any data release, in every economy, and the excess movement after an FOMC decision is
still detectable two weeks later. US payrolls are the largest data release, but whether the
number is higher or lower than last month predicts nothing about direction: only the
surprise against consensus does, and that column is not in the dataset. US Core CPI is the
one data release whose direction is consistently legible from its change alone, on six of
seven pairs. US Core PCE, rated high-impact by the calendar, does not move price at all when
it lands because CPI and PPI have already told the market what it will say. Rate decisions
by the RBA, RBNZ and Bank of Canada are visible in the currency in the days *before* the
announcement. And US CPI was a second-tier event for fourteen years until inflation became
the policy question in 2022, at which point its impact rose twenty-fold.

The rest of this document is the evidence.

---

## 1. The idea, and the concept behind it

### 1.1 What was asked for

The originating question (planning.md §0b) is: *"US announces its unemployment rate, it
drops, and right after that the market moves in favour of the dollar for a week or so. So we
can say this event has this impact, for this long, of this size."* The stated goal (§0a) is
not an answer but a place to ask that kind of question repeatedly: a testbed for learning
how calendar events and price relate.

### 1.2 Is the idea sound?

Yes, with two qualifications the plan itself already states and one it does not.

**It is a well-studied question with a known answer shape.** This is the event-study
literature on macro announcements. Andersen, Bollerslev, Diebold and Vega
([AER 2003](https://www.aeaweb.org/articles?id=10.1257%2F000282803321455151)) established
with six years of five-minute data that FX reacts to the *surprise* component of
announcements, that the conditional-mean adjustment is essentially complete within minutes,
that volatility stays elevated for longer, and that bad news moves price more than good news.
Payrolls was the largest single announcement in their sample and in most that followed
([St. Louis Fed 2007 on jumps and cojumps](https://s3.amazonaws.com/real-dev.stlouisfed.org/wp/2007/2007-032.pdf)).
The project's Mode A results reproduce the volatility half of this on nineteen years of
hourly data without having read the surprise column, which is a genuine cross-check. The
project cannot yet reproduce the conditional-mean half because it has no point-in-time
consensus. That gap is the single most important fact about the current state.

**The originating hypothesis is probably wrong, and the tool can say so.** "Moves in favour
of the dollar for a week" is a directional drift claim. The literature says the directional
adjustment is fast; what persists is volatility. The project's own data agrees: the FOMC
excess *absolute* move is still significant at +1w and +2w, but no post-release *direction*
coefficient for any US indicator survives correction beyond +4h except a handful with small
or biased samples. A tool that can demonstrate its own founding hypothesis is not supported
by the data is doing its job.

**The qualification the plan does not state.** The plan treats the forecast column as "an
enrichment layer, not a gate" (§3.5, locked decision 14). For magnitude and duration that is
correct. For direction it is a gate. The "direction" mode built to work around it (change
against previous print) recovers signs for indicators whose previous print is a good proxy for
consensus (CPI, PMIs, GDP revisions) and recovers nothing for indicators where it is not
(payrolls, FOMC). The plan's own §3.5 predicts this attenuation; the data shows it is total
for the most important release in the calendar.

### 1.3 The concept: the three modes

| Mode | Question | Input | State | My reading |
|---|---|---|---|---|
| A | Does price move *more than usual* after this release? | timestamps + price | 231 pairings computed | Sound. The workhorse. |
| direction | Does the currency strengthen when the indicator comes in *above its last print*? | + `actual`, `previous` | 229 pairings computed | Useful, honestly labelled, but weaker than the README implies; see §4.3. |
| B | Does it strengthen when the indicator *beats consensus*? | + point-in-time forecast | 16 forecasts held | Blocked. The one that answers the founding question. |

Keeping the three separate and stamping every row with its mode is the right call. The
`Mode` enum, the separate `engine_version` strings and the refusal to merge direction into B
are all correct.

### 1.4 What is conceptually novel here versus a paper

Nothing in the statistics. What is unusual is the engineering posture: the validation harness
that asks price whether the calendar's timestamps are where they claim, the agency
cross-check, the per-field provenance, the refusal to coalesce `actual_first_print` and
`actual_current`, and the release-group derivation from co-timing. Most published event
studies assume their timestamps. This one checked 65,495 of them and found the market's
dominant news hour masquerading as a clock bug, which is a small but real methodological
contribution worth writing up.

---

## 2. Architecture

### 2.1 Layout as built

```
collectors/   normalisers/   analytics/        ← no Django imports; 222 tests
sources/  calendar_data/  prices/  studies/  quality/  consolidation/  commentary/  dashboard/
SQLite (248 MB)  +  Parquet (43 MB, 7 pairs × 236 months × h1)
```

### 2.2 What works

- **The pure/impure split is real and enforced.** `analytics/eventstudy.py`,
  `analytics/direction.py`, `analytics/volcheck.py` and `analytics/timeutils.py` import
  pandas and numpy only. The Django halves (`studies/engine.py`, `studies/directions.py`,
  `quality/validation.py`) load data and store results. A notebook can import the engine
  today. This is the one §0a promise that is fully kept.
- **Engine versioning.** `es-1` → `es-2` when the baseline exclusion was added; the old rows
  are still in the database (43,477 `es-1` impacts, 110 curve points) and the UI labels them
  stale rather than hiding them. `DIRECTION_VERSION` is derived from `ENGINE_VERSION` so a
  Mode A change invalidates the direction cache too. Correct.
- **Provenance.** 87,298 `SourceObservation` rows, 291 `ValueRevision` rows, content-hashed
  raw payloads, `source_map_json` per release. The merge is re-runnable from stored
  observations without re-crawling. This is what makes the dataset a product.
- **SQLite + Parquet split by workload.** Right for one user. The 43 MB Parquet store for 19
  years of hourly bars confirms §4.5's arithmetic.
- **Job runner.** DB-backed table plus worker thread, 361 jobs recorded, progress polled by
  the page. No Redis, no Celery. Correct for the scale.
- **Tests.** 222 pass in 12 seconds, and they pin arithmetic against constructed series where
  the answer is known by hand (a step function must show up in `+4h` and not in `-1d`). That
  is the right kind of test for this code.
- **Documentation posture.** README, planning.md, the roadmap screen and
  `docs/agency_verification.md` all state limits. The "Known limits at P2" section is longer
  than the feature list. This is rare and valuable.

### 2.3 What does not

- **The study engine is the missing centre.** `StudySpec`, `StudyRun`, `Hypothesis`,
  `CurrencyState`: zero rows each. `SPEC_FIELDS` in `studies/views.py` is a list of strings
  rendered on a page, not a schema anything reads. `WindowScheme` has four choices and one is
  implemented. `OutlierPolicy` has four choices and none is implemented. `Sample` has an
  explore/holdout split that nothing applies. The plan's §6.9 note that a holdout "can't be
  added later" is now in force: the full 2007–2026 sample has been looked at for 231
  pairings.
- **A column is overloaded.** `DecayCurve.r_squared` holds the abs-ratio for Mode A rows and
  a real R² for direction rows. `studies/views.py` has a comment acknowledging this. It will
  bite the first time someone queries the table without reading that comment.
- **The FDR family is per curve, not per sweep.** `benjamini_hochberg` runs over the 11
  horizons of one pairing. The ranking page then reads 231 pairings' `+1h` rows and reports
  106 as significant. That is 231 tests corrected as 231 separate families of 11. For a
  ranking, the family is the sweep. The vol-check gets this right (`apply_fdr` across all
  359 indicators); the study engine does not.
- **Co-timed confounding is derived but not used.** 25 release groups exist and the ranking
  page de-duplicates on them, but Mode A still attributes the whole 12:30 hour to whichever
  indicator was selected. When US CPI and weekly Claims share a Thursday 12:30, both curves
  contain the same bars. `EventRelease.confounded` is a boolean that nothing sets. The §6.3
  joint regression does not exist.
- **Only one calendar source is loaded.** `cross_source` is `single_source` on every row by
  construction. MT5 and DBnomics collectors exist and are unloaded. The agency check
  (21 releases) is the only external verification of values.
- **Minor.** `actual_provenance` is `unknown` on all 86,450 rows and `actual_first_print` is
  null on all of them even though the agency check established that `actual_current` *is* the
  first print for this source. The `us_employment_situation` release group has no members
  (the derived group `usd_average_hourly_earnings_m_m_...` holds them). Django admin is off
  by design, which is fine, but there is then no way to hand-edit a canonical code.

---

## 3. Methodology

### 3.1 The measurement, as implemented

For one release at `t0` and one pair, on hourly bars:

- **Anchor price** = open of the bar containing `t0`.
- **`+h` return** = log(close of the bar containing `t0+h` / anchor).
- **`−h` return** = log(anchor / open of the bar containing `t0−h`).
- **Baseline** = the same return measured at `t0 − k` weeks for k = 1…12, skipping any k
  whose window contains another release of the same indicator, walking back to k = 48 if
  needed. Mean, sample SD and mean absolute value are kept.
- **Abnormal return** = return − baseline mean.
- **Abs excess** = |return| − baseline mean |return|. **This is the Mode A effect size.**
- **Ratio** = |return| / baseline mean |return|. The pooled value is the median over events.
- **Pooled** per horizon: mean abs excess, its standard error, t, two-sided p, BH-adjusted p
  across the 11 horizons, and 2.8 × SE as the detectability floor.

Horizons on hourly bars: −5d, −1d, −4h, −1h, +1h, +4h, +1d, +3d, +1w, +2w, +1M. The minute
rungs are correctly refused.

### 3.2 What is right about it

- Matched weekday-and-hour baseline rather than an all-hours average. FX intraday seasonality
  is large; comparing a Friday 12:30 to a Tuesday 03:00 would attribute the London–New York
  overlap to the release.
- Excluding the indicator's own prior releases from the baseline. Without it a monthly
  release at the same slot four weeks ago is one of twelve baseline draws.
- Sample SD (ddof=1), fat-tail-aware use of the median for the vol-check normal, coverage
  against *open* hours so Friday releases are not rejected for having a weekend in `+1d`.
- Reporting the detectability floor so a null reads as "no effect larger than X".
- Pre-release horizons measured with the sign that makes "rise into the release" positive,
  and tested.
- The direction mode's trailing sigma uses only past changes, which avoids the look-ahead
  the plan warns about in §4.3.

### 3.3 Problems found, with evidence

**(a) The ratio's neutral point is not 1.0.** I ran `measure_event` at 400 random weekday
London/New York hours on EUR/USD between 2010 and 2025, avoiding any hour within ±1h of a
medium- or high-impact USD or EUR release, and pooled exactly as the engine does:

| Horizon | Median ratio at random non-event hours | Mean abs excess (pips) | t |
|---|---:|---:|---:|
| −1d | 0.875 | +0.3 | +0.15 |
| −4h | 0.812 | +0.8 | +0.81 |
| −1h | 0.784 | +0.1 | +0.27 |
| +1h | **0.699** | −0.9 | −1.38 |
| +4h | 0.739 | −0.5 | −0.47 |
| +1d | 0.802 | −0.3 | −0.14 |
| +3d | 0.839 | −3.4 | −1.13 |
| +1w | 0.769 | −6.4 | −1.61 |

The mean abs excess is centred on zero at every horizon, so the effect size and its p-value
are unbiased. The ratio is not: for a Gaussian, median|x| / mean|x| = 0.845, and FX hourly
returns are fatter-tailed than Gaussian, so at +1h the neutral reading is 0.70. Consequences:

- The ranking page says "how many times its own normal move". A 1.0 there is a 43% excess,
  not normal. Core PCE at 0.76× is *normal*, not quiet.
- The pre-release "quieter than normal" reading (median ratio 0.75 at −1h across all
  pairings) is mostly this bias. The *real* pre-release quiet is only where the mean excess
  is significantly negative, which it is for payrolls, FOMC and ECB (§5.4).
- Ranking by median ratio and reading "1.0 = normal" understates every effect and
  compresses the top of the table.

Fix: report the ratio as mean|x| / baseline mean|x| (consistent numerator and denominator),
or divide the median ratio by its empirical null per horizon, or show the null line on the
chart. The stored per-event ratios need no recomputation; only the pooling changes.

**(b) The "+1h" window is two bars wide.** For a 12:30 release the anchor is the open of the
12:00 bar and the "+1h" price is the close of the 13:00 bar, so the window spans 12:00 to
14:00: 90 minutes after the release plus 30 before. For an on-the-hour release (FOMC at 18:00
UTC) it spans 18:00 to 20:00. The baseline uses the same construction so the comparison is
fair, but the label is wrong by a factor of up to two and the pre-release half-hour is inside
the "post" window. Every other rung has the same one-bar overhang, which matters less as the
window grows. Either relabel (+1h → "release bar + 1") or wait for M1 bars.

**(c) Weekly series lose their baseline.** From the stored `EventImpact` rows on EUR/USD:

| Indicator (releases) | −1h | +1h | +1d | +3d | +1w | +2w | +1M |
|---|---:|---:|---:|---:|---:|---:|---:|
| Unemployment Claims (1,022) | 735 | **469** | 440 | 435 | **41** | 41 | 39 |
| Crude Oil Inventories (1,026) | 1,017 | 1,017 | 1,013 | 433 | 343 | **0** | 0 |
| Natural Gas Storage (1,027) | 886 | 771 | 701 | 495 | **0** | 0 | 0 |

The count is releases that received a baseline. A window of one week or more always contains
the next weekly release, so no look-back week is clean and the baseline fails. The 469 at
+1h for Claims is a subtler case: Claims lands at 12:30 UTC in summer and 13:30 in winter,
so the [12:30, 13:30] window includes the prior week's release only when the look-back
crosses no DST boundary, and the survivors are a season-biased subset. The Claims "+1M:
58 pips excess, p=0.0005" row in the database (n=39) is an artefact of this. So is the
"NFP +1M direction, n=86" survivor in §4.3, for the same reason applied to a monthly series
at a 30-day horizon.

Fix: for horizons longer than the release frequency, do not exclude; instead measure the
baseline over the same span and accept that it contains the prior release, or use a
different-weekday baseline. Whatever the choice, `baseline_n` should be stored per row and
the curve should show it, so a collapse from 1,020 to 41 is visible on the page.

**(d) Same UTC hour is not the same session hour across a DST change.** The baseline
matches on UTC hour k weeks earlier. For up to 12 weeks after each US or EU clock change,
some look-backs land one session-hour off. Small, systematic, twice a year. A local-time
match (New York for USD releases) would fix it.

**(e) Outlier policy is unimplemented.** `is_outlier` is `False` on every row. The SNB
Libor Rate direction fit on USD/CHF:

| Date (UTC) | Previous | Actual | Change | +1h abnormal (pips) |
|---|---:|---:|---:|---:|
| 2008-11-20 12:00 | 2.00 | 1.00 | −1.00 | +40 |
| 2009-03-12 13:00 | 0.50 | 0.25 | −0.25 | +203 |
| 2014-12-18 07:00 | 0.25 | −0.25 | −0.50 | +20 |
| **2015-01-15 09:30** | **−0.25** | **−0.75** | **−0.50** | **−1,133** |

Ten non-zero changes in 55 releases; one observation contributes essentially all of the
R² of 0.35 and the beta of +0.013 that survives FDR at seven horizons. `MarketEvent` already
holds "2015-01-15 09:30 SNB removes the EUR/CHF floor"; nothing consults it. The winsorise
and exclude policies need to exist, and the default `flag` needs to actually set the bit and
show a marker on the scatter.

**(f) FDR family.** Discussed in §2.3. At the sweep level, 106 of 231 pairings significant
at +1h is still far above the ~12 expected by chance at 5%, so the headline survives; the
individual borderline rows (p_fdr between 0.01 and 0.05) may not.

**(g) Direction mode uses `previous`, not `revised_previous`.** 26,090 releases carry a
revision to the prior print. For payrolls the revision is published in the same instant and
is itself news. Using the pre-revision `previous` is defensible (it is what the market
"knew"), but the alternative should be a switch, and the README's "99.9% carry both numbers"
should say which.

**(h) Vol-check per-release grades.** The agency document already covers this: 20,911
releases are graded `offset` against 13,384 `confirmed_hour`, and for a single release that
grade is noise. The indicator-level binomial verdict is right; the per-release label still
reads as a verdict on the event page. Not yet changed.

### 3.4 Methodological choices I would defend against criticism

- Hourly bars. The plan called for M1; the build has H1. For clock validation and for
  horizons of +1h and beyond, hourly is adequate and is what made a 19-year, 7-pair,
  231-pairing sweep feasible on a laptop in one session. M1 event windows are the right next
  step, not a prerequisite that should have blocked this.
- Absolute rather than signed effect in Mode A. Without a surprise sign, the signed mean of a
  symmetric event is zero by construction. Reporting excess |move| is the honest Mode A
  statistic and matches the volatility-response literature.
- Deriving release groups from co-timing at a 90% threshold rather than hand-assigning them.
  The result (25 groups, 55 indicators) is correct on inspection: it found the Employment
  Situation, the CPI bundle, the PMI flashes and the GDP price-index pairs, and did not fuse
  US and Canadian employment.

---

## 4. What the study found

All Mode A figures are excess absolute move in pips over the matched baseline, on `es-2`,
n = releases with a usable baseline. p is BH-adjusted across the curve. "Ratio" is the stored
median ratio; read it against the null of ~0.70 at +1h, ~0.80 at +1d.

### 4.1 Which kinds of news move FX (Mode A, +1h, de-duplicated by release group)

| Concept | Median ratio | Best | Best member | Pairings | Share significant | Calendar's own rating |
|---|---:|---:|---|---:|---:|---:|
| policy rate | **2.37** | 3.85 | USD Federal Funds Rate × EURUSD | 16 | 94% | 3.00 |
| labour | 0.98 | 3.06 | NZD Employment report × NZDUSD | 34 | 53% | 3.00 |
| energy | 0.96 | 1.10 | USD Crude Oil Inventories | 2 | 50% | 1.50 |
| consumption | 0.95 | 1.33 | NZD Retail Sales | 9 | 33% | 3.00 |
| growth | 0.90 | 1.76 | NZD GDP q/q | 31 | 16% | 2.94 |
| survey | 0.88 | 1.44 | GBP Flash PMIs | 42 | 29% | 2.91 |
| inflation | 0.83 | 2.69 | NZD CPI q/q | 31 | 32% | 2.87 |

Against the empirical null of 0.70, policy rate is 3.4× normal, labour 1.4×, everything else
1.2–1.35×. The calendar rates all of these "high". The measured order says: **rate decisions
are a different class of event from everything else, and the calendar's three-level scale
cannot express that.**

### 4.2 Individual indicators, headline curves (EUR/USD unless stated)

**USD Federal Funds Rate (n=160).** The largest and longest-lived effect in the database.

| Horizon | Ratio | Excess pips | SE | p |
|---|---:|---:|---:|---:|
| −1d | 0.59 | −9.1 | 3.3 | 0.011 |
| −4h | 0.63 | −4.7 | 1.4 | 0.002 |
| +1h | **3.85** | **+37.0** | 3.5 | 3e-19 |
| +4h | 3.51 | +36.3 | 3.4 | 3e-19 |
| +1d | 1.37 | +30.5 | 5.6 | 7e-7 |
| +3d | 1.21 | +27.0 | 6.0 | 4e-5 |
| +1w | 1.23 | **+23.5** | 7.4 | **0.003** |
| +2w | 1.02 | +21.3 | 9.9 | 0.047 |
| +1M | 0.90 | −1.4 | 14.4 | 0.92 |

The excess is flat at 36–37 pips through +4h (the press conference is inside that window from
2011 on), then decays but stays significant for two weeks. The market is significantly
*quieter* the day before and the four hours before. Ratios of 3.8–3.6 on every pair
(USDCHF 3.59, NZDUSD 3.35, AUDUSD 3.19, USDJPY 2.95, GBPUSD 2.84, USDCAD 2.39).

**USD Employment Situation (payrolls + unemployment rate + earnings; n=234).**

| Horizon | Ratio | Excess pips | p |
|---|---:|---:|---:|
| −4h | **0.45** | **−8.0** | 4e-11 |
| −1h | 0.62 | −2.1 | 0.005 |
| +1h | 1.77 | +20.9 | 2e-18 |
| +4h | 1.28 | +16.3 | 1e-9 |
| +1d | 1.32 | +17.7 | 3e-9 |
| +3d | 1.15 | +14.8 | 2e-5 |
| +1w | 0.95 | +11.8 | 0.10 |

Significant through +3d, gone by +1w. The four London hours before payrolls are the quietest
window in the whole study: 45% of normal (against a null of ~0.81 at −4h), which is the
liquidity-withdrawal signature the plan's §3.4 predicted. On USD/JPY the +1h excess is 38.6
pips (ratio 2.09).

**EUR Main Refinancing Rate (n=189).** +21.7 pips at +1h, **+29.2 at +4h** (the press
conference), +29.2 at +3d, and **+28.0 at +1w (p=0.003)**. Quiet at −1d, −4h and −1h. The
only data-or-policy event other than the FOMC whose excess is still significant a week out.

**USD CPI m/m (n=233).** +8.3 pips at +1h (p=0.0002), **+16.8 at +1d** (p=0.0002), +12.8
at +3d, insignificant from +1w. The +1d excess exceeds the +1h excess, which is unusual and is
explained by the regime split in §4.4: half the total effect comes from 2022–23. The Core and
headline, m/m and y/y curves are identical because they are one release group.

**Other rate decisions.** NZD OCR +27.4 pips at +1h on NZDUSD (ratio 3.29, significant to
+1w); AUD Cash Rate +15.1 (2.33×, to +1d); GBP Bank Rate +21.1 (1.65×, to +3d); CAD
Overnight +? (1.78×); BOJ +37.1 pips on USDJPY (2.03×, n=86, to +3d); SNB +18.6 on USDCHF
(n=29, +1h and +4h only).

**Other data.** CAD Employment +21.6 pips on USDCAD (1.65×, to +3d). ISM Manufacturing
+5.1 pips (1.02×, +1h only). Retail Sales +3.9 pips (1.02×, +1h only, p=0.04). Advance GDP:
nothing at any horizon (n=78). Core PCE: nothing at any horizon (n=234; ratio 0.76 at +1h,
which is the null).

### 4.3 High-impact indicators that do not move price

Thirty of the 80 importance-3 indicators studied show **no** FDR-significant excess at any
post-release horizon on any pair. The US ones, all measured on seven pairs:

| Indicator | n | Note |
|---|---:|---|
| Core PCE Price Index m/m | 234 | Fully anticipated from CPI and PPI two weeks earlier. See §5.3. |
| PPI m/m and Core PPI m/m | 234 | Ratio 0.80 at +1h: the null. |
| Advance GDP q/q | 78 | The most-watched GDP print; still nothing at hourly resolution. |
| Prelim GDP q/q | 78 | |
| CB Consumer Confidence | 236 | |
| Flash Manufacturing PMI | 172 | |

Plus CAD CPI, CAD Retail Sales, CHF CPI, German Prelim CPI, GBP GDP m/m, JPY Tankan and
Prelim GDP, NZD PPI and Retail Sales, AUD PPI, Retail Sales and CapEx, each on its one pair.
The calendar rates every one of these "high". This table is the §6.4 disagreement the plan
wanted, and it is the most immediately useful output of the whole project.

### 4.4 Regime dependence (recomputed for this review; not in the UI)

Mean +1h and +1d excess in pips on EUR/USD, split on the `MarketEvent` regime dates:

| Regime | n | NFP +1h | NFP +1d | CPI +1h | CPI +1d | FOMC +1h | FOMC +1d | ECB +1h | ECB +1d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| pre-crisis 2007–08 | ~20 | 19 | 16 | −2 | 14 | 33 | 8 | 12 | 31 |
| crisis 2008–09 | ~15 | 37 | 46 | 5 | 51 | 51 | 85 | 4 | −51 |
| ZIRP/QE 2010–15 | ~70 | 27 | 20 | **2** | **2** | 41 | 31 | 28 | 35 |
| normalisation 2016–20 | ~50 | 14 | 8 | 7 | 12 | 34 | 25 | 33 | 45 |
| COVID 2020–21 | ~22 | 9 | 4 | 0 | 6 | 28 | 21 | 6 | 4 |
| inflation/hiking 2022–23 | ~24 | 18 | 22 | **36** | **59** | 43 | 34 | 34 | 33 |
| post-hiking 2024– | ~30 | 24 | 24 | 17 | 19 | 31 | 28 | 7 | −1 |

Three things stand out. FOMC is the only event that is large in every regime. CPI was
inert for the entire ZIRP decade (2 pips) and became the largest data release in the calendar
in 2022 (36 pips at +1h, 59 at +1d, more than payrolls). Payrolls faded through the 2010s and
COVID and recovered. The pooled 19-year curve for CPI is therefore an average of two
different indicators, and this is the argument for the regime split being a first-class
control rather than a P4 feature.

### 4.5 Direction mode

229 pairings, 11 horizons each. Survivors after per-curve FDR:

| Horizon | −5d | −1d | −4h | −1h | +1h | +4h | +1d | +3d | +1w | +2w | +1M |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Survivors of 229 | 5 | 6 | 10 | 12 | **42** | 26 | 7 | 9 | 7 | 6 | 4 |
| Median R² | .003 | .003 | .004 | .004 | .010 | .007 | .005 | .003 | .003 | .003 | .008 |

Signs oriented as "own currency strengthens when the indicator rises":

**Consistent across pairs (the credible findings).**

| Indicator | Pairs surviving at +1h | Sign | R² range | n |
|---|---:|---|---|---:|
| USD Core CPI m/m | 6 of 7 (all but USDCAD) | USD up | 0.05–0.08 | 226 |
| USD Core CPI y/y | 5 of 7 | USD up | 0.03–0.05 | 227 |
| USD CPI y/y | EURUSD, USDCHF (+1h→+2w) | USD up | 0.02–0.05 | 227 |
| USD ISM Services PMI | 4 of 7 | USD up | 0.06–0.08 | 228 |
| USD Final GDP q/q | 5 of 7 | USD up | 0.11–0.18 | 70 |
| USD Flash Services PMI | 5 pairs across horizons | USD up | 0.04–0.11 | 146 |
| NZD Employment Change / Unemployment Rate | NZDUSD, mirrored signs | NZD up / NZD down | 0.16 / 0.22 | 71 |
| CAD Employment Change / Unemployment Rate | USDCAD, mirrored signs | CAD up / CAD down | 0.03 / 0.05 | 228 |
| CAD CPI m/m, Retail Sales, Overnight Rate | USDCAD | CAD up | 0.03–0.05 | 137–228 |
| EUR German Flash Manufacturing PMI | EURUSD | EUR up | 0.09 | 214 |
| USD Unemployment Claims | USDJPY | USD **down** when claims rise | 0.03 | 469 |

The mirrored labour pairs in two independent economies are the strongest internal
consistency check the direction mode has, and it passes.

**Null where the calendar says the event is the biggest.** US payrolls: p_raw between 0.18
and 0.72 on all seven pairs at +1h, +4h and +1d; R² never above 0.008. Federal Funds Rate on
EURUSD: nothing post-release. These are the two events with the largest Mode A effects. The
reason is the plan's own §3.5: the change against the previous print is not the surprise.
Payrolls consensus is far from last month's print, and the rate decision is almost always
fully priced, so "did it go up" carries no information about "did it beat".

**Suspect survivors.**

- CHF Libor Rate, seven horizons, R² 0.35: one day (§3.3e).
- GBP Official Bank Rate: −0.0009 at +1h (GBP *weakens* on a hike) then +0.004 at +2w
  (strengthens). n=121, unstable sign. Do not read.
- NFP +1M on EURUSD and USDCHF, n=86: the baseline-collapse subset (§3.3c).
- ISM Services +1M on USDJPY, n=20, R² 0.38: n at the gate.

**Pre-release survivors (the leakage test).** 33 of 2,519 pre-release fits survive. The
pattern is not leakage:

| Indicator | Pair | Horizons | Sign |
|---|---|---|---|
| AUD Cash Rate | AUDUSD | −5d, −1d | AUD rises before a hike (R² 0.08 at −5d) |
| NZD Official Cash Rate | NZDUSD | −5d, −1d, −4h, −1h | NZD rises before a hike |
| CAD Overnight Rate | USDCAD | −1d, −4h | CAD rises before a hike |
| CAD Employment Change | USDCAD | −4h, −1h | CAD rises before a strong print |
| USD ISM Manufacturing PMI | USDJPY | −1d, −4h, −1h | USD rises before a strong ISM |
| USD ISM Services PMI | USDJPY | −4h, −1h | same |
| USD Core PCE m/m | GBPUSD, NZDUSD, USDCHF, USDJPY | −4h, −1h | USD rises before a high PCE |
| USD CPI y/y, Core CPI y/y | several | −5d, −1d, −1h | USD rises before a high CPI |

Rate decisions from the three "telegraphing" central banks (RBA, RBNZ, BoC) are in the
currency days early; the Fed's is not (only GBPUSD −1h, and only just). The ISM and Core
PCE cases are the plan's "correlated early data" mechanism: the regional Fed surveys and the
flash PMI precede the ISM; CPI and PPI precede Core PCE. The CAD Employment case, hours before
an 08:30 release, is the one that looks most like positioning on a known-in-advance
seasonal pattern, and is worth a closer look.

---

## 5. Timestamp validation

### 5.1 Results

65,495 releases graded against price. Per-release grades: 13,384 `confirmed_hour`, 20,911
`offset`, 20,608 `no_spike`, 10,592 `confounded`, 689 `uncheckable`. Per-indicator verdicts,
recomputed for this review from the stored grades with the project's `summarise` and
`apply_fdr`:

| Verdict | All indicators | Importance-3 only |
|---|---:|---:|
| aligned | 259 | 72 |
| scattered | 97 | 8 |
| offset | 2 | 0 |
| insufficient | 1 | 0 |

The two offsets are EUR Retail Sales m/m (+1h, modal share 38%) and Belgian NBB Business
Climate (−3h, 29%), both low-impact. No high-impact clock is wrong. The best-located
releases: NZD OCR 93% in the stored hour at 4.5× normal range, FOMC 90% at 5.0×, US
Employment Situation 81% at 2.5×.

The eight importance-3 "scattered" verdicts (US Flash PMIs, US and JP Prelim GDP, AUD and
NZD PPI, Tankan, ANZ Business Confidence) all have a modal offset of 0 or a 60–120-minute
offset on tiny counts and median ratios of 1.0–1.15: these are releases too quiet to locate,
not clocks in doubt. The screen says so.

### 5.2 Assessment

This is the best-engineered part of the project. Two self-corrections are documented (the
binomial tail replacing a majority rule; the confounding refusal after 39 false offsets that
turned out to be the 13:30 UTC US cluster) and both were forced by the data. The agency check
(21 of 21 correct, DST boundary confirmed from BLS's own EST/EDT wording in March 2008) is
exactly the kind of external verification the plan asked for. The only gap is that
`confirmed` at ±2 minutes needs M1 bars and remains reserved, which is stated.

One thing the harness cannot see: releases that are systematically *late* relative to the
scheduled minute (a few NZ and AU statistical releases historically landed at :45 rather than
:30) would read as aligned at hourly resolution. M1 windows around tier-1 releases would
settle that cheaply.

---

## 6. What we learned about macroeconomics and FX

These are the claims the data supports, with the caveat that everything here is association
under a matched control, not causation, and everything at horizons beyond a day is
underpowered by the plan's own arithmetic.

1. **Monetary policy decisions are a separate class of event.** Every central bank in the
   sample produces a +1h excess of 15–37 pips against its own pair, three to five times the
   normal hourly range, and the FOMC's excess is still distinguishable from zero two weeks
   later. No data release comes close on persistence. The calendar rates FOMC, CPI, payrolls
   and retail sales identically.

2. **Payrolls is the biggest data release but its direction is not readable from the
   number.** 21 pips of excess movement on EUR/USD, 39 on USD/JPY, but zero relation between
   "higher than last month" and which way price went. Direction lives in the surprise against
   consensus. The founding question of this project cannot be answered without the forecast
   column, and the direction mode demonstrates that rather than papering over it.

3. **Inflation is legible from its change; growth and labour are not.** US Core CPI is the
   one indicator whose sign is consistent on six pairs from the change alone. This makes
   sense: month-on-month CPI is a persistent series, so last month is a decent forecast of
   this month and "higher than last month" is close to "higher than expected". Payrolls is
   noisy and heavily revised; last month says little about consensus.

4. **The market pre-empts what it can compute.** Core PCE moves nothing when it lands
   (ratio 0.76 at +1h, which is the null) because CPI and PPI two weeks earlier already
   determine it; its only significant coefficients are *before* the release. The ISM is
   partly pre-empted by the regional surveys. Final GDP moves price more than Advance GDP
   in the direction mode, which is odd until you notice Advance GDP shares its 12:30 slot
   with other releases and Final GDP does not.

5. **Rate decisions from smaller central banks are priced days early.** AUD rises for five
   days before an RBA hike, NZD for five days before an RBNZ hike, CAD for a day before a BoC
   hike. The Fed shows no such drift. Either the Fed is less predictable from the tape, or
   its decisions are priced further out than five days, or the pre-window is dominated by
   other US releases. The tool cannot yet distinguish these.

6. **Liquidity withdraws before scheduled news, and the size of the withdrawal tracks the
   size of the event.** The four hours before payrolls run at 45% of normal range, before
   the FOMC 63%, before the ECB 66%. The literature reports the same for order flow in the
   minutes before 8:30 ET releases
   ([Evans and Lyons](https://faculty.georgetown.edu/evansm1/wpapers_files/Announcements_dec03.pdf);
   [Haynes, CFTC](https://www.cftc.gov/sites/default/files/idc/groups/public/@economicanalysis/documents/file/oce_macroannouncement.pdf)).
   Here it is visible at hourly resolution over nineteen years.

7. **What matters changes with the policy question.** CPI produced 2 pips of excess per
   release for the ZIRP decade and 36 in 2022–23. The same release, the same publisher, the
   same time slot. An event's importance is a property of the regime, not of the indicator,
   and any pooled nineteen-year number for CPI is describing two different things.

8. **Directional effects are fast; volatility effects are slow.** Consistent with
   Ederington and Lee and with ABDV: no direction coefficient for a major US release survives
   beyond +4h with a clean sample, while the FOMC's excess absolute move survives to +2w. The
   "dollar strengthens for a week after a good payrolls" hypothesis is not supported.
   "The dollar moves more than usual for a week after the FOMC" is.

9. **Cross-economy consistency is real.** Employment up / unemployment up carry mirrored
   signs for the same currency in Canada and New Zealand independently, and a rate hike
   strengthens the currency at +1h for CAD, the only rate series clean enough to read. The
   sign structure of macro→FX is the same everywhere; only the magnitude and the market's
   ability to pre-empt differ.

10. **The calendar's traffic light is wrong about a third of its high-impact list.** 30 of 80
    importance-3 indicators studied produce no measurable excess. It is right about the
    ordering within labour and policy rate, and wrong to put PPI, Core PCE, Advance GDP and
    Consumer Confidence in the same tier as payrolls.

---

## 7. Calendar → chart, and chart → calendar

**Calendar → chart (what a release does to price).** The tool answers this well at hourly
resolution for magnitude and duration: how much more than usual, and for how long. It answers
direction only where the change-vs-previous proxy holds (inflation, PMIs, GDP revisions,
small-economy labour) and honestly refuses where it does not (payrolls, FOMC). Duration reads
as hours for most data releases, one to three days for payrolls and CPI, one to two weeks for
the ECB and FOMC. Anticipation reads as a *reduction* in movement for the largest events.

**Chart → calendar (what price tells you about the release before it lands).** Three
mechanisms are visible:

- *Telegraphed policy*: the currency drifts in the direction of the coming decision for RBA,
  RBNZ and BoC. Price is a leading indicator of those decisions, because the banks lead the
  market to them.
- *Derived data*: Core PCE and the ISM are partly computable from earlier releases, and price
  has moved on the earlier release. Nothing about the later release is being leaked; it was
  never new.
- *Quiet*: the market withdrawing liquidity in the hours before payrolls, the FOMC and the
  ECB is the tape telling you a big scheduled event is imminent. The size of the quiet scales
  with the size of the event.

There is no evidence of the third §3.4 mechanism, genuine information leakage, but the test
that would find it (pre-release return regressed on the *eventual surprise*) needs the
forecast column and has not been run. The pre-release coefficients on the change are
consistent with predictability, not leakage.

**What the chart cannot tell you.** Which way payrolls will surprise. Whether a given CPI
print will matter (regime). Anything about a release the tool has not measured, and most
medium- and low-impact series have not been (only 231 of a possible ~4,000 pairings exist).

---

## 8. Prioritised list of what to fix or improve

Ordered by how much a reader of the current screens is being misled.

1. **Ratio neutral point (§3.3a).** Change the pooled ratio to mean/mean or show the
   empirical null per horizon on every chart. One function; recompute nothing.
2. **Weekly-series baseline collapse (§3.3c).** Store `baseline_n` per `EventImpact` row,
   display it, and change the exclusion rule for horizons ≥ the release interval. Re-run the
   weekly series. Delete or flag the current Claims/Crude/NatGas long-horizon rows.
3. **Implement the outlier policy (§3.3e).** At minimum set `is_outlier` from `MarketEvent`
   windows and from a per-pairing MAD threshold, exclude flagged rows from the direction fit
   by default, and draw them as hollow markers on the scatter. Re-run CHF Libor and anything
   touching January 2015 on USDCHF, October 2016 on GBPUSD, January 2019 on USDJPY.
4. **Relabel the hourly rungs (§3.3b)** or annotate the page: "+1h on hourly bars = release
   bar plus the next bar". Then collect M1 for ±2h around tier-1 releases (the plan's own
   event-window design, a few GB) and let the minute rungs run for those.
5. **Sweep-level FDR** for the ranking screen. The vol-check already has the pattern.
6. **Regime as a first-class split.** The CPI result alone justifies it. `Regime` choices
   exist in `quality/enums.py` and `MarketEvent` holds the cut dates; the curve just needs a
   `regime` filter on the pooling step and a stacked display. Also the cheapest way to get a
   holdout back: explore on 2007–2018, confirm on 2019–, per the `Sample` enum that already
   exists.
7. **Start the forward capture running.** 16 point-in-time forecasts after several weeks of
   the project's life means the Task Scheduler entry is not in place or not firing. Every
   missed week is gone. This is the only path to Mode B.
8. **Load MT5 as a second calendar source.** It switches on `cross_source`, supplies
   `period` so releases can be re-keyed off the provisional `release:<minute>` identity, and
   its stored forecast can be tested for point-in-time-ness against the forward captures
   (P0.5 Q3). `mt5_audit` is written and waiting.
9. **Set `actual_provenance = first_print`** for the ForexFactory source now that the agency
   check has established it, and populate `actual_first_print` from `actual_current` for that
   source. Then ALFRED can fill `actual_current` with revised values and the two columns will
   mean what their names say.
10. **Local-time baseline matching (§3.3d).** Small.
11. **Co-timed confounding in Mode A.** Either exclude from a pairing's sample the releases
    whose hour also holds a heavier release of another indicator (the vol-check's blocked-bar
    logic, reused), or build the §6.3 joint regression. The former is an afternoon.
12. **Reword `VolCheck.OFFSET`** and show the series verdict beside the per-release grade, as
    the agency document recommends.
13. **Stop overloading `r_squared`.** Add `abs_ratio` to `DecayCurve`.
14. **The study engine (§0a).** `StudySpec` as a real JSON schema, a form, a runner that
    pools across pairs and across a concept, and a `Hypothesis` row created on every run. This
    is P4 and it is the thing that turns a very good vertical slice into the platform the
    plan describes.

---

## 9. Follow-up: the questions this dataset can answer next

In rough order of effort.

- **Pool across pairs.** The NFP curve exists on seven pairs. Pooling them at the concept
   level (§6.4 wants this) multiplies n by seven and would make the +1w and +2w rungs
   readable for payrolls and CPI.
- **Does the press conference or the decision move the ECB?** The 45-minute gap between the
   13:15/12:45 decision and the press conference is inside the "+1h" two-bar window. M1
   bars would split it. The same question for the FOMC from 2011.
- **Which member of the Employment Situation drives the move?** Only answerable with
   surprises (§3.3 Case B). Blocked on Mode B.
- **Asymmetry.** `beta_up` and `beta_down` are computed and stored nowhere. ABDV found bad
   news moves more than good. Cheap to surface.
- **Does a big move earlier in the week dampen the next release?** `CurrencyState` is
   designed and empty. The cumulative abnormal move over 1/3/5 days is a query on
   `EventImpact`; the interaction term is one more regressor.
- **Event-anchored windows.** "Until the next tier-1 event for either currency" is defined
   and unimplemented. With release groups and importances in place, computing the window
   end per event is a bisect.
- **Regime × indicator heatmap.** The §4.4 table for every indicator. Would show at a glance
   which releases mattered when.
- **Vol-check at M1** for tier-1 releases: the `confirmed` grade, and the :45-vs-:30 question
   for the antipodean releases.
- **A written finding per indicator** (P6). The evidence for the top fifteen is in the
   database now. Commentary has zero rows.

---

## 10. Verification performed for this review

- Read planning.md (918 lines), README, `docs/agency_verification.md`, and the full source of
  `analytics/`, `studies/`, `quality/validation.py`, the two derivation commands and the
  ranking view.
- Ran the test suite: 222 tests, all pass, 12.4 s.
- Queried every results table: 86,450 releases, 66,186 timestamped, 608,344 impacts, 5,160
  curve points, 231 Mode A and 229 direction pairings, 25 release groups, 0 study specs, 0
  hypotheses, 0 currency states, 0 outlier flags, 16 point-in-time forecasts.
- Recomputed indicator-level vol-check verdicts from the stored grades with the project's
  own `summarise`/`apply_fdr`: 259/97/2/1, matching the README.
- Calibrated the ratio statistic on 400 random non-event hours (§3.3a). This is a check the
  project does not perform and should.
- Recomputed +1h and +1d excess for NFP, CPI, FOMC, ECB and ISM on EUR/USD with
  `measure_event` and split by the `MarketEvent` regime dates (§4.4).
- Listed the per-release inputs behind the CHF Libor direction fit (§3.3e).
- Counted baseline survivors per horizon for the three weekly US series (§3.3c).
- Cross-checked the qualitative findings against ABDV 2003 and the pre-announcement liquidity
  literature (sources linked inline).

Nothing in the stored numbers disagreed with what the README claims. The problems found are
in how some of those numbers are labelled and pooled, and in what has not been built yet, not
in the arithmetic that produced them.


Sources used for the literature cross-check: ABDV 2003, AER (https://www.aeaweb.org/articles?id=10.1257%2F000282803321455151), St. Louis Fed 2007 on jumps and macro announcements (https://s3.amazonaws.com/real-dev.stlouisfed.org/wp/2007/2007-032.pdf), Evans and Lyons on news transmission (https://faculty.georgetown.edu/evansm1/wpapers_files/Announcements_dec03.pdf), Haynes, CFTC, on automated trading around announcements (https://www.cftc.gov/sites/default/files/idc/groups/public/@economicanalysis/documents/file/oce_macroannouncement.pdf).