from __future__ import annotations

import asyncio
from typing import Any, Type, TypeVar

import structlog
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

from src.swarm.evidence import EvidenceBundle
from src.swarm.models import AgentCost, PricedInVerdict, RedTeamVerdict, Thesis

T = TypeVar("T", bound=BaseModel)

logger = structlog.get_logger("SwarmAgents")

SHARED_RULES = """You are analysing a small or mid cap US stock following a specific catalyst. A separate system will size and execute any position; your job is judgement, not execution.

Rules that apply to you:
- Use only the evidence provided. If a figure is not in the evidence, do not state it. Saying "not disclosed in the material provided" is correct and expected.
- Never invent revenue numbers, trial results, contract values, or dates.
- Small caps move on mechanism, not sentiment. Say what changes about the business or the flow of orders.
- Be concrete about magnitude. "Significant upside" is useless; "roughly 20% because the contract is a fifth of revenue" is useful."""

BULL_SYSTEM = SHARED_RULES + """

You argue the LONG side. Build the strongest honest case that this stock rises from here.

You are not required to believe it — you are required to construct the best case the evidence supports, and to state your genuine confidence in it. If the evidence is thin, say so with a low confidence rather than dressing up a weak case."""

BEAR_SYSTEM = SHARED_RULES + """

You argue the SHORT side. Build the strongest honest case that this stock falls from here.

Pay particular attention to dilution mechanics, cash burn, competitive response, and whether the catalyst is smaller than it appears. If the evidence genuinely does not support a bearish case, say so with a low confidence rather than manufacturing one."""

REDTEAM_SYSTEM = """You are the red team on a proposed trade. You are not a balanced analyst and you are not here to weigh both sides.

Your only job is to find the reason this trade loses money, and to state it in its strongest form.

Do not balance arguments. Do not write "on the other hand". Do not produce a measured assessment. Attack the stated key assumption directly.

Look specifically for:
- whether the catalyst changes fundamental value or only the narrative
- dilution mechanics: at-the-market offerings, convertibles, lock-up expiries, shelf registrations
- whether similar events at this company previously failed to move the stock, or reversed
- whether liquidity would even allow an exit at the assumed price
- whether the thesis rests on data that may be stale, misread, or already public
- whether the move has already happened while the system was still deciding

If after genuine effort you cannot find a strong counterargument, say so plainly and set kill_confidence low. Manufacturing an objection is worse than reporting that none exists — a fabricated kill costs real money by blocking a good trade."""

PRICEDIN_SYSTEM = """You judge one question only: how much of this catalyst is already reflected in the price?

This is the most common way a catalyst trade fails. The information is real, the thesis is correct, and the move already happened before the position was opened.

You are given deterministic measurements. Weigh them:
- A large move today on heavy volume means the market has seen this.
- A gap at the open means it was digested before the session started.
- A move in extended hours means the same, and is easy to miss.
- Elevated short interest means a squeeze can extend a move beyond fair value, but also that the bearish case is widely held.
- A stock already at the top of its 52-week range has less room than one at the bottom, for the same news.

Set priced_in_score at 0 only when the market has genuinely not reacted, and at 1 when the move is complete. Most real cases sit between 0.3 and 0.8.

remaining_move_pct is the number the position sizing depends on. Be conservative: it is the move still available from the current price, not the total move of the catalyst."""

EVIDENCE_BLOCK = """{evidence}

---
Produce your assessment."""

REDTEAM_BLOCK = """{evidence}

---
BULL CASE UNDER REVIEW
- Core argument: {bull_argument}
- Key assumption: {bull_assumption}
- Confidence: {bull_confidence}

BEAR CASE UNDER REVIEW
- Core argument: {bear_argument}
- Key assumption: {bear_assumption}
- Confidence: {bear_confidence}

Attack the side that currently wins. Find the reason this trade loses."""

PRICEDIN_BLOCK = """{evidence}

---
DETERMINISTIC MEASUREMENTS
{measurements}

How much of this is already in the price?"""


