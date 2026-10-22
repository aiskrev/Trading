"""Generate /schemas/*.json from the pydantic contracts. `--check` fails on drift (AT-05)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / ".deps")]

from trading_agent.contracts.audit import AuditRecord  # noqa: E402
from trading_agent.contracts.critic import CriticBody, CriticVerdict  # noqa: E402
from trading_agent.contracts.execution import ApprovedOrder, BrokerAck, Fill  # noqa: E402
from trading_agent.contracts.market import MarketSnapshot  # noqa: E402
from trading_agent.contracts.account import AccountSnapshot  # noqa: E402
from trading_agent.contracts.proposal import OrderProposal, ProposalBody  # noqa: E402
from trading_agent.contracts.risk import RiskDecision  # noqa: E402
from trading_agent.contracts.data_quality import DataIssue  # noqa: E402
from trading_agent.market_data.records import QuoteRecord  # noqa: E402
from trading_agent.market_data.streaming import FeedHealth  # noqa: E402
from trading_agent.account_data.records import AccountRecord  # noqa: E402

SCHEMAS = {
    "market_snapshot.v1.json": MarketSnapshot,
    "account_snapshot.v1.json": AccountSnapshot,
    "proposal_body.v1.json": ProposalBody,
    "order_proposal.v1.json": OrderProposal,
    "critic_body.v1.json": CriticBody,
    "critic_verdict.v1.json": CriticVerdict,
    "risk_decision.v1.json": RiskDecision,
    "approved_order.v1.json": ApprovedOrder,
    "broker_ack.v1.json": BrokerAck,
    "fill.v1.json": Fill,
    "audit_record.v1.json": AuditRecord,
    "data_issue.v1.json": DataIssue,
    "quote_record.v1.json": QuoteRecord,
    "feed_health.v1.json": FeedHealth,
    "account_record.v1.json": AccountRecord,
}
OUT = ROOT / "schemas"


def render(model) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv) -> int:
    check = "--check" in argv
    drift = []
    OUT.mkdir(exist_ok=True)
    for name, model in SCHEMAS.items():
        path, text = OUT / name, render(model)
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                drift.append(name)
        else:
            path.write_bytes(text.encode("utf-8"))
    if drift:
        print("schema drift: " + ", ".join(drift) + " (run scripts/export_schemas.py)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
