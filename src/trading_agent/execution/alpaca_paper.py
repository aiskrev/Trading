"""Alpaca PAPER execution port (roadmap P6). Same interface as SimulatedBroker; same IOC semantics.

submit(order):
  1. OrderVerifier.verify: signature, key, time window, environment, account, epoch, policy (else OrderRefused);
  2. quantity = whole shares, floor(approved notional / limit price) (Alpaca's notional orders are market-only),
     and for a SELL never more than the broker says is held (no accidental short); 0 shares => REJECTED, nothing sent;
  3. the order_id is recorded durably in the IdempotencyStore BEFORE the request (DuplicateOrder if seen);
  4. POST /v2/orders: limit, time_in_force "ioc", client_order_id derived from the signed order;
  5. the order is followed until it is final (filled / canceled / expired / rejected); if it is still open after
     `fill_wait_s` it is cancelled and followed again; still not final => BrokerStateUnknown (HALT);
  6. one aggregate Fill (filled_qty at filled_avg_price, fee 0: Alpaca charges no commission; regulatory sell
     fees are not modelled) and an ack; the idempotency record is closed.

Every answer the port cannot interpret (no response, timeout, 429, 5xx, an echo of a different order, an
unexpected shape) raises BrokerStateUnknown: the pipeline HALTs, the order stays RESERVED, and recovery decides
by `lookup_order(client_order_id)` (GET /v2/orders:by_client_order_id). 403 (buying power) and 422 (refused
input) are definite rejections. broker_positions() retries once and then raises (HALT).
"""
import time
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from trading_agent.contracts.execution import AckStatus, ApprovedOrder, BrokerAck, Fill
from trading_agent.core.clock import Clock
from trading_agent.core.errors import RateLimited, TradingAgentError, TransportError
from trading_agent.core.types import IdFactory, Side
from trading_agent.execution.alpaca_http import AlpacaTradingClient
from trading_agent.execution.idempotency import IdempotencyStore, Phase, client_order_id
from trading_agent.execution.ports import ExecutionResult
from trading_agent.execution.verifier import OrderVerifier

