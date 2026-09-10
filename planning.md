# Planning — Forex Macro Event Impact Explorer

**Version:** v0.6 — single consolidated document. Supersedes v0.1–v0.5; all decision history is folded into Appendix A so nothing is lost.
**Environment:** Windows 11, single user, local.

---

## 0a. The goal — a testbed, not an answer machine

> "Both of your questions are valid and there are probably like 20 more edge cases or more. But more important thing here is to create a platform where I can test all edge cases and learn about macroeconomics and calendar events' impacts. That's the goal rather than getting a clear result or forecast or predict something."

**This is the governing requirement and it outranks everything below it.** The product is a place to ask questions, not a report that answers one. Concretely:

| Consequence | What it means in the build |
|---|---|
| Studies are **configured, not coded** | A study is a saved specification — event selector, instruments, window scheme, baseline, controls, filters, mode — that you edit in the UI and re-run. Adding a new question must never mean editing Python. |
| **Nothing is hardcoded** that you might want to vary | Window lengths, baseline method, outlier policy, pooling, regime cuts, conditioning variables: all parameters with defaults, not constants. |
| A **hypothesis log** is a core feature | Every test recorded with its question, settings, result and date. This is what makes it a learning tool rather than a chart generator, and it's also the defence in §6.9. |
| **The Python layer stays open** | `analytics/` returns DataFrames from plain functions. You can bypass the UI entirely and work in a notebook against the same data and the same engine. The UI is a convenience over the library, not a wall around it. |
| **Being wrong must be cheap and visible** | Re-running a study after changing one setting takes seconds, results are cached and versioned, and a changed setting invalidates the cache rather than silently mixing generations. |
| The **danger shifts** | When testing is cheap, false discoveries get cheap too. §6.9 is now the most important honesty feature in the plan, not a footnote. |

Everything in §0b–§15 describes the *first* set of questions to point the platform at. They are examples of use, not the definition of the product.

---

## 0b. The first question, in your words

> US announces its unemployment rate, it drops 10%, and right after that the market moves in favour of the dollar — EURUSD falls for a week or so, GBPUSD similarly. So we can say this event has *this* impact, for *this* long, of *this* size. Effects of multiple events overlap; some events matter more than others; some effects show up even before the announcement.

That is the product. Four measurable quantities per event type, per pair:

| Quantity | Question it answers |
|---|---|
| **Direction** | Which way does the pair move, and how consistently? |
| **Magnitude** | How much, in pips and in σ of normal movement? |
| **Duration** | How long before the move dissipates into noise? |
| **Anticipation** | How much of the move happens *before* the release? |

