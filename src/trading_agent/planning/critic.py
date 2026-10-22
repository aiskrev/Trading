from typing import Any, Dict

from trading_agent.contracts.account import AccountSnapshot
from trading_agent.contracts.critic import CriticBody, CriticVerdict, CriticVerdictKind
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import OrderProposal
from trading_agent.core.errors import LLMUnavailable, SchemaFailure
from trading_agent.planning.output_parser import parse_strict
from trading_agent.planning.ports import LLMClient
from trading_agent.planning.prompts import PromptSpec


class ClaudeCritic:
    """Independent LLM review. Advisory only: it can tighten (REDUCE/DENY) but the gateway decides.

    Any critic failure is a DENY (fail closed).
    """

    def __init__(self, llm: LLMClient, prompt: PromptSpec) -> None:
        self.llm = llm
        self.prompt = prompt

    def review(self, proposal: OrderProposal, market: MarketSnapshot, account: AccountSnapshot,
               risk_policy_summary: Dict[str, Any]) -> CriticVerdict:
        payload = {
            "order_proposal": proposal.model_dump(mode="json"),
            "market_snapshot": market.model_dump(mode="json"),
            "account_snapshot": account.model_dump(mode="json"),
            "risk_policy_summary": risk_policy_summary,
            "output_schema": CriticBody.model_json_schema(),
        }
        try:
            body = parse_strict(self.llm.complete(self.prompt.text, self.prompt.prompt_id,
                                                  self.prompt.version, payload), CriticBody)
        except LLMUnavailable:
            body = CriticBody(verdict=CriticVerdictKind.DENY, reason_codes=["CRITIC_UNAVAILABLE"])
        except SchemaFailure:
            body = CriticBody(verdict=CriticVerdictKind.DENY, reason_codes=["CRITIC_SCHEMA_FAILURE"])
        except Exception:
            body = CriticBody(verdict=CriticVerdictKind.DENY, reason_codes=["CRITIC_FAILURE"])
        return CriticVerdict(**body.model_dump(), proposal_id=proposal.proposal_id,
                             model_version=self.llm.model_version, prompt_version=self.prompt.qualified_version)
