from typing import Callable, Dict

from trading_agent.core.errors import AdapterNotPermitted
from trading_agent.core.mode import Mode, assert_mode_permitted
from trading_agent.execution.alpaca_paper import AlpacaPaperBroker
from trading_agent.execution.ports import ExecutionPort
from trading_agent.execution.simulated_broker import SimulatedBroker


def _alpaca_paper(verifier, clock, ids, idempotency=None, trading_client=None, **_simulator_only) -> ExecutionPort:
    """PAPER: the Alpaca paper account. Reachable only once PAPER is a permitted mode (owner decision)."""
    if trading_client is None:
        raise AdapterNotPermitted("PAPER needs an Alpaca trading client built by the composition root")
    if idempotency is None or not idempotency.durable:
        raise AdapterNotPermitted("PAPER needs a durable idempotency store")
    return AlpacaPaperBroker(verifier, trading_client, clock, ids, idempotency=idempotency)


_alpaca_paper.__name__ = "AlpacaPaperBroker"

# The complete list of execution adapters. create_execution_port refuses any mode that is not permitted at
# this stage (core.mode.PERMITTED_MODES_THIS_STAGE), so the PAPER entry is unreachable until the owner permits it.
# No live adapter exists.
_ADAPTERS: Dict[Mode, Callable[..., ExecutionPort]] = {
    Mode.BACKTEST: SimulatedBroker,
    Mode.PAPER: _alpaca_paper,
}


def permitted_adapters() -> Dict[Mode, str]:
    return {m: f.__name__ for m, f in _ADAPTERS.items()}


def create_execution_port(mode: Mode, **deps) -> ExecutionPort:
    assert_mode_permitted(mode)
    factory = _ADAPTERS.get(mode)
    if factory is None:
        raise AdapterNotPermitted(f"no execution adapter permitted for {mode.value}")
    return factory(**deps)
