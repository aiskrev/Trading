from collections import deque
from decimal import Decimal
from typing import Deque, Dict, List, Optional

from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot, Quote
from trading_agent.features.registry import Feature, default_features


class FeaturePipeline:
    """Rolling, point-in-time features. Only data at or before the snapshot is used (no look-ahead)."""

    def __init__(self, features: Optional[List[Feature]] = None, max_history: int = 200,
                 feature_version: str = "features.v1") -> None:
        self.features = features if features is not None else default_features()
        self.feature_version = feature_version
        self._history: Dict[str, Deque[Quote]] = {}
        self._max = max_history

    def adjust_history(self, symbol: str, price_factor: Decimal, size_factor: Decimal = Decimal(1)) -> None:
        """Point-in-time corporate-action adjustment, applied on the ex-date only.

        Rescales stored past quotes so they are comparable with post-action prices (e.g. a 2:1 split
        uses price_factor 1/2, size_factor 2). Nothing earlier than the ex-date ever sees this factor.
        """
        hist = self._history.get(symbol)
        if not hist:
            return
        q6 = Decimal("0.000001")
        self._history[symbol] = deque(
            (Quote(bid=(q.bid * price_factor).quantize(q6), ask=(q.ask * price_factor).quantize(q6),
                   bid_size=q.bid_size * size_factor, ask_size=q.ask_size * size_factor,
                   last=(q.last * price_factor).quantize(q6), ts=q.ts) for q in hist), maxlen=self._max)

    def last_mid(self, symbol: str) -> Optional[Decimal]:
        hist = self._history.get(symbol)
        return hist[-1].mid if hist else None

    def update(self, snapshot: MarketSnapshot) -> FeatureSet:
        values = {}
        for symbol, quote in snapshot.quotes.items():
            hist = self._history.setdefault(symbol, deque(maxlen=self._max))
            if not hist or hist[-1].ts < quote.ts:
                hist.append(quote)
            computed = {}
            for f in self.features:
                v = f.compute(list(hist))
                if v is not None:
                    computed[f.name] = v
            values[symbol] = computed
        return FeatureSet(snapshot_id=snapshot.snapshot_id, feature_version=self.feature_version, values=values)
