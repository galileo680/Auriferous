from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


EVENT_STATUS_NEW = "NEW"
EVENT_STATUS_TRIAGED = "TRIAGED"
EVENT_STATUS_ANALYZED = "ANALYZED"
EVENT_STATUS_REJECTED = "REJECTED"
EVENT_STATUS_TRADED = "TRADED"

TRADE_STATUS_PENDING = "PENDING"
TRADE_STATUS_OPEN = "OPEN"
TRADE_STATUS_CLOSED = "CLOSED"
TRADE_STATUS_EXPIRED = "EXPIRED"

DRAWDOWN_NORMAL = "NORMAL"
DRAWDOWN_CAUTION = "CAUTION"
DRAWDOWN_DEFENSIVE = "DEFENSIVE"
DRAWDOWN_HALT = "HALT"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    catalyst_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    direction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    dedup_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    triage_result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EVENT_STATUS_NEW, index=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), index=True
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_events_status_priority", "status", "priority"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "ticker": self.ticker,
            "market": self.market,
            "catalyst_type": self.catalyst_type,
            "direction": self.direction,
            "payload": self.payload,
            "priority": self.priority,
            "dedup_key": self.dedup_key,
            "triage_result": self.triage_result,
            "status": self.status,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
        }

    def __repr__(self) -> str:
        return f"<Event(id={self.id}, {self.source}, {self.ticker}, {self.status})>"


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("events.id"), nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    bull_thesis: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    bear_thesis: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    redteam_verdict: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    pricedin_verdict: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    conviction: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4), nullable=True)
    expected_move_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    decision: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    veto_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_cost_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), index=True
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "ticker": self.ticker,
            "direction": self.direction,
            "conviction": float(self.conviction) if self.conviction is not None else None,
            "expected_move_pct": float(self.expected_move_pct) if self.expected_move_pct is not None else None,
            "decision": self.decision,
            "veto_reason": self.veto_reason,
            "llm_cost_usd": float(self.llm_cost_usd) if self.llm_cost_usd is not None else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:
        return f"<Analysis(id={self.id}, {self.ticker}, {self.decision})>"


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("analyses.id"), nullable=True, index=True
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    instrument: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    contract_spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    con_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    entry_filled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    capital_at_risk: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    kelly_fraction_used: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    invalidation: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    horizon_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TRADE_STATUS_PENDING, index=True
    )
    exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    exit_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    pnl_realized: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    broker_stop_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    drawdown_state_at_entry: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), index=True
    )

    __table_args__ = (
        Index("idx_trades_status_instrument", "status", "instrument"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "analysis_id": self.analysis_id,
            "ticker": self.ticker,
            "market": self.market,
            "instrument": self.instrument,
            "direction": self.direction,
            "contract_spec": self.contract_spec,
            "con_id": self.con_id,
            "quantity": self.quantity,
            "entry_price": float(self.entry_price) if self.entry_price is not None else None,
            "capital_at_risk": float(self.capital_at_risk),
            "kelly_fraction_used": float(self.kelly_fraction_used) if self.kelly_fraction_used is not None else None,
            "invalidation": self.invalidation,
            "horizon_days": self.horizon_days,
            "status": self.status,
            "exit_price": float(self.exit_price) if self.exit_price is not None else None,
            "exit_reason": self.exit_reason,
            "pnl_realized": float(self.pnl_realized) if self.pnl_realized is not None else None,
            "drawdown_state_at_entry": self.drawdown_state_at_entry,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
        }

    def __repr__(self) -> str:
        return f"<Trade(id={self.id}, {self.ticker}, {self.instrument}, {self.status})>"


class ShadowTrade(Base):
    __tablename__ = "shadow_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("analyses.id"), nullable=True, index=True
    )
    trade_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("trades.id"), nullable=True, index=True
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    book: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    origin: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    origin_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    catalyst_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True, index=True)
    conviction: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4), nullable=True)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    sl_level: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    tp_level: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    expected_holding_days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=func.now())
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    close_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), nullable=True)
    pnl_virtual: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    __table_args__ = (
        Index("idx_shadow_book_status", "book", "status"),
    )

    def __repr__(self) -> str:
        return f"<ShadowTrade(id={self.id}, {self.ticker}, {self.book}, {self.origin})>"


class CalibrationSnapshot(Base):
    __tablename__ = "calibration_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalyst_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    conviction_bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False)
    hit_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4), nullable=True)
    avg_win_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    avg_loss_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2), nullable=True)
    edge: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), nullable=True)
    as_of: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), index=True
    )

    __table_args__ = (
        Index("idx_calibration_lookup", "catalyst_type", "conviction_bucket", "as_of"),
    )

    def __repr__(self) -> str:
        return (
            f"<CalibrationSnapshot({self.catalyst_type}/{self.conviction_bucket}, "
            f"n={self.sample_size}, hit={self.hit_rate})>"
        )


class EquityCurve(Base):
    __tablename__ = "equity_curve"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    high_water_mark: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    drawdown_state: Mapped[str] = mapped_column(String(20), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    open_premium: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return (
            f"<EquityCurve(equity={self.equity}, dd={self.drawdown_pct}, "
            f"state={self.drawdown_state})>"
        )


class ErrorLog(Base):
    __tablename__ = "error_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    error_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=func.now(), index=True
    )
