from decimal import ROUND_DOWN, Decimal
from typing import Callable, Dict, Optional, Set

from trading_agent.contracts.execution import AckStatus, ApprovedOrder, BrokerAck, Fill
from trading_agent.contracts.market import Quote
from trading_agent.core.clock import Clock
from trading_agent.core.errors import DuplicateOrder, ForgedOrder
from trading_agent.core.signing import OrderSigner
from trading_agent.core.types import IdFactory, Side
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.ports import ExecutionResult

QTY = Decimal("0.000001")


class SimulatedBroker:
    """Immediate-or-cancel limit-order simulator for BACKTEST.

    `faults` lets fault-injection tests make it reject, partially fill, or fill silently
    (booking a position without reporting the fill) to exercise reconciliation.
    """

    def __init__(self, signer: OrderSigner, cost_model: CostModel, quote_for: Callable[[str], Optional[Quote]],
                 clock: Clock, ids: IdFactory) -> None:
        self._signer = signer
        self._cost = cost_model
        self._quote_for = quote_for
        self._clock = clock
        self._ids = ids
        self._seen: Set[str] = set()
        self._positions: Dict[str, Decimal] = {}
        self.faults = {"reject_next": 0, "partial_fill_ratio": None, "silent_fill_next": 0}

    def submit(self, order: ApprovedOrder) -> ExecutionResult:
        if not isinstance(order, ApprovedOrder) or not self._signer.verify(order.signing_payload(), order.signature):
            raise ForgedOrder(f"order {getattr(order, 'order_id', '?')} failed signature verification")
        if order.order_id in self._seen:
            raise DuplicateOrder(order.order_id)
        self._seen.add(order.order_id)
        now = self._clock.now()

        if self.faults["reject_next"] > 0:
            self.faults["reject_next"] -= 1
            return ExecutionResult(BrokerAck(order_id=order.order_id, status=AckStatus.REJECTED,
                                             reason="SIMULATED_REJECT", at=now), [])
        quote = self._quote_for(order.symbol)
        if quote is None:
            return ExecutionResult(BrokerAck(order_id=order.order_id, status=AckStatus.REJECTED,
                                             reason="NO_MARKET", at=now), [])
        marketable = quote.ask <= order.limit_price if order.side is Side.BUY else quote.bid >= order.limit_price
        if not marketable:
            return ExecutionResult(BrokerAck(order_id=order.order_id, status=AckStatus.EXPIRED,
                                             reason="NOT_MARKETABLE_IOC", at=now), [])

        price = self._cost.fill_price(order.side, quote, order.limit_price)
        qty = (order.notional / price).quantize(QTY, rounding=ROUND_DOWN)
        ratio = self.faults["partial_fill_ratio"]
        if ratio is not None:
            qty = (qty * Decimal(str(ratio))).quantize(QTY, rounding=ROUND_DOWN)
        if qty <= 0:
            return ExecutionResult(BrokerAck(order_id=order.order_id, status=AckStatus.EXPIRED,
                                             reason="ZERO_QUANTITY", at=now), [])
        fill = Fill(fill_id=self._ids.next("fill"), order_id=order.order_id, symbol=order.symbol, side=order.side,
                    quantity=qty, price=price, fee=self._cost.fee(qty * price), at=now)
        signed = qty if order.side is Side.BUY else -qty
        self._positions[order.symbol] = self._positions.get(order.symbol, Decimal(0)) + signed
        ack = BrokerAck(order_id=order.order_id, status=AckStatus.ACCEPTED, at=now)
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
