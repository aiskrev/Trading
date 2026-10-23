"""Create a demo live run on MOCK feeds so the control room has something to show.

BACKTEST mode only (the live loop refuses any other mode), the synthetic dataset, scripted demo models, the simulated
broker. It places no real order, calls no model and needs no credentials. One restart is included on purpose, so the
recovery and idempotency records appear.

  python -m ui.make_demo_run [--runs-dir runs] [--name demo-live]
"""
import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT / ".deps", ROOT):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs-dir", default=str(ROOT / "runs"))
    ap.add_argument("--name", default="demo-live")
    ap.add_argument("--steps", type=int, default=45)
    a = ap.parse_args(argv)

    from trading_agent.testing.live import ManualClock, at, drive, make_runner

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", a.name) or a.name == "ui":
        print("refusing: the run name must be a plain folder name")
        return 2
    run_dir = Path(a.runs_dir) / a.name
    if run_dir.exists():
        if not (run_dir / "manifest.json").is_file():
            print(f"refusing: {run_dir} exists and is not a run folder")      # never delete something else
            return 2
        shutil.rmtree(run_dir)                      # a previous run folder of the same name
    clock = ManualClock(at("18:50"))
    runner, replay = make_runner(run_dir, clock)
    first = max(a.steps // 2, 5)
    drive(runner, replay, clock, first)
    print(f"segment 1: {first} cycles, state {runner.state.state.value}, held {runner.portfolio.quantities()}")
    runner2, replay2 = make_runner(run_dir, clock, start=clock.now())   # the process "restarts"
    print(f"restart: {runner2.recovery.get('kind')}, state {runner2.state.state.value}")
    drive(runner2, replay2, clock, a.steps - first)
    print(f"segment 2 done: state {runner2.state.state.value}; run written to {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
