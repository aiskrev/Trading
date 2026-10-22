"""Run a planner prompt against the frozen eval requests with the REAL model. Run it yourself, in your own terminal.

  python scripts/run_model_eval.py trading_planner.v1 --split development --limit 3     # smoke test, a few cents
  python scripts/run_model_eval.py trading_planner.v1 --split development --all
  python scripts/run_model_eval.py trading_planner.v2-candidate --split validation --all --baseline trading_planner.v1

This SPENDS MONEY: it prints an upper bound first and asks you to type YES. It needs the Anthropic key in the vault:

  python scripts/vault.py put-secret anthropic-api-key          (hidden prompt)
  python scripts/vault.py grant model-client --read anthropic-api-key      (prints a token ONCE)
  export TRADING_VAULT_TOKEN=<that token>                       (for this shell only)

The vault passphrase is read from a hidden prompt, never from arguments or the environment. The key goes from the vault
into the SDK client and nowhere else. The holdout split is refused here: it has its own sealed procedure.
Outputs already recorded for a request are skipped (recordings are immutable), so a stopped run can be resumed.
"""
import argparse
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / ".deps")]

from evals.planner_quality import run as ev  # noqa: E402
from evals.planner_quality import scenarios as sc  # noqa: E402
from trading_agent.core.errors import TradingAgentError  # noqa: E402
from trading_agent.model_client.anthropic_transport import API_KEY_NAME, AnthropicMessagesTransport  # noqa: E402
from trading_agent.model_client.client import RetryPolicy, SdkLLMClient  # noqa: E402
from trading_agent.secret_store.vault import Vault, VaultSecretsProvider  # noqa: E402

MODEL = "claude-sonnet-5-5"
PRICE_IN_PER_M, PRICE_OUT_PER_M = 2.0, 10.0       # Sonnet 5.5, USD per million tokens (cached 2026-10-06)
CHARS_PER_TOKEN = 3.0                             # conservative: JSON is token-dense
MAX_TOKENS = 6000
DEFAULT_VAULT_DIR = Path.home() / ".trading-agent"


def open_vault_secrets(vault_dir: Path, prompt: Callable[[str], str], env=os.environ):
    token = env.get("TRADING_VAULT_TOKEN")
    if not token:
        raise TradingAgentError("TRADING_VAULT_TOKEN is not set (see the usage text: vault.py grant ...)")
    vault = Vault.open(vault_dir / "trading-agent.vault", prompt("Vault passphrase: "),
                       vault_dir / "trading-agent.vault.access.jsonl")
    return VaultSecretsProvider(vault.as_client(token))


def upper_bound_usd(requests: List[Dict[str, Any]], max_tokens: int) -> float:
    tokens_in = sum(len(json.dumps(r["payload"], sort_keys=True)) + len(r["system"]) for r in requests) / CHARS_PER_TOKEN
    return (tokens_in * PRICE_IN_PER_M + len(requests) * max_tokens * PRICE_OUT_PER_M) / 1e6


def main(argv: Optional[List[str]] = None, prompt: Callable[[str], str] = getpass.getpass,
         confirm: Callable[[str], str] = input, out: Callable[[str], None] = print,
         secrets_factory: Optional[Callable[[], Any]] = None, sdk: Optional[Any] = None,
         out_dir: Path = ROOT / "runs" / "model_eval") -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("label", help="prompt label, e.g. trading_planner.v1 or trading_planner.v2-candidate")
    ap.add_argument("--split", choices=["development", "validation"], default="development")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--limit", type=int, help="only the first N scenarios (smoke test)")
    group.add_argument("--all", action="store_true")
    ap.add_argument("--baseline", help="prompt label to compare against, on the same split")
    ap.add_argument("--effort", choices=["low", "medium", "high"], default="medium")
    ap.add_argument("--vault-dir", default=str(DEFAULT_VAULT_DIR))
    a = ap.parse_args(argv)

    spec = ev.prompt_for(a.label)
    recorded = ev.recorded_outputs(a.label, a.split)
    scenarios = [s for s in sc.load(a.split) if recorded.get(s["scenario_id"]) is None]
    if a.limit is not None:
        scenarios = scenarios[:max(a.limit, 0)]
    requests = [{"scenario_id": s["scenario_id"], "payload": s["payload"], "system": spec.text} for s in scenarios]
    if not requests:
        out("nothing to do: every selected request already has a recorded output")
    else:
        bound = upper_bound_usd(requests, MAX_TOKENS)
        out(f"{len(requests)} requests, prompt {a.label} (sha256 {spec.sha256[:12]}), model {MODEL}, effort {a.effort}, "
            f"split {a.split} of {sc.SET_VERSION}.")
        out(f"Upper bound on cost: about ${bound:.2f} (every call using its full {MAX_TOKENS}-token output budget; "
            f"the real cost is usually far lower).")
        if confirm("Type YES to spend it: ").strip() != "YES":
            out("cancelled, nothing was sent")
            return 1

        try:
            secrets = secrets_factory() if secrets_factory else open_vault_secrets(Path(a.vault_dir), prompt)
        except TradingAgentError as exc:
            out(f"error: {exc}")
            return 1
        client = SdkLLMClient(AnthropicMessagesTransport(secrets, API_KEY_NAME, sdk=sdk), MODEL,
                              max_tokens=MAX_TOKENS, effort=a.effort, retry=RetryPolicy(total_deadline_s=60,
                                                                                         attempt_timeout_s=40))
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        responses, failures = out_dir / f"{a.label}.{a.split}.{stamp}.responses.jsonl", []
        with responses.open("w", encoding="utf-8") as fh:
            for i, r in enumerate(requests, 1):
                try:
                    text = client.complete(spec.text, spec.prompt_id, spec.version, r["payload"])
                except TradingAgentError as exc:
                    failures.append({"scenario_id": r["scenario_id"], "error": f"{type(exc).__name__}: {exc}"[:200]})
                    out(f"[{i}/{len(requests)}] {r['scenario_id']}: no usable output ({type(exc).__name__})")
                    continue
                fh.write(json.dumps({"request_key": ev._key(spec, r["payload"]), "scenario_id": r["scenario_id"],
                                     "output": text}, sort_keys=True) + "\n")
                fh.flush()
                out(f"[{i}/{len(requests)}] {r['scenario_id']}: ok")
        if failures:
            (out_dir / f"{a.label}.{a.split}.{stamp}.failures.json").write_text(json.dumps(failures, indent=1), "utf-8")
        imported = ev.import_outputs(a.label, responses)
        _provenance(a, imported, len(failures), stamp)
        out(f"imported {imported} outputs; {len(failures)} calls produced no usable output (they count as NOT_RUN)")

    report = ev.run_label(a.label, a.split)
    comparison = ev.compare(report, ev.run_label(a.baseline, a.split)) if a.baseline else None
    ev.write_report(report, comparison)
    out(ev.render(report, comparison))
    return 0


def _provenance(a, imported: int, failed: int, stamp: str) -> None:
    """Who produced the recordings: model, effort and when, so a later reader can tell runs apart."""
    path = ev.RECORDINGS / f"{a.label}.provenance.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": stamp, "label": a.label, "split": a.split, "set_version": sc.SET_VERSION,
                             "model": MODEL, "effort": a.effort, "max_tokens": MAX_TOKENS, "imported": imported,
                             "no_output": failed}, sort_keys=True) + "\n")


if __name__ == "__main__":
    sys.exit(main())
