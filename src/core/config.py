from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator


class SystemConfig(BaseModel):
    name: str = "Auriferous"
    mode: Literal["paper", "live"] = "paper"
    log_level: str = "INFO"
    base_currency: str = "USD"


class CapitalConfig(BaseModel):
    initial_usd: float = Field(default=2450.0, gt=0)
    option_bucket_pct: float = Field(default=0.65, gt=0, le=1.0)
    futures_bucket_pct: float = Field(default=0.25, ge=0, le=1.0)
    cash_buffer_pct: float = Field(default=0.10, ge=0, lt=1.0)

    @model_validator(mode="after")
    def validate_buckets(self) -> "CapitalConfig":
        total = self.option_bucket_pct + self.futures_bucket_pct + self.cash_buffer_pct
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"capital buckets must sum to 1.0, got {total:.4f}"
            )
        return self

    @property
    def option_bucket_usd(self) -> float:
        return self.initial_usd * self.option_bucket_pct

    @property
    def futures_bucket_usd(self) -> float:
        return self.initial_usd * self.futures_bucket_pct


class BrokerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=4002, ge=1, le=65535)
    client_id: int = Field(default=10, ge=1)
    timeout_seconds: int = Field(default=30, ge=5)
    readonly: bool = False
    market_data_type: Literal[1, 2, 3, 4] = 1


class DatabaseConfig(BaseModel):
    type: Literal["postgresql", "sqlite"] = "postgresql"
    host: str = "localhost"
    port: int = 5432
    name: str = "auriferous"
    user: Optional[str] = None
    password: Optional[str] = None

    def url(self) -> str:
        if self.type == "sqlite":
            return f"sqlite+aiosqlite:///{self.name}.db"
        user = self.user or os.getenv("DB_USER", "postgres")
        password = self.password or os.getenv("DB_PASSWORD", "")
        credentials = f"{user}:{password}" if password else user
        return f"postgresql+asyncpg://{credentials}@{self.host}:{self.port}/{self.name}"


class LLMConfig(BaseModel):
    provider: str = "openai"
    model: str = "gpt-5"
    fast_model: str = "gpt-5-mini"
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=256)
    timeout_seconds: int = Field(default=300, ge=10)
    api_key: Optional[str] = None
    cost_per_1m_input: float = Field(default=1.25, ge=0)
    cost_per_1m_output: float = Field(default=10.0, ge=0)


class SentinelConfig(BaseModel):
    poll_seconds: int = Field(default=30, ge=5)
    volume_anomaly_ratio: float = Field(default=4.0, gt=1.0)
    volume_anomaly_atr_mult: float = Field(default=1.5, gt=0)
    edgar_items: list[str] = Field(
        default_factory=lambda: ["1.01", "1.03", "2.02", "4.01", "4.02", "5.02", "7.01", "8.01"]
    )
    halt_codes: list[str] = Field(default_factory=lambda: ["T1", "T12", "LUDP"])
    pdufa_window_days: tuple[int, int] = (-5, 2)
    contact_email: str = ""


class JobsConfig(BaseModel):
    sentinel_seconds: int = Field(default=30, ge=10)
    triage_seconds: int = Field(default=60, ge=15)
    swarm_seconds: int = Field(default=120, ge=30)
    structurer_seconds: int = Field(default=180, ge=30)
    governor_seconds: int = Field(default=120, ge=30)
    executor_seconds: int = Field(default=60, ge=15)
    position_seconds: int = Field(default=60, ge=15)
    reconcile_seconds: int = Field(default=300, ge=60)
    shadow_seconds: int = Field(default=300, ge=60)
    calibration_seconds: int = Field(default=86400, ge=3600)
    watchdog_seconds: int = Field(default=300, ge=60)
    pdufa_refresh_seconds: int = Field(default=604800, ge=3600)
    universe_refresh_seconds: int = Field(default=604800, ge=3600)
    earnings_refresh_seconds: int = Field(default=86400, ge=3600)
    pdufa_lookback_days: int = Field(default=180, ge=30)
    universe_refresh_enabled: bool = False


