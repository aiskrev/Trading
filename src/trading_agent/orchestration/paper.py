"""PAPER composition (roadmap P6): Alpaca data + status + execution, the risk service, the real model.

`build_paper_runner` wires a LiveLoopRunner from already-built parts; it holds no vault code (the vault is
opened by the script, scripts/run_paper_session.py). It refuses to build unless:
  * the runtime config's mode is PAPER and PAPER is a permitted mode (only the owner widens that set);
  * the risk gateway runs PAPER, the approved policy, and the Alpaca paper account alias;
  * the Alpaca paper account is ACTIVE, not blocked, in USD, and, for a new run, holds no positions
    (the run's book starts flat; anything else would be an unexplained position => refuse).

`run_session` drives the loop on a fixed cadence until the market has closed, the runner stopped (HALT on an
operational error), `max_cycles` is reached, or the operator presses Ctrl-C. Every cycle persists a DecisionPacket.
"""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional, Sequence

from trading_agent.account_data.alpaca_status import AlpacaMarketStatus
from trading_agent.contracts.market import VenueStatus
from trading_agent.core.accounts import ALPACA_PAPER_ALIAS
from trading_agent.core.clock import Clock
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode, RuntimeConfig, assert_mode_permitted
from trading_agent.core.signing import TrustedKey
from trading_agent.core.types import IdFactory
from trading_agent.execution.alpaca_http import AlpacaTradingClient
from trading_agent.execution.alpaca_paper import AlpacaPaperBroker
from trading_agent.execution.verifier import OrderVerifier
from trading_agent.market_data.alpaca_stream import AlpacaQuoteDecoder, AlpacaStreamTransport
from trading_agent.market_data.instruments import InstrumentRegistry
from trading_agent.market_data.quote_book import QuoteBook
from trading_agent.market_data.streaming import StreamingQuoteAdapter
from trading_agent.orchestration.live_loop import ROOT, LiveLoopRunner, load_ops_policy
from trading_agent.risk.policy import RiskPolicy

SOURCE_ID = "alpaca"
CREDENTIAL = "alpaca-paper"
NEW_YORK = "America/New_York"


def session_id_for(now: datetime) -> str:
    from zoneinfo import ZoneInfo
    return now.astimezone(ZoneInfo(NEW_YORK)).date().isoformat()


def build_paper_runner(run_dir: Path, config: RuntimeConfig, policy: RiskPolicy, registry: InstrumentRegistry,
                       secrets, risk_gateway, planner_llm, critic_llm, clock: Clock,
                       stream_transport: Optional[AlpacaStreamTransport] = None,
                       trading_client: Optional[AlpacaTradingClient] = None,
                       market_status: Optional[AlpacaMarketStatus] = None,
                       config_dir: Path = ROOT / "config", ops_policy_path: Optional[Path] = None) -> LiveLoopRunner:
    if config.mode is not Mode.PAPER:
        raise ConfigError(f"this composition is for PAPER, the runtime config says {config.mode.value}")
    assert_mode_permitted(config.mode)
    if set(registry.symbols) != set(policy.universe):
        raise ConfigError("the instrument registry and the risk policy name different symbols")
    if risk_gateway is None:
        raise ConfigError("PAPER needs the risk service (no in-process signer)")
    if (risk_gateway.mode is not Mode.PAPER or risk_gateway.policy.source_sha256 != policy.source_sha256
            or risk_gateway.account_alias != ALPACA_PAPER_ALIAS):
        raise ConfigError("the risk service runs another mode, policy or account")
    if trading_client is None or market_status is None or stream_transport is None:
        raise ConfigError("PAPER needs the Alpaca stream, status and trading clients")

    # Pre-flight at the broker (read-only): account usable; a new run starts flat.
    verifier = OrderVerifier([TrustedKey(key_id=risk_gateway.key_id, public_key=risk_gateway.public_key)],
                             Mode.PAPER, ALPACA_PAPER_ALIAS, {policy.source_sha256}, clock)
    probe = AlpacaPaperBroker(verifier, trading_client, clock, IdFactory("preflight"))
    account = probe.account_summary()
    new_run = not (Path(run_dir) / "manifest.json").exists()
    if new_run and probe.broker_positions():
        raise ConfigError("the Alpaca paper account holds positions; close them in the Alpaca dashboard before "
                          "starting a new run (the run's book starts flat)")

    symbols = sorted(policy.universe)
    adapter = StreamingQuoteAdapter(
        SOURCE_ID, stream_transport, AlpacaQuoteDecoder(SOURCE_ID, registry), symbols, clock,
        QuoteBook(SOURCE_ID, timedelta(seconds=policy.max_quote_age_seconds), timedelta(seconds=2)),
        secrets=secrets, credential_name=CREDENTIAL, heartbeat_timeout=timedelta(seconds=60))
    ops = load_ops_policy(ops_policy_path or Path(config_dir) / "ops_policy.v1.yaml")
    return LiveLoopRunner(run_dir, config, policy, ops, planner_llm, critic_llm, [adapter], clock,
                          session_id_for(clock.now()), instrument_status=market_status.instrument_status,
                          venue_status=market_status.venue_status, starting_cash=Decimal(account["cash"]),
                          run_id="paper", config_dir=Path(config_dir), risk_gateway=risk_gateway,
                          account_alias=ALPACA_PAPER_ALIAS, broker_deps={"trading_client": trading_client})


def run_session(runner: LiveLoopRunner, clock: Clock, cycle_seconds: float = 60.0, max_cycles: Optional[int] = None,
                closed_cycles_to_stop: int = 3, sleep: Callable[[float], None] = time.sleep,
                report: Callable[[str], None] = print) -> int:
    """Run cycles until done. Returns the number of cycles run."""
    cycles, closed_in_a_row = 0, 0
    try:
        while not runner.stopped and (max_cycles is None or cycles < max_cycles):
            started = clock.now()
            packet = runner.step()
            cycles += 1
            report(_line(packet))
            closed = packet.market.venue_status is VenueStatus.CLOSED
            closed_in_a_row = closed_in_a_row + 1 if closed else 0
            if closed_in_a_row >= closed_cycles_to_stop:
                report("market closed: session ends")
                break
            elapsed = (clock.now() - started).total_seconds()
            sleep(max(cycle_seconds - elapsed, 0.0))
    except KeyboardInterrupt:
        report("stopped by the operator")
    if runner.stopped:
        report(f"stopped: {runner.stopped}")
    return cycles


def _line(packet) -> str:
    d = packet.risk_decision
    order = packet.approved_order or {}
    fills = sum((f.quantity for f in packet.fills), Decimal(0))
    reasons = ",".join(d.reason_codes[:3]) if d is not None and d.reason_codes else (
        packet.proposal.reason_code if packet.proposal is not None and packet.proposal.reason_code else "")
    what = f" {order.get('side')} {order.get('symbol')} {order.get('notional')}" if order else ""
    return (f"{packet.at.astimezone(timezone.utc):%H:%M:%S} {packet.outcome:<12} {packet.state_after.value:<11}"
            f" venue={packet.market.venue_status.value:<7} quotes={len(packet.market.quotes)}{what}"
            f"{f' filled={fills}' if fills else ''}{f' ({reasons})' if reasons else ''}")
