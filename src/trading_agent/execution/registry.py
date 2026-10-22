from typing import Callable, Dict

from trading_agent.core.errors import AdapterNotPermitted
from trading_agent.core.mode import Mode, assert_mode_permitted
from trading_agent.execution.ports import ExecutionPort
from trading_agent.execution.simulated_broker import SimulatedBroker

# The complete list of execution adapters. No live or paper adapter exists at this stage.
_ADAPTERS: Dict[Mode, Callable[..., ExecutionPort]] = {
    Mode.BACKTEST: SimulatedBroker,
}


def permitted_adapters() -> Dict[Mode, str]:
    return {m: f.__name__ for m, f in _ADAPTERS.items()}


def create_execution_port(mode: Mode, **deps) -> ExecutionPort:
    assert_mode_permitted(mode)
    factory = _ADAPTERS.get(mode)
    if factory is None:
        raise AdapterNotPermitted(f"no execution adapter permitted for {mode.value}")
    return factory(**deps)