class TriageConfig(BaseModel):
    min_expected_move_pct: float = Field(default=8.0, gt=0)
    max_per_day: int = Field(default=400, ge=1)
    max_per_hour: int = Field(default=60, ge=1)


class SwarmConfig(BaseModel):
    redteam_kill_threshold: float = Field(default=0.70, gt=0, le=1.0)
    pricedin_veto_threshold: float = Field(default=0.65, gt=0, le=1.0)
    min_conviction: float = Field(default=0.55, gt=0, le=1.0)
    max_per_day: int = Field(default=25, ge=1)
    max_cost_usd_per_day: float = Field(default=8.0, gt=0)
    fetch_filing_text: bool = True


class StructurerConfig(BaseModel):
    max_spread_pct: float = Field(default=0.12, gt=0, lt=1.0)
    min_open_interest: int = Field(default=250, ge=0)
    min_volume: int = Field(default=25, ge=0)
    min_days_to_expiry: int = Field(default=14, ge=1)
    event_expiry_buffer_days: int = Field(default=7, ge=0)
    target_delta_min: float = Field(default=0.35, gt=0, lt=1.0)
    target_delta_max: float = Field(default=0.45, gt=0, lt=1.0)
    spread_long_delta: float = Field(default=0.45, gt=0, lt=1.0)
    spread_short_delta: float = Field(default=0.20, gt=0, lt=1.0)
    iv_rank_spread_threshold: float = Field(default=60.0, ge=0, le=100)
    iv_rank_skip_threshold: float = Field(default=80.0, ge=0, le=100)
    max_premium_pct_of_equity: float = Field(default=0.10, gt=0, le=1.0)
    max_stock_price: float = Field(default=200.0, gt=0)
    binary_event_max_horizon_days: int = Field(default=21, ge=1)
    stock_max_price_for_direct: float = Field(default=60.0, gt=0)
    min_conviction_for_long_premium: float = Field(default=0.60, gt=0, le=1.0)

    @model_validator(mode="after")
    def validate_deltas(self) -> "StructurerConfig":
        if self.target_delta_min >= self.target_delta_max:
            raise ValueError("target_delta_min must be below target_delta_max")
        if self.iv_rank_spread_threshold >= self.iv_rank_skip_threshold:
            raise ValueError("iv_rank_spread_threshold must be below iv_rank_skip_threshold")
        return self


class RiskConfig(BaseModel):
    kelly_fraction: float = Field(default=0.25, gt=0, le=1.0)
    fallback_hit_rate: float = Field(default=0.35, gt=0, lt=1.0)
    min_calibration_n: int = Field(default=15, ge=1)
    max_position_pct: float = Field(default=0.10, gt=0, le=1.0)
    max_total_premium_pct: float = Field(default=0.35, gt=0, le=1.0)
    max_concurrent_options: int = Field(default=6, ge=1)
    max_concurrent_futures: int = Field(default=2, ge=0)
    max_same_catalyst_type: int = Field(default=2, ge=1)
    max_same_sector: int = Field(default=2, ge=1)
    drawdown_caution: float = Field(default=0.10, gt=0, lt=1.0)
    drawdown_defensive: float = Field(default=0.20, gt=0, lt=1.0)
    drawdown_halt: float = Field(default=0.30, gt=0, lt=1.0)

    @model_validator(mode="after")
    def validate_drawdown_ladder(self) -> "RiskConfig":
        if not (self.drawdown_caution < self.drawdown_defensive < self.drawdown_halt):
            raise ValueError(
                "drawdown thresholds must increase: caution < defensive < halt"
            )
        return self