def _usage(raw: Any, cost_in: float, cost_out: float) -> AgentCost:
    metadata = getattr(raw, "usage_metadata", None) or {}
    input_tokens = int(metadata.get("input_tokens") or 0)
    output_tokens = int(metadata.get("output_tokens") or 0)

    return AgentCost(
        calls=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usd=(input_tokens / 1_000_000 * cost_in) + (output_tokens / 1_000_000 * cost_out),
    )


class SwarmAgents:

    def __init__(
        self,
        llm: Any,
        cost_per_1m_input: float,
        cost_per_1m_output: float,
    ) -> None:
        self._llm = llm
        self._cost_in = cost_per_1m_input
        self._cost_out = cost_per_1m_output
        self._logger = structlog.get_logger("SwarmAgents")

    async def _invoke(
        self,
        system: str,
        human: str,
        schema: Type[T],
        payload: dict[str, Any],
    ) -> tuple[T, AgentCost]:
        prompt = ChatPromptTemplate.from_messages([("system", system), ("human", human)])
        chain = prompt | self._llm.with_structured_output(schema, include_raw=True)

        response = await chain.ainvoke(payload)

        if isinstance(response, dict):
            parsed = response.get("parsed")
            error = response.get("parsing_error")
            if parsed is None:
                raise ValueError(f"structured output failed for {schema.__name__}: {error}")
            return parsed, _usage(response.get("raw"), self._cost_in, self._cost_out)

        return response, AgentCost(calls=1)

    async def bull(self, evidence: EvidenceBundle) -> tuple[Thesis, AgentCost]:
        thesis, cost = await self._invoke(
            BULL_SYSTEM, EVIDENCE_BLOCK, Thesis, {"evidence": evidence.render()}
        )
        thesis.stance = "BULL"
        return thesis, cost

    async def bear(self, evidence: EvidenceBundle) -> tuple[Thesis, AgentCost]:
        thesis, cost = await self._invoke(
            BEAR_SYSTEM, EVIDENCE_BLOCK, Thesis, {"evidence": evidence.render()}
        )
        thesis.stance = "BEAR"
        return thesis, cost

    async def redteam(
        self,
        evidence: EvidenceBundle,
        bull: Thesis,
        bear: Thesis,
    ) -> tuple[RedTeamVerdict, AgentCost]:
        return await self._invoke(
            REDTEAM_SYSTEM,
            REDTEAM_BLOCK,
            RedTeamVerdict,
            {
                "evidence": evidence.render(),
                "bull_argument": bull.core_argument,
                "bull_assumption": bull.key_assumption,
                "bull_confidence": f"{bull.confidence:.2f}",
                "bear_argument": bear.core_argument,
                "bear_assumption": bear.key_assumption,
                "bear_confidence": f"{bear.confidence:.2f}",
            },
        )

    async def pricedin(self, evidence: EvidenceBundle) -> tuple[PricedInVerdict, AgentCost]:
        return await self._invoke(
            PRICEDIN_SYSTEM,
            PRICEDIN_BLOCK,
            PricedInVerdict,
            {
                "evidence": evidence.render(),
                "measurements": render_measurements(evidence.priced_in_inputs()),
            },
        )

    async def run_all(
        self,
        evidence: EvidenceBundle,
    ) -> tuple[Thesis, Thesis, RedTeamVerdict, PricedInVerdict, AgentCost]:
        (bull, bull_cost), (bear, bear_cost), (pricedin, priced_cost) = await asyncio.gather(
            self.bull(evidence),
            self.bear(evidence),
            self.pricedin(evidence),
        )

        redteam, red_cost = await self.redteam(evidence, bull, bear)

        total = AgentCost()
        for item in (bull_cost, bear_cost, priced_cost, red_cost):
            total.merge(item)

        return bull, bear, redteam, pricedin, total


def render_measurements(values: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in values.items():
        label = key.replace("_", " ")
        if value is None:
            lines.append(f"- {label}: not available")
        elif isinstance(value, float):
            lines.append(f"- {label}: {value:+.2f}")
        else:
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)
