# Replay simulator

Candle-by-candle replay of EUR/USD ticks with simulated orders, an automatic
journal and drawing tools, plus a live mode on prices recorded from your MT5
terminal. Built from `Replay Simulator Step-by-Step Build Guide.md`, steps
1 to 6. It is a separate app from the Django site: its own server, its own
port, its own journal. It reads the trading-research tick file and writes
nothing there.

## Run

```powershell
.\simulator.cmd
```

That is the only command. It always opens at **<http://127.0.0.1:8001/>**;
Django keeps <http://127.0.0.1:8000/>. Launching again while it runs opens
the running one. If another program holds 8001 it says so and stops, rather
than moving to some other port (`SIM_PORT` overrides the port for one run).

Everything else happens by itself and shows in the page:

1. **First start**: the 2023–24 tick file is imported and candles are built
   (about a minute). The page shows the progress and opens the session form
   when it is done.
2. **Every start**: new hours are fetched from Dukascopy in the background,
   and candles are rebuilt every 20 days fetched, so new days become playable
   while the rest downloads. The top bar shows it (`Updating 2025-03-14 · 12%`).
3. **Live recording**, once you have switched it on in **Market data**, starts
   again on its own.

One-time install, if the venv predates the simulator:

```powershell
.\.venv\Scripts\python.exe -m pip install -r simulator\requirements.txt
```

## Using it

