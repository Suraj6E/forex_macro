# Replay simulator

Candle-by-candle tick replay with simulated orders, tick volume, the economic
calendar on the chart, an automatic journal and drawing tools, plus a live
mode and broker tick history from your MT5 terminal. Nine instruments:
EUR/USD, GBP/USD, USD/JPY, EUR/JPY, GBP/JPY, EUR/GBP, silver (XAG/USD), WTI
crude (US) and Brent crude (Europe). Built from `Replay Simulator
Step-by-Step Build Guide.md`, steps 1 to 6. It is a separate app from the
Django site: its own server, its own port, its own journal. It reads the
trading-research tick file and fxmacro's calendar database and writes
nothing to either.

## Run

```powershell
.\simulator.cmd
```

That is the only command. It always opens at **<http://127.0.0.1:8001/>**;
Django keeps <http://127.0.0.1:8000/>. Launching again while it runs opens
the running one. If another program holds 8001 it says so and stops, rather
than moving to some other port (`SIM_PORT` overrides the port for one run).

Everything else happens by itself and shows in the page:

1. **Once**: the 2023–24 EUR/USD tick file is imported, tick files from older
   versions are rewritten in the compact layout, and candles are built (a
   minute or less). The page shows the progress and opens the session form
   when it is done.
2. **While it runs**: Dukascopy ticks are downloaded for every instrument
   switched on in **Market data**, newest days first, fifteen days of one
   instrument and then the next, so each becomes playable early and older
   history fills in behind. Candles are rebuilt as days arrive.
3. **Live recording and broker history**, once you have switched the
   recorder on in **Market data**, start again on their own.

One-time install, if the venv predates the simulator:

```powershell
.\.venv\Scripts\python.exe -m pip install -r simulator\requirements.txt
```

## Using it

