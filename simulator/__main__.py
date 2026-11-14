"""Entry point: `python -m simulator` (or `simulator.cmd`).

Starts the simulator on its one fixed port and opens it in the browser. If
the simulator is already running there, it only opens the browser, so a
second launch never starts a second server on another port.

Everything else happens inside the app, shown in the page: preparing the
market data on first start, fetching new Dukascopy hours, recording live
ticks from MT5. `check` is a diagnostic that prints the tick file's layout.
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

    print(f"Replay simulator on {URL}  (Django stays on :8000). Data jobs show in the page.")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning", ws_ping_interval=20)
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
    print(f"  store: {'ready' if source.ready else 'not built yet - start the simulator once'}")
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
    sub.add_parser("check", help="step 1 check: tick file, row groups, day load times")
    args = parser.parse_args(argv)
    if args.cmd in (None, "run"):
        return cmd_run(args)
    return cmd_check(args)


if __name__ == "__main__":
    sys.exit(main())