class PositionsConfig(BaseModel):
    option_stop_premium_pct: float = Field(default=-0.60, lt=0, ge=-1.0)
    scale_out_at_gain_pct: float = Field(default=1.00, gt=0)
    scale_out_fraction: float = Field(default=0.50, gt=0, lt=1.0)
    theta_exit_days: int = Field(default=7, ge=0)
    futures_atr_stop_mult: float = Field(default=2.5, gt=0)
    bff_roll_hour_utc: int = Field(default=20, ge=0, le=23)
    bff_roll_weekday: int = Field(default=3, ge=0, le=6)


class ShadowConfig(BaseModel):
    enabled: bool = True
    parallel_book_enabled: bool = True


class AlertsConfig(BaseModel):
    enabled: bool = True
    webhook_url: Optional[str] = None


class UniverseConfig(BaseModel):
    min_market_cap: float = Field(default=300_000_000.0, ge=0)
    max_market_cap: float = Field(default=10_000_000_000.0, gt=0)
    min_dollar_volume: float = Field(default=3_000_000.0, ge=0)
    min_price: float = Field(default=3.0, gt=0)
    max_price: float = Field(default=200.0, gt=0)
    min_total_open_interest: int = Field(default=1000, ge=0)
    refresh_days: int = Field(default=7, ge=1)


class AuriferousConfig(BaseModel):
    system: SystemConfig = Field(default_factory=SystemConfig)
    capital: CapitalConfig = Field(default_factory=CapitalConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sentinel: SentinelConfig = Field(default_factory=SentinelConfig)
    jobs: JobsConfig = Field(default_factory=JobsConfig)
    triage: TriageConfig = Field(default_factory=TriageConfig)
    swarm: SwarmConfig = Field(default_factory=SwarmConfig)
    structurer: StructurerConfig = Field(default_factory=StructurerConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    positions: PositionsConfig = Field(default_factory=PositionsConfig)
    shadow: ShadowConfig = Field(default_factory=ShadowConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)

    @model_validator(mode="after")
    def validate_live_mode(self) -> "AuriferousConfig":
        if self.system.mode == "live":
            if not self.llm.api_key:
                raise ValueError("LLM API key is required in live mode")
            if self.broker.market_data_type != 1:
                raise ValueError("live mode requires real-time market data (type 1)")
        return self


class ConfigLoader:
    _instance: Optional[AuriferousConfig] = None
    _config_path: Optional[Path] = None

    @classmethod
    def load(
        cls,
        config_path: str | Path = "config/auriferous.yaml",
        env_path: str | Path | None = "config/.env",
        reload: bool = False,
    ) -> AuriferousConfig:
        if cls._instance is not None and not reload:
            return cls._instance

        config_path = Path(config_path)
        if not config_path.is_absolute():
            config_path = Path.cwd() / config_path

        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        if env_path:
            env_path = Path(env_path)
            if not env_path.is_absolute():
                env_path = Path.cwd() / env_path
            if env_path.exists():
                load_dotenv(env_path)

        with open(config_path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        cls._apply_env_overrides(raw)

        cls._instance = AuriferousConfig(**raw)
        cls._config_path = config_path
        return cls._instance

    @classmethod
    def get(cls) -> AuriferousConfig:
        if cls._instance is None:
            raise RuntimeError("Configuration not loaded — call ConfigLoader.load() first")
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None
        cls._config_path = None

    @staticmethod
    def _apply_env_overrides(raw: dict) -> None:
        llm = raw.setdefault("llm", {})
        if not llm.get("api_key"):
            llm["api_key"] = os.getenv("OPENAI_API_KEY")

        database = raw.setdefault("database", {})
        if not database.get("user"):
            database["user"] = os.getenv("DB_USER")
        if not database.get("password"):
            database["password"] = os.getenv("DB_PASSWORD")

        alerts = raw.setdefault("alerts", {})
        if not alerts.get("webhook_url"):
            alerts["webhook_url"] = os.getenv("ALERT_WEBHOOK_URL")
