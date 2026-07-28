from __future__ import annotations

from typing import Any

import structlog
from langchain_core.prompts import ChatPromptTemplate

from src.database.models import Event
from src.triage.models import MarketContext, TriageResult, normalize_catalyst_type

SYSTEM_MESSAGE = """You are a triage filter for a catalyst-driven trading system. You are not an analyst — you are a gate whose job is to throw almost everything away.

Roughly ninety-five percent of the events you see are noise. Routine filings, boilerplate disclosures, scheduled announcements, minor personnel changes, and ordinary business updates do not move a stock meaningfully. Your default answer is not actionable.

The bar for passing: could this specific event plausibly move THIS specific stock by the required threshold within days, given its size and what the market already knows?

Judge on mechanism, not on how important the news sounds:
- A contract worth 1% of revenue does not move a stock. A contract worth 30% does.
- A CEO leaving a struggling company is noise. A CEO leaving after an accounting restatement is not.
- An FDA decision on a company's only drug is enormous. On its fifth is not.
- Guidance that confirms consensus is noise. Guidance that breaks it is not.
- Dilution matters in inverse proportion to market cap.

Weigh the market context you are given. If the stock has already moved sharply on heavy volume, or moved in extended hours, the information is likely priced in and the remaining move is small — say so in expected_move_pct.

Be honest about magnitude. Most events move a stock less than three percent. Inflating expected_move_pct to push an event through wastes money on analysis that will be rejected downstream, and corrupts the calibration that sizes real positions.

Set direction to UNCLEAR only when the sign is genuinely indeterminate, not when you are merely uncertain about magnitude. UNCLEAR is a rejection, so use it honestly rather than defensively.

Keep reasoning to three sentences at most. State the mechanism by which the price would move."""

HUMAN_MESSAGE = """EVENT
- Source: {source}
- Ticker: {ticker}
- Detected: {detected_at}
- Deterministic priority: {priority} (1 = most severe)
- Preliminary direction from the filing type: {preliminary_direction}

EVENT DETAILS
{payload}

MARKET CONTEXT
{context}

Decide whether this warrants full analysis."""


class TriageAgent:

    def __init__(self, llm: Any) -> None:
        self._llm = llm
        self._logger = structlog.get_logger("TriageAgent")
        self._prompt = ChatPromptTemplate.from_messages([
            ("system", SYSTEM_MESSAGE),
            ("human", HUMAN_MESSAGE),
        ])
        self._chain = self._prompt | self._llm.with_structured_output(TriageResult)

    async def evaluate(self, event: Event, context: MarketContext) -> TriageResult:
        result: TriageResult = await self._chain.ainvoke({
            "source": event.source,
            "ticker": event.ticker,
            "detected_at": event.detected_at.isoformat() if event.detected_at else "unknown",
            "priority": event.priority,
            "preliminary_direction": event.direction or "UNCLEAR",
            "payload": render_payload(event.payload),
            "context": context.render(),
        })

        result.catalyst_type = normalize_catalyst_type(result.catalyst_type)
        result.direction = (result.direction or "UNCLEAR").strip().upper()
        result.expected_move_pct = abs(result.expected_move_pct)
        return result


def render_payload(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "(no structured details)"

    lines: list[str] = []
    for key, value in payload.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        text = str(value)
        if len(text) > 600:
            text = text[:600] + " […]"
        lines.append(f"- {key}: {text}")

    return "\n".join(lines) if lines else "(no structured details)"
