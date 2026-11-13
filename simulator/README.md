# Replay simulator

Candle-by-candle replay of EUR/USD ticks with simulated orders, an automatic
journal and drawing tools. Built from `Replay Simulator Step-by-Step Build
Guide.md`, steps 1 to 6. It is a separate app from the Django site: its own
server, its own port, its own journal. It reads ticks from the
trading-research pipeline and writes nothing there.

## Run

```powershell
.\simulator.cmd
```

or, from the repo root, `.\.venv\Scripts\python.exe -m simulator` (or
`py -m simulator` with the venv active).

It always opens at **<http://127.0.0.1:8001/>**. Django keeps
<http://127.0.0.1:8000/>. Launching again while it runs opens the running
one instead of starting a second server. If another program holds 8001, it
says so and stops rather than moving to some other port (`SIM_PORT`
overrides the port for one run).

The first run builds the cache, about 20 seconds once. After that, startup
is instant.

One-time install, if the venv predates the simulator:

```powershell
.\.venv\Scripts\python.exe -m pip install -r simulator\requirements.txt
```

## Using it

| | |
|---|---|
| Start | Pick a start time (UTC) or **Random**, balance, leverage, commission, slippage. **Hide dates** shows weekday and time only. |
| Replay | **Play**/**Pause** (Space), speeds 1x to max (a market day in ~3 s), **+1 candle** (Right), **+1 tick** (Shift+Right), **Jump** to a time. |
| Orders | Market, limit or stop; size in lots or as % risk from the stop. SL/TP lines on the chart drag to edit. **Close** works per position. |
| Drawings | Horizontal line, trend line, rectangle, position box. A selected position box has **Send as order**. Esc cancels, Delete removes. |
| Journal | <http://127.0.0.1:8001/journal>: stats with a 95% win-rate range, equity curve, splits by session/weekday/tag/exit, tags and notes, chart snapshots at entry and exit, **Review** to reopen any trade on the chart. |

Everything saves as it happens. Closing the tab, or stopping the server
mid-trade, loses nothing; reopening resumes at the same time with the same
positions.

## The rule everything rests on

The simulation clock decides what exists. The browser receives complete
candles that ended at or before the clock, plus the forming candle built
from ticks up to the clock, never the stored version of that candle, which
already holds the rest of its period. Orders act on the tick after the one
on screen. Drawings made later in the replay are hidden after a jump back.
Reviewing a trade from a session still in play stops at that session's
clock.

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

- **Tick file**: `../trading-research/data/clean/EURUSD_clean.parquet`, 46.1M
  ticks, 2023-01-02 to 2024-12-31, 516 trading days. `SIM_TICK_FILE`
  points elsewhere.
- **Quality flags**: kept 0 (clean) and 3. Flag 3 marks the first quote
  after a pause of more than 5 s. The price is genuine, often the first one
  after news, and dropping it would hide exactly the jumps a stop must see.
  That is 1.59M ticks, 3.5%. Dropped flags: 1 (spread > 10x median, 87,669
  ticks, mostly rollover), 2 (none present) and 4 (85 outliers). This
  departs from the guide's `quality_flag == 0`; change `KEEP_FLAGS` in
  `config.py` and rerun `build-cache` to compare.
- **Per-day cache**: the guide expected day reads from the 1.47 GB file to be
  slow. Measured: 0.6 s cold, 0.02 s warm, because the 44 row groups are
  time-sorted and carry statistics. The cache (460 MB, ~4 ms per day) is
  still built, because it lists trading days instantly and feeds the candle
  build.
- **Candles**: Bid OHLC for 1M, 5M, 15M, 1H, 4H and 1D, built from the kept
  ticks and aligned to UTC midnight.
- **Dead time**: after 3 minutes of simulated silence (weekends, holidays)
  the clock jumps to the next tick. It waits first, so the jump reveals
  nothing that had not already happened.
- **Chart**: TradingView Lightweight Charts 4.2.3, vendored, attribution logo
  on. Only `web/chart.js` talks to it.

## Commands

| | |
|---|---|
| `python -m simulator` | start on 8001 and open the browser (`--no-browser` to skip) |
| `python -m simulator build-cache` | rebuild the tick and candle cache, e.g. after changing `KEEP_FLAGS` |
| `python -m simulator check` | step 1 check: tick file, row groups, trading days, day load times |
| `python -m unittest discover -s simulator/tests -t .` | the tests (43) |

## Layout

```
simulator/
  config.py          paths, port, flags, defaults
  __main__.py        launcher: one port, one instance, cache on first run
  data/ticks.py      one day of ticks; the per-day cache builder
  data/bars.py       Bid candles for every timeframe, held in memory
  engine/clock.py    speed and frame timing
  engine/broker.py   orders, positions, fills, account
  engine/session.py  clock + candles + broker; the no-look-ahead rule
  journal/store.py   SQLite: sessions, orders, trades, notes, snapshots, drawings
  journal/stats.py   win rate with range, expectancy, drawdown, splits
  server.py          FastAPI, one WebSocket per tab
  web/               index.html, journal.html, app.js, chart.js, drawings.js
  tests/             no look-ahead, fills, journal
  cache/             built from the tick file (gitignored)
  journal.sqlite3    your trading history (gitignored)
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