Plus two structural questions: **which events matter most** (measured, not taken from a website's traffic-light rating), and **how to separate overlapping events** from each other.

### What this changes from v0.3

I had made the forecast/consensus column the load-bearing element — the earlier §3 said that without it the app "explains nothing." **That was too strong, and it was measuring a different thing than you asked for.** Your question is *what does the market do after this event*. That is answerable with two inputs: event timestamps and price. Forecast values make the answer sharper — they let you separate "the number was good" from "the number beat expectations" — but they are an **enrichment layer, not a gate.**

Practical consequence: **the project de-risks substantially.** If your MT5 calendar only reaches 2017, you still have a working tool over 2007–2017 using event timestamps alone. That was previously listed as the biggest open risk; it is now a degradation, not a blocker.

The collection plan doesn't change — we still want every field we can get. What changes is the order of dependency, and §3 below is rewritten accordingly.

### A note on the word (resolved)

You've confirmed you meant **macroeconomics**, which is right and is the term used throughout this document. Worth knowing anyway: the seminal paper on exactly this question is titled *Micro Effects of Macro Announcements* (Andersen, Bollerslev, Diebold & Vega, 2003), where "micro" refers to the fine-grained price behaviour and "macro" to the announcement. The field's name for what you're building is **event-study analysis of macroeconomic announcements**.

---

## 1. Locked decisions

| # | Decision | Consequence |
|---|---|---|
| 1 | **Windows 11** | MT5 runs natively. No Wine, no VM, no fallback needed. This removes the only risk that could have forced a plan change. |
| 2 | **The dataset is a product.** Multi-source, merged, provenance-tracked. | §6 (consolidation) becomes a core subsystem, not glue code. Exportable to CSV/Parquet. |
| 3 | **2007 → today.** ~19 years. | ~228 NFP releases, ~228 US CPI prints, ~152 FOMC decisions. Big enough that regime splits become statistically meaningful — a real upgrade over the 5-year plan. Also means the sample contains several genuine data landmines (§4.6). |
| 4 | **Backtest *and* forward test.** Continuous forward capture. | The one thing that can't be purely manual — §7.3. Miss a week and that week's point-in-time forecast is gone permanently. |
| 5 | **No paid licence.** | MT5 calendar + ForexFactory + official agencies + DBnomics/ALFRED. |
| 6 | **Majors only.** | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD. Economies: US, EZ, UK, JP, CH, AU, CA, NZ. |
| 7 | **1-minute resolution.** | ~50M bars total (§4.5). Ticks only for event windows, never bulk. |
| 8 | **Manual, button-triggered collection**, with source and quality visible. | Ingestion console is a primary screen (§7). One exception in §7.3. |
| 9 | **Crawl all public sources**, API or not. | Risks stated once in §5.4, then dropped. |
| 10 | **Data and commentary strictly separated.** | Enforced in the data model (§9), not by convention. |
| 11 | **Django** for the web layer. | Django + SQLite + Parquet/DuckDB + HTMX. |
| 12 | **All timestamps in UTC.** | With an important correction to your assumption — §4.1. |
| 13 | **Horizons from −5 days to +1 month**, not just minutes. | §3.2. The decay curve — effect size and R² against horizon — becomes the headline output. Attribution confidence falls as the horizon grows; that is measured and displayed, not hidden. |
| 14 | **Forecast values are an enrichment layer, not a gate.** | §3.5. The timestamp-and-price path stands alone, so patchy forecast history degrades the answers rather than blocking them. Downgrades the largest risk in v0.3. |
| 15 | **Same-instant releases are attributed as a group**, never individually. | §3.3 Case B. Accepted as the working unit, not a compromise: you want indicator combinations anyway. |
| 16 | **The platform is the product** (§0a). | Studies are saved specifications, not code. Hypothesis log is core. Python layer stays open for notebook work. |
| 17 | **The measured curve is the deliverable.** Shape classification (jump / drift / anything else) is an optional lens, off by default. | §3.2a. The platform reports what price did at each horizon. It does not decide for you what shape that is. |
| 18 | **Event-anchored windows** alongside fixed ones. | §3.2b. "Until the next big event for either currency" is a window scheme, tested directly rather than approximated by a fixed horizon. |
| 19 | **Conditioning variables** (currency state) are first-class. | §3.4a. Lets you test whether a recent big move dampens or amplifies the next event's impact. |
| 20 | **Both single-event and pooled curves.** | §6.2. A curve for one specific release, not only the average across 228 of them. Different questions, both available. |
| 21 | **Flexibility for future hypotheses is the requirement.** Statistical guards against false discovery are available but optional. | §6.9. You asked for an architecture that accommodates tests you haven't thought of yet. That is a design constraint. The FDR/holdout machinery is a tool you can switch on, not a gate. |

---

## 2. What the tool is

A local Windows app that:

1. **Collects**, on a button press, FX-relevant economic calendar events and 1-minute FX price history from a register of named free sources, 2007→now.
2. **Merges** them into one canonical dataset, keeping field-level provenance and flagging disagreement rather than silently picking a winner.
3. **Shows** where every number came from and how much to trust it.
4. **Aligns** events to price on a UTC timeline and measures, for each event type and pair: **direction, magnitude, duration and anticipation** (§0), across horizons from −5 days to +1 month.
5. **Disentangles** overlapping events by regressing on all events in the window jointly, and refuses to separate ones that share a timestamp.
6. **Keeps collecting forward**, weekly, so the dataset improves as it runs.
7. **Separates** measured data from any interpretation of it.

Not: a trading system, a signal generator, a backtester, or a real-time product.

---

## 3. What we are measuring

### 3.1 The minimum viable chain

**Premise 1.** An economic release lands at a known instant.
**Premise 2.** We have the price of each major pair on both sides of that instant.
**Inference 1.** We can measure what price did afterwards — direction, size, and how long before it dissipates.
**Premise 3.** Price also moves for reasons unrelated to this event.
**Inference 2.** A single occurrence tells us nothing; we need the *average* behaviour across many occurrences of the same event type, measured against what price normally does at that weekday and hour.
**Inference 3.** Therefore the two hard requirements are (a) accurate release timestamps in one unambiguous clock and (b) continuous 1-minute price coverage over the full window. Nothing else is load-bearing.
**Conclusion.** Timestamps and price are the foundation. Forecast values, revisions and vintages are refinements layered on top — each one sharpens the answer, none of them gates it.

### 3.2 The horizon ladder, and why the answer decays

Your example spans a week. That is a much longer horizon than the minute-scale windows v0.3 planned for, and it introduces a real problem that I want stated plainly rather than discovered in phase 4.

**Reasoning:**
> *Premise:* The event's price effect is roughly fixed in size — say a release typically moves EURUSD 40 pips.
> *Premise:* Background noise grows with the square root of time. If EURUSD's daily σ is ~55 pips, its hourly σ is ~11 pips and its weekly σ is ~120 pips.
> *Inference:* At +1 hour the event is 40 pips against 11 pips of noise — it dominates. At +1 week it is 40 pips against 120 pips — it is a minority contributor even if the entire move persists.
> *Inference:* Expressed as variance share: the event explains most of the +1h window, and at best around 10% of the +1w window. In practice less, because part of the initial move typically reverts.
> *Conclusion:* Attribution confidence decays with horizon. Short windows give clean answers about a small thing; long windows ask the interesting question but return mostly noise.

*(The 40/55/120 figures above are placeholders for illustration, not measurements. The tool's job is to produce the real ones. The arithmetic linking them — noise scaling with √time, variance shares — is standard and not in dispute.)*

**The statistical-power consequence, which is the uncomfortable part.** A standard power calculation says detecting an effect that explains ~2% of variance, at 80% power, needs roughly 390 observations. Nineteen years gives about 228 monthly releases. **So for a single indicator on a single pair at weekly horizon, we are probably underpowered.** Three honest responses, all of which the tool should support:

1. **Pool.** Same indicator across all 7 pairs, or same event class (all inflation prints) across countries. Raises n into the thousands.
2. **Report power, not just p-values.** Show the minimum effect size detectable at each horizon given the n available. A null result then means "no effect larger than X", which is informative, rather than "no effect", which is not.
3. **Accept that "how long does it last" may resolve to "hours."** That is a finding, not a failure — and it directly contradicts a widespread belief, which makes it worth producing.

**Design consequence — the horizon ladder.** Every event is measured at every horizon, not just short ones:

`−5d, −1d, −4h, −1h, −15m, −5m` → `t0` → `+1m, +5m, +15m, +30m, +1h, +4h, +1d, +3d, +1w, +2w, +1M`

and the primary output is a **decay curve**: effect size and R² plotted against horizon. That single chart answers "for how long" better than any table.

### 3.2a Two shapes of effect — and a correction to §3.2

**§3.2 above assumes the wrong shape for your hypothesis, and the assumption is doing all the work.** Keeping it here rather than deleting it, because it is correct for the case it describes. But there are two shapes, they behave oppositely, and the platform must test which one occurs rather than assume either.

**Shape 1 — a jump.** Price steps to a new level at release and stays there. The effect is a fixed number of pips. Noise grows with √time. So detectability only ever falls with horizon. This is what §3.2 models, and it's why §3.2 concluded that long horizons are hopeless.

**Shape 2 — a drift.** The release changes the *direction of pressure* for a period. Price keeps moving day after day until the pressure stops. This is your hypothesis: "USD has higher strength for the next few days." Here the effect *accumulates* — it grows roughly with time — while noise still grows only with √time.

> *Premise:* Under a drift lasting D days at rate μ per day, the accumulated effect at horizon t is μ·min(t, D).
> *Premise:* Noise at horizon t is σ√t.
> *Inference:* Detectability is μt/(σ√t) = (μ/σ)√t while t ≤ D — it **rises** with horizon.
> *Inference:* Past D the effect stops growing but noise doesn't, so detectability falls.
> *Conclusion:* Under a drift the detectability curve is **humped, and its peak sits at D.** Measuring at every horizon therefore *estimates the duration* rather than requiring us to guess it.

**This is the single most useful consequence of the whole horizon ladder.** The shape of the curve identifies which mechanism is operating, and if it's a drift, the peak reads off how many days it lasted.

Illustration, using the same placeholder scale as §3.2 (σ = 55 pips/day; drift of 20 pips/day lasting 5 days vs. a one-off 40-pip jump):

| Horizon | 1d | 2d | 3d | 5d | 7d | 10d | 14d |
|---|---|---|---|---|---|---|---|
| Drift (hypothesis) | 0.36 | 0.51 | 0.63 | **0.81** | 0.69 | 0.58 | 0.49 |
| Jump | **0.73** | 0.51 | 0.42 | 0.33 | 0.27 | 0.23 | 0.19 |

**Design consequences.** You've since clarified that the classification isn't the point: *"it doesn't matter if something hump, jump or dump, entire goal is to see what it does at specific time."* So:

- **The curve is the output.** Measured value at every horizon, plotted. No fitted model sits between you and the numbers.
- **Shape classification is an optional lens**, off by default. Switch it on when you want a summary; ignore it otherwise.
- The arithmetic above is kept because it explains *why* a curve rises or falls, which is useful when reading one. It is an explanation, not a processing step.
- No single "the effect is X pips" headline figure anywhere. A horizon is always attached.

**A note on noise cancelling out.** Random movement does not shrink with time — it grows. What corrects it is **repetition**: across ~228 releases the random parts average toward zero and the common effect survives. That is why sample size, not horizon length, is the binding constraint.

### 3.2b Event-anchored windows

Your framing was "until the next event, or the next big event from USD or EUR." That is not a fixed horizon, and approximating it with one loses the thing you're actually testing.

**Window schemes, all selectable per study:**

| Scheme | Definition | Use |
|---|---|---|
| `fixed` | the ladder in §3.2 | Comparability across events; the default |
| `until_next_own` | t0 → next tier-1 event for the *same* currency | Tests "the effect holds until that currency next hears something" |
| `until_next_either` | t0 → next tier-1 event for *either* currency in the pair | Your stated version |
| `until_next_any` | t0 → next event of any importance | Strictest, shortest windows |

**The catch, stated up front:** variable-length windows contain variable amounts of noise, so raw returns across them aren't comparable. Every result is normalised by √(window length), and window length is reported alongside every estimate. A study that skipped this would show long windows as more impactful purely because they're longer.

### 3.3 Overlapping events — and one limit that cannot be engineered away

You identified this correctly as a core difficulty. There are two distinct cases, and they are not equally solvable.

**Case A — events at different times inside the same window.** Over a week, dozens of releases across eight economies land inside the measurement window. Studying one at a time attributes other events' effects to whichever event you happen to be looking at.

*Fix:* multivariate regression. Regress the return over the window on **every** event that occurred inside it simultaneously, rather than one event at a time. Each coefficient is then the effect of that event *holding the others constant*. This is the standard treatment and it works, with two caveats: it needs more observations than the one-at-a-time version, and correlated events (US and Canadian employment often land the same morning) inflate the standard errors.

**Case B — events at the same instant. This one is not solvable.** Your example is the clearest possible case of it: **the US unemployment rate is not a standalone release.** It is published inside the Employment Situation report, at 08:30 New York, in the same instant as nonfarm payrolls and average hourly earnings. Three numbers, one timestamp.

> *Premise:* Two variables released at the identical instant produce identical time-series regressors.
> *Inference:* No amount of data separates them — the regression is perfectly collinear.
> *Conclusion:* The price move at 08:30 on payrolls Friday can be attributed to *the employment report*, never to the unemployment rate alone.

The only partial escape is the **surprise mix**: when payrolls beat but unemployment also rose, the two components disagree, and across many such occasions you can estimate their separate weights. **This requires forecast values** — which is the one place where the consensus column stops being an enrichment and becomes necessary. It is also, for what it's worth, the analysis that produces the genuinely non-obvious findings, such as wage growth often mattering more to FX than the headline jobs number.

**Design consequences:** a `release_group` field marking events sharing a timestamp; the UI attributing the move to the group, never to one member; and a collinearity check that refuses to report separate coefficients for perfectly co-timed events.

### 3.4 Anticipation — the pre-release window

You noted that some effect appears before the announcement. Real, and there are at least three distinct mechanisms worth distinguishing because they have different signatures:

- **Positioning.** Traders square up or lean into an expected outcome in the hours before. Signature: drift starting hours out, low volume, often partially reversed at release.
- **Liquidity withdrawal.** Market makers widen or step back in the final minutes. Signature: spread widening and volatility collapse just before t0 — a *reduction* in movement, not an increase.
- **Information leakage or correlated early data.** Signature: directional drift that correlates with the eventual surprise.

The third is the interesting one, and it is testable: regress the pre-release return on the *eventual* surprise. A significant coefficient means the market knew something. That test **needs forecast values** — another enrichment-layer dependency, noted here rather than assumed away.

Pre-release windows are therefore first-class, not an afterthought: `−5d` through `−5m`, with spread and realised volatility tracked alongside return.

### 3.4a Currency state — does a recent big move change the next reaction?

Your second hypothesis: if USD already moved hard on Tuesday, Friday's event lands with *less* force, because part of the repricing already happened.

Testable, and worth noting that a credible story predicts the opposite. When a market is already moving, attention and positioning are elevated, and reactions can be *larger*, not smaller. Neither direction is safe to assume, which makes it a good question for the platform to settle per event type rather than in general.

**Mechanism:** define a **state** for each currency at each moment, then test whether the reaction to a new event varies with it.

Candidate state variables, all computed from data we already hold:

- Signed cumulative abnormal move for that currency over the last 1/3/5/10 days
- Signed cumulative surprise over the same lookbacks (Mode B only)
- Days since that currency's last tier-1 event
- Current realised volatility relative to its own trailing average
- Regime tag (§4.6)

**The test:** the reaction to event B is modelled as a base effect plus an adjustment that depends on the state before B. A negative adjustment means dampening; positive means amplification.

**Design consequences:** state variables are computed once and stored per currency per timestamp, selectable as controls or as interaction terms in any study, and usable as filters ("show me only events that followed a quiet week"). They are a platform feature, not one analysis — most of the "20 more edge cases" you mention will be state questions of this kind.

### 3.5 The surprise refinement (formerly the core, now the enrichment layer)

The earlier framing stays valid on its own terms and is retained rather than deleted, but demoted to what it is — the layer that makes the answers sharper:

**Premise.** Scheduled releases are known in advance; the expected value is largely priced before the release.
**Inference.** Price should respond to the *surprise* — actual minus expected — more than to the level.
**Inference.** `surprise = actual − forecast`, standardised by that indicator's own historical surprise σ, is what makes different indicators comparable and different releases of the same indicator commensurate.

**What you lose without it:** you can measure that an event moves price, but not *why* one occurrence moved it 60 pips and another 5. Effect sizes get noisier because good and bad surprises average against each other. You cannot decompose same-instant releases (§3.3 Case B) or test for leakage (§3.4).

**What you keep without it:** direction consistency, absolute-move magnitude, volatility response, duration and decay, spread behaviour, and an empirical ranking of which events matter — measured on absolute moves, which need no forecast at all.

**Conclusion.** Build the timestamp-and-price path first; it stands alone. Layer surprise on top wherever the forecast column exists, and mark every study with which of the two modes produced it.

### 3.6 What the tool can and cannot establish

- **Can:** that a class of event is followed by a measurably abnormal move, of a given size, direction consistency and decay profile, relative to matched normal periods.
- **Can:** rank event types by measured impact and compare that ranking to the traffic-light ratings the calendar sites publish. Cheap to compute, and a genuinely useful output.
- **Cannot:** establish causation. Simultaneity, common drivers and unobserved news are always available alternative explanations. The honest claim is *association under controls*, and the UI should word it that way.
- **Cannot:** separate same-instant releases (§3.3 Case B).
- **Cannot:** tell you a pattern will persist. That is `HYPOTHESIS`, permanently.

---

## 4. The hard problems

### 4.1 Timestamps — and a correction to your assumption

You said: *"Make all timestamp as forex market timestamp (I believe it's constant UTC or something)."*

**Half right, and the half that's wrong matters.**

- **Right:** UTC is constant. It has no daylight saving, never shifts, and is the correct internal storage format. Storing everything in UTC is exactly the right call.
- **Wrong:** there is no such thing as *the* forex market timestamp. FX is decentralised — no central exchange, no official clock, no consolidated tape. Every venue and every data provider stamps in whatever zone it likes.

And there's a subtlety that will bite if we're not explicit:

**The forex trading day is anchored to 17:00 New York, not to a fixed UTC hour.** The week runs from Sunday ~17:00 New York to Friday 17:00 New York, and the daily rollover happens at that instant. Because New York observes DST, that boundary is **21:00 UTC in summer and 22:00 UTC in winter**. This is also why most MT5 brokers run their servers on EET/EEST (UTC+2/+3) — it makes the daily candle close land on the New York 17:00 rollover.

So:

- **Storage:** everything UTC, timezone-aware, converted at the ingestion boundary by a per-source converter. Nothing downstream ever sees a naive datetime.
- **Display:** UTC by default, with an optional toggle to New York / London / Tokyo. Every chart axis is labelled with the zone it's showing.
- **Trading day:** a *derived* attribute anchored to 17:00 `America/New_York`, stored explicitly, with a note in the UI that it moves in UTC terms twice a year. Never inferred from the UTC calendar date.
- **The `+1d` window is defined as `t0 + 24h`, not "the next daily bar."** Daily bars inherit the rollover ambiguity; a fixed 24-hour offset doesn't.

**Per-source clocks — verified, not recalled:**

| Source | Clock | Conversion |
|---|---|---|
| HistData.com | Eastern Standard Time **with no DST adjustment** — stated in their own FAQ and file spec | Fixed UTC−5 year-round. **Do not use `America/New_York`** — it would apply DST and shift half the history by an hour. |
| Dukascopy | UTC / GMT | Direct. Verify against a known release before trusting. |
| MT5 calendar | **Trade server time** (`TimeTradeServer`), per MQL5 docs | Broker-dependent, usually EET/EEST, and **server DST rules have changed over the years**. The MQL5 community publishes a helper (`TimeServerDST.mqh`) specifically to correct historical server timezone shifts when exporting calendar history. Over a 2007-start window this is not optional. |
| ForexFactory feed | Carries a timestamp field | Verify against a known release; don't assume. |
| Official agencies | Local time with local DST | Use IANA zones. |

**Structural trap:** US and EU DST switch on different dates (US: 2nd Sun Mar / 1st Sun Nov; EU: last Sun Mar / last Sun Oct). For 2–3 weeks a year the NY↔London offset is not the usual one. Any hardcoded offset silently misaligns those weeks — 19 years × 2 windows = ~38 corrupted stretches.

**Validation rule:** a job cross-checks each high-impact event's stored timestamp against the realised-volatility spike in the price series. If the spike isn't within ±2 minutes, flag the row. This is how we find our own bugs.

### 4.2 Getting forecast/actual/previous/revised back to 2007

This is now the central data task. Four routes, and we need more than one.

**Route A — MetaTrader 5's built-in calendar. Primary.**

Verified from MQL5 docs and community threads:

- Ships with MT5, covering **900+ indicators across the 18 largest economies** — comfortably more than our 8.
- The `MqlCalendarValue` structure carries exactly your list: `time`, `period` (reporting period), `revision`, `actual_value`, `forecast_value`, `prev_value`, `revised_prev_value`, `impact_type`, `event_id`.
- **Depth is uncertain and terminal-dependent.** One user reports ~90,000 historical events back to January 2007; another in the same thread only saw 2017 onward. **We measure yours in P0.5 before planning around it.**

Three verified gotchas:

1. **Values are stored multiplied by 1,000,000**, and unset fields hold `LONG_MIN` (−9223372036854775808) rather than null. Divide by 1e6; treat `LONG_MIN` as null. Miss this and you get either six-orders-of-magnitude errors or −9.2×10¹⁸ outliers that will quietly destroy every standard deviation you compute.
2. **The Python `MetaTrader5` package has no calendar function at all** — it exposes prices, symbols, orders, positions, history, and nothing else. The standard bridge is an MQL5 script or EA inside the terminal writing CSV to `MQL5\Files`, which Python reads. Free exporters already exist in the MQL5 Code Base; we'll write a small one so we control the field set and the encoding (event names are localised by the terminal, so **force UTF-8 output** or non-English installs produce mangled bytes).
3. **Server-time clock** with historical DST shifts (§4.1).

**Route B — ForexFactory. Secondary, and your forward-capture channel.**

Verified: published as `ff_calendar_thisweek` in `.json`, `.csv`, `.xml` and `.ics` at `nfs.faireconomy.media`. Two verified constraints:

- The **weekly feed carries forecast and previous but not the released actual.**
- **Polling it hard gets you blocked quickly.** Community guidance is once a week, cached.

You said this is for real-time/forward use — correct, and it's the right tool for that. Its structural value is explained in §4.3.

For *historical* actuals, ForexFactory's calendar pages themselves carry actual and revised values, but that requires HTML crawling — brittle, heavier block risk, and a phase-3 job at the earliest.

**Route C — official agencies + ALFRED. Authoritative actuals and first prints.**

BLS, BEA, Census, Eurostat, ECB, ONS, BoE, Destatis, BoJ, RBA, BoC, SNB, RBNZ. ALFRED is FRED's archival counterpart and serves **vintages** — the number as first published, before revisions. This is the correct source for `actual_first_print`.

**Route D — DBnomics. The efficient way to cover eight countries' actuals.**

Verified: a free platform aggregating publicly available economic data from national and international statistical institutions into one standardised format with a unified Web API, deliberately non-opinionated — original codes preserved and numerical values never modified. That last property is exactly what you want in a provenance-tracked pipeline.

**Reasoning for including it:**
> *Premise:* We need actuals for 8 economies, and writing 13 separate agency scrapers is a large, permanently-maintained surface.
> *Premise:* DBnomics already aggregates those agencies behind one API, without altering values.
> *Inference:* One integration replaces most of that scraper surface for the actuals column.
> *Caveat:* It carries no survey forecasts, and it serves time series indexed by reference period rather than release events — so it validates and fills `actual`, it does not replace the calendar.
> *Conclusion:* Include as the actuals backbone; keep 2–3 direct agency integrations for the tier-1 US releases where release timestamps matter most.

**Honest gap:** if your MT5 terminal only reaches 2017, the 2007–2017 forecast column has no free structured source, and Route B doesn't backfill. The fallbacks are the ForexFactory HTML crawl (brittle, slow, block-prone) or public scraped datasets on GitHub/Kaggle (free, but unknown provenance — usable only with a `quality = unverified` grade). **This is the single biggest open risk in the plan and P0.5 resolves it in an afternoon.**

### 4.3 The look-ahead problem — the subtlest failure mode

Three different numbers can occupy the same "actual" cell: what printed at release, what it was revised to later, and what the database says today. Same for the forecast: a week before, an hour before, or whatever the site shows now.

Calendar sites revise displayed forecasts and show revised actuals on historical pages. **Scraping history today can therefore capture numbers the market never saw** — look-ahead contamination that silently inflates every result and produces clean-looking, wrong output.

**Mitigations, all structural:**

- Separate columns: `actual_first_print`, `actual_current`, `forecast_point_in_time`, `forecast_stored`. Never coalesce silently.
- Surprise is computed from the first print and the point-in-time forecast where available.
- Every chart states which pair of columns it used.
- **Forward capture (§7.3):** from day one, fetch next week's calendar *before* the releases, store it immutably, mark `point_in_time = true`. Backfilled history gets `point_in_time = false` and a lower grade. The trustworthy fraction of the dataset then grows on its own — which is precisely why your forward-testing requirement is also a data-quality feature.

### 4.4 Consolidating multiple sources — the heart of "creating the dataset"

**Reasoning for the join key:**
> *Premise:* Sources disagree on release timestamps (different clocks, different rounding, occasional errors).
> *Inference:* Timestamp cannot be part of the identity key — we'd create duplicate rows for the same release whenever two sources differ by a minute.
> *Premise:* Every source identifies the *reporting period* of a release (MT5 exposes it directly as `period`; agencies index by it natively).
> *Inference:* Reporting period is stable across sources in a way that release time is not.
> *Conclusion:* Identity is `(currency, canonical_code, reference_period, revision_no)`. Timestamps become *attributes* of that identity, one per source, and their disagreement becomes a measurable quality signal rather than a duplication bug.

**Field-level provenance.** Each of `forecast`, `actual`, `previous`, `revised_previous`, `release_time` records which source supplied it. The merged row is a composite, not a copy of one source.

**Priority order (configurable, not hardcoded):**

| Field | 1st | 2nd | 3rd |
|---|---|---|---|
| `release_time` | official agency schedule | MT5 | ForexFactory |
| `actual_first_print` | ALFRED vintage / agency | MT5 | ForexFactory |
| `actual_current` | DBnomics / agency | MT5 | — |
| `forecast_point_in_time` | FF weekly snapshot (captured pre-release) | — | — |
| `forecast_stored` | MT5 | FF backfill | scraped dataset (`unverified`) |
| `previous` / `revised_previous` | MT5 | agency | FF |

**Conflict handling.** Numeric fields compare within a per-indicator tolerance; timestamps within ±2 minutes. Outside tolerance the row is flagged `cross_source = disagree`, both values are retained, and it appears on the Data Quality screen. **We never silently pick.** A disagreement is information — it usually means one source has a bug, and sometimes it means a genuine revision we hadn't modelled.

**Export.** Since the dataset is a deliverable, the console gets an export button: full Parquet, or filtered CSV, with a companion `provenance.json` describing sources, fetch dates, parser versions and quality distribution. A dataset without that file is not reproducible.

### 4.5 Storage and download volume — checked, not assumed

- FX trades roughly 120 hours/week ⇒ ~374,000 M1 bars per pair per year ⇒ **~7.1M per pair over 19 years ⇒ ~50M bars for 7 pairs.** As Parquet with columnar compression this is well under 1 GB. DuckDB scans it comfortably on a laptop. Not a problem.
- **HistData is ~1,600 monthly zip files** (19 years × 12 × 7 pairs). Scripted download, resumable, with a per-file completion record. Not hard, but it's a job that runs for a while — which is exactly why §7 needs a real job runner rather than a request-cycle download.
- **Bulk Dukascopy tick is off the table.** A single month of EURUSD ticks can exceed 500 MB; 19 years × 7 pairs would be roughly 800 GB. **Ticks are fetched only for event windows** (±2h around tier-1 releases), and only for pairs involving that event's currency. That's a few GB, not a few hundred.

### 4.6 What a 2007 start actually contains

Longer history is mostly a gift — ~228 NFP observations makes regime splitting real rather than theatrical. But the sample now contains events that will wreck naive statistics:

- **2008–09 crisis** — volatility regime unlike anything since.
- **15 Jan 2015, SNB removes the EUR/CHF floor** — an intraday move of a size that will dominate any USDCHF variance calculation it's included in. Not an economic release, but it lands inside event windows.
- **7 Oct 2016 GBP flash crash** and **3 Jan 2019 JPY flash crash** — both are provider-dependent: different feeds show different extremes because there was no consolidated price. A concrete reason to cross-validate HistData against Dukascopy.
- **Mar 2020 COVID**, **2022 inflation shock**.

**Design consequence:** an `outlier_policy` on every study — `include` / `flag` / `winsorise` / `exclude` — defaulting to `flag`, never to silent exclusion. Plus a `market_event` table of known dislocations so the UI can annotate them rather than have you rediscover them as inexplicable dots on a scatter plot.

**Regime tags** for the split control: `pre-crisis` (2007–08), `crisis` (2008–09), `ZIRP/QE` (2010–15), `normalisation` (2015–19), `COVID` (2020–21), `inflation/hiking` (2022–23), `post-hiking` (2024–).

### 4.7 Consequence of the 1-minute decision

- **HistData M1 bars are bid-only** — their spec confirms ask appears in tick data, not bar data. HistData alone cannot give spread.
- **Dukascopy** gives bid and ask with millisecond timestamps, aggregatable to M1.
- **Plan:** HistData as the bulk M1 backbone (2000+, simple monthly zips); Dukascopy for event-window ticks, spread measurement, and as the independent second opinion. Two free sources means cross-validation — a limitation turned into a quality feature.

---

## 5. Source register

Each of these is a row in a `source` table with its own fetch button, policy and health — not a hardcoded script.

### 5.1 Calendar / events

| Source | Gives | Access | Depth | Role |
|---|---|---|---|---|
| **MT5 calendar** | forecast, actual, previous, revised-previous, revision no., impact, period, event time | MQL5 script → CSV → Python | 2007+ or 2017+, **measure it** | Primary. The only free source with all four value fields at depth |
| **ForexFactory weekly feed** | forecast, previous, impact, currency | `nfs.faireconomy.media/ff_calendar_thisweek.{json,csv,xml,ics}` | current week | **Forward point-in-time capture.** No actuals in the feed |
| **ForexFactory calendar pages** | + actual, revised | HTML crawl | deep | 2007–2017 backfill *if* MT5 falls short. Brittle |
| **ALFRED** | actual **vintages** (first prints) | free API key | very deep | Ground truth for `actual_first_print`, US |
| **DBnomics** | actuals across agencies, one unified API, values unmodified | free Web API | deep | Actuals backbone for all 8 economies |
| **Official agencies** | actuals + official release schedule | HTML / RSS / ICS | deep | Authoritative release timestamps for tier-1 |
| **Philly Fed SPF** | true survey consensus, quarterly | free download | 1968+ | Independent cross-check on MT5's forecast values |

### 5.2 Price

| Source | Gives | Depth | Clock | Role |
|---|---|---|---|---|
| **HistData.com** | M1 OHLC, **bid only** | 2000+ | **EST fixed, no DST** | Bulk M1 backbone |
| **Dukascopy** | tick bid/ask + volumes | ~2003+ | UTC (verify) | Event-window ticks, spread, cross-validation |
| **MT5 terminal** | M1 via Python API | broker-dependent | server time | Third opinion; not for depth |

### 5.3 What "quality" means concretely

You asked to see source and quality. These are visible fields, not internal notes.

**Per row:** `timestamp_confidence` (exact / minute / date_only / inferred) · `forecast_provenance` (point_in_time / vendor_stored / unverified / none) · `actual_provenance` (first_print / current_may_be_revised / unknown) · `cross_source` (agree / disagree / single_source) · `vol_check` (confirmed / off_by_Nmin / no_spike / not_checked) · `confounded` (another event within ±15 min) · `source_map` (which source supplied each field).

**Per source:** coverage %, gap list, last successful fetch, consecutive failures, terms note, robots status, fetch policy.

**Per fetch:** raw payload on disk with content hash, HTTP status, bytes, rows seen/new/changed, duration, parser version.

**Per chart:** a badge showing source, n, and the *worst* quality grade present in the sample. You should never see a chart without knowing what's underneath it.

### 5.4 On crawling — said once, then dropped

Your call, your machine, nothing redistributed, so the practical exposure is low. Three factual points, not arguments:

1. Some sites' terms prohibit automated access. That's contractual, not criminal; realistic enforcement is an IP block. **Verified:** people polling the ForexFactory feed hard do get locked out fast.
2. **Politeness is self-interested here.** Rate limiting, caching, an honest User-Agent, honouring `robots.txt` — these keep the tool working. That's the reason, not ethics.
3. **HTML scrapers are permanently unstable.** They break on redesign, silently, and usually emit plausible-looking wrong data rather than an error.

Design consequences: raw snapshots (so a parser fix is a re-parse, not a re-crawl), per-source fetch policy in the DB, parser version stamped on every row, and a schema-drift alarm when a parser's output shape changes.

---

## 6. Analytics

Two modes throughout, and every study is stamped with which one produced it:

- **Mode A — event-only.** Needs timestamps and price. Measures direction consistency, absolute magnitude, volatility, duration, spread. Works over the whole 2007→now span regardless of forecast coverage.
- **Mode B — surprise-conditioned.** Needs the forecast column. Adds signed effect sizes, same-instant decomposition and leakage tests. Runs wherever the data supports it.

### 6.1 Per event × instrument — the horizon ladder

- Anchor `t0` = actual release time; fall back to scheduled and mark it.
- **Pre-windows:** `−5d, −1d, −4h, −1h, −15m, −5m`.
- **Post-windows:** `+1m, +5m, +15m, +30m, +1h, +4h, +1d, +3d, +1w, +2w, +1M`.
- **Baseline:** `[−60m, −5m]` for short horizons; matched weekday-and-hour periods over the prior N weeks for long ones.
- Metrics per window: log return, absolute return, realised volatility, MFE/MAE, spread where available, and **abnormal** versions of each (observed minus matched-normal). The abnormal version is what makes this an event study rather than a picture of a move.
- `+1d` and beyond are defined as fixed offsets from `t0` (`t0 + 24h`), never as "the next daily bar" — daily bars inherit the rollover ambiguity of §4.1.

### 6.2 The curve — the primary output

**This is the deliverable.** *Signal relative to noise, plotted against measurement horizon.* Everything else in §6 supports producing it, and nothing replaces it.

Two levels, both first-class, neither substituting for the other:

**Level 1 — single-event curve.** One specific release, e.g. the US Employment Situation of 6 September 2024, against EUR/USD. At each horizon: that event's abnormal move divided by the pair's typical movement over the same span. **No averaging across events.** The noise figure comes from the pair's own behaviour in surrounding weeks, not from other releases, so a single event produces a complete curve. This is the *"not averaged out, value"* view.

**Level 2 — pooled curve.** The same computation across every occurrence of that event type, with confidence bands. Answers "what does this event type tend to do" rather than "what did this one do."

**The honest difference between them**, since it governs what each can support:

| | Single-event curve | Pooled curve |
|---|---|---|
| Shows | What happened around this release | What this event type tends to do |
| Can't distinguish | The event from anything else that happened that day | — |
| Sample | n = 1 | n = however many releases |
| Right for | Inspecting cases, spotting oddities, reading a specific week | Claims about the event type in general |

Both go in the platform. A single-event curve is not a weaker version of the pooled one; it answers a question the pooled one can't, and vice versa.

**Derived conveniences**, computed from the curve and shown beside it rather than instead of it:

- **Half-life** — the horizon at which the abnormal move has decayed to half its peak.
- **Significance horizon** — the last horizon at which the pooled effect is distinguishable from zero.
- **Detectability floor** — the smallest effect the available n *could* have detected at that horizon, so "no effect" reads as "no effect larger than X" (§3.2).

Whether abnormal returns persist for days after FX macro releases is a live question in the literature, not a settled one. This output is labelled `ESTIMATE`, and any claim about persistence into the future is `HYPOTHESIS`.

### 6.3 Overlap — joint estimation

- **Same-instant events** are grouped by `release_group` and modelled as one regressor. A collinearity check refuses to report separate coefficients for perfectly co-timed releases (§3.3 Case B).
- **Different-instant events inside the window** enter the model together:
  `abnormal_ret(window) = α + Σⱼ βⱼ·eventⱼ + ε`
  where `eventⱼ` is a dummy in Mode A and a standardised surprise in Mode B. Each βⱼ is then that event's effect holding the others constant.
- Report the **variance inflation factor** per regressor. Events that habitually co-occur (US and Canadian employment often land the same morning) will have inflated standard errors; the UI says so rather than presenting a confident-looking coefficient.
- Long horizons contain many events, so the model grows. Cap regressors at the highest-importance events in the window and pool the rest into a controls term.

### 6.4 Which events matter — measured, not asserted

Rank event types by mean abnormal absolute move at short horizon — **Mode A, no forecast needed**. Then compare that measured ranking to the high/medium/low ratings the calendar sites publish.

Worth building early: it's cheap, it needs only the foundation layer, it produces something immediately interesting, and disagreements between the measured and published rankings are exactly the kind of thing you're trying to learn.

### 6.5 Surprise-conditioned estimation (Mode B)

- `surprise_z = (actual_first_print − forecast) / rolling_std(surprise, last 20 releases)`
- `abnormal_ret(window) = α + β·surprise_z + ε`, with standard error, R², n.
- Separate fits for positive and negative surprises (asymmetry).
- **Same-instant decomposition:** for a release group, regress on all members' surprises jointly. This is the only way to learn whether the headline or a component drives the move.
- **Leakage test:** regress the pre-release return on the eventual surprise (§3.4).
- Regime splits per §4.6 — viable given 19 years.

### 6.6 Honesty features — product requirements, not chores

- **n and R² everywhere.** R² in this literature is typically low; a β without an R² misleads by omission.
- **Multiple-testing control.** 7 pairs × ~40 indicators × **17 horizons** ≈ tens of thousands of tests; at p<0.05 a large crop of spurious "significant" results is guaranteed by chance. The longer horizon ladder makes this worse than in v0.3, not better. Benjamini–Hochberg FDR flag beside every raw p-value.
- **Detectability floor** at every horizon, so nulls are interpretable (§6.2).
- **Attribution-confidence badge** per horizon: high at minutes, degrading to low at weeks, with the variance-share estimate behind it (§3.2).
- **Minimum-n gate.** Below 20, no coefficient — show the raw scatter instead.
- **VIF reporting** on joint models (§6.3).
- **Confound flag** and **session flag** (Asian hours, holidays, month-end, rollover). Note the confound window must scale with the horizon — ±15 min is right for `+5m` and meaningless for `+1w`.
- **Outlier policy** per §4.6, defaulting to `flag`.

### 6.7 Modelled expectation — optional, and a partial substitute for missing forecast history

Fit a simple baseline forecast per indicator (AR on the released series, or a random walk on the last print) and store `forecast_modelled` alongside the survey forecast.

Two uses:

1. **Coverage backstop.** Where the survey forecast is missing — likely for 2007–2017 if the MT5 calendar is shallow — a modelled expectation lets Mode B run in degraded form. It measures surprise-versus-model rather than surprise-versus-market, which is a *different quantity*, so it gets its own column, its own label, and never merges with the survey column.
2. **A second variable.** `survey_forecast − modelled_forecast` shows when economists collectively leaned away from what the data alone implied — itself a studied phenomenon.

Labelled `ESTIMATE`. Low priority; build it only if P0.5 shows the forecast history is thin.

### 6.8 The study engine — how §0a is actually delivered

A **study specification** is a saved, versioned object. Running it produces a result; editing any field produces a new version and invalidates the cache. Fields:

```
events      : indicator codes, release groups, countries, importance tier
instruments : which of the 7 pairs
mode        : A (event-only) | B (surprise-conditioned)
windows     : fixed ladder | until_next_own | until_next_either | until_next_any
baseline    : matched weekday-hour | trailing mean | none
shape       : jump | drift | both  (§3.2a)
controls    : other events in window, session, regime
conditions  : state variables as filters or interactions  (§3.4a)
pooling     : none | across pairs | across countries | across event class
outliers    : include | flag | winsorise | exclude
sample      : explore (2007–2018) | holdout (2019–2026) | full
```

Defaults produce a sensible study with zero configuration; every field is exposed for when you want to vary it. **Adding a new question never means editing Python.**

**The Python escape hatch is a requirement, not a nicety.** `analytics/` exposes plain functions taking and returning DataFrames, with no Django imports (§8). A notebook can build a spec, run it, and get the same numbers the UI shows. The UI is a convenience layer over the library.

### 6.9 Flexibility for future hypotheses — and optional guards

**Correcting a conflation from v0.5.** You said you mentioned multiple hypotheses *for the future* — meaning keep the architecture flexible for tests you haven't thought of yet. I read that as a request for statistical guardrails and made them mandatory. Those are two different things, and only the first is your requirement.

**The requirement: flexibility.** Concretely, what must hold so a hypothesis you invent in a year is testable without a rewrite:

- Every dimension is a parameter, not a constant (§6.8).
- New conditioning variables can be added to `currency_state` without touching the study engine.
- New window schemes plug in as functions returning `(start, end)` per event; the engine doesn't care how they're computed.
- New metrics plug into `event_impact` as columns; the curve renderer takes any numeric column.
- Results are stored per event per horizon (§9), never only as summaries, so a new question can usually be answered by re-querying rather than re-computing.
- The Python layer is open, so anything the UI can't express, a notebook can.

**Optional guards, off by default.** These exist because testing many things does raise the chance that something looks real by accident. Switch them on when you want them:

- **Run history.** Every study run recorded with its specification and result. Worth leaving on regardless — it's how you reproduce a chart you made three weeks ago, independent of any statistics argument.
- **Running test count** with an FDR-adjusted threshold alongside the raw one.
- **Holdout split.** Reserve a slice of years, explore on the rest, confirm against the reserve once.

**One factual note and then I'll drop it:** the holdout is the only one of the three that can't be added later. Reserving years after you've already looked at them provides nothing. Cheap now, impossible retroactively — worth an explicit yes or no rather than a default.

### 6.10 Fact vs. hypothesis labelling

Three labels used in code and UI:

- **`ESTABLISHED`** — peer-reviewed and replicated. FX reacts to the surprise, not the level (Andersen, Bollerslev, Diebold & Vega, AER 2003). Adjustment is fast while volatility stays elevated longer (Ederington & Lee, JF 1993). Standardising surprises by their own σ is the accepted pooling method (Balduzzi, Elton & Green, JFQA 2001). Spreads widen around releases, so mid-price returns overstate what was capturable.
- **`ESTIMATE`** — anything measured from our sample: every β, every reaction curve, every hit rate.
- **`HYPOTHESIS`** — sign stability across regimes; persistence of any pattern into the future; anything from fewer than ~30 observations; **any multi-day effect**, since post-announcement drift in FX is contested rather than settled, and our own long-horizon estimates are underpowered by construction (§3.2).
- **`ASSOCIATION, NOT CAUSATION`** — the correct wording for every measured effect. The tool observes what follows an event under controls; it cannot rule out common drivers or unobserved news (§3.6).
- **`UNKNOWN`** — e.g. whether MT5's stored `forecast_value` is the consensus *as of release* or a later snapshot. I found no documentation either way. P0.5 tests it by comparing MT5's stored forecast against a live-captured ForexFactory snapshot for the same release.

---

## 7. Collection design

### 7.1 The ingestion console

A Sources page lists each source with state (coverage, last fetch, health, quality grade), a date-range picker and a **Fetch** button. Pressing it enqueues a job; the page shows live progress; the result is a run record with rows seen/new/changed, errors, and a link to the raw snapshot.

### 7.2 Why a job runner, and which one

> *Premise:* 1,600 HistData files and 19 years of MT5 calendar export take minutes to hours.
> *Inference:* A synchronous request times out; a page refresh would restart it.
> *Inference:* So it must run outside the request cycle with state in the DB, so progress survives a reload.
> *Premise:* Single user, local, occasional button presses.
> *Inference:* Celery + Redis means installing and supervising two extra services on Windows for one user.
> *Conclusion:* A DB-backed `job` table plus a background worker thread started with Django, or `django-q2` (SQLite broker, no Redis). Same durability, no infrastructure.

**Idempotency:** every fetch is re-runnable. Upsert on the natural key from §4.4. A re-fetch that changes a value writes a `value_revision` row rather than overwriting — so you can see a site quietly changing a forecast after the fact, which is itself a quality signal.

### 7.3 Forward capture — the one exception to "manual only"

> *Premise:* Point-in-time forecast can only be captured *before* the release happens.
> *Premise:* You want continuous forward analysis, indefinitely.
> *Inference:* A missed week is permanently lost — unlike historical data, it cannot be re-fetched later.
> *Inference:* Relying on remembering to press a button every week guarantees eventual gaps.
> *Conclusion:* One scheduled task, and only one — a weekly ForexFactory snapshot, plus a post-week MT5 calendar refresh to fill in the actuals.

**On Windows 11 this needs no daemon:** a Task Scheduler entry calling `python manage.py capture_forward` weekly. Native, survives reboots, visible in the OS. The console shows the schedule and every capture, and a **Capture now** button for manual runs. Everything else stays button-driven as you asked.

Suggested cadence: Sunday (before the week opens) for the forward snapshot; Saturday for the actuals backfill.

---

## 8. Architecture

```
                    ┌──────────────────────────────────────┐
                    │  Django (UI + ORM + admin)           │
                    │  Sources console  [Fetch] [Export]   │
                    │  Calendar · Event · Study · Quality   │
                    └──────────────┬───────────────────────┘
                                   │ enqueue
                    ┌──────────────▼───────────────────────┐
                    │  job table (SQLite) + worker thread  │
                    │  ◄── Windows Task Scheduler (weekly) │
                    └──────────────┬───────────────────────┘
        ┌──────────────┬───────────┼───────────┬──────────────┐
        ▼              ▼           ▼           ▼              ▼
   mt5_calendar   forexfactory   alfred    dbnomics    histdata/dukascopy
        │              │           │           │              │
        └──────────────┴───────────┴───────────┴──────────────┘
                                   │  raw snapshot (content-hashed, on disk)
                                   ▼
                        normalisers (per source, versioned)
                                   ▼
                        consolidation / merge  (§4.4)
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
     SQLite: indicators, events, sources,    Parquet: price bars
     jobs, quality, commentary               (pair / year-month)
                └──────────────────┬──────────────────┘
                                   ▼
                    analytics/ (pandas + DuckDB)
                    alignment · event study · stats
                                   ▼
                          results cache
```

**Why SQLite + Parquet rather than one database:**
> *Premise:* Django's ORM needs a supported relational backend; DuckDB isn't one.
> *Premise:* 50M M1 bars is small for Parquet/DuckDB and unpleasant in SQLite for repeated analytical scans.
> *Premise:* Event metadata is small, highly relational, and benefits from Django admin for manual inspection and correction — which you'll need for the canonical-code mapping.
> *Conclusion:* Split by workload. DuckDB can query Parquet and SQLite in one statement, so joining isn't a problem. Postgres + TimescaleDB stays documented as the migration path if this ever becomes multi-user — **kept as a fallback, not built.**

**Stack:** Django 5.x · SQLite · server-rendered templates + HTMX (single user, no build step; **React + DRF kept as a documented alternative** if the study screens need real client-side state) · `lightweight-charts` for price-around-event, Plotly for scatter/regression · pandas, numpy, statsmodels, DuckDB, pyarrow · MQL5 export script for the calendar bridge.

**Windows specifics:** Python 3.12 (MT5 package compatibility is the constraint — confirm before installing) · MT5 terminal installed with the calendar enabled in Tools → Options → Server · calendar CSV lands in `%APPDATA%\MetaQuotes\Terminal\<hash>\MQL5\Files` · **force UTF-8 in the MQL5 exporter**, since the terminal localises event names and the local codepage will mangle them · long-path support on if the raw snapshot tree gets deep.

**Repo layout:**

```
fxmacro/
  manage.py
  config/            # settings, urls
  sources/           # Source, FetchRun, RawSnapshot + console
  calendar_data/     # Indicator, EventRelease, ValueRevision
  prices/            # Instrument, parquet paths, loaders
  collectors/        # one module per source. NO django imports
  normalisers/       # per-source → canonical, versioned
  consolidation/     # merge, priority, conflict detection
  analytics/         # alignment, event study, stats. pandas only
  commentary/        # Commentary model, epistemic labels
  quality/           # vol-check, cross-source, coverage
  studies/           # study views/templates
  mql5/              # CalendarExport.mq5
  data/
    raw/             # immutable fetched payloads, content-hashed
    parquet/         # price bars
    exports/         # dataset exports + provenance.json
```

`collectors/` and `analytics/` import nothing from Django — deliberate, so they stay unit-testable and a future move off Django touches only the web layer.

---

## 9. Data model

```
source(id, key, name, kind, base_url, timezone_rule, fetch_policy_json,
       terms_note, robots_status, enabled, last_fetch_at, health)

fetch_run(id, source_id, started_at, finished_at, status, params_json,
          rows_seen, rows_new, rows_changed, error_text,
          snapshot_path, snapshot_sha256, parser_version)

indicator(id, country, currency, name, canonical_code, unit, frequency,
          importance, explainer_md, transmission_channel)

release_group(id, key, name, country, description)
              -- events sharing one timestamp, e.g. US Employment Situation
              -- = payrolls + unemployment rate + average hourly earnings

event_release(id, indicator_id, release_group_id, reference_period, revision_no,
              release_time_utc, scheduled_time_utc, trading_day,
              actual_first_print, actual_current,
              forecast_point_in_time, forecast_stored, forecast_modelled,
              previous, revised_previous,
              surprise_raw, surprise_z,
              timestamp_confidence, forecast_provenance, actual_provenance,
              cross_source, vol_check, confounded,
              source_map_json,        -- which source gave which field
              first_seen_at, last_seen_at)

source_observation(id, event_release_id, source_id, fetch_run_id,
                   observed_at, raw_json)   -- what each source said, kept

value_revision(id, event_release_id, observed_at, field,
               old_value, new_value, fetch_run_id)   -- append-only

market_event(id, ts_utc, label, kind, note)   -- SNB 2015, flash crashes, COVID

instrument(id, symbol, base_ccy, quote_ccy, pip_size)

price_coverage(instrument_id, source_id, month, bar_count, gap_count,
               first_ts_utc, last_ts_utc, parquet_path)

event_impact(event_release_id, instrument_id, price_source_id, horizon,
             ret, abs_ret, abnormal_ret, realized_vol, mfe, mae,
             spread_avg, n_bars, bars_missing, outlier_policy,
             engine_version, computed_at)
             -- one row per horizon on the ladder, pre- and post-, per event

study_spec(id, name, version, spec_json, created_at, parent_version_id)
           -- the §6.8 configuration object; editing forks a new version

study_run(id, study_spec_id, started_at, finished_at, sample,
          n_observations, engine_version, result_ref, notes)
          -- sample = explore | holdout | full

hypothesis(id, question_text, study_spec_id, created_at, status, note_md)
           -- optional plain-English label on a run; nothing depends on it
           -- holdout fields added only if you switch that guard on (§6.9)

currency_state(currency, ts_utc,
               cum_abn_move_1d, cum_abn_move_3d, cum_abn_move_5d, cum_abn_move_10d,
               cum_surprise_1d, cum_surprise_3d, cum_surprise_5d,
               days_since_tier1, rel_volatility, regime)
               -- §3.4a; usable as filter, control or interaction term

decay_curve(indicator_id_or_group_id, instrument_id, mode, horizon,
            effect_size, std_error, r_squared, p_raw, p_fdr,
            detectability_floor, n, attribution_confidence,
            regime, outlier_policy, engine_version)
            -- cached §6.2 output; half-life and significance horizon derived from it

commentary(id, scope_type, scope_id, body_md,
           basis,             -- literature | computed | generated | user_note
           epistemic_status,  -- established | estimate | hypothesis | unknown
           citation, author, created_at)
```

Notes:

- **`source_observation` is what makes the dataset a product.** The merged `event_release` is a derived view; every source's raw claim is retained separately, so the merge can be re-run with different priorities without re-crawling.
- `value_revision` is append-only — how you discover a site quietly changing a number.
- `engine_version` on `event_impact` so a methodology change invalidates the cache instead of mixing generations.
- `canonical_code` maps each source's naming onto one internal code (MT5 event id, FF title, FRED series id → `US_NFP`). **This will be the buggiest artifact in the project.** It gets a dedicated admin screen and an "unmapped events" report.
- `commentary.basis` + `epistemic_status` make your data/commentary separation structural. A `generated` row can never render inside the data panel.

---

## 10. Screens

1. **Sources console** — coverage bar, last fetch, health, quality grade, date-range picker, **Fetch**, run history, raw-snapshot links, **Export dataset**.
2. **Data quality** — coverage gaps, timestamp-confidence breakdown, cross-source disagreements, `no_spike` events, unmapped events, forward-capture log. Deliberately prominent.
3. **Calendar browser** — filterable 2007→now, signed z-score chip, quality badge per row.
4. **Event detail** — price chart centred on t0 with windows shaded; actual / forecast / previous / revised with per-field source labels; last-20-occurrences strip. **Data panel above; commentary panel below, visually distinct, collapsed by default, always labelled with basis and epistemic status.**
5. **Indicator study** — the main analysis screen. Average reaction path from −5d to +1M with confidence band; the **decay curve** (effect size and R² against horizon) with half-life and significance horizon called out; surprise-vs-return scatter with fitted line and `market_event` annotations where Mode B is available; n, R² and detectability floor always visible; regime, outlier-policy and Mode A/B toggles.
6. **Event ranking** — measured impact ordering (§6.4) side by side with the calendar sites' published ratings, disagreements highlighted. Mode A, so it works from the foundation layer alone.
7. **Overlap inspector** — for a chosen window, every event inside it, the joint model's coefficients, VIFs, and an explicit note on any `release_group` that cannot be decomposed.
8. **Comparison** — one indicator across 7 pairs, or one pair across indicators. Heatmap greyed out below min-n.
9. **Study builder** — the §6.8 specification as a form. Sensible defaults, every field editable, run in seconds, fork-on-edit with version history. This is the screen you'll live in.
10. **Run history** — every study you've run, with its specification, result and date, re-runnable in one click. Primarily a reproducibility and "what did I already try" tool. Optional statistical extras (test count, FDR threshold, holdout tracking) are toggles on this screen, off by default (§6.9).

---

## 11. Phases

**P0 — Skeleton.** Django project, models, admin, job table + worker, Sources console rendering with zero data, one collector stubbed end to end.

**P0.5 — Source audit. Do this first; it resolves the plan's biggest unknowns in about a day.** Four questions, four answers:
1. **How far back does *your* MT5 calendar go?** Export everything, count events, find the earliest. Determines whether 2007–2017 needs a backfill route at all.
2. **Where does each source think 08:30 New York is?** Pick an NFP Friday, confirm the volatility spike lands where each source's clock claims — including one release in each DST window.
3. **Is MT5's `forecast_value` point-in-time or updated?** Capture a live ForexFactory snapshot before a release, compare to MT5's stored forecast after. Resolves the `UNKNOWN` in §6.10.
4. **Do HistData and Dukascopy agree** on a normal hour and on a flash-crash hour?

**P1 — Vertical slice, Mode A.** US Employment Situation → EUR/USD, full history, end to end: fetch button → normalise → merge → align → decay curve across the full horizon ladder, with a quality badge. **No forecast column required**, which is what makes this the right first slice — it proves the foundation without waiting on the riskiest data.

**P2 — Validation harness.** Vol-spike cross-check, cross-source comparison, coverage reporting, Data Quality screen. Manually verify ~20 events against agency release pages, spread across the 19 years.

**P3 — Breadth + consolidation.** Tier-1 across all 8 economies, all 7 pairs, DBnomics/ALFRED integration, the canonical-code mapping work, `release_group` assignment, dataset export. **Event ranking screen (§6.4) lands here** — it needs breadth but not forecasts, so it's the first genuinely interesting output.

**P4 — Study engine and the curve.** The §6.8 specification object and study builder screen; single-event and pooled curves (§6.2); event-anchored windows (§3.2b); joint regression across events in the window with VIF reporting; confound windows scaled to horizon; detectability floors; outlier policies; regime splits. Run history on by default; other §6.9 guards available and off.

*Reasoning for pulling the engine forward:* hardcoded studies in P1–P3 are fine as scaffolding, but every week they remain hardcoded is a week of questions you can't ask without me. The engine is what makes §0a true, so it lands before the analysis breadth rather than after.

**P4.5 — Currency state.** State variables computed and stored (§3.4a); usable as filters, controls and interaction terms. Unlocks the dampening/amplification question and most of the "20 more edge cases."

**P5 — Mode B.** Surprise-conditioned estimation wherever the forecast column exists: signed effect sizes, asymmetry, same-instant decomposition, leakage tests. Modelled expectation (§6.7) only if P0.5 showed the forecast history is thin.

**P6 — Commentary.** Hand-written explainers for the top 15 indicators; epistemic labelling wired; generated-commentary path with its label.

**P7 — Ongoing.** Weekly forward capture running; dataset quality improving on its own.

---

## 12. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| False discoveries from testing many hypotheses | Medium — rises with how much you test | Medium — a false finding is anti-learning | §6.9 guards, **available but off by default**. Run history worth leaving on; holdout is a yes/no you should make deliberately since it can't be added later |
| **Long-horizon results are underpowered** | Medium — **downgraded**, since a drift accumulates while noise only grows with √t (§3.2a) | Medium | Pool across pairs and event classes; report detectability floors; fit both shapes and read duration off the curve's peak |
| Variable-length windows compared without normalising | High if unmanaged | High — longer windows look more impactful purely for being longer | Normalise by √(window length); report window length with every estimate (§3.2b) |
| **Same-instant releases can't be separated** | **Certain** | Medium — limits how fine-grained the answers get | Structural: `release_group`, group-level attribution, collinearity refusal (§3.3 Case B) |
| MT5 calendar only reaches 2017 | Medium — genuinely unknown | **Downgraded from v0.3: Medium.** Costs Mode B over 2007–2017; Mode A is unaffected | P0.5 Q1. Fallbacks: FF page crawl, unverified public datasets (quality-graded), or modelled expectation (§6.7) |
| Overlapping events misattributed at long horizons | High if estimated one-at-a-time | High — produces confident nonsense | Joint regression (§6.3), VIF reporting, horizon-scaled confound windows |
| Timestamp offset wrong for one source | High if unmanaged | Severe — wrong but plausible output | P0.5 Q2 + per-source converters + vol-check job |
| Look-ahead contamination from revised values | High if unmanaged | Severe | Separate columns, never coalesce, forward capture |
| ×1e6 / `LONG_MIN` mishandling on MT5 import | Medium | High but detectable | Unit tests against known NFP values; range assertions |
| MT5 server DST shifts across 19 years | **High** | High | `TimeServerDST`-style correction; validate at each DST boundary |
| Getting blocked by ForexFactory | Medium | Medium | Weekly cadence, caching, honest UA, policy in DB |
| HTML parser breaks silently | High over time | Medium | Raw snapshots, parser versioning, drift alarm |
| 2015 SNB / flash crashes distorting stats | **Certain** | Medium | `market_event` table, outlier policy defaulting to `flag` |
| Spurious findings from multiple testing | Very high if unmanaged | Medium | FDR, min-n gate, R² always shown |
| Missing a forward-capture week | Medium | Low each, cumulative | Task Scheduler + visible capture log with gap warnings |

---

## 13. Non-goals

- Not a signal generator or backtester.
- No execution, no broker orders.
- Not real-time (near-real-time weekly capture is the ceiling).
- Not a general macro database — FX-relevant scheduled releases for 8 economies.
- No multi-user, no auth, no deployment hardening.

---

## 14. Open questions (none blocking)

1. **Longest horizon worth carrying.** The ladder currently runs to +1 month. Beyond about a week the estimates will be dominated by noise (§3.2). Keeping them costs little and lets you *see* the decay; dropping them declutters the UI. My inclination is to keep them and label them, because watching an effect vanish is the point.
2. **Should the tool report the "no forecast" mode as a first-class answer**, or always caveat it as incomplete? My inclination: first-class. Direction consistency and absolute magnitude are real measurements in their own right.
3. Generated commentary: do you want me producing it (labelled), or only hand-written and literature-cited?
4. Should the Sources console accept **manual file upload** (drag in a HistData zip, or an MT5 CSV you exported by hand)? Cheap, and it saves fighting a downloader on a bad day.
5. P0 skeleton first so you can click around the shape, or straight through to the full P1 slice?

---

## 15. What I'd build first

**P0.5, before P0 if you like.** A single script that exports your MT5 calendar, pulls one month from each price source, and prints:

1. Earliest calendar event available, and how many events carry a non-null forecast by year — this tells us where Mode B starts.
2. Where each source thinks 08:30 New York is, on four specific dates: one in each DST window, one in the US/EU offset gap, one in winter.
3. Price-coverage continuity over a `−5d … +1M` window around one payrolls Friday, since the long horizons need unbroken bars across weekends and holidays and that's a different failure mode from the short ones.

That artifact de-risks more of this project than any other day of work, and it's a couple of hundred lines.

---

## Appendix A — Decision history

Consolidated from v0.1 and v0.2. Kept so the reasoning behind reversals stays visible.

| Originally | Now | Why |
|---|---|---|
| Consensus probably needs a paid licence | Free routes verified: MT5 calendar + FF feed | You ruled out a licence; I searched instead of recalling |
| Crawl official sources only | Crawl all public sources; risks stated once in §5.4 | Your decision |
| FastAPI + React + Postgres/Timescale | Django + SQLite + Parquet/DuckDB + HTMX | You require Django; single-user answers the DB question |
| Scheduled ingestion implied | Manual button-triggered, with exactly one scheduled task (§7.3) | Your requirement, plus the point-in-time constraint |
| Streamlit/Dash offered as a fast path | Dropped | Django is a requirement |
| 5 years history | 2007 → today (~19 years) | Your requirement. Upgrades regime analysis, adds §4.6 landmines |
| Single-source calendar assumed | Multi-source consolidation as a core subsystem (§4.4) | Your "could be multiple source" |
| Consensus as an input | The merged dataset is itself a deliverable, with export + provenance | Your earlier clarification |
| **Forecast column load-bearing; "without it the app explains nothing"** | **Enrichment layer (§3.5); Mode A stands alone** | **My framing was too strong and answered a different question than you asked. Your goal — what does price do after this event — needs timestamps and price. Retained as §3.5 rather than deleted, because it's still correct about what surprise adds** |
| Windows capped at +24h | Ladder from −5d to +1M, decay curve as headline output (§3.2, §6.2) | Your example spans a week. "For how long" becomes a measured quantity rather than an assumption |
| Overlap handled by a ±15 min confound flag | Joint regression across all events in the window, plus `release_group` for same-instant releases (§3.3, §6.3) | Your point about overlapping effects. A flag suppresses contaminated observations; a joint model actually separates them — except when it can't, which §3.3 Case B now states plainly |
| Pre-release treated as one `[−5m, 0]` window | Full pre-release ladder with three distinguishable mechanisms (§3.4) | Your point that effects appear before the announcement |
| MT5 shallow history = biggest risk | Downgraded to Medium | Mode A doesn't depend on it |
| **A tool that answers a fixed set of questions** | **A platform for testing arbitrary hypotheses (§0a)** | **Your stated goal. Studies become saved specifications; the hypothesis log and holdout become core; the Python layer stays open for notebook work** |
| **"Long horizons are mostly noise" (§3.2)** | **True for a jump, false for a drift (§3.2a). Retained, but scoped** | **I modelled a one-off jump; your hypothesis is sustained pressure. Under a drift the effect accumulates faster than the noise, so long horizons get *better* up to the duration — and the peak of that curve estimates the duration** |
| Fixed horizons only | Event-anchored windows added (§3.2b) | Your "until the next big event." Tests the actual claim instead of approximating it |
| No notion of market state | Currency state as a first-class concept (§3.4a) | Your dampening hypothesis, and the general shape of most further questions |
| Multiple-testing control as one bullet among several | The top risk, with a holdout reserved from day one (§6.9) | Follows directly from the platform reframe: cheap testing means cheap false positives |
| "Microeconomics" used in §0 | Corrected to macroeconomics throughout | Your correction |
| **Shape classification (jump vs drift) as a required output** | **Optional lens, off by default (§3.2a)** | **Your correction: the curve at each horizon is the goal, not a label for its shape** |
| **Pooled averages implied as the unit of analysis** | **Single-event curves are first-class alongside pooled ones (§6.2)** | **Your "not averaged out, value." A single release yields a full curve, since the noise estimate comes from the pair's own behaviour rather than from other releases** |
| **False-discovery guards mandatory; holdout a locked decision** | **Flexibility is the requirement; guards are optional and off by default (§6.9)** | **You meant keep the architecture open for future tests. I conflated that with wanting statistical guardrails. Only the flexibility is your requirement** |
| MT5 possibly needing Wine or a VM | Native | Windows 11 |
| "Forex market timestamp" | UTC storage; trading day derived from 17:00 New York (§4.1) | Your assumption was half right; the correction is documented rather than silently applied |
| Timescale, React+DRF, Celery | **Retained as documented fallbacks**, not built | The migration paths should exist if constraints change. Say the word and I'll strip them |