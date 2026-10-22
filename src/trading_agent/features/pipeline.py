from collections import deque
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
