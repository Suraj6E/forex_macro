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

    from simulator.data.bars import BarStore
    from simulator.data.ticks import ParquetTickSource

    if config.TICK_FILE.exists() and not (ParquetTickSource().cached and BarStore.exists()):
        print("First run: building the tick and candle cache (about 20 seconds, once).")
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


def cmd_build_cache(args: argparse.Namespace) -> int:
    from simulator.data.bars import BarStore
    from simulator.data.ticks import ParquetTickSource, build_tick_cache

    if not config.TICK_FILE.exists():
        print(f"Tick file not found: {config.TICK_FILE}\nSet SIM_TICK_FILE to its path.")
        return 1
    t0 = time.time()
    print(f"Splitting {config.TICK_FILE} into one file per day ...")
    manifest = build_tick_cache(progress=print)
    kept = sum(manifest["ticks_per_day"].values())
    print(
        f"  {len(manifest['days'])} trading days, {kept:,} ticks kept "
        f"(flags {manifest['keep_flags']}; all flags seen: {manifest['flag_counts']})"
    )
    print("Building Bid candles for every timeframe ...")
    store = BarStore.build(ParquetTickSource(), progress=print)
    store.save()
    for tf, bars in store.by_tf.items():
        print(f"  {tf}: {len(bars):,} candles")
    print(f"Done in {time.time() - t0:.0f}s. Cache: {config.CACHE_DIR}")
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
    print(f"  per-day cache: {'built' if source.cached else 'not built - run build-cache'}")
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
    sub.add_parser("build-cache", help="split ticks per day and build candles (run once)")
    sub.add_parser("check", help="step 1 check: tick file, row groups, day load times")
    args = parser.parse_args(argv)
    if args.cmd in (None, "run"):
        return cmd_run(args)
    return {"build-cache": cmd_build_cache, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
