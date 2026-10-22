import pytest

from trading_agent.core.errors import ConfigError, UnknownInstrument
from trading_agent.market_data.instruments import InstrumentRegistry, InstrumentSpec
from trading_agent.testing.feeds import registry


@pytest.mark.parametrize("source,vendor,expected", [
    ("vendor_a", "AAA.US", "AAA"),
    ("vendor_a", "  xnas:aaa ", "AAA"),
    ("broker_a", "AAA  US   Equity", "AAA"),
    ("vendor_a", "BBB", "BBB"),          # canonical form accepted from any source
    ("other", "bbb", "BBB"),
])
def test_normalizes_known_symbols(source, vendor, expected):
    assert registry().normalize(source, vendor) == expected


@pytest.mark.parametrize("source,vendor", [
    ("vendor_a", "ZZZ.US"),
    ("broker_a", "AAA.US"),               # alias belongs to vendor_a only
    ("vendor_a", ""),
    ("vendor_a", "   "),
    ("vendor_a", None),
    ("vendor_a", 123),
    ("vendor_a", "A" * 40),
    ("vendor_a", "AAA\x00"),
])
def test_rejects_unknown_or_bad_symbols(source, vendor):
    with pytest.raises(UnknownInstrument):
        registry().normalize(source, vendor)


def test_conflicting_alias_is_config_error():
    specs = [InstrumentSpec(symbol="AAA", asset_class="EQUITY", aliases={"v": ["X"]}),
             InstrumentSpec(symbol="BBB", asset_class="EQUITY", aliases={"v": ["x"]})]
    with pytest.raises(ConfigError):
        InstrumentRegistry(specs, "t")


def test_registry_matches_risk_universe():
    from trading_agent.testing.harness import risk_policy
    assert registry().symbols == risk_policy().universe


def test_vendor_symbol_for_subscription():
    assert registry().vendor_symbol("vendor_a", "AAA") == "AAA.US"
    assert registry().vendor_symbol("nobody", "AAA") == "AAA"