FINAL = {"filled", "canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}
SHARE = Decimal(1)


class BrokerStateUnknown(TradingAgentError):
    """The port cannot tell what the broker did. The caller HALTs; recovery settles it by lookup."""


class _Order(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    client_order_id: str
    symbol: str
    side: str
    type: str
    time_in_force: str
    status: str
    qty: Optional[Decimal] = None
    filled_qty: Decimal = Field(ge=0)
    filled_avg_price: Optional[Decimal] = None
    limit_price: Optional[Decimal] = None
    filled_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class _Position(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    symbol: str
    qty: Decimal
    side: str


class _Account(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    trading_blocked: bool
    account_blocked: bool


class AlpacaPaperBroker:
    def __init__(self, verifier: OrderVerifier, client: AlpacaTradingClient, clock: Clock, ids: IdFactory,
                 idempotency: Optional[IdempotencyStore] = None, fill_wait_s: float = 3.0, poll_s: float = 0.25,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(verifier, OrderVerifier):
            raise TypeError("AlpacaPaperBroker needs an OrderVerifier (public keys only)")
        self._verifier, self._client, self._clock, self._ids = verifier, client, clock, ids
        self._idem = idempotency if idempotency is not None else IdempotencyStore.in_memory()
        self._wait, self._poll, self._sleep, self._now = fill_wait_s, poll_s, sleep, monotonic
        self._broker_ids: Dict[str, str] = {}      # our order_id -> Alpaca order id

    # ------------------------------------------------------------------ ExecutionPort
    def submit(self, order: ApprovedOrder) -> ExecutionResult:
        self._verifier.verify(order)
        now = self._clock.now()
        qty = (order.notional / order.limit_price).to_integral_value(rounding=ROUND_DOWN)
        if order.side is Side.SELL:
            qty = min(qty, max(self.broker_positions().get(order.symbol, Decimal(0)), Decimal(0))
                      .to_integral_value(rounding=ROUND_DOWN))
        if qty < SHARE:
            return ExecutionResult(BrokerAck(order_id=order.order_id, status=AckStatus.REJECTED,
                                             reason="BELOW_ONE_SHARE", at=now), [])
        cid = self._idem.reserve(order, now)       # durable before the request; DuplicateOrder if seen
        payload = {"symbol": order.symbol, "qty": str(qty), "side": order.side.value.lower(), "type": "limit",
                   "time_in_force": "ioc", "limit_price": str(order.limit_price), "client_order_id": cid}
        status, body = self._call("POST", "/v2/orders", payload)
        if status in (403, 422):
            ack = BrokerAck(order_id=order.order_id, status=AckStatus.REJECTED, reason=f"HTTP_{status}",
                            at=self._clock.now())
            self._idem.close(order.order_id, Phase.COMPLETED, ack.status.value, ack.at)
            return ExecutionResult(ack, [])
        placed = self._order(status, body)
        if (placed.client_order_id, placed.symbol, placed.side, placed.qty) != (cid, order.symbol,
                                                                                order.side.value.lower(), qty):
            raise BrokerStateUnknown("the broker echoed a different order")
        self._broker_ids[order.order_id] = placed.id
        final = self._follow(placed)
        result = self._result(order, final)
        self._idem.close(order.order_id, Phase.COMPLETED, result.ack.status.value, result.ack.at)
        return result

    def cancel(self, order_id: str) -> BrokerAck:
        broker_id = self._broker_ids.get(order_id)
        if broker_id is None:
            return BrokerAck(order_id=order_id, status=AckStatus.REJECTED, reason="UNKNOWN_ORDER", at=self._clock.now())
        status, _ = self._call("DELETE", f"/v2/orders/{broker_id}")
        ok = status in (200, 204)
        return BrokerAck(order_id=order_id, status=AckStatus.CANCELLED if ok else AckStatus.REJECTED,
                         reason=None if ok else f"HTTP_{status}", at=self._clock.now())

    def broker_positions(self) -> Dict[str, Decimal]:
        last: Optional[Exception] = None
        for _ in range(2):
            try:
                status, body = self._call("GET", "/v2/positions")
                if status != 200 or not isinstance(body, list):
                    raise BrokerStateUnknown(f"positions: HTTP {status}")
                out: Dict[str, Decimal] = {}
                for raw in body:
                    p = _Position.model_validate(raw)
                    if p.symbol in out:
                        raise BrokerStateUnknown("positions: duplicate symbol")
                    if p.qty != 0:
                        out[p.symbol] = p.qty
                return out
            except (BrokerStateUnknown, ValidationError) as exc:
                last = exc
        raise BrokerStateUnknown(f"positions unavailable: {type(last).__name__}")

    def lookup_order(self, client_order_id: str) -> Optional[BrokerAck]:
        status, body = self._call("GET", f"/v2/orders:by_client_order_id?client_order_id={client_order_id}")
        if status == 404:
            return None
        o = self._order(status, body)
        if o.client_order_id != client_order_id:
            raise BrokerStateUnknown("lookup answered for another order")
        return BrokerAck(order_id=client_order_id, status=_ack_status(o), reason=o.status, at=self._clock.now())

    def account_summary(self) -> Dict[str, Any]:
        """Cash, equity and buying power, for the session start (read-only). Refuses a blocked account."""
        status, body = self._call("GET", "/v2/account")
        try:
            acct = _Account.model_validate(body) if status == 200 else None
        except ValidationError:
            acct = None
        if acct is None:
            raise BrokerStateUnknown(f"account: HTTP {status}")
        if acct.status != "ACTIVE" or acct.trading_blocked or acct.account_blocked or acct.currency != "USD":
            raise BrokerStateUnknown("account is not active, is blocked, or is not in USD")
        return {"cash": acct.cash, "equity": acct.equity, "buying_power": acct.buying_power}

    # ------------------------------------------------------------------ helpers
    def _call(self, method: str, path: str, payload=None):
        try:
            status, body = self._client.request(method, path, payload)
        except (TransportError, RateLimited) as exc:
            raise BrokerStateUnknown(f"{method} {path.split('?')[0]}: {type(exc).__name__}") from None
        if status == 429 or status >= 500:
            raise BrokerStateUnknown(f"{method} {path.split('?')[0]}: HTTP {status}")
        return status, body

    @staticmethod
    def _order(status: int, body) -> _Order:
        if status != 200:
            raise BrokerStateUnknown(f"order: HTTP {status}")
        try:
            return _Order.model_validate(body)
        except ValidationError:
            raise BrokerStateUnknown("order: unexpected shape") from None

    def _follow(self, placed: _Order) -> _Order:
        current = placed
        for cancel_sent in (False, True):
            deadline = self._now() + self._wait
            while current.status not in FINAL and self._now() < deadline:
                self._sleep(self._poll)
                current = self._order(*self._call("GET", f"/v2/orders/{placed.id}"))
            if current.status in FINAL:
                return current
            if not cancel_sent:
                status, _ = self._call("DELETE", f"/v2/orders/{placed.id}")
                if status not in (200, 204, 422):     # 422: no longer cancelable (it just became final)
                    raise BrokerStateUnknown(f"cancel: HTTP {status}")
                current = self._order(*self._call("GET", f"/v2/orders/{placed.id}"))
        raise BrokerStateUnknown("order still open after cancel")

    def _result(self, order: ApprovedOrder, o: _Order) -> ExecutionResult:
        at = o.filled_at or o.updated_at or self._clock.now()
        fills = []
        if o.filled_qty > 0:
            if o.filled_avg_price is None or o.filled_avg_price <= 0:
                raise BrokerStateUnknown("fill without a price")
            if o.qty is not None and o.filled_qty > o.qty:
                raise BrokerStateUnknown("filled more than ordered")
            fills.append(Fill(fill_id=self._ids.next("fill"), order_id=order.order_id, symbol=order.symbol,
                              side=order.side, quantity=o.filled_qty, price=o.filled_avg_price, fee=Decimal(0),
                              at=at))
        status = _ack_status(o)
        reason = None if status is AckStatus.ACCEPTED else o.status.upper()
        return ExecutionResult(BrokerAck(order_id=order.order_id, status=status, reason=reason, at=at), fills)


def _ack_status(o: _Order) -> AckStatus:
    if o.filled_qty > 0:
        return AckStatus.ACCEPTED
    if o.status == "rejected":
        return AckStatus.REJECTED
    if o.status in ("canceled", "expired", "done_for_day"):
        return AckStatus.EXPIRED
    return AckStatus.ACCEPTED   # open (new/accepted/pending): the broker has it
