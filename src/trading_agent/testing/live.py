"""Mock 'live' feed for the live loop: replays dataset ticks into a ScriptedStreamTransport as the clock passes."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import List, Optional

from trading_agent.backtest.dataset import load_dataset
from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.execution.cost_model import CostModel
from trading_agent.market_data.ports import RawTick
from trading_agent.orchestration.live_loop import LiveLoopRunner, load_ops_policy
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.testing.feeds import feed, quote_msg, registry
from trading_agent.testing.harness import ROOT, risk_policy, runtime_config

VENDOR = "vendor_a"


class ManualClock:
    """A clock tests can move anywhere, including backwards (to test that the loop refuses)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, t: datetime) -> None:
        self._now = t

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        self._now += timedelta(seconds=seconds)


class Replay:
    def __init__(self, ticks: List[RawTick], transport, clock, start: Optional[datetime] = None) -> None:
        reg = registry()
        self._vendor = {s: reg.vendor_symbol(VENDOR, s) for s in reg.symbols}
        self._ticks = [t for t in ticks if start is None or t.ts >= start]
        self._i = 0
        self._seq = 0
        self.transport = transport
        self.clock = clock
        self.paused = False

    def pump(self) -> None:
        while not self.paused and self._i < len(self._ticks) and self._ticks[self._i].ts <= self.clock.now():
            t = self._ticks[self._i]
            self._seq += 1
            self.transport.push(quote_msg(self._seq, t.ts, self._vendor[t.symbol], str(t.bid), str(t.ask),
                                          bid_size=str(t.bid_size), ask_size=str(t.ask_size), last=str(t.last)))
            self._i += 1


def day_ticks(day: str = "2026-01-13", start_utc: str = "18:40", end_utc: str = "19:40") -> List[RawTick]:
    ds = load_dataset("synthetic-ca-2026jan.v1")
    lo = datetime.fromisoformat(f"{day}T{start_utc}:00+00:00")
    hi = datetime.fromisoformat(f"{day}T{end_utc}:00+00:00")
    return [t for t in ds.ticks if lo <= t.ts < hi]


def make_runner(run_dir: Path, clock, session_id: str = "2026-01-13", planner_fn=demo_planner_response,
                critic_fn=demo_critic_response, config=None, start: Optional[datetime] = None, ticks=None):
    adapter, transport, _ = feed(sessions=[[]], clock=clock, source_id=VENDOR, heartbeat_s=120, max_age_s=5)
    runner = LiveLoopRunner(run_dir, config or runtime_config(), risk_policy(),
                            load_ops_policy(ROOT / "config" / "ops_policy.v1.yaml"),
                            ScriptedLLM(planner_fn, "demo-strategy.v1"), ScriptedLLM(critic_fn, "demo-critic.v1"),
                            [adapter], clock, session_id,
                            instrument_status=lambda s: InstrumentStatus.TRADING,
                            venue_status=lambda t: VenueStatus.OPEN, cost_model=CostModel())
    replay = Replay(ticks if ticks is not None else day_ticks(), transport, clock, start=start)
    return runner, replay


def drive(runner, replay, clock, steps: int, every_s: float = 60.0):
    out = []
    for _ in range(steps):
        if runner.stopped:
            break
        replay.pump()
        out.append(runner.step())
        clock.advance(every_s)
    return out


def at(hhmm: str, day: str = "2026-01-13", ms: int = 500) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+00:00").replace(microsecond=ms * 1000, tzinfo=timezone.utc)
