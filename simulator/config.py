"""Paths, port and defaults for the replay simulator.

The simulator is a separate app from the Django site: its own server, its own
port, its own SQLite journal. It reads ticks from the trading-research
pipeline and never writes there.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Returned by /api/health so the launcher can tell "the simulator is already
#: running on this port" apart from "something else holds the port".
APP_NAME = "fx-replay-simulator"

SIM_DIR = Path(__file__).resolve().parent
REPO_DIR = SIM_DIR.parent
WEB_DIR = SIM_DIR / "web"

#: One fixed address. Django keeps 8000; the simulator always lives on 8001,
#: so a bookmark keeps working and a second launch reuses the running server
#: instead of starting another on a different port.
HOST = "127.0.0.1"
PORT = int(os.environ.get("SIM_PORT", "8001"))

#: Layer 0 output of trading-research: one file of EUR/USD ticks, UTC.
TICK_FILE = Path(
    os.environ.get(
        "SIM_TICK_FILE",
        REPO_DIR.parent / "trading-research" / "data" / "clean" / "EURUSD_clean.parquet",
    )
)

#: Regenerable: rebuilt from TICK_FILE by `python -m simulator build-cache`.
CACHE_DIR = Path(os.environ.get("SIM_CACHE_DIR", SIM_DIR / "cache"))
TICK_CACHE_DIR = CACHE_DIR / "ticks"
BAR_CACHE_DIR = CACHE_DIR / "bars"

#: Not regenerable: sessions, orders, trades, notes, drawings, snapshots.
JOURNAL_DB = Path(os.environ.get("SIM_JOURNAL_DB", SIM_DIR / "journal.sqlite3"))

SYMBOL = "EURUSD"

#: Layer 0 quality flags: 0 clean, 1 spread > 10x median, 2 suspect price,
#: 3 first tick after a > 5 s pause, 4 > 5 sigma outlier.
#: Flag 3 is a timing label on a genuine quote, often the first price after
#: news. Dropping it would hide exactly the jumps a stop loss must see, so it
#: is kept. Flags 1, 2 and 4 are price problems and are dropped.
KEEP_FLAGS = (0, 3)

# EUR/USD in a USD account.
PIP = 0.0001
CONTRACT_SIZE = 100_000  # units of EUR per lot; one pip is 10 USD per lot

DEFAULT_BALANCE = 10_000.0
DEFAULT_LEVERAGE = 30.0
DEFAULT_TIMEFRAME = "m15"