| | |
|---|---|
| Start | **Sessions** → pick the **Instrument** and the **Data** (Dukascopy history, or your broker's), then **Replay history** (a start time in UTC, or **Random**; **Hide dates** shows weekday and time only) or **Live market**. Balance, leverage, commission, slippage. |
| Replay | **Play**/**Pause** (Space), speeds 1x to max (a market day in ~3 s), **+1 candle** (Right), **+1 tick** (Shift+Right), **Jump** to a time. |
| Live | The clock is the real clock; candles form as ticks arrive. No play, step or jump: the market has its own clock. |
| Volume | Tick volume under the candles, and `V` in the price line: the number of quotes in the candle. |
| News | A dot per impact level under each candle with releases: red high, yellow medium, near-white low. Filled is released, a ring is still to come. Hover for the names, click for the details. **High / Medium / Low** in the chart corner hide a level. |
| Orders | Market, limit or stop; size in lots or as % risk from the stop. SL/TP lines on the chart drag to edit. **Close** works per position. The ticket shows what a lot is and what a pip is worth in USD. |
| Drawings | Horizontal line, trend line, rectangle, position box. A selected position box has **Send as order**. Esc cancels, Delete removes. |
| Market data | The top-bar button (also in the session form): per instrument what is stored, from which day to download (**From**), on or off, what is missing; disk space; the calendar; the recorder. |
| Journal | <http://127.0.0.1:8001/journal>: stats with a 95% win-rate range, equity curve, splits by session/weekday/tag/exit/instrument, tags and notes, chart snapshots at entry and exit, **Review** to reopen any trade on the chart. |

Everything saves as it happens. Closing the tab, or stopping the server
mid-trade, loses nothing; reopening resumes at the same time with the same
positions. A live session that was closed catches up on reopening: every tick
recorded meanwhile is given to its orders and positions, in order.

## The economic calendar

Events come from fxmacro's own calendar (`db.sqlite3` in this repository,
`SIM_CALENDAR_DB` to point elsewhere), opened read only: about 62,000
releases of USD, EUR, GBP and JPY data with a time of day, 2007 onward, rated
high, medium or low by the calendar sites. It is as current as fxmacro's
collection; the simulator never collects. Each instrument shows its own
currencies (EUR/JPY: EUR and JPY; silver and oil: USD). Events without a
rating (bank holidays) or without a time of day are left out.

The replay clock rules here as it does for prices. The schedule, forecast
and previous are known ahead, as they are on the day. The actual and the
revised previous appear only once the clock passes the release. The actual
is the first print: the database may also hold a later revision, which at
that point in the replay has not happened. An indicator's earlier releases
stop at the clock. In **Hide dates** sessions the period label is hidden too.

## Live market data and broker history

In **Market data**, pick an MT5 terminal and press **Record from this
terminal**, then open that terminal yourself.

- **It never opens a terminal.** Opening one restores its charts and can
  switch on an Expert Advisor that trades the account, so the recorder only
  attaches to a terminal you opened. It waits until you do.
- **It only reads.** It has no order code. The list marks terminals whose
  last server looks like a demo; a demo account is the safe source. On this
  machine that is the FTMO terminal (FTMO-Demo).
- **Every instrument the broker has**, found by name (`EURUSD`, `XAGUSD`,
  `USOIL.cash`, `UKOIL.cash`, ...). Ones it lacks are listed.
- **Broker history.** A terminal keeps only a small buffer of recent ticks
  on disk (FTMO's held 124 KB per symbol, plus one month of EUR/USD); older
  ones come from the broker's server when asked for. Between live polls the
  recorder asks, day by day, newest first, back to each instrument's
  **From** day, until the broker has nothing older (ten empty weekdays in a
  row), and remembers where that is. The result is a second **Data** choice
  next to Dukascopy, with the broker's own prices and spreads.
- **Times are converted to UTC.** MT5 stamps ticks in the broker's server
  time. Today's offset is read from a tick seen arriving and read again
  after any pause of 30 minutes. Past days need their own offset: most MT5
  brokers keep New York + 7 h (UTC+2 in winter, UTC+3 in US summer time),
  and when the offset measured now matches that rule, history uses it for
  each day; otherwise the measured offset, all year.
- **Gaps fill themselves.** On start it copies the terminal's ticks since
  the last recorded one, from the start of the UTC day three days back at
  most, and the history pass fills anything older.
- **One feed per broker server and instrument.** Prices and spreads differ
  by broker, so recorded data is never mixed with Dukascopy history.
- Closed days are cleaned with the same Layer 0 rules and stored like
  history. The open day is an append-only file, so a crash loses at most the
  half-written last line, which is trimmed on the next start.

## The rule everything rests on

The simulation clock decides what exists. The browser receives complete
candles that ended at or before the clock, plus the forming candle built
from ticks up to the clock, never the stored version of that candle, which
already holds the rest of its period (its volume included). Orders act on
the tick after the one on screen. Drawings made later in the replay are
hidden after a jump back. Reviewing a trade from a session still in play
stops at that session's clock. Calendar actuals follow the same clock. In
live mode the clock is now, and nothing later exists yet.

`tests/test_no_lookahead.py` checks this directly. It rebuilds every candle
from raw ticks at or before the clock and requires exact equality, for full
history at 100 random times (synthetic and real data) and for every frame
sent during play, including timeframe switches mid-replay.

## Fill rules and money

You buy at the Ask and sell at the Bid. Limits fill at the triggering quote,
which is the limit price or better. Stops fill at the triggering quote plus
slippage, which can be worse. A long's SL/TP are checked against the Bid,
a short's against the Ask. When one quote satisfies both, the stop loss
wins. The full table is in `engine/broker.py`, with one test per row in
`tests/test_fills.py`.

The account is in USD. Profit is the price move times lots times the
contract, in the instrument's quote currency, converted to USD: as is for
EUR/USD, GBP/USD, silver and oil; divided by the price for USD/JPY; through
USD/JPY or GBP/USD (their last complete minute at that moment) for EUR/JPY,
GBP/JPY and EUR/GBP. Margin is the position's USD value over the leverage.

| | One lot | Pip | Pip value |
|---|---|---|---|
| EUR/USD, GBP/USD | 100,000 base | 0.0001 | 10 USD |
| USD/JPY, EUR/JPY, GBP/JPY | 100,000 base | 0.01 | 1,000 JPY (about 6–7 USD) |
| EUR/GBP | 100,000 EUR | 0.0001 | 10 GBP |
| Silver | 5,000 oz | 0.01 | 50 USD |
| WTI, Brent | 1,000 barrels | 0.01 | 10 USD |

Brokers differ (some sell oil in lots of 100 barrels); the ticket always
shows the lot and the pip value. Commission is USD per lot, round turn,
charged at close. Slippage is in pips, applied to market and stop fills
only. Both default to zero and are printed with every session's results.

## Decisions and measurements

- **History**: the trading-research Layer 0 file for EUR/USD (46.1M ticks,
  2023-01-02 to 2024-12-31) plus the Dukascopy datafeed for everything else,
  which is the same source. Each instrument's Dukascopy code and price scale
  were checked against a real hour (12 Jun 2024, 13:00 UTC): GBP/JPY
  200.707, EUR/GBP 0.84230, silver 29.878, WTI (`LIGHTCMDUSD`) 79.033,
  Brent (`BRENTCMDUSD`) 83.063. `SIM_TICK_FILE` points at the Layer 0 file
  if it moves.
- **Compact tick files**: times as integer milliseconds, prices as integer
  points (Ask as a spread over the Bid), delta-encoded and zstd-compressed:
  about 2.1 bytes a tick against 10. The 2023–24 EUR/USD history went from
  464 MB to 103 MB. Every source quotes whole points and whole milliseconds,
  so nothing is lost; some 2023 days of the Layer 0 file held prices one
  unit in the last place off, which now compare equal to the level they
  stand for. EUR/USD takes about 50 MB a year; instruments that quote more
  often take more (silver sent 19,700 quotes in one busy hour, EUR/USD
  9,900).
- **Candles follow their ticks**: the ticks counted in each day's stored
  1-minute candles must equal the ticks that day keeps. A day that differs
  (a download stopped between writing a day and rebuilding its candles) is
  rebuilt at the next start.
- **Rate limit**: the datafeed answers HTTP 429, or refuses connections for
  hours, when pushed: after two days of downloading at three requests a
  second (15 Nov 2026) it refused connections from this machine while its
  website still answered. Requests now go at about one a second, and when
  the feed stops answering every worker pauses together, 2 s doubling to 15
  minutes. A year of one instrument is about 6,000 hour files, so about two
  hours; the panel estimates each instrument's time and size. Downloads stop
  before the disk drops under 3 GB free (`SIM_MIN_FREE_GB`). Stopping is
  safe: it resumes where it left off, and an hour that failed is fetched
  again next time. Getting round the limit (other addresses, disguised
  requests, scraping the website) is not done; the broker's history is the
  other source.
- **Memory**: an instrument's candles are loaded only while a session uses
  it (or converts its profit through it).
- **Cleaning new ticks**: downloaded and recorded days go through the same
  per-day checks as Layer 0 (`data/quality.py`). Checked on 12 Jun 2024 and
  14 Mar 2023: every one of 98,891 and 198,298 ticks matched Layer 0 in
  price and quality flag. Each day had one extra tick, the first of the day,
  which Layer 0 drops as a side effect of removing rows with blanks; it is a
  valid quote and is kept.
- **Holes**: the Layer 0 file lacks six weekdays (1–2 Nov 2023, 12–13 Dec
  2023, 14 May 2024, 2 Dec 2024) and most Sunday evenings, when the week
  opens at 22:00 UTC. The download fills them.
- **Quality flags**: every tick is stored with its flag; flags 0 (clean) and
  3 are used. Flag 3 marks the first quote after a pause of more than 5 s.
  The price is genuine, often the first one after news, and dropping it
  would hide exactly the jumps a stop must see (1.59M ticks, 3.5%). Dropped:
  1 (spread > 10x the day's median, mostly rollover), 2 (bad price) and 4
  (outliers). This departs from the guide's `quality_flag == 0`; changing
  `KEEP_FLAGS` in `config.py` rebuilds the candles on the next start,
  without fetching anything again.
- **Volume**: tick volume, the number of quotes per candle. Spot FX has no
  exchange and so no traded volume; tick volume is what MT5 and most FX
  charts show. Dukascopy and each broker quote at different rates, so the
  scale differs between feeds; within one it is consistent.
- **Candles**: Bid OHLC for 1M, 5M, 15M, 1H, 4H and 1D, aligned to UTC
  midnight.
- **Dead time**: after 3 minutes of simulated silence (weekends, holidays)
  the clock jumps to the next tick. It waits first, so the jump reveals
  nothing that had not already happened.
- **Chart**: TradingView Lightweight Charts 4.2.3, vendored, attribution logo
  on. Only `web/chart.js` talks to it. News dots are placed again for every
  draw and every click: the price axis widens when the crosshair appears,
  which moves every candle sideways.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s simulator/tests -t .
```

80 tests: no look-ahead, fill rules, profit in USD for each kind of
instrument, journal and statistics, the Dukascopy decoder, cleaning and
resumable updater (against a fake datafeed), compact tick files and their
migration, candles kept in step with their ticks, the calendar's clock rule
(against a small copy of fxmacro's tables), and the MT5 recorder and its
history (against a fake terminal on New York + 7 h, across summer and
winter).

`python -m simulator check` prints the tick file's layout and day load times
(guide step 1).

## Layout

```
simulator/
  config.py           paths, port, flags, defaults
  instruments.py      the nine instruments: digits, pip, lot, currencies
  __main__.py         launcher: one port, one instance
  server.py           FastAPI, one WebSocket per tab; background data jobs
  data/ticks.py       per-day tick files (compact layout); the Layer 0 import
  data/quality.py     Layer 0's cleaning rules, per UTC day
  data/dukascopy.py   decoder and resumable, rate-aware updater
  data/bars.py        Bid candles with tick volume, rebuilt per day
  data/calendar.py    fxmacro's economic calendar, read only, on the clock
  live/mt5rec.py      MT5 recorder: attach-only, read-only, UTC, history
  live/feed.py        reading a recorded feed; its stored and live candles
  live/winproc.py     installed and running terminals; a writer lock
  engine/             clock, broker, session (replay and live)
  journal/            SQLite store and statistics
  web/                pages and scripts (news.js: calendar dots and popup)
  tests/
  store/              market data: dukascopy/<instrument>/ and
                      mt5/<server>/<instrument>/ (gitignored)
  journal.sqlite3     your trading history (gitignored)
```

## Not built

- **Step 7, research overlays** (swings, labels, model predictions), and the
  question it raises about `approach2/dataset.py` swing timing. Both need
  the trading-research outputs.
- **Step 8, public product**. Data rights and the chart licence come first,
  and both are other companies' answers.
- Partial closes, a margin stop-out, and several instruments in one session.

Limits that no code removes: replay is not live trading; you may remember
past price action (Random and Hide dates reduce this, they do not remove
it); and tick fills ignore market impact and queue position, so results
lean optimistic.
