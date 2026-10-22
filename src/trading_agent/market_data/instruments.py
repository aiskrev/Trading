"""Normalized instrument identifiers.

Every vendor symbol is mapped to one canonical Symbol via the versioned registry in
config/instruments.v1.yaml. Unknown symbols are rejected, never guessed.
"""
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Tuple, Union

import yaml
from pydantic import Field, ValidationError

from trading_agent.contracts.base import Contract
from trading_agent.core.errors import ConfigError, UnknownInstrument
from trading_agent.core.types import Symbol

MAX_VENDOR_SYMBOL_LEN = 32


class InstrumentSpec(Contract):
    symbol: Symbol
    asset_class: str = Field(pattern=r"^[A-Z_]{2,16}$")
    aliases: Dict[str, List[str]] = Field(default_factory=dict)  # source_id -> vendor symbols


class InstrumentRegistry:
    def __init__(self, specs: Iterable[InstrumentSpec], version: str) -> None:
        self.version = version
        self._specs = {s.symbol: s for s in specs}
        self._aliases: Dict[Tuple[str, str], str] = {}
        for spec in self._specs.values():
            for source, names in spec.aliases.items():
                for name in names:
                    key = (source, _clean(name))
                    if key in self._aliases and self._aliases[key] != spec.symbol:
                        raise ConfigError(f"alias {key} maps to both {self._aliases[key]} and {spec.symbol}")
                    self._aliases[key] = spec.symbol

    @classmethod
    def load(cls, path: Union[str, Path]) -> "InstrumentRegistry":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            specs = [InstrumentSpec.model_validate(s) for s in raw["instruments"]]
            return cls(specs, raw["version"])
        except (OSError, KeyError, TypeError, yaml.YAMLError, ValidationError) as exc:
            raise ConfigError(f"invalid instrument registry {path}: {exc}") from exc

    @property
    def symbols(self) -> FrozenSet[str]:
        return frozenset(self._specs)

    def normalize(self, source_id: str, vendor_symbol: object) -> str:
        if not isinstance(vendor_symbol, str) or not vendor_symbol.strip():
            raise UnknownInstrument("empty or non-string symbol")
        if len(vendor_symbol) > MAX_VENDOR_SYMBOL_LEN or not vendor_symbol.isprintable():
            raise UnknownInstrument("symbol too long or non-printable")
        key = _clean(vendor_symbol)
        if (source_id, key) in self._aliases:
            return self._aliases[(source_id, key)]
        if key in self._specs:
            return key
        raise UnknownInstrument(f"{source_id}:{key[:MAX_VENDOR_SYMBOL_LEN]}")

    def vendor_symbol(self, source_id: str, symbol: str) -> str:
        """The symbol to subscribe with for `source_id` (first alias, else canonical)."""
        names = self._specs[symbol].aliases.get(source_id)
        return names[0] if names else symbol


def _clean(name: str) -> str:
    return " ".join(name.strip().upper().split())