| | |
|---|---|
| Start | **Sessions** → **Replay history** (pick a start time in UTC, or **Random**; **Hide dates** shows weekday and time only) or **Live market**. Balance, leverage, commission, slippage. |
| Replay | **Play**/**Pause** (Space), speeds 1x to max (a market day in ~3 s), **+1 candle** (Right), **+1 tick** (Shift+Right), **Jump** to a time. |
| Live | The clock is the real clock; candles form as ticks arrive. No play, step or jump: the market has its own clock. |
| Orders | Market, limit or stop; size in lots or as % risk from the stop. SL/TP lines on the chart drag to edit. **Close** works per position. |
| Drawings | Horizontal line, trend line, rectangle, position box. A selected position box has **Send as order**. Esc cancels, Delete removes. |
| Market data | The top-bar button (also in the session form): history range and update progress, **Update now**, and the live recorder. |
| Journal | <http://127.0.0.1:8001/journal>: stats with a 95% win-rate range, equity curve, splits by session/weekday/tag/exit, tags and notes, chart snapshots at entry and exit, **Review** to reopen any trade on the chart. |

Everything saves as it happens. Closing the tab, or stopping the server
mid-trade, loses nothing; reopening resumes at the same time with the same
positions. A live session that was closed catches up on reopening: every tick
recorded meanwhile is given to its orders and positions, in order.

## Live market data

In **Market data**, pick an MT5 terminal and press **Record from this
terminal**, then open that terminal yourself.

- **It never opens a terminal.** Opening one restores its charts and can
  switch on an Expert Advisor that trades the account, so the recorder only
  attaches to a terminal you opened. It waits until you do.
- **It only reads.** It has no order code. The list marks terminals whose
  last server looks like a demo; a demo account is the safe source. On this
  machine that is the FTMO terminal (FTMO-Demo).
- **Times are converted to UTC.** MT5 stamps ticks in the broker's server
  time. The offset is read from a tick seen arriving (a stale tick an exact
  number of hours old would give a wrong offset), and read again after any
  pause of 30 minutes, since brokers change it for daylight saving while the
  market is closed.
- **Gaps fill themselves.** On start it copies the terminal's own tick
  history since the last recorded tick (up to 3 days), so stopping the
  recorder or the simulator for a while leaves no hole.
- **One feed per broker server.** Prices and spreads differ by broker, so
  recorded data is never mixed with Dukascopy history. A live session's chart
  history is what was recorded from that server. Recorded days can also be
  replayed like history (pick the feed under **Data** in the session form).
- Closed days are cleaned with the same Layer 0 rules and stored like
  history. The open day is an append-only file, so a crash loses at most the
  half-written last line, which is trimmed on the next start.

## The rule everything rests on

The simulation clock decides what exists. The browser receives complete
candles that ended at or before the clock, plus the forming candle built
from ticks up to the clock, never the stored version of that candle, which
already holds the rest of its period. Orders act on the tick after the one
on screen. Drawings made later in the replay are hidden after a jump back.
Reviewing a trade from a session still in play stops at that session's
clock. In live mode the clock is now, and nothing later exists yet.

`tests/test_no_lookahead.py` checks this directly. It rebuilds every candle
from raw ticks at or before the clock and requires exact equality, for full
history at 100 random times (synthetic and real data) and for every frame
sent during play, including timeframe switches mid-replay.

## Fill rules

You buy at the Ask and sell at the Bid. Limits fill at the triggering quote,
which is the limit price or better. Stops fill at the triggering quote plus
slippage, which can be worse. A long's SL/TP are checked against the Bid,
a short's against the Ask. When one quote satisfies both, the stop loss
wins. The full table is in `engine/broker.py`, with one test per row in
`tests/test_fills.py`, plus a weekend gap through a stop and a profit
checked to the cent.

Money: EUR/USD in a USD account, 1 lot = 100,000 EUR, 1 pip = 10 USD per
lot. Commission is USD per lot, round turn, charged at close. Slippage is in
pips, applied to market and stop fills only. Both default to zero and are
printed with every session's results.

## Decisions and measurements

- **History**: the trading-research Layer 0 file (46.1M ticks, 2023-01-02 to
  2024-12-31) plus the Dukascopy datafeed for everything after, which is the
  same source. `SIM_TICK_FILE` points at the file if it moves.
- **Cleaning new ticks**: downloaded and recorded days go through the same
  per-day checks as Layer 0 (`data/quality.py`). Checked on 12 Jun 2024 and
  14 Mar 2023: every one of 98,891 and 198,298 ticks matched Layer 0 in
  price and quality flag. Each day had one extra tick, the first of the day,
  which Layer 0 drops as a side effect of removing rows with blanks; it is a
  valid quote and is kept.
- **Holes**: the Layer 0 file lacks six weekdays (1–2 Nov 2023, 12–13 Dec
  2023, 14 May 2024, 2 Dec 2024) and most Sunday evenings, when the week
  opens at 22:00 UTC. The update fills them.
- **Rate limit**: the datafeed answers HTTP 429 or stops answering when
  pushed. Requests are paced at about 3 a second across all workers, and
  when it stops answering every worker pauses together, 2 s doubling to 2
  minutes. A full catch-up of ~23 months is roughly 16,000 hour files, so it
  takes over an hour the first time and seconds after that. Stopping is
  safe: it resumes where it left off, and an hour that failed is fetched
  again next time.
- **Quality flags**: every tick is stored with its flag; flags 0 (clean) and
  3 are used. Flag 3 marks the first quote after a pause of more than 5 s.
  The price is genuine, often the first one after news, and dropping it
  would hide exactly the jumps a stop must see (1.59M ticks, 3.5%). Dropped:
  1 (spread > 10x the day's median, mostly rollover), 2 (bad price) and 4
  (outliers). This departs from the guide's `quality_flag == 0`; changing
  `KEEP_FLAGS` in `config.py` rebuilds the candles on the next start,
  without fetching anything again.
- **Candles**: Bid OHLC for 1M, 5M, 15M, 1H, 4H and 1D, aligned to UTC
  midnight.
- **Dead time**: after 3 minutes of simulated silence (weekends, holidays)
  the clock jumps to the next tick. It waits first, so the jump reveals
  nothing that had not already happened.
- **Chart**: TradingView Lightweight Charts 4.2.3, vendored, attribution logo
  on. Only `web/chart.js` talks to it.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s simulator/tests -t .
```

60 tests: no look-ahead, fill rules, journal and statistics, the Dukascopy
decoder, cleaning and resumable updater (against a fake datafeed), and the
MT5 recorder (against a fake terminal whose clock is two hours ahead).

`python -m simulator check` prints the tick file's layout and day load times
(guide step 1).

## Layout

```
simulator/
  config.py           paths, port, flags, defaults
  __main__.py         launcher: one port, one instance
  server.py           FastAPI, one WebSocket per tab; background data jobs
  data/ticks.py       per-day tick files with flags; the Layer 0 import
  data/quality.py     Layer 0's cleaning rules, per UTC day
  data/dukascopy.py   decoder and resumable, rate-aware updater
  data/bars.py        Bid candles for every timeframe, rebuilt incrementally
  live/mt5rec.py      MT5 recorder: attach-only, read-only, UTC, backfill
  live/feed.py        reading a feed while it is recorded; its candles
  live/winproc.py     installed and running terminals; a writer lock
  engine/             clock, broker, session (replay and live)
  journal/            SQLite store and statistics
  web/                pages and scripts
  tests/
  store/              market data: dukascopy/ and mt5/<server>/ (gitignored)
  journal.sqlite3     your trading history (gitignored)
```

## Not built

- **Step 7, research overlays** (swings, labels, model predictions), and the
  question it raises about `approach2/dataset.py` swing timing. Both need
  the trading-research outputs.
- **Step 8, public product**. Data rights and the chart licence come first,
  and both are other companies' answers.
- Partial closes, a margin stop-out, and instruments other than EUR/USD.

Limits that no code removes: replay is not live trading; you may remember
2023–2024 price action (Random and Hide dates reduce this, they do not
remove it); and tick fills ignore market impact and queue position, so
results lean optimistic.
