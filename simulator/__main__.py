"""Entry point: `python -m simulator [run|build-cache|check]`.

`run` (the default) starts the simulator on its one fixed port and opens it
in the browser. If the simulator is already running there, it only opens the
browser, so a second launch never starts a second server on another port.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import date

from simulator import config

URL = f"http://{config.HOST}:{config.PORT}/"


def _probe() -> str:
    """'ours', 'other' or 'free' for the configured port."""
    try:
        with urllib.request.urlopen(URL + "api/health", timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return "ours" if body.get("app") == config.APP_NAME else "other"
    except (urllib.error.URLError, ValueError, OSError):
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return "other" if s.connect_ex((config.HOST, config.PORT)) == 0 else "free"


def cmd_run(args: argparse.Namespace) -> int:
    state = _probe()
    if state == "ours":
        print(f"Simulator already running at {URL} - opening it.")
        if not args.no_browser:
            webbrowser.open(URL)
        return 0
    if state == "other":
        print(
            f"Port {config.PORT} is held by another program, not the simulator.\n"
            f"Stop that program, or set SIM_PORT to another port for this run."
        )
        return 1

    if not _store_ready():
        print("First run: building the market data (about 30 seconds, once).")
        if cmd_build_cache(args) != 0:
            return 1

    import uvicorn

    from simulator.server import app

    if not args.no_browser:
        def open_when_up() -> None:
            for _ in range(100):
                time.sleep(0.2)
                if _probe() == "ours":
                    webbrowser.open(URL)
                    return
        threading.Thread(target=open_when_up, daemon=True).start()

    print(f"Replay simulator on {URL}  (Django stays on :8000)")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning", ws_ping_interval=20)
    return 0


def _store_ready() -> bool:
    from simulator.data.bars import BarStore
    from simulator.data.ticks import ParquetTickSource

    return ParquetTickSource().ready and BarStore.exists(config.DUKA_DIR / "bars", config.KEEP_FLAGS)


def cmd_build_cache(args: argparse.Namespace) -> int:
    """Import the Layer 0 file into the store (if not yet) and build candles."""
    from simulator.data.bars import BarStore
    from simulator.data.ticks import FeedStore, ParquetTickSource, import_layer0

    t0 = time.time()
    if not FeedStore(config.DUKA_DIR).manifest().get("layer0"):
        if not config.TICK_FILE.exists():
            print(f"Tick file not found: {config.TICK_FILE}. Set SIM_TICK_FILE to its path.")
            return 1
        print(f"Importing {config.TICK_FILE} ...")
        m = import_layer0(progress=lambda msg: None)
        print(f"  {m['layer0']['days']} days; flags in the file: {m['layer0']['flag_counts']}")
    print(f"Building Bid candles (keeping flags {list(config.KEEP_FLAGS)}) ...")
    store = BarStore.build(ParquetTickSource(), progress=print)
    store.save(config.DUKA_DIR / "bars", config.KEEP_FLAGS)
    print(f"Done in {time.time() - t0:.0f}s. Store: {config.STORE_DIR}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Fetch missing Dukascopy hours (new days and holes), then rebuild candles."""
    from simulator.data import dukascopy
    from simulator.data.bars import BarStore
    from simulator.data.ticks import ParquetTickSource

    if not _store_ready() and cmd_build_cache(args) != 0:
        return 1
    print("Fetching missing hours from Dukascopy (Ctrl+C stops; rerun resumes) ...")
    summary = dukascopy.update(progress=lambda day, done, total: print(f"  {day} ({done}/{total})"))
    print(f"  {summary['days']} days written, {summary['ticks']:,} ticks, {summary['failed_hours']} hours to retry")
    if summary["first_changed"] is not None:
        source = ParquetTickSource()
        bars = BarStore.load(config.DUKA_DIR / "bars")
        bars.rebuild_from(source, summary["first_changed"])
        bars.save(config.DUKA_DIR / "bars", config.KEEP_FLAGS)
        print("  candles rebuilt")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """Record live ticks from an MT5 terminal you have opened (never starts one)."""
    from simulator.live.mt5rec import MT5Recorder
    from simulator.live.winproc import running_terminals

    terminal = args.terminal or next(iter(running_terminals()), None)
    if not terminal:
        print("No MT5 terminal is running. Open one (a demo account is safest), then rerun.")
        return 1
    rec = MT5Recorder(terminal)
    rec.start()
    print(f"Recording from {terminal}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(5)
            st = rec.status
            print(f"  {st.get('state')}: {st.get('message')} | last tick {st.get('last_tick_ms')} | today {st.get('ticks_today')}")
    except KeyboardInterrupt:
        rec.stop()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Guide step 1: does the tick file exist, and how fast does a day load?"""
    import random

    import pyarrow.parquet as pq

    from simulator.data.ticks import ParquetTickSource

    if not config.TICK_FILE.exists():
        print(f"Tick file not found: {config.TICK_FILE}")
        return 1
    meta = pq.ParquetFile(config.TICK_FILE).metadata
    print(f"{config.TICK_FILE}\n  {meta.num_rows:,} rows in {meta.num_row_groups} row groups")
    source = ParquetTickSource()
    print(f"  per-day cache: {'built' if source.ready else 'not built - run build-cache'}")
    days = source.days()
    print(f"  {len(days)} trading days, {days[0]} to {days[-1]}")
    for day in random.sample(days, 3) + [date(2024, 3, 16)]:
        t = time.perf_counter()
        ticks = ParquetTickSource().load_day(day)
        print(f"  {day} {day:%a}: {len(ticks):>7,} ticks in {time.perf_counter() - t:.3f}s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m simulator", description="FX replay simulator")
    parser.add_argument("--no-browser", action="store_true", help="start without opening a browser")
    sub = parser.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="start the simulator and open it (default)")
    run.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS)
    sub.add_parser("build-cache", help="import the Layer 0 file and build candles (run once)")
    sub.add_parser("update", help="fetch missing Dukascopy hours, then rebuild candles")
    rec = sub.add_parser("record", help="record live ticks from a running MT5 terminal")
    rec.add_argument("--terminal", help="path of the terminal64.exe to attach to")
    sub.add_parser("check", help="step 1 check: tick file, row groups, day load times")
    args = parser.parse_args(argv)
    if args.cmd in (None, "run"):
        return cmd_run(args)
    return {"build-cache": cmd_build_cache, "update": cmd_update, "record": cmd_record, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
