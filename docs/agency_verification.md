# Agency verification — P2

*Run 2026-09-18. Twenty-one releases checked by hand against the issuing
agency's own release page: the value, the reporting month, and the stated
embargo time.*

planning.md §11 asks P2 to "manually verify ~20 events against agency release
pages, spread across the 19 years." This is that check. It is the only part of
the validation harness that compares our data against the *source of truth*
rather than against price — the vol-check can only say whether price agrees
with our clock, never whether ForexFactory copied the number down correctly.

## Method

Three indicators with an authoritative, archived publisher:

| Indicator | Publisher | Page |
|---|---|---|
| Non-Farm Employment Change | BLS Employment Situation | `bls.gov/news.release/archives/empsit_MMDDYYYY.htm` |
| CPI m/m | BLS Consumer Price Index | `bls.gov/news.release/archives/cpi_MMDDYYYY.htm` |
| Federal Funds Rate | FOMC statement | `federalreserve.gov/newsevents/pressreleases/monetaryYYYYMMDDa.htm` |

For each release the page was read for the headline value, the month it
covers, and the embargo line ("embargoed until 8:30 a.m. (EST/EDT)" or "For
release at 2:00 p.m. EDT"). The embargo line is what makes this a *timestamp*
check and not only a value check: it states the intended release instant in
Eastern time, which converts to UTC only if our DST handling is right.

## Result — 21 of 21 correct

Every value matched. Every reporting month matched. Every release time matched
after conversion from the agency's stated Eastern time.

| Release (UTC) | Indicator | Ours | Agency says | Embargo |
|---|---|---:|---:|---|
| 2007-12-11 19:15 | Federal Funds Rate | 4.25 | "lower … to 4-1/4 percent" | 2:15 p.m. EST |
| 2008-03-07 13:30 | Non-Farm Employment | −63,000 | "−63,000" (Feb 2008) | 8:30 a.m. EST |
| 2008-03-14 12:30 | CPI m/m | 0.0 | "virtually unchanged" (Feb 2008) | 8:30 a.m. EDT |
| 2008-09-05 12:30 | Non-Farm Employment | −84,000 | "−84,000" (Aug 2008) | 8:30 a.m. EDT |
| 2008-09-16 12:30 | CPI m/m | −0.1 | "decreased 0.1 percent" (Aug 2008) | 8:30 a.m. EDT |
| 2008-09-16 18:14 | Federal Funds Rate | 2.00 | "keep … at 2 percent" | (2:15 p.m. EDT convention) |
| 2009-03-06 13:30 | Non-Farm Employment | −651,000 | "−651,000" (Feb 2009) | 8:30 a.m. EST |
| 2009-03-18 12:30 | CPI m/m | 0.4 | "increased 0.4 percent" (Feb 2009) | 8:30 a.m. EDT |
| 2011-03-04 13:30 | Non-Farm Employment | 192,000 | "increased by 192,000" (Feb 2011) | 8:30 a.m. EST |
| 2012-03-16 12:30 | CPI m/m | 0.4 | "increased 0.4 percent" (Feb 2012) | 8:30 a.m. EDT |
| 2013-09-06 12:30 | Non-Farm Employment | 169,000 | "increased by 169,000" (Aug 2013) | 8:30 a.m. EDT |
| 2015-09-16 12:30 | CPI m/m | −0.1 | "decreased 0.1 percent" (Aug 2015) | 8:30 a.m. EDT |
| 2016-06-15 18:00 | Federal Funds Rate | 0.50 | "1/4 to 1/2 percent" | 2:00 p.m. EDT |
| 2016-09-02 12:30 | Non-Farm Employment | 151,000 | "increased by 151,000" (Aug 2016) | 8:30 a.m. EDT |
| 2018-03-21 18:00 | Federal Funds Rate | 1.75 | "1-1/2 to 1-3/4 percent" | 2:00 p.m. EDT |
| 2019-03-12 12:30 | CPI m/m | 0.2 | "increased 0.2 percent" (Feb 2019) | 8:30 a.m. EDT |
| 2020-09-04 12:30 | Non-Farm Employment | 1,371,000 | "rose by 1.4 million" (Aug 2020) | 8:30 a.m. ET |
| 2022-06-15 18:00 | Federal Funds Rate | 1.75 | "1-1/2 to 1-3/4 percent" | 2:00 p.m. EDT |
| 2023-05-03 18:00 | Federal Funds Rate | 5.25 | "5 to 5-1/4 percent" | 2:00 p.m. EDT |
| 2023-09-13 12:30 | CPI m/m | 0.6 | "increased 0.6 percent" (Aug 2023) | 8:30 a.m. ET |
| 2024-03-08 13:30 | Non-Farm Employment | 275,000 | "increased by 275,000" (Feb 2024) | 8:30 a.m. ET |

Two conventions were confirmed rather than assumed:

- **`actual_current` holds the first print, not the revised figure.** Feb 2009
  payrolls are stored as −651,000, which is what BLS published that morning;
  the series has been revised several times since. Despite the column name,
  nothing in this sample carried a revision. `actual_first_print` is null for
  every release in the database, so the first-print value is currently reachable
  only through `actual_current` — worth making explicit before anything joins
  on it.
- **Federal Funds Rate stores the upper bound of the target range.** 2016-06-15
  reads 0.50 against a stated range of 1/4 to 1/2 percent; 2023-05-03 reads 5.25
  against 5 to 5-1/4. Consistent across all six FOMC checks.

### The DST boundary, confirmed from the agency side

March 2008 straddles the DST change (it began 9 March that year), and both
sides of it are in this sample:

- 7 March, payrolls: BLS says **EST**, we store 13:30 UTC.
- 14 March, CPI: BLS says **EDT**, we store 12:30 UTC.

Same publisher, same 8:30 local time, one week apart, and our stored instants
differ by exactly the hour the agency's own embargo line says they should.
That is P0.5 Q2 answered against the publisher rather than against price.

## What this found — the per-release vol_check grade is not a verdict

Three of the twenty-one carry `vol_check = offset`, whose label reads *"offset
— spike found, but not where the timestamp says"*:

| Release | Indicator | vol_check | Agency |
|---|---|---|---|
| 2008-03-07 13:30 | Non-Farm Employment | `offset` | timestamp correct |
| 2008-03-14 12:30 | CPI m/m | `offset` | timestamp correct |
| 2008-09-16 12:30 | CPI m/m | `offset` | timestamp correct |

A fourth, the −651,000 February 2009 payrolls — one of the largest prints in
the sample — reads `no_spike`.

None of these is a clock error. They are what a single-release scan does when
the biggest bar in a ±3 hour window happens to fall somewhere else: for one
release that is noise, not evidence. The indicator-level verdict already
handles this correctly — it is a binomial tail against the uniform null over
every release of the series, and it reads *aligned* for all three of these
indicators. Across the whole of CPI m/m, 87 of 235 individual releases are
graded `offset` while the series as a whole is comfortably aligned.

So the number is doing its job; the **word** is not. Read off a single event
page, "spike found, but not where the timestamp says" asserts that this
release's timestamp is wrong, which the agency page disproves in three cases
here. The per-release value is an input to the series test, not a finding about
that release.

Recommended, not done here because it changes a visible label:

- Reword `VolCheck.OFFSET` so the per-release phrasing does not read as a
  verdict — something closer to *"the strongest bar in the window fell
  elsewhere; only the series verdict decides the clock."*
- On the event detail page, show the **indicator's** verdict beside the
  release's own grade, so the two are never read in isolation.

## Coverage limit

BLS's online archive does not reach 2007 — `empsit_03092007.htm` and
`cpi_09192007.htm` both return 404, and so does every 2007 page tried. The
Federal Reserve's does (the 2007-12-11 statement above). Two intended 2007
checks were therefore replaced with March and September 2008 releases, which is
why the sample is 21 rather than 20 and why the earliest BLS confirmation is
March 2008 rather than 2007. web.archive.org is not reachable from this
environment; someone with a browser could close that gap.
