"""Read-only check of the Alpaca paper connection. Run it yourself, in your own terminal. It places NO orders.

One-time setup (keys from the Alpaca dashboard, PAPER account; never paste them into chat):

  python scripts/vault.py put-secret alpaca-paper        (hidden prompt; enter KEY_ID:SECRET as one value)
  python scripts/vault.py grant market-data --read alpaca-paper     (prints a token ONCE)
  export TRADING_VAULT_TOKEN=<that token>                (for this shell only)

Then:

  python scripts/alpaca_check.py                         # market clock, asset status, 20 s of IEX quotes
  python scripts/alpaca_check.py --feed test --symbols FAKEPACA --seconds 10    # Alpaca's test stream

It prints the market clock, each symbol's status and the quotes received; secrets are never printed.
"""
import argparse
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps")]

from trading_agent.account_data.alpaca_status import (ALLOWED_HOSTS, PAPER_TRADING_HOST,  # noqa: E402
                                                      AlpacaMarketStatus, AlpacaReadOnlyClient)
from trading_agent.account_data.https_transport import HttpsGetTransport  # noqa: E402
from trading_agent.core.clock import SystemClock  # noqa: E402
from trading_agent.core.errors import TradingAgentError  # noqa: E402
from trading_agent.market_data.alpaca_stream import AlpacaQuoteDecoder, AlpacaStreamTransport  # noqa: E402
from trading_agent.market_data.instruments import InstrumentRegistry, InstrumentSpec  # noqa: E402
from trading_agent.market_data.quote_book import QuoteBook  # noqa: E402
from trading_agent.market_data.streaming import StreamingQuoteAdapter  # noqa: E402
from trading_agent.secret_store.vault import Vault, VaultSecretsProvider  # noqa: E402

CREDENTIAL = "alpaca-paper"
DEFAULT_VAULT_DIR = Path.home() / ".trading-agent"


def open_vault_secrets(vault_dir: Path, prompt, env=os.environ):
    token = env.get("TRADING_VAULT_TOKEN")
    if not token:
        raise TradingAgentError("TRADING_VAULT_TOKEN is not set (see the usage text: vault.py grant ...)")
    vault = Vault.open(vault_dir / "trading-agent.vault", prompt("Vault passphrase: "),
                       vault_dir / "trading-agent.vault.access.jsonl")
    return VaultSecretsProvider(vault.as_client(token))


def main(argv=None, secrets=None, out=print) -> int:
    ap = argparse.ArgumentParser(description="Read-only Alpaca paper connection check (no orders).")
    ap.add_argument("--symbols", default="SPY,AAPL,MSFT")
    ap.add_argument("--feed", default="iex", choices=["iex", "sip", "delayed_sip", "test"])
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--vault-dir", default=str(DEFAULT_VAULT_DIR))
    a = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    clock = SystemClock()
    try:
        if secrets is None:
            import getpass
            secrets = open_vault_secrets(Path(a.vault_dir), getpass.getpass)
        status = AlpacaMarketStatus(AlpacaReadOnlyClient(HttpsGetTransport(PAPER_TRADING_HOST, ALLOWED_HOSTS),
                                                         secrets, CREDENTIAL, clock), clock)
        out(f"market: {status.venue_status(clock.now()).value}"
            + (f" ({status.last_problem})" if status.last_problem else ""))
        if a.feed != "test":
            for s in symbols:
                out(f"{s}: {status.instrument_status(s).value}")
        registry = InstrumentRegistry([InstrumentSpec(symbol=s, asset_class="EQUITY") for s in symbols], "check")
        adapter = StreamingQuoteAdapter("alpaca", AlpacaStreamTransport(a.feed), AlpacaQuoteDecoder("alpaca", registry),
                                        symbols, clock, QuoteBook("alpaca", timedelta(seconds=30), timedelta(seconds=5)),
                                        secrets=secrets, credential_name=CREDENTIAL,
                                        heartbeat_timeout=timedelta(seconds=60))
        deadline, quotes = time.monotonic() + a.seconds, 0
        while time.monotonic() < deadline:
            batch = adapter.poll()
            for i in batch.issues:
                out(f"issue: {i.code.value} {i.detail}")
            for r in batch.records:
                quotes += 1
                if quotes <= 20:
                    out(f"{r.instrument}: bid {r.bid} x {r.bid_size}  ask {r.ask} x {r.ask_size}  at {r.source_ts}")
            time.sleep(0.2)
        adapter._close_quietly()
        out(f"stream: {adapter.health().state.value}, {quotes} quotes in {a.seconds:.0f} s")
        return 0 if quotes or a.feed == "test" else 1
    except TradingAgentError as exc:
        out(f"refused: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
