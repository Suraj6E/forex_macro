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

#: The instrument the Layer 0 file holds, and the default everywhere else.
SYMBOL = "EURUSD"

#: Layer 0 output of trading-research: one file of EUR/USD ticks, UTC,
#: 2023-01-02 to 2024-12-31. Imported once into the store.
TICK_FILE = Path(
    os.environ.get(
        "SIM_TICK_FILE",
        REPO_DIR.parent / "trading-research" / "data" / "clean" / "EURUSD_clean.parquet",
    )
)

#: Market data the simulator owns: imported, downloaded and recorded ticks,
#: plus the candles built from them. Durable: downloads take time to redo
#: and live recordings cannot be redone at all.
STORE_DIR = Path(os.environ.get("SIM_STORE_DIR", SIM_DIR / "store"))

#: Replay history, one folder per instrument: Dukascopy downloads, plus the
#: Layer 0 import for EUR/USD.
DUKA_ROOT = STORE_DIR / "dukascopy"


def duka_dir(symbol: str) -> Path:
    return DUKA_ROOT / symbol


DUKA_DIR = duka_dir(SYMBOL)

#: Which instruments to download and from which day, chosen in the UI.
DATA_SETTINGS = STORE_DIR / "data_settings.json"

#: Where downloads start for an instrument nobody has set a date for: the
#: same day the Layer 0 file starts, so every instrument covers the same span.
DEFAULT_FROM = "2023-01-01"

#: Downloads stop before the store's disk has less than this free.
MIN_FREE_BYTES = int(float(os.environ.get("SIM_MIN_FREE_GB", "3")) * 1024**3)

#: The economic calendar of the fxmacro project (the Django app in this
#: repository), read only.
CALENDAR_DB = Path(os.environ.get("SIM_CALENDAR_DB", REPO_DIR / "db.sqlite3"))

#: Live recordings, one folder per MT5 trade server (prices differ by broker,
#: so feeds are never mixed).
MT5_DIR = STORE_DIR / "mt5"

#: Recorder settings chosen in the UI (terminal path, start with the app).
LIVE_SETTINGS = STORE_DIR / "live_settings.json"

#: Not regenerable: sessions, orders, trades, notes, drawings, snapshots.
JOURNAL_DB = Path(os.environ.get("SIM_JOURNAL_DB", SIM_DIR / "journal.sqlite3"))

#: Fetch new Dukascopy hours in the background whenever the server starts.
AUTO_UPDATE = os.environ.get("SIM_AUTO_UPDATE", "1") != "0"

#: Quality flags, the same in every feed (Layer 0's definitions):
#: 0 clean, 1 spread > 10x the day's median, 2 bad price, 3 first quote after
#: a > 5 s pause, 4 > 5 sigma from the rolling median.
#: Flag 3 is a timing label on a genuine quote, often the first price after
#: news. Dropping it would hide exactly the jumps a stop loss must see, so it
#: is kept. Flags 1, 2 and 4 are price problems and are dropped. Every tick
#: is stored with its flag; this filter applies when ticks are read.
KEEP_FLAGS = (0, 3)

# Defaults for EUR/USD in a USD account; every instrument's own numbers are
# in instruments.py.
PIP = 0.0001
CONTRACT_SIZE = 100_000  # units of EUR per lot; one pip is 10 USD per lot

DEFAULT_BALANCE = 10_000.0
DEFAULT_LEVERAGE = 30.0
DEFAULT_TIMEFRAME = "m15"
