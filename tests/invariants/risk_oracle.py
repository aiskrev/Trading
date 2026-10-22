"""Independent re-statement of every pre-trade limit, used to check gateway output.

Deliberately written without importing any rule code: if the gateway approves an order, the
order must satisfy every limit below when recomputed from scratch.
"""
from datetime import timedelta
from decimal import Decimal

from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import Side

BPS = Decimal(10000)
EPS = Decimal("0.01")


def violations(order, decision, proposal, market, account, policy, now, state, broker, critic=None):
    """Return a list of violated limits (empty = order is within policy)."""
    v = []
    sym = order.symbol
    if sym != proposal.symbol or order.side is not proposal.side:
        v.append("instrument/side changed")
    if order.notional > proposal.requested_notional:
        v.append("size increased")
    if order.notional != decision.approved_notional:
        v.append("order != decision")
    if sym not in policy.instruments:
        v.append("universe")
    if sym in policy.restricted:
        v.append("restricted")
    if market.instrument_status.get(sym) is not InstrumentStatus.TRADING:
        v.append("instrument status")
    if market.venue_status is not VenueStatus.OPEN:
        v.append("venue")
    if not policy.session.is_open(now):
        v.append("session")
    if state is TradingState.HALTED:
        v.append("halted")
    q = market.quotes.get(sym)
    if q is None:
        return v + ["no quote"]
    if now - q.ts > timedelta(seconds=policy.max_quote_age_seconds):
        v.append("stale quote")
    if now - market.as_of > timedelta(seconds=policy.max_snapshot_age_seconds):
        v.append("stale snapshot")
    mid = (q.bid + q.ask) / 2
    if abs(order.limit_price - mid) / mid * BPS > policy.price_collar_bps:
        v.append("collar")
    if (q.ask - q.bid) / mid * BPS > policy.max_spread_bps:
        v.append("spread")
    signed = order.limit_price - mid if order.side is Side.BUY else mid - order.limit_price
    if signed / mid * BPS + policy.commission_bps > policy.max_slippage_bps:
        v.append("slippage")
    if order.notional > policy.max_order_notional:
        v.append("max order notional")
    if any(o.symbol == sym for o in account.open_orders):
        v.append("open-order conflict")
    if account.orders_today >= policy.max_orders_per_day:
        v.append("daily order count")
    if critic is not None and critic.verdict.value == "DENY":
        v.append("critic deny overridden")
    if critic is not None and critic.verdict.value == "REDUCE" and order.notional > critic.max_notional:
        v.append("critic reduce ignored")
    if broker is None:
        v.append("no broker view")
    else:
        for s in set(broker.positions) | set(account.positions):
            if abs(broker.positions.get(s, Decimal(0)) - account.quantity(s)) > policy.position_tolerance:
                v.append("broker mismatch")

    held = account.quantity(sym)
    reducing = order.side is Side.SELL and held > 0
    if order.side is Side.SELL:
        if held <= 0:
            v.append("short sale")
        elif order.notional > held * order.limit_price + EPS:
            v.append("oversell")
        return v

    # ---- BUY: post-trade exposure limits
    if not reducing and order.notional < policy.min_order_notional:
        v.append("below min notional")
    marks = {s: p.quantity * account_mid(market, s) for s, p in account.positions.items()}
    buys = [o for o in account.open_orders if o.side is Side.BUY]
    pending = sum((o.notional for o in buys), Decimal(0))
    new_pos = held * mid + sum((o.notional for o in buys if o.symbol == sym), Decimal(0)) + order.notional
    if new_pos > policy.max_position_notional + EPS:
        v.append("position")
    if sum((abs(x) for x in marks.values()), Decimal(0)) + pending + order.notional > policy.max_gross_exposure + EPS:
        v.append("gross")
    if sum(marks.values(), Decimal(0)) + pending + order.notional > policy.max_net_exposure + EPS:
        v.append("net")
    sector = policy.instruments[sym].sector
    in_sector = sum((abs(x) for s, x in marks.items() if policy.instruments[s].sector == sector), Decimal(0))
    in_sector += sum((o.notional for o in buys if policy.instruments[o.symbol].sector == sector), Decimal(0))
    if in_sector + order.notional > policy.max_sector_exposure[sector] + EPS:
        v.append("sector")
    equity = account.cash + sum(marks.values(), Decimal(0))
    if new_pos > policy.max_concentration_pct * equity + EPS:
        v.append("concentration")
    if order.notional * (1 + policy.commission_bps / BPS) > account.cash - pending - policy.min_cash_buffer + EPS:
        v.append("buying power")
    if account.realized_pnl_today <= -policy.daily_loss_limit:
        v.append("daily loss")
    hw = account.equity_high_water
    if hw is None or (hw - equity) / hw >= policy.max_drawdown_pct:
        v.append("drawdown")
    return v


def account_mid(market, symbol):
    q = market.quotes[symbol]
    return (q.bid + q.ask) / 2
