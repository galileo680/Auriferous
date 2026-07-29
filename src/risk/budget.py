from __future__ import annotations

MODE_LIVE = "live"
LLM_ANNUAL_COST_CAP_PCT = 0.15
DAYS_PER_YEAR = 365


def daily_llm_budget(equity: float, mode: str, paper_cap_usd: float) -> float:
    if mode != MODE_LIVE:
        return paper_cap_usd
    live_cap = equity * LLM_ANNUAL_COST_CAP_PCT / DAYS_PER_YEAR
    return round(max(live_cap, 0.0), 2)
