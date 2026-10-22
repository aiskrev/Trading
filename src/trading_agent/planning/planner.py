from typing import Any, Dict, List, NamedTuple, Optional

from trading_agent.contracts.account import AccountSnapshot
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import Decision, OrderProposal, ProposalBody
from trading_agent.core.errors import LLMUnavailable, SchemaFailure
from trading_agent.core.types import IdFactory
from trading_agent.planning.output_parser import parse_strict
from trading_agent.planning.ports import LLMClient
from trading_agent.planning.prompts import PromptSpec


class PlanResult(NamedTuple):
    proposal: OrderProposal
    failure: Optional[str]  # None, LLM_UNAVAILABLE, SCHEMA_FAILURE, MODEL_FAILURE
    raw_output: Optional[str] = None  # exact model text, kept for the DecisionPacket


class ClaudePlanner:
    """Asks the model for a ProposalBody; any failure becomes NO_TRADE (fail closed).

    Attribution fields are filled by code, so the model cannot misreport versions or snapshots.
    """

    def __init__(self, llm: LLMClient, prompt: PromptSpec, strategy_version: str,
                 strategy_rules: Dict[str, Any], allowed_universe: List[str], ids: IdFactory) -> None:
        self.llm = llm
        self.prompt = prompt
        self.strategy_version = strategy_version
        self.strategy_rules = strategy_rules
        self.allowed_universe = sorted(allowed_universe)
        self.ids = ids

    def build_payload(self, market: MarketSnapshot, account: AccountSnapshot, features: FeatureSet,
                      active_triggers: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        payload = {
            "strategy_version": self.strategy_version,
            "strategy_rules": self.strategy_rules,
            "allowed_universe": self.allowed_universe,
            "market_snapshot": market.model_dump(mode="json"),
            "account_snapshot": account.model_dump(mode="json"),
            "features": features.model_dump(mode="json"),
            "valid_evidence_refs": sorted(market.evidence_refs() | account.evidence_refs() | features.evidence_refs()),
            "output_schema": ProposalBody.model_json_schema(),
        }
        if active_triggers is not None:  # deterministic triggers that caused this evaluation
            payload["active_triggers"] = active_triggers
        return payload

    def plan(self, market: MarketSnapshot, account: AccountSnapshot, features: FeatureSet,
             active_triggers: Optional[List[Dict[str, str]]] = None) -> PlanResult:
        failure: Optional[str] = None
        raw: Optional[str] = None
        try:
            raw = self.llm.complete(self.prompt.text, self.prompt.prompt_id, self.prompt.version,
                                    self.build_payload(market, account, features, active_triggers))
            body = parse_strict(raw, ProposalBody)
        except LLMUnavailable:
            failure = "LLM_UNAVAILABLE"
        except SchemaFailure:
            failure = "SCHEMA_FAILURE"
        except Exception:  # any model-side surprise fails closed
            failure = "MODEL_FAILURE"
        if failure:
            body = ProposalBody(decision=Decision.NO_TRADE, public_rationale=f"Fail-closed NO_TRADE: {failure}.",
                                reason_code=failure)
        return PlanResult(self._wrap(body, market, account, self.llm.model_version), failure,
                          raw if isinstance(raw, str) else None)

    def no_trade(self, market: MarketSnapshot, account: AccountSnapshot, reason_code: str, detail: str) -> OrderProposal:
        """Deterministic NO_TRADE without consulting the model (a prerequisite failed or no trigger fired)."""
        body = ProposalBody(decision=Decision.NO_TRADE, reason_code=reason_code,
                            public_rationale=f"NO_TRADE ({reason_code}): {detail}"[:1000])
        return self._wrap(body, market, account, "none:deterministic-gate")

    def _wrap(self, body: ProposalBody, market: MarketSnapshot, account: AccountSnapshot,
              model_version: str) -> OrderProposal:
        return OrderProposal(
            **body.model_dump(),
            proposal_id=self.ids.next("proposal"),
            market_snapshot_id=market.snapshot_id,
            account_snapshot_id=account.snapshot_id,
            model_version=model_version,
            prompt_version=self.prompt.qualified_version,
            prompt_sha256=self.prompt.sha256,
            strategy_version=self.strategy_version,
        )

