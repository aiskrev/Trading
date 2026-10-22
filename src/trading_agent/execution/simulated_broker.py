from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Dict, Optional, Set

from trading_agent.contracts.execution import AckStatus, ApprovedOrder, BrokerAck, Fill
from trading_agent.contracts.market import Quote
from trading_agent.core.clock import Clock
from trading_agent.core.errors import DuplicateOrder
from trading_agent.core.types import IdFactory, Side
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.ports import ExecutionResult
from trading_agent.execution.verifier import OrderVerifier

QTY = Decimal("0.000001")


class SimulatedBroker:
    """Immediate-or-cancel limit-order simulator for BACKTEST.

    * Latency: with `fill_quote_after`, the order is matched against the first quote at or after
      decision time + latency (never the decision quote). No such quote (e.g. after the close) => EXPIRED.
    * Partial fills: with `partial_fills`, quantity is capped at displayed size x max_participation;
      the IOC remainder is cancelled.
    * `faults` lets fault-injection tests make it reject, partially fill, or fill silently.
    """

    def __init__(self, verifier: OrderVerifier, cost_model: CostModel, quote_for: Callable[[str], Optional[Quote]],
                 clock: Clock, ids: IdFactory, latency_ms: int = 0,
                 fill_quote_after: Optional[Callable[[str, object], Optional[Quote]]] = None,
                 partial_fills: bool = False, max_participation: Decimal = Decimal(1)) -> None:
        if not isinstance(verifier, OrderVerifier):
            raise TypeError("SimulatedBroker needs an OrderVerifier (public keys only)")
        self._verifier = verifier
        self._cost = cost_model
        self._quote_for = quote_for
        self._clock = clock
        self._ids = ids
        self._latency = timedelta(milliseconds=latency_ms)
        self._fill_quote_after = fill_quote_after
        self._partial = partial_fills
        self._participation = max_participation
        self._seen: Set[str] = set()
        self._positions: Dict[str, Decimal] = {}
        self.faults = {"reject_next": 0, "partial_fill_ratio": None, "silent_fill_next": 0}

    def submit(self, order: ApprovedOrder) -> ExecutionResult:
        self._verifier.verify(order)  # raises OrderRefused (a ForgedOrder) on any failed check
        if order.order_id in self._seen:
            raise DuplicateOrder(order.order_id)
        self._seen.add(order.order_id)
        now = self._clock.now()

        if self.faults["reject_next"] > 0:
            self.faults["reject_next"] -= 1
            return self._ack(order, AckStatus.REJECTED, "SIMULATED_REJECT", now)
        if self._fill_quote_after is not None:
            quote = self._fill_quote_after(order.symbol, now + self._latency)
            if quote is None:
                return self._ack(order, AckStatus.EXPIRED, "NO_QUOTE_AFTER_LATENCY", now)
        else:
            quote = self._quote_for(order.symbol)
            if quote is None:
                return self._ack(order, AckStatus.REJECTED, "NO_MARKET", now)
        fill_at = max(now, quote.ts)
        marketable = quote.ask <= order.limit_price if order.side is Side.BUY else quote.bid >= order.limit_price
        if not marketable:
            return self._ack(order, AckStatus.EXPIRED, "NOT_MARKETABLE_IOC", fill_at)

        price = self._cost.fill_price(order.side, quote, order.limit_price)
        qty = (order.notional / price).quantize(QTY, rounding=ROUND_DOWN)
        if order.side is Side.SELL:
            qty = min(qty, max(self._positions.get(order.symbol, Decimal(0)), Decimal(0)))  # never oversell
        if self._partial:
            displayed = quote.ask_size if order.side is Side.BUY else quote.bid_size
            qty = min(qty, (displayed * self._participation).quantize(QTY, rounding=ROUND_DOWN))
        ratio = self.faults["partial_fill_ratio"]
        if ratio is not None:
            qty = (qty * Decimal(str(ratio))).quantize(QTY, rounding=ROUND_DOWN)
        if qty <= 0:
            return self._ack(order, AckStatus.EXPIRED, "ZERO_QUANTITY", fill_at)
        fill = Fill(fill_id=self._ids.next("fill"), order_id=order.order_id, symbol=order.symbol, side=order.side,
                    quantity=qty, price=price, fee=self._cost.fee(qty * price, order.side), at=fill_at)
        signed = qty if order.side is Side.BUY else -qty
        self._positions[order.symbol] = self._positions.get(order.symbol, Decimal(0)) + signed
        ack = BrokerAck(order_id=order.order_id, status=AckStatus.ACCEPTED, at=fill_at)
        if self.faults["silent_fill_next"] > 0:
            self.faults["silent_fill_next"] -= 1
            return ExecutionResult(ack, [])
        return ExecutionResult(ack, [fill])

    def cancel(self, order_id: str) -> BrokerAck:
        # IOC orders never rest, so there is nothing to cancel.
        return BrokerAck(order_id=order_id, status=AckStatus.REJECTED, reason="NOTHING_TO_CANCEL",
                         at=self._clock.now())

    def broker_positions(self) -> Dict[str, Decimal]:
        return {s: q for s, q in self._positions.items() if q != 0}

    def apply_corporate_action(self, symbol: str, split_ratio: Optional[Decimal] = None,
                               delisted: bool = False) -> None:
        """Broker-side book event (split or delisting). Places no order and touches no cash."""
        if symbol not in self._positions:
            return
        if delisted:
            del self._positions[symbol]
        elif split_ratio is not None:
            self._positions[symbol] *= split_ratio

    @staticmethod
    def _ack(order: ApprovedOrder, status: AckStatus, reason: str, at) -> ExecutionResult:
        return ExecutionResult(BrokerAck(order_id=order.order_id, status=status, reason=reason, at=at), [])
