"""Run one PAPER trading session on the Alpaca paper account. Run it yourself, in your own terminal.

It refuses to start until the owner has done every step in docs/paper-runbook.md, in particular:
  * PAPER is a permitted mode and approvals cover risk-policy.v3, the strategy and the prompts for PAPER;
  * config/runtime.paper.yaml exists (written by the owner; Claude never sets a mode);
  * the risk service runs in PAPER mode (python -m trading_agent.risk_service --config config/runtime.paper.yaml ...);
  * the vault holds alpaca-paper (KEY_ID:SECRET) and anthropic-api-key, and TRADING_VAULT_TOKEN is set.

  python scripts/run_paper_session.py --pki-dir ~/trading-agent-pki --run-dir ~/trading-agent-runs/paper

Every cycle (default 60 s) prints one line and persists a DecisionPacket. Ctrl-C stops it; the next start
recovers (same session: REDUCE_ONLY floor; unresolved orders are settled with the broker, else HALT).
"""
import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps")]

from trading_agent.account_data.alpaca_status import (ALLOWED_HOSTS, PAPER_TRADING_HOST,  # noqa: E402
                                                      AlpacaMarketStatus, AlpacaReadOnlyClient)
from trading_agent.account_data.https_transport import HttpsGetTransport  # noqa: E402
from trading_agent.core.clock import SystemClock  # noqa: E402
from trading_agent.core.errors import TradingAgentError  # noqa: E402
from trading_agent.core.mode import load_runtime_config  # noqa: E402
from trading_agent.core.types import IdFactory  # noqa: E402
from trading_agent.execution.alpaca_http import AlpacaTradingClient, HttpsTradingTransport  # noqa: E402
from trading_agent.market_data.alpaca_stream import AlpacaStreamTransport  # noqa: E402
from trading_agent.market_data.instruments import InstrumentRegistry  # noqa: E402
from trading_agent.model_client.anthropic_transport import API_KEY_NAME, AnthropicMessagesTransport  # noqa: E402
from trading_agent.model_client.client import RetryPolicy, SdkLLMClient  # noqa: E402
from trading_agent.orchestration.paper import CREDENTIAL, build_paper_runner, run_session  # noqa: E402
from trading_agent.risk.policy import load_risk_policy  # noqa: E402
from trading_agent.risk_service.client import RemoteRiskGateway  # noqa: E402
from trading_agent.risk_service.tls import client_context  # noqa: E402
from trading_agent.secret_store.vault import Vault, VaultSecretsProvider  # noqa: E402

MODEL = "claude-sonnet-5-5"
DEFAULT_VAULT_DIR = Path.home() / ".trading-agent"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One PAPER session on the Alpaca paper account.")
    ap.add_argument("--config", default=str(ROOT / "config" / "runtime.paper.yaml"))
    ap.add_argument("--run-dir", required=True, help="outside the repository; reused across restarts")
    ap.add_argument("--pki-dir", required=True)
    ap.add_argument("--risk-port", type=int, default=8766)
    ap.add_argument("--feed", default="iex", choices=["iex", "sip"])
    ap.add_argument("--cycle-seconds", type=float, default=60.0)
    ap.add_argument("--max-cycles", type=int, default=None)
    ap.add_argument("--vault-dir", default=str(DEFAULT_VAULT_DIR))
    a = ap.parse_args(argv)
    clock = SystemClock()
    try:
        config = load_runtime_config(a.config)            # refuses a mode that is not permitted
        config_dir = Path(a.config).resolve().parent
        policy = load_risk_policy(config_dir / (config.risk_policy_version.replace("-", "_") + ".yaml"))
        registry = InstrumentRegistry.load(config_dir / "instruments.v2.yaml")
        token = os.environ.get("TRADING_VAULT_TOKEN")
        if not token:
            raise TradingAgentError("TRADING_VAULT_TOKEN is not set (docs/paper-runbook.md)")
        vault_dir = Path(a.vault_dir)
        vault = Vault.open(vault_dir / "trading-agent.vault", getpass.getpass("Vault passphrase: "),
                           vault_dir / "trading-agent.vault.access.jsonl")
        secrets = VaultSecretsProvider(vault.as_client(token))
        pki = Path(a.pki_dir)
        gateway = RemoteRiskGateway(config.mode, {policy.source_sha256}, IdFactory("orch"), clock,
                                    port=a.risk_port, context=client_context(pki / "ca.pem", pki / "orchestrator.pem",
                                                                             pki / "orchestrator.key"))
        llm = SdkLLMClient(AnthropicMessagesTransport(secrets, API_KEY_NAME), MODEL,
                           retry=RetryPolicy(total_deadline_s=40, attempt_timeout_s=30))
        runner = build_paper_runner(
            Path(a.run_dir), config, policy, registry, secrets, gateway, llm, llm, clock,
            stream_transport=AlpacaStreamTransport(a.feed),
            trading_client=AlpacaTradingClient(HttpsTradingTransport(), secrets, CREDENTIAL, clock),
            market_status=AlpacaMarketStatus(AlpacaReadOnlyClient(HttpsGetTransport(PAPER_TRADING_HOST, ALLOWED_HOSTS),
                                                                  secrets, CREDENTIAL, clock), clock),
            config_dir=config_dir)
    except TradingAgentError as exc:
        print(f"refused to start: {exc}")
        return 2
    print(f"PAPER session {runner.session_id}: run {a.run_dir}, recovery {runner.recovery.get('kind')}, "
          f"state {runner.state.state.value}")
    cycles = run_session(runner, clock, a.cycle_seconds, a.max_cycles)
    print(f"{cycles} cycles; packets in {Path(a.run_dir) / 'packets.jsonl'}; "
          f"metrics: python -m trading_agent.orchestration.session_metrics {a.run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
