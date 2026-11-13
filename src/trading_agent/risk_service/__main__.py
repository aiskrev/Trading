"""Composition root of the risk service process.

    python -m trading_agent.risk_service --pki-dir DIR --log FILE [--port 8766] [--kill-switch PATH ...]

Starts only in a permitted mode (BACKTEST today) and only with a risk policy approved for that mode. The
signing key is ephemeral (generated at start, never written); moving it into the vault is ADR 0003 step 4.
The certificates come from `scripts/make_dev_certs.py` (outside the repository). Ctrl-C stops it.
"""
import argparse
import secrets
import sys
from pathlib import Path

from trading_agent.core.accounts import account_alias_for
from trading_agent.core.clock import SystemClock
from trading_agent.core.errors import TradingAgentError
from trading_agent.core.mode import load_runtime_config
from trading_agent.core.signing import EphemeralSigner
from trading_agent.risk.policy import load_risk_policy
from trading_agent.risk_service.pki import inside_git_worktree
from trading_agent.risk_service.protocol import SERVICE_IDENTITY
from trading_agent.risk_service.server import RiskServiceServer
from trading_agent.risk_service.service import build_risk_service
from trading_agent.risk_service.tls import server_context

ROOT = Path(__file__).resolve().parents[3]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m trading_agent.risk_service", description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(ROOT / "config" / "runtime.backtest.yaml"))
    ap.add_argument("--pki-dir", required=True, help="directory made by scripts/make_dev_certs.py")
    ap.add_argument("--log", required=True, help="hash-chained request log (outside the repository)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--kill-switch", action="append", default=[str(ROOT / "ops" / "KILL_SWITCH")],
                    help="a file whose presence stops all signing (repeatable)")
    args = ap.parse_args(argv)
    try:
        config = load_runtime_config(args.config)
        config_dir = Path(args.config).resolve().parent
        policy = load_risk_policy(config_dir / (config.risk_policy_version.replace("-", "_") + ".yaml"))
        pki = Path(args.pki_dir)
        if inside_git_worktree(pki) or inside_git_worktree(Path(args.log).resolve().parent):
            raise TradingAgentError("keys and the request log must be outside the git repository")
        service_id = f"rs-{secrets.token_hex(4)}"   # ids unique per start: order_ids never repeat after a restart
        service = build_risk_service(policy, config.mode, EphemeralSigner(service_id), SystemClock(), config_dir,
                                     args.log, service_id, kill_switch_paths=args.kill_switch,
                                     account_alias=account_alias_for(config.mode))
        context = server_context(pki / "ca.pem", pki / f"{SERVICE_IDENTITY}.pem", pki / f"{SERVICE_IDENTITY}.key")
        server = RiskServiceServer(service, context, args.host, args.port)
    except (TradingAgentError, OSError) as exc:
        print(f"risk service refused to start: {exc}", file=sys.stderr)
        return 2
    host, port = server.address
    info = service.info
    print(f"risk service {info.service_id} on https://{host}:{port} mode={info.environment.value} "
          f"policy={info.policy_version} ({info.policy_sha256[:12]}) key_id={info.key_id} "
          f"public_key={info.public_key_hex}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
