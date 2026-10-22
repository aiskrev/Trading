"""Deterministic stand-in for Claude in BACKTEST: a tiny mean-reversion rule that emits the same
JSON a real planner would. It exists to exercise the pipeline, not to make money."""
import json
from decimal import Decimal
from typing import Any, Dict

ENTRY_DEV_BPS = Decimal(-30)
EXIT_DEV_BPS = Decimal(0)
ORDER_NOTIONAL = Decimal(5000)
DUST_NOTIONAL = Decimal(1)


def demo_planner_response(payload: Dict[str, Any]) -> str:
    quotes = payload["market_snapshot"]["quotes"]
    positions = payload["account_snapshot"]["positions"]
    features = payload["features"]["values"]
    if payload["market_snapshot"]["venue_status"] != "OPEN":
        return _no_trade("Venue not OPEN.")
    for symbol in payload["allowed_universe"]:
        feats = features.get(symbol, {})
        if symbol not in quotes or "dev_from_sma_10_bps" not in feats:
            continue
        dev = Decimal(feats["dev_from_sma_10_bps"])
        q = quotes[symbol]
        held = Decimal(positions[symbol]["quantity"]) if symbol in positions else Decimal(0)
        flat = held * Decimal(q["bid"]) < DUST_NOTIONAL  # notional sizing leaves sub-cent remainders
        if flat and dev <= ENTRY_DEV_BPS:
            return _trade(symbol, "BUY", q["ask"], ORDER_NOTIONAL, dev)
        if not flat and dev >= EXIT_DEV_BPS:
            notional = (held * Decimal(q["bid"])).quantize(Decimal("0.01"))
            return _trade(symbol, "SELL", q["bid"], notional, dev)
    return _no_trade("No symbol meets entry or exit rule.")


def _trade(symbol: str, side: str, price: str, notional: Decimal, dev: Decimal) -> str:
    return json.dumps({
        "decision": "TRADE", "symbol": symbol, "side": side, "order_type": "LIMIT",
        "limit_price": str(price), "requested_notional": str(notional),
        "invalidation_conditions": ["dev_from_sma_10_bps crosses back through entry threshold"],
        "evidence_refs": [f"quote:{symbol}", f"feature:{symbol}:dev_from_sma_10_bps", "venue:status"],
        "public_rationale": f"{symbol} mid deviates {dev}bps from 10-bar SMA; {side} per demo rule.",
    })


def _no_trade(reason: str) -> str:
    return json.dumps({"decision": "NO_TRADE", "public_rationale": reason})


def demo_critic_response(payload: Dict[str, Any]) -> str:
    return json.dumps({"verdict": "PASS", "reason_codes": [], "evidence_refs": []})
