# Replay Simulator: Step-by-Step Build Guide

Nov 11, 2026 · @Suraj Neupane

## Decisions and starting facts

Build a new chart with TradingView Lightweight Charts now, and keep TradingView Advanced Charts as a later swap for the public product. The simulator lives in a new top-level `simulator/` folder and only reads from the existing pipeline.

| Item | Status | Detail |
| --- | --- | --- |
| Chart library for steps 1 to 7 | Decision | Lightweight Charts. Apache 2.0, free for personal use, TradingView attribution required. No built-in drawing tools or indicators, so we write those. ([TradingView](https://www.tradingview.com/free-charting-libraries/)) |
| Advanced Charts | Fact | 80+ drawing tools and 100+ indicators, but not offered for personal, hobby or testing use. Access is by request, for companies with public projects, and the code may not sit in a public repo. ([TradingView](https://www.tradingview.com/free-charting-libraries/), [quick start](https://www.tradingview.com/charting-library-docs/latest/quick-start)) |
| Advanced Charts and a paywall | Fact, needs care | The free licence requires a public environment, not behind a paywall. A paid product would need a negotiated licence. ([docs](https://www.tradingview.com/charting-library-docs/latest/introduction/)) |
| Tick data | Fact from repo code, file not seen | Layer 0 writes one file, `data/clean/EURUSD_clean.parquet`, with `timestamp`, `Ask`, `Bid`, `AskVolume`, `BidVolume`, `quality_flag`, `mid_price`, `spread` and session tags. `data/` is gitignored, so step 1 checks that the file exists on your machine. |
| Existing bars | Fact, and a reason not to reuse them | Layer 1 bars take open and close from the mid price, high from the Ask and low from the Bid. That mix suits features but is not a tradable candle. The simulator builds its own Bid candles from ticks. |
| Time zone | Assumption | Tick timestamps are UTC. Layer 0 session tags are written in UTC, which supports this. |
| Scope | Assumption | EUR/USD only until step 8. Windows 11, Python 3.13. |

The reference product is FX Replay at [practicesimulator.com](https://practicesimulator.com/): candle-by-candle replay, an automatic journal, analytics and custom indicators.

## Architecture: one engine, three uses

Everything rests on one rule: the simulation clock decides what exists. Any data with a timestamp later than the clock must be unreachable, by construction and not by discipline.

The logic runs in this order:

1. Premise: a practice tool, a research instrument and a public product all need a clock that walks through history and reveals only the past.
2. Deduction: if the clock, candle building and order fills sit in one engine with no screen code inside it, the personal tool is that engine plus a chart.
3. Deduction: the research instrument is the same engine plus read-only layers (events, labels, predictions), each filtered by the same clock.
4. Deduction: the public product is the same engine plus accounts, hosting, billing and licensed data. None of those change trading logic.
5. Verdict: build engine, then chart, then orders, then journal, then drawings, then research overlays, then product. Each step is usable alone.

The folder layout keeps the simulator apart from the research pipeline:

```
trading-research/
  layer0 ... layer4, approach1 ... approach3    existing, untouched
  simulator/                                    new section
    config.py          paths to ../data, port, defaults
    data/ticks.py      loads one day of ticks from the clean parquet
    data/bars.py       builds Bid candles for any timeframe
    engine/clock.py    simulation time, speed, play, pause, step
    engine/broker.py   orders, positions, fills, account balance
    engine/session.py  ties clock, bars and broker together
    journal/store.py   SQLite tables for sessions, orders, trades, drawings
    journal/stats.py   win rate, expectancy, drawdown, equity curve
    overlays/          step 7: events, labels, predictions
    server.py          FastAPI app with one WebSocket per session
    web/               index.html, app.js, chart.js, drawings.js
    tests/             no-look-ahead and fill tests
```

Two design choices deserve a reason:

- The engine runs in Python on the server side, and the browser only draws. Reason: the ticks are in parquet, and step 7 needs your PyTorch models, which are Python. Cost: for a public product, one Python session per user is expensive. Step 8 revisits this.
- The chart sits behind a small adapter file, `web/chart.js`, with about six functions (set history, update bar, add marker, add line, set drawings, clear). Reason: swapping to Advanced Charts later then touches one file.

## Step 1: New section and tick access

Goal: prove the tick file exists and that one day of ticks loads in well under a second.

1. Create the `simulator/` folder with the layout above. Add `fastapi`, `uvicorn` and `websockets` to a separate `simulator/requirements.txt`, so the research requirements stay unchanged.
2. Check the tick file from the repo root:

```
python -c "import pyarrow.parquet as pq; f = pq.ParquetFile('data/clean/EURUSD_clean.parquet'); print(f.metadata.num_rows, f.metadata.num_row_groups); print(f.schema_arrow)"
```

3. Read the three numbers it prints. Row count tells you total ticks. Row-group count matters most: day filters are fast only if the file has many row groups sorted by time.
4. Write `data/ticks.py` with one function, `load_day(date)`. It uses the same filter Layer 2 already uses: `timestamp >= day_start`, `timestamp < day_end`, `quality_flag == 0`. It returns only `timestamp`, `Bid`, `Ask`.
5. Time `load_day` on three random dates.

If a day takes more than about one second, the file is one large block and every read scans all of it. The fix is a one-off script that rewrites ticks into one small file per day under `simulator/cache/ticks/2024-03-14.parquet`. This is likely needed (hypothesis), because Layer 0 writes the file in a single `to_parquet` call with default settings.

Done when: `load_day` returns a sorted table for any trading day in under one second, and returns an empty table for a Saturday without an error.

## Step 2: Replay engine

Goal: a clock that moves through ticks and produces candles for every timeframe, with a test that proves nothing from the future leaks.

1. Build history candles once. A script reads ticks day by day and writes Bid candles for 1M, 5M, 15M, 1H, 4H and 1D into `simulator/cache/bars/`. Each candle stores open, high, low, close from the Bid, plus the last spread. Bid candles are the common chart convention, and the Ask is recovered as Bid plus spread.
2. Write `engine/clock.py`. It holds `sim_time`, `speed` and `playing`. Every 50 ms of real time it advances `sim_time` by 50 ms times `speed`. Example: at speed 60, one real second plays one market minute.
3. Write `engine/session.py`. On each clock advance it takes all ticks with `timestamp <= sim_time` that were not yet sent, feeds them to the broker (step 4), and updates the forming candle of each timeframe.
4. Handle the forming candle correctly. At 10:17 the 1H candle that started at 10:00 is only 17 minutes old. Its high and low must come from ticks up to 10:17 only. Never read the finished 10:00 candle from the cache, because it already contains 10:18 to 10:59.
5. Add step controls: next tick, next candle of the visible timeframe, and jump to a date. A jump resets forming candles and reloads history strictly before the new time.
6. Skip dead time. When no tick arrives for more than a few minutes (weekends, holidays), jump the clock to the next tick.

The test that guards the whole project goes in `tests/test_no_lookahead.py`: run a session to a random time, collect everything the session would send to the screen, and assert that no value depends on a tick after `sim_time`. A direct way is to compare each forming candle against one rebuilt from raw ticks up to `sim_time`.

Done when: that test passes for 100 random times, and a full day replays at maximum speed in a few seconds.

## Step 3: Chart page

Goal: open `http://localhost:8000`, pick a date, press play and watch candles form.

1. Write `server.py` with FastAPI. It serves the `web/` folder and opens one WebSocket per browser tab. Each WebSocket owns one session from step 2.
2. Define the messages. Browser to server: `start(date, timeframe)`, `play`, `pause`, `speed(x)`, `step(kind)`, `timeframe(tf)`. Server to browser: `history(bars)`, `bar_update(bar)`, `tick(bid, ask, time)`, later `account` and `fills`.
3. Write `web/chart.js`, the adapter. Use the Lightweight Charts standalone build copied into `web/vendor/`, so the tool works offline. `setHistory` calls `series.setData`, and `updateBar` calls `series.update` with the forming candle.
4. Keep the TradingView attribution logo on. It is on by default through the `attributionLogo` layout option and satisfies the licence. ([release note](https://newreleases.io/project/github/tradingview/lightweight-charts/release/v4.2.0))
5. Add the top bar: date picker, timeframe buttons, play and pause, speed selector (1x, 10x, 60x, 300x, max), step buttons, and the simulation time in UTC.
6. On a timeframe switch, the server sends history for the new timeframe up to `sim_time`, plus the forming candle. The clock does not move.
7. Throttle screen updates. The server may process thousands of ticks per second at high speed, but it sends at most about 20 updates per second. The broker still sees every tick.

Lightweight Charts 5.1 is the current major line and supports several panes, which a later volume or equity pane can use ([versions](https://newreleases.io/project/npm/lightweight-charts/release/4.2.0), [comparison](https://www.luxalgo.com/vela/compare/)).

Done when: replay at 60x is smooth, a timeframe switch during replay shows no candle beyond the clock, and a page refresh resumes at the same time.

## Step 4: Order simulation from ticks

Goal: orders fill at the price a real account would have received from the quoted Bid and Ask.

The fill rules follow from one fact: you buy at the Ask and sell at the Bid.

| Order | Triggers when | Fills at |
| --- | --- | --- |
| Market buy | Immediately | Current Ask |
| Market sell | Immediately | Current Bid |
| Buy limit at P | Ask <= P | P, or the Ask if it gapped below P |
| Sell limit at P | Bid >= P | P, or the Bid if it gapped above P |
| Buy stop at P | Ask >= P | The Ask of that tick, which can be worse than P |
| Sell stop at P | Bid <= P | The Bid of that tick, which can be worse than P |
| Stop loss or take profit of a long | Checked against the Bid | Stop loss as a sell stop, take profit as a sell limit |
| Stop loss or take profit of a short | Checked against the Ask | Stop loss as a buy stop, take profit as a buy limit |

1. Write `engine/broker.py` with three lists: pending orders, open positions, closed trades. `on_tick(bid, ask, time)` checks pending orders first, then the stop loss and take profit of open positions.
2. Add the account: starting balance, equity, used margin. For EUR/USD in a USD account, 1 lot is 100,000 EUR and one pip (0.0001) is worth 10 USD per lot. Example: 0.5 lot, 20 pip stop, loss = 0.5 x 20 x 10 = 100 USD.
3. Add size by risk. The trader enters the risk in percent and the stop price, and the broker computes lots. Example: 10,000 USD balance, 1% risk, 20 pip stop gives 100 / (20 x 10) = 0.5 lot.
4. Add an optional commission per lot and an optional fixed slippage in pips. Default both to zero and show them in the journal, so results state their own assumptions.
5. Add the order panel in the page: buy and sell buttons, order type, size or risk, stop loss and take profit. Draw each open order as a price line on the chart. Dragging a line edits the order.
6. Apply new orders on the next tick, not the tick being displayed. This removes any chance of filling at a price the trader already saw.
7. Write `tests/test_fills.py` with hand-made tick sequences. One case per row of the table, plus a weekend gap through a stop and a tick that touches stop loss and take profit in the same instant.

What tick data cannot show (fact): your own order's effect on the market, the queue position of a limit order, and broker-specific slippage. A limit order that the price only touches is filled here, while a real one may not be. Results are therefore slightly optimistic.

Done when: all fill tests pass, and a manual trade's profit matches a hand calculation to the cent.

## Step 5: Journal and statistics

Goal: every session, order and trade is saved without any action from the trader, and the numbers are honest about sample size.

1. Write `journal/store.py` on SQLite, which ships with Python. Tables: `sessions` (start date, settings, commission, slippage), `orders`, `trades` (entry, exit, size, stop, profit in USD and in R), `notes`, and `drawings` for step 6.
2. Record R for every trade. R is profit divided by the initial risk. Example: risk 100 USD, profit 250 USD, result +2.5R. R makes trades of different sizes comparable.
3. Save a chart snapshot at entry and at exit. The chart library can export its canvas as an image.
4. Write `journal/stats.py`: number of trades, win rate, average win and loss in R, expectancy in R, profit factor, largest drawdown, average holding time, and results split by session (London, New York) and by weekday.
5. Show an uncertainty range next to the win rate. With 30 trades and a true win rate near 50%, the 95% range is about plus or minus 18 points. With 100 trades it is about plus or minus 10 points. This is arithmetic, not opinion: the standard error is the square root of p(1 - p) / n.
6. Add a journal page: trade table, equity curve, and a click on any trade that reopens the chart at that entry time in review mode.
7. Add tags and a free-text note per trade, entered at close. Tags such as `breakout` or `revenge` make the split statistics useful.

Done when: closing the browser mid-session loses nothing, and statistics recomputed by hand from the trades table match the page.

## Step 6: Drawing tools

Goal: horizontal line, trend line, rectangle and a long/short position box, all saved and restored. This is the step where Lightweight Charts costs the most work, because it ships no drawing tools.

1. Store every drawing as data, not pixels: type, a list of (time, price) points, colour, and the timeframe it was drawn on. Pixels change with zoom, time and price do not.
2. Horizontal line first. The library has price lines built in (`createPriceLine`), so this needs only click handling and a delete action.
3. Trend line and rectangle next, as series primitives. A primitive is a small class that receives the canvas and draws on it. Convert points with `timeScale().timeToCoordinate` and `series.priceToCoordinate` on every redraw. ([plugin docs](https://docsearch.algolia.com/mcp/docs/repo/tradingview/lightweight-charts))
4. Write one shared interaction layer in `web/drawings.js`: select a tool, click to place points, drag a handle to edit, press Delete to remove. All tools reuse it.
5. Add the position box: entry, stop and target as three levels with the risk-to-reward ratio printed. One click turns it into a real order from step 4.
6. Save drawings through the WebSocket into the `drawings` table, keyed by session. Restore them on load.
7. Decide what a drawing may show after a jump back in time. A line drawn at 14:00 with knowledge of that day should be hidden when the clock returns to 09:00. Hide any drawing created at a later `sim_time`.

A community line-tools plugin exists and reports support for version 5 ([repository](https://github.com/difurious/lightweight-charts-line-tools)). Its quality is unverified. Try it for one hour before writing primitives by hand, and keep whichever is less work.

Done when: drawings survive a page refresh, a timeframe switch and a zoom, and they stay attached to the right price and time.

## Step 7: Research overlays

Goal: show events, labels and model predictions on the replay chart, each at the moment it could first have been known. The stored timestamp of a research output is often earlier than that moment.

| Overlay | Stored timestamp | Earliest honest reveal | Basis |
| --- | --- | --- | --- |
| Swing high or low | The swing bar itself | 5 bars later, about 30 minutes after the swing bar opens | Fact: `structure_events.py` compares bar i with 5 bars on each side (`SWING_LOOKBACK = 5`) and stamps the event at bar i |
| Breakout and fakeout | To check | After the confirm bars | The detector uses `BREAKOUT_CONFIRM_BARS`. I did not trace its timestamp |
| Labels (`strong_long`, `strong_short`) | The labelled bar | 24 bars after that bar closes, about 2 hours | Fact: the labeler looks 24 bars forward |
| Model prediction | The input bar | That bar's close, and only inside the model's own validation quarter | Fact: folds validate on 2024 Q1, Q2, Q3, Q4 |

1. Write `overlays/reveal.py` with one function per overlay that returns `reveal_time` for each row. The session sends a row only when `sim_time >= reveal_time`.
2. Add events as chart markers with a toggle per event type.
3. Add labels in two modes. Live mode shows a label only after its reveal time. Review mode, after the session ends, shows all labels so you can compare them with your trades.
4. Add predictions. Precompute them per fold into a parquet file with the existing `evaluate.py` path, since live inference during replay is not needed. For a replay date in 2024 Q2, load fold 2 only. Dates before 2024 are training data for every fold, so show no predictions there.
5. Add blind mode: predictions are recorded but hidden until the session ends. Then the journal compares your trades with the model's calls on the same bars.
6. Extend the no-look-ahead test from step 2 to cover every overlay.

One consequence reaches beyond the simulator. If Approach 2 builds a bar's event sequence from events stamped at or before that bar, a swing in the last 5 bars uses future prices. Whether `approach2/dataset.py` already delays swings is unverified. It is worth checking before trusting Approach 2 results.

Done when: the extended test passes, and blind mode produces a you-versus-model table for one full validation week.

## Step 8: Public product

Goal: other people can sign up and use it. Most of this step is not programming, and two items can stop it entirely: data rights and the chart licence.

1. Settle data rights first. Your ticks come from Dukascopy (`data_downloader.sh`). Using them yourself and serving them to paying users are different rights. Read their terms or ask them in writing. I have not verified what they allow. If the answer is no, a licensed data vendor is a recurring cost that decides the price of the product.
2. Settle the chart licence. Stay on Lightweight Charts with attribution, or request Advanced Charts. The free Advanced Charts licence excludes use behind a paywall, so a paid product needs a commercial agreement. ([docs](https://www.tradingview.com/charting-library-docs/latest/introduction/))
3. Decide where the engine runs. Option A keeps Python sessions on the server: simple, but each active user holds memory and a process. Option B ports clock, candles and broker to TypeScript and runs them in the browser, with the server only delivering day files: cheap to host, but any user can read the future from the downloaded file. For a practice tool that only cheats themselves. For leaderboards or funded challenges it is unacceptable, so those need option A.
4. Add accounts and storage: sign-in, Postgres instead of SQLite, per-user sessions, journals and drawings.
5. Add more markets. Each needs its pip size, contract size, quote currency conversion and trading hours. The EUR/USD shortcut of 10 USD per pip stops being true.
6. Add billing and plan limits. For reference, FX Replay lists paid plans from 17.99 USD and 35.00 USD per month, with a free plan. ([practicesimulator.com](https://practicesimulator.com/))
7. Add the legal layer: terms, privacy policy, and a clear statement that results are hypothetical and the service is not a broker and gives no advice. Get this reviewed by a lawyer in your jurisdiction. I am not one.
8. Add operations: hosting, backups, monitoring, error tracking, support email.

An honest size estimate (judgement, not fact): steps 1 to 7 are weeks of part-time work for one person. Step 8 is months, and items 1 and 2 depend on other companies' answers.

Done when: a stranger can sign up, replay a week, and see their journal the next day, on data you are allowed to serve.

## Limits and open questions

Three limits are structural and no amount of engineering removes them:

- Replay practice is not live trading. Nothing is at stake, and you can restart a bad session. Skill measured here is an upper bound on live skill (hypothesis, widely held, not proven for you).
- If you have already studied 2023 and 2024 EUR/USD for this research, you may remember major days. Random start dates and a hidden date display reduce this but cannot remove it.
- Tick fills ignore market impact and queue position, as step 4 states.

Open questions for you:

- [ ] Does `data/clean/EURUSD_clean.parquet` exist on your machine, and what date range does it cover? Step 1 answers this.
- [ ] Should fills use only clean ticks (`quality_flag == 0`), or also the wide-spread ticks at rollover (flag 1)? Clean only is easier. Including flag 1 is more realistic for positions held overnight.
- [ ] Should chart time be UTC or your local time? The guide assumes UTC.
- [ ] Is the swing-event timing in Approach 2 already delayed in `dataset.py`? See step 7.
- [ ] Do you want me to start building at step 1, in a new branch of `trading-research`?

## Sources

- [practicesimulator.com](https://practicesimulator.com/)
- [TradingView free charting libraries](https://www.tradingview.com/free-charting-libraries/)
- [TradingView charting libraries comparison](https://in.tradingview.com/HTML5-stock-forex-bitcoin-charting-library/)
- [Advanced Charts introduction](https://www.tradingview.com/charting-library-docs/latest/introduction/)
- [Advanced Charts quick start](https://ar.tradingview.com/charting-library-docs/latest/quick-start.md)
- [Lightweight Charts plugin overview](https://docsearch.algolia.com/mcp/docs/repo/tradingview/lightweight-charts)
- [Lightweight Charts v4.2.0 release note](https://newreleases.io/project/github/tradingview/lightweight-charts/release/v4.2.0)
- [Lightweight Charts versions on npm](https://newreleases.io/project/npm/lightweight-charts/release/4.2.0)
- [Vela comparison of charting libraries](https://www.luxalgo.com/vela/compare/)
- [lightweight-charts-line-tools](https://github.com/difurious/lightweight-charts-line-tools)
- Repository `Suraj6E/trading-research`: `layer0/SchemaValidation.py`, `layer1/layer1_tick_to_bar.py`, `layer2/microstructure.py`, `approach1/config.py`, `approach2/detectors/structure_events.py`
