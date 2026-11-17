"""The instruments the simulator replays, and what a lot of each is worth.

Every number here is used for money: digits for prices on screen and on
disk, the pip for distances, the contract for profit and margin, and the
quote currency for converting profit into the USD account.

Dukascopy names the oils differently from most brokers: WTI is
`LIGHTCMDUSD` (US light sweet crude) and Brent `BRENTCMDUSD`. Its price
files are scaled by 10**digits for every instrument here.

Contract sizes follow the common CFD convention: 100,000 units for the
currency pairs, 100 ounces for gold, 5,000 ounces for silver and 1,000
barrels for oil. A pip of gold is 0.10 USD, the usual retail convention,
so a pip of a lot is worth 10 USD, as on EUR/USD. Brokers
differ (some sell oil in lots of 100 barrels), so the ticket always says
what one lot is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str  # our key, used for folders and the journal
    label: str
    duka: str  # Dukascopy datafeed code
    digits: int  # decimals of a quote; Dukascopy points are 10**-digits
    pip: float
    contract: float  # units per lot
    base: str
    quote: str  # profit currency
    currencies: tuple[str, ...]  # whose economic calendar moves it
    unit: str = "units"
    kind: str = "fx"
    #: Names a broker may use for it in MT5, tried in order after `symbol`.
    mt5_names: tuple[str, ...] = ()

    @property
    def scale(self) -> int:
        return 10**self.digits

    def to_json(self) -> dict:
        out = asdict(self)
        out["currencies"] = list(self.currencies)
        out["mt5_names"] = list(self.mt5_names)
        return out


INSTRUMENTS: dict[str, Instrument] = {
    i.symbol: i
    for i in (
        Instrument("EURUSD", "EUR/USD", "EURUSD", 5, 0.0001, 100_000, "EUR", "USD", ("EUR", "USD"), "EUR"),
        Instrument("GBPUSD", "GBP/USD", "GBPUSD", 5, 0.0001, 100_000, "GBP", "USD", ("GBP", "USD"), "GBP"),
        Instrument("USDJPY", "USD/JPY", "USDJPY", 3, 0.01, 100_000, "USD", "JPY", ("USD", "JPY"), "USD"),
        Instrument("EURJPY", "EUR/JPY", "EURJPY", 3, 0.01, 100_000, "EUR", "JPY", ("EUR", "JPY"), "EUR"),
        Instrument("GBPJPY", "GBP/JPY", "GBPJPY", 3, 0.01, 100_000, "GBP", "JPY", ("GBP", "JPY"), "GBP"),
        Instrument("EURGBP", "EUR/GBP", "EURGBP", 5, 0.0001, 100_000, "EUR", "GBP", ("EUR", "GBP"), "EUR"),
        Instrument("XAUUSD", "Gold", "XAUUSD", 3, 0.1, 100, "XAU", "USD", ("USD",), "oz", "metal",
                   ("XAUUSD", "GOLD")),
        Instrument("XAGUSD", "Silver", "XAGUSD", 3, 0.01, 5_000, "XAG", "USD", ("USD",), "oz", "metal",
                   ("XAGUSD", "SILVER")),
        Instrument("WTI", "WTI crude oil (US)", "LIGHTCMDUSD", 3, 0.01, 1_000, "WTI", "USD", ("USD",), "barrels",
                   "energy", ("USOIL", "XTIUSD", "WTI", "USOUSD", "CL", "OIL.WTI", "WTIUSD")),
        Instrument("BRENT", "Brent crude oil (Europe)", "BRENTCMDUSD", 3, 0.01, 1_000, "BRENT", "USD", ("USD",),
                   "barrels", "energy", ("UKOIL", "XBRUSD", "BRENT", "UKOUSD", "OIL.BRENT", "BRENTUSD")),
    )
}

DEFAULT_SYMBOL = "EURUSD"

#: Which instrument converts a profit currency into USD, and whether its
#: price is USD per unit (GBPUSD) or units per USD (USDJPY).
CONVERSION = {"JPY": ("USDJPY", True), "GBP": ("GBPUSD", False)}


def get(symbol: str | None) -> Instrument:
    key = (symbol or DEFAULT_SYMBOL).upper()
    try:
        return INSTRUMENTS[key]
    except KeyError:
        raise ValueError(f"Unknown instrument {symbol!r}; expected one of {', '.join(INSTRUMENTS)}") from None
