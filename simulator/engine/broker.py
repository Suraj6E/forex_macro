"""Orders, positions, fills and the account (guide step 4).

Every fill follows from one fact: you buy at the Ask and sell at the Bid.

    Order            Triggers when    Fills at
    market buy       next tick        Ask (+ slippage)
    market sell      next tick        Bid (- slippage)
    buy limit at P   Ask <= P         that Ask: P, or better if it gapped below P
    sell limit at P  Bid >= P         that Bid: P, or better if it gapped above P
    buy stop at P    Ask >= P         that Ask (+ slippage), can be worse than P
    sell stop at P   Bid <= P         that Bid (- slippage), can be worse than P

A long's stop loss is a sell stop and its take profit a sell limit, both
checked against the Bid; a short's are a buy stop and a buy limit against the
Ask. On each tick pending orders are checked first, then manual closes, then
stop losses, then take profits, so a position opened by a tick can be closed
by the same tick, and the stop loss wins when one tick satisfies both.

Nothing acts on the tick the trader is looking at. An order or close request
made at simulated time T only becomes eligible on ticks strictly after T.

What tick data cannot show: the trader's own effect on price, a limit order's
place in the queue, and broker-specific slippage. A limit that price only
touches is filled here; a real one may not be. Results lean optimistic.

Money is EUR/USD in a USD account: profit = price change x lots x 100,000, so
one pip (0.0001) is 10 USD per lot. Amounts are rounded to the cent when
realised, so a trade's profit matches a hand calculation exactly.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from simulator import config

BUY, SELL = "buy", "sell"
MARKET, LIMIT, STOP = "market", "limit", "stop"
SIDES = (BUY, SELL)
TYPES = (MARKET, LIMIT, STOP)

MIN_LOTS = 0.01
MAX_LOTS = 100.0
PRICE_DIGITS = 6  # 5-decimal quotes plus fractional-pip slippage


class OrderError(ValueError):
    """An order or change the broker refuses; the message is shown as is."""


class _Unset:
    pass


UNSET: Any = _Unset()


@dataclass
class Order:
    id: int
    side: str
    type: str
    lots: float
    price: float | None
    sl: float | None
    tp: float | None
    placed_ns: int
    risk_pct: float | None = None
    status: str = "pending"  # pending | filled | cancelled | rejected
    filled_ns: int | None = None
    fill_price: float | None = None
    position_id: int | None = None
    reason: str = ""


@dataclass
class Position:
    id: int
    order_id: int
    side: str
    lots: float
    entry_price: float
    entry_ns: int
    sl: float | None
    tp: float | None
    initial_sl: float | None
    commission: float
    close_requested_ns: int | None = None


@dataclass
class Trade:
    id: int
    position_id: int
    order_id: int
    side: str
    lots: float
    entry_ns: int
    entry_price: float
    exit_ns: int
    exit_price: float
    exit_reason: str  # sl | tp | manual | end
    initial_sl: float | None
    sl: float | None
    tp: float | None
    pips: float
    risk_usd: float | None
    gross_usd: float
    commission_usd: float
    net_usd: float
    r_multiple: float | None


@dataclass
class Broker:
    balance: float = config.DEFAULT_BALANCE
    leverage: float = config.DEFAULT_LEVERAGE
    commission_per_lot: float = 0.0  # USD per lot, round turn, charged at close
    slippage_pips: float = 0.0  # adverse, on market and stop fills only
    pip: float = config.PIP
    contract_size: float = config.CONTRACT_SIZE

    starting_balance: float = field(default=0.0)
    pending: list[Order] = field(default_factory=list)
    positions: list[Position] = field(default_factory=list)
    bid: float | None = None
    ask: float | None = None
    quote_ns: int | None = None
    next_order_id: int = 1
    next_position_id: int = 1
    next_trade_id: int = 1
    events: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.starting_balance:
            self.starting_balance = self.balance
        if self.balance <= 0:
            raise ValueError("Starting balance must be positive")
        if self.leverage <= 0:
            raise ValueError("Leverage must be positive")
        if self.commission_per_lot < 0 or self.slippage_pips < 0:
            raise ValueError("Commission and slippage cannot be negative")

    # -- derived numbers ---------------------------------------------------

    @property
    def pip_value(self) -> float:
        """USD per pip per lot: 10 for EUR/USD."""
        return self.pip * self.contract_size

    @property
    def _slip(self) -> float:
        return self.slippage_pips * self.pip

    def _gross(self, side: str, entry: float, exit_: float, lots: float) -> float:
        move = exit_ - entry if side == BUY else entry - exit_
        return move * lots * self.contract_size

    def unrealized(self) -> float:
        if self.bid is None:
            return 0.0
        total = 0.0
        for p in self.positions:
            mark = self.bid if p.side == BUY else self.ask
            total += self._gross(p.side, p.entry_price, mark, p.lots) - p.commission
        return total

    def equity(self) -> float:
        return self.balance + self.unrealized()

    def used_margin(self) -> float:
        return sum(p.lots * self.contract_size * p.entry_price / self.leverage for p in self.positions)

    def account(self) -> dict:
        equity = self.equity()
        used = self.used_margin()
        return {
            "balance": round(self.balance, 2),
            "equity": round(equity, 2),
            "unrealized": round(equity - self.balance, 2),
            "used_margin": round(used, 2),
            "free_margin": round(equity - used, 2),
            "margin_level": round(100 * equity / used, 1) if used else None,
            "starting_balance": self.starting_balance,
            "leverage": self.leverage,
            "commission_per_lot": self.commission_per_lot,
            "slippage_pips": self.slippage_pips,
        }

    def stop_pips(self, entry: float, sl: float) -> float:
        """Distance in pips, to a tenth of a pip (5-decimal quotes)."""
        return round(abs(entry - sl) / self.pip, 1)

    def lots_for_risk(self, risk_pct: float, entry: float, sl: float) -> float:
        """Lots that lose `risk_pct` of the balance if `sl` is hit at `entry`.

        Rounded down to 0.01 so the risk is never exceeded. 10,000 USD, 1%,
        20 pips: 100 / (20 x 10) = 0.5 lot.
        """
        if not (0 < risk_pct <= 100):
            raise OrderError("Risk must be between 0 and 100 percent")
        pips = self.stop_pips(entry, sl)
        if pips <= 0:
            raise OrderError("Stop loss must differ from the entry price")
        risk_usd = self.balance * risk_pct / 100
        return math.floor(risk_usd / (pips * self.pip_value) * 100 + 1e-9) / 100

    # -- requests ----------------------------------------------------------

    def place(
        self,
        side: str,
        type: str,
        now_ns: int,
        *,
        lots: float | None = None,
        risk_pct: float | None = None,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
    ) -> Order:
        if side not in SIDES:
            raise OrderError(f"Side must be buy or sell, not {side!r}")
        if type not in TYPES:
            raise OrderError(f"Order type must be market, limit or stop, not {type!r}")
        if self.bid is None or self.ask is None:
            raise OrderError("No price yet: start the replay first")
        price = _price(price)
        sl, tp = _price(sl), _price(tp)

        if type == MARKET:
            price = None
            ref = self.ask if side == BUY else self.bid
        else:
            if price is None or price <= 0:
                raise OrderError("A limit or stop order needs a price")
            self._check_entry_side(side, type, price)
            ref = price
        self._check_brackets(side, type, ref, sl, tp)

        if risk_pct is not None:
            if sl is None:
                raise OrderError("Sizing by risk needs a stop loss")
            lots = self.lots_for_risk(float(risk_pct), ref, sl)
        if lots is None:
            raise OrderError("Give a size in lots or a risk in percent")
        lots = round(float(lots), 2)
        if lots < MIN_LOTS:
            raise OrderError(f"Size {lots} is below the minimum of {MIN_LOTS} lot")
        if lots > MAX_LOTS:
            raise OrderError(f"Size {lots} is above the maximum of {MAX_LOTS:g} lots")
        # Early refusal; the binding check is at fill, when margin may differ.
        required = lots * self.contract_size * ref / self.leverage
        free = self.equity() - self.used_margin()
        if required > free:
            raise OrderError(
                f"{lots:.2f} lots needs {required:,.2f} USD of margin; "
                f"{max(free, 0):,.2f} USD is free at {self.leverage:g}:1"
            )

        order = Order(
            id=self.next_order_id,
            side=side,
            type=type,
            lots=lots,
            price=price,
            sl=sl,
            tp=tp,
            placed_ns=now_ns,
            risk_pct=risk_pct,
        )
        self.next_order_id += 1
        self.pending.append(order)
        self._event("placed", order=order)
        return order

    def cancel(self, order_id: int) -> Order:
        order = self._pending(order_id)
        self.pending.remove(order)
        order.status = "cancelled"
        self._event("cancelled", order=order)
        return order

    def modify_order(self, order_id: int, *, price=UNSET, sl=UNSET, tp=UNSET) -> Order:
        order = self._pending(order_id)
        new_price = order.price if price is UNSET else _price(price)
        new_sl = order.sl if sl is UNSET else _price(sl)
        new_tp = order.tp if tp is UNSET else _price(tp)
        if order.type == MARKET:
            if price is not UNSET:
                raise OrderError("A market order has no price to move")
            ref = self.ask if order.side == BUY else self.bid
        else:
            if new_price is None or new_price <= 0:
                raise OrderError("A limit or stop order needs a price")
            self._check_entry_side(order.side, order.type, new_price)
            ref = new_price
        self._check_brackets(order.side, order.type, ref, new_sl, new_tp)
        order.price, order.sl, order.tp = new_price, new_sl, new_tp
        self._event("modified", order=order)
        return order

    def modify_position(self, position_id: int, *, sl=UNSET, tp=UNSET) -> Position:
        pos = self._position(position_id)
        new_sl = pos.sl if sl is UNSET else _price(sl)
        new_tp = pos.tp if tp is UNSET else _price(tp)
        if pos.side == BUY:
            if new_sl is not None and new_sl >= self.bid:
                raise OrderError("A long's stop loss must be below the Bid")
            if new_tp is not None and new_tp <= self.bid:
                raise OrderError("A long's take profit must be above the Bid")
        else:
            if new_sl is not None and new_sl <= self.ask:
                raise OrderError("A short's stop loss must be above the Ask")
            if new_tp is not None and new_tp >= self.ask:
                raise OrderError("A short's take profit must be below the Ask")
        pos.sl, pos.tp = new_sl, new_tp
        self._event("position_modified", position=pos)
        return pos

    def close(self, position_id: int, now_ns: int) -> Position:
        """Close at market on the next tick, like any other new order."""
        pos = self._position(position_id)
        if pos.close_requested_ns is None:
            pos.close_requested_ns = now_ns
            self._event("close_requested", position=pos)
        return pos

    def close_all_now(self, reason: str = "end") -> list[Trade]:
        """Close everything at the last quote seen. Used when a session ends."""
        trades = []
        for order in list(self.pending):
            self.cancel(order.id)
        for pos in list(self.positions):
            if pos.side == BUY:
                trades.append(self._close(pos, self.bid - self._slip, self.quote_ns, reason))
            else:
                trades.append(self._close(pos, self.ask + self._slip, self.quote_ns, reason))
        return trades

    # -- ticks -------------------------------------------------------------

    def on_tick(self, ts: int, bid: float, ask: float) -> None:
        """Apply one quote. The reference implementation of every fill rule."""
        self.bid, self.ask, self.quote_ns = bid, ask, ts

        for order in list(self.pending):
            if order.placed_ns >= ts:
                continue
            fill = self._fill_price(order, bid, ask)
            if fill is not None:
                self.pending.remove(order)
                self._open(order, fill, ts)

        for pos in list(self.positions):
            if pos.close_requested_ns is not None and pos.close_requested_ns < ts:
                exit_ = bid - self._slip if pos.side == BUY else ask + self._slip
                self._close(pos, exit_, ts, "manual")

        for pos in list(self.positions):
            if pos.side == BUY:
                if pos.sl is not None and bid <= pos.sl:
                    self._close(pos, bid - self._slip, ts, "sl")
                elif pos.tp is not None and bid >= pos.tp:
                    self._close(pos, bid, ts, "tp")
            else:
                if pos.sl is not None and ask >= pos.sl:
                    self._close(pos, ask + self._slip, ts, "sl")
                elif pos.tp is not None and ask <= pos.tp:
                    self._close(pos, ask, ts, "tp")

    def on_ticks(self, ts: np.ndarray, bid: np.ndarray, ask: np.ndarray) -> None:
        """Apply many quotes; same result as calling on_tick on each.

        Between two ticks that change something, every order and level is
        fixed, so the next tick that can trigger anything is found with one
        vectorised comparison per order, and only that tick goes through
        on_tick. A day with two open orders costs a few array scans, not
        hundreds of thousands of Python calls.
        """
        n = len(ts)
        i = 0
        while i < n:
            j = self._next_trigger(ts, bid, ask, i)
            if j is None:
                break
            self.on_tick(int(ts[j]), float(bid[j]), float(ask[j]))
            i = j + 1
        if n:
            self.bid, self.ask, self.quote_ns = float(bid[-1]), float(ask[-1]), int(ts[-1])

    def _next_trigger(self, ts, bid, ask, i: int) -> int | None:
        best: int | None = None

        def consider(mask_start: int, mask: np.ndarray | None) -> None:
            nonlocal best
            if mask_start >= len(ts):
                return
            if mask is None:
                hit = mask_start
            else:
                k = int(np.argmax(mask))
                if not mask[k]:
                    return
                hit = mask_start + k
            if best is None or hit < best:
                best = hit

        for o in self.pending:
            a = max(i, int(np.searchsorted(ts, o.placed_ns, side="right")))
            if a >= len(ts):
                continue
            if o.type == MARKET:
                consider(a, None)
            elif o.side == BUY:
                consider(a, ask[a:] <= o.price if o.type == LIMIT else ask[a:] >= o.price)
            else:
                consider(a, bid[a:] >= o.price if o.type == LIMIT else bid[a:] <= o.price)
        for p in self.positions:
            if p.close_requested_ns is not None:
                consider(max(i, int(np.searchsorted(ts, p.close_requested_ns, side="right"))), None)
            if p.side == BUY:
                if p.sl is not None:
                    consider(i, bid[i:] <= p.sl)
                if p.tp is not None:
                    consider(i, bid[i:] >= p.tp)
            else:
                if p.sl is not None:
                    consider(i, ask[i:] >= p.sl)
                if p.tp is not None:
                    consider(i, ask[i:] <= p.tp)
        return best

    # -- internals ---------------------------------------------------------

    def _fill_price(self, order: Order, bid: float, ask: float) -> float | None:
        slip = self._slip
        if order.type == MARKET:
            return ask + slip if order.side == BUY else bid - slip
        if order.side == BUY:
            if order.type == LIMIT and ask <= order.price:
                return ask  # = min(P, Ask): P on a touch, better on a gap
            if order.type == STOP and ask >= order.price:
                return ask + slip
        else:
            if order.type == LIMIT and bid >= order.price:
                return bid
            if order.type == STOP and bid <= order.price:
                return bid - slip
        return None

    def _open(self, order: Order, fill: float, ts: int) -> Position | None:
        fill = round(fill, PRICE_DIGITS)
        required = order.lots * self.contract_size * fill / self.leverage
        free = self.equity() - self.used_margin()
        if required > free:
            order.status = "rejected"
            order.reason = (
                f"Not enough free margin: needs {required:,.2f} USD, "
                f"{max(free, 0):,.2f} USD free at {self.leverage:g}:1"
            )
            self._event("rejected", order=order)
            return None
        pos = Position(
            id=self.next_position_id,
            order_id=order.id,
            side=order.side,
            lots=order.lots,
            entry_price=fill,
            entry_ns=ts,
            sl=order.sl,
            tp=order.tp,
            initial_sl=order.sl,
            commission=round(order.lots * self.commission_per_lot, 2),
        )
        self.next_position_id += 1
        self.positions.append(pos)
        order.status, order.fill_price, order.filled_ns, order.position_id = "filled", fill, ts, pos.id
        self._event("filled", order=order, position=pos)
        return pos

    def _close(self, pos: Position, exit_price: float, ts: int, reason: str) -> Trade:
        exit_price = round(exit_price, PRICE_DIGITS)
        gross = round(self._gross(pos.side, pos.entry_price, exit_price, pos.lots), 2)
        net = round(gross - pos.commission, 2)
        risk = None
        if pos.initial_sl is not None:
            risk = round(abs(pos.entry_price - pos.initial_sl) * pos.lots * self.contract_size, 2)
        sign = 1 if pos.side == BUY else -1
        trade = Trade(
            id=self.next_trade_id,
            position_id=pos.id,
            order_id=pos.order_id,
            side=pos.side,
            lots=pos.lots,
            entry_ns=pos.entry_ns,
            entry_price=pos.entry_price,
            exit_ns=ts,
            exit_price=exit_price,
            exit_reason=reason,
            initial_sl=pos.initial_sl,
            sl=pos.sl,
            tp=pos.tp,
            pips=round(sign * (exit_price - pos.entry_price) / self.pip, 1),
            risk_usd=risk,
            gross_usd=gross,
            commission_usd=pos.commission,
            net_usd=net,
            r_multiple=round(net / risk, 3) if risk else None,
        )
        self.next_trade_id += 1
        self.positions.remove(pos)
        self.balance = round(self.balance + net, 2)
        self._event("closed", trade=trade, position=pos)
        return trade

    def _check_entry_side(self, side: str, type: str, price: float) -> None:
        if side == BUY and type == LIMIT and not price < self.ask:
            raise OrderError(f"A buy limit must be below the Ask ({self.ask:.5f})")
        if side == BUY and type == STOP and not price > self.ask:
            raise OrderError(f"A buy stop must be above the Ask ({self.ask:.5f})")
        if side == SELL and type == LIMIT and not price > self.bid:
            raise OrderError(f"A sell limit must be above the Bid ({self.bid:.5f})")
        if side == SELL and type == STOP and not price < self.bid:
            raise OrderError(f"A sell stop must be below the Bid ({self.bid:.5f})")

    def _check_brackets(self, side: str, type: str, ref: float, sl, tp) -> None:
        """Stop loss and take profit must sit on the losing and winning side."""
        if side == BUY:
            lo_ref = self.bid if type == MARKET else ref  # a long exits at the Bid
            if sl is not None and not sl < lo_ref:
                raise OrderError("A buy's stop loss must be below its entry")
            if tp is not None and not tp > ref:
                raise OrderError("A buy's take profit must be above its entry")
        else:
            hi_ref = self.ask if type == MARKET else ref  # a short exits at the Ask
            if sl is not None and not sl > hi_ref:
                raise OrderError("A sell's stop loss must be above its entry")
            if tp is not None and not tp < ref:
                raise OrderError("A sell's take profit must be below its entry")

    def _pending(self, order_id: int) -> Order:
        for o in self.pending:
            if o.id == int(order_id):
                return o
        raise OrderError(f"No pending order #{order_id}")

    def _position(self, position_id: int) -> Position:
        for p in self.positions:
            if p.id == int(position_id):
                return p
        raise OrderError(f"No open position #{position_id}")

    def _event(self, kind: str, **objects) -> None:
        event = {"kind": kind}
        for name, obj in objects.items():
            event[name] = asdict(obj)
        self.events.append(event)

    def drain_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    # -- persistence -------------------------------------------------------

    def to_state(self) -> dict:
        return {
            "balance": self.balance,
            "leverage": self.leverage,
            "commission_per_lot": self.commission_per_lot,
            "slippage_pips": self.slippage_pips,
            "starting_balance": self.starting_balance,
            "pending": [asdict(o) for o in self.pending],
            "positions": [asdict(p) for p in self.positions],
            "bid": self.bid,
            "ask": self.ask,
            "quote_ns": self.quote_ns,
            "next_order_id": self.next_order_id,
            "next_position_id": self.next_position_id,
            "next_trade_id": self.next_trade_id,
        }

    @classmethod
    def from_state(cls, state: dict) -> "Broker":
        broker = cls(
            balance=state["balance"],
            leverage=state["leverage"],
            commission_per_lot=state["commission_per_lot"],
            slippage_pips=state["slippage_pips"],
            starting_balance=state["starting_balance"],
        )
        broker.pending = [Order(**o) for o in state["pending"]]
        broker.positions = [Position(**p) for p in state["positions"]]
        broker.bid, broker.ask, broker.quote_ns = state["bid"], state["ask"], state["quote_ns"]
        broker.next_order_id = state["next_order_id"]
        broker.next_position_id = state["next_position_id"]
        broker.next_trade_id = state["next_trade_id"]
        return broker


def _price(value) -> float | None:
    if value is None or value == "":
        return None
    value = float(value)
    if not math.isfinite(value):
        raise OrderError("Prices must be finite numbers")
    return round(value, 5)
