"""AT-02 (only SimulatedBroker in BACKTEST) and AT-03 (mode frozen, non-BACKTEST refused)."""
import inspect
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import AdapterNotPermitted, ConfigError, ModeNotPermitted
from trading_agent.core.mode import PERMITTED_MODES_THIS_STAGE, Mode, RuntimeConfig, load_runtime_config
from trading_agent.core.signing import EphemeralSigner
from trading_agent.core.types import IdFactory
from trading_agent.execution import registry
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.ports import ExecutionPort
from trading_agent.execution.simulated_broker import SimulatedBroker
from trading_agent.testing.harness import ROOT, T0, verifier_for


def _deps():
    clock = SimulatedClock(T0)
    return dict(verifier=verifier_for(EphemeralSigner(), clock), cost_model=CostModel(), quote_for=lambda s: None,
                clock=clock, ids=IdFactory("t"))


def test_only_backtest_is_permitted_this_stage():
    assert PERMITTED_MODES_THIS_STAGE == frozenset({Mode.BACKTEST})


def test_registry_has_only_simulated_broker():
    assert registry.permitted_adapters() == {Mode.BACKTEST: "SimulatedBroker"}
    assert isinstance(registry.create_execution_port(Mode.BACKTEST, **_deps()), SimulatedBroker)


@pytest.mark.parametrize("mode", [m for m in Mode if m is not Mode.BACKTEST])
def test_every_other_mode_is_refused(mode):
    with pytest.raises((ModeNotPermitted, AdapterNotPermitted)):
        registry.create_execution_port(mode, **_deps())


def test_runtime_config_is_frozen_and_has_no_mode_setter():
    cfg = load_runtime_config(ROOT / "config" / "runtime.backtest.yaml")
    assert cfg.mode is Mode.BACKTEST
    with pytest.raises(ValidationError):
        cfg.mode = Mode.FULL_CONFIGURED_LIVE
    setters = [n for n in dir(cfg) if "mode" in n.lower() and n.startswith("set")]
    assert setters == []


def test_runtime_config_rejects_unknown_fields():
    raw = yaml.safe_load((ROOT / "config" / "runtime.backtest.yaml").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate({**raw, "override_mode": "LIVE"})


@pytest.mark.parametrize("mode", ["PAPER", "CANARY_LIVE", "FULL_CONFIGURED_LIVE"])
def test_startup_refuses_non_backtest_mode(tmp_path, mode):
    raw = yaml.safe_load((ROOT / "config" / "runtime.backtest.yaml").read_text(encoding="utf-8"))
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump({**raw, "mode": mode}), encoding="utf-8")
    with pytest.raises(ModeNotPermitted):
        load_runtime_config(path)


def test_invalid_config_refuses_to_start(tmp_path):
    path = tmp_path / "runtime.yaml"
    path.write_text("mode: [not, a, mode]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_runtime_config(path)


def test_execution_port_surface_is_narrow():
    proto = {n for n, _ in inspect.getmembers(ExecutionPort, inspect.isfunction) if not n.startswith("_")}
    assert proto == {"submit", "cancel", "broker_positions"}
    impl = {n for n in dir(SimulatedBroker) if not n.startswith("_")}
    # apply_corporate_action is a broker-side book event for BACKTEST (split/delisting): it places no order
    assert impl == {"submit", "cancel", "broker_positions", "apply_corporate_action"}
    params = list(inspect.signature(SimulatedBroker.submit).parameters)
    assert params == ["self", "order"]
