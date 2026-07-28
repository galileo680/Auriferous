from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.config import (
    AuriferousConfig,
    CapitalConfig,
    ConfigLoader,
    RiskConfig,
    StructurerConfig,
    SystemConfig,
)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "auriferous.yaml"


def test_shipped_config_loads():
    ConfigLoader.reset()
    config = ConfigLoader.load(config_path=CONFIG_PATH, env_path=None, reload=True)
    assert config.system.name == "Auriferous"
    assert config.capital.initial_usd == 2450
    assert config.broker.client_id == 10
    assert config.database.name == "auriferous"
    ConfigLoader.reset()


def test_capital_buckets_must_sum_to_one():
    with pytest.raises(ValidationError):
        CapitalConfig(
            initial_usd=2450,
            option_bucket_pct=0.65,
            futures_bucket_pct=0.25,
            cash_buffer_pct=0.25,
        )


def test_capital_bucket_helpers():
    capital = CapitalConfig(initial_usd=2450)
    assert capital.option_bucket_usd == pytest.approx(1592.5)
    assert capital.futures_bucket_usd == pytest.approx(612.5)


def test_drawdown_ladder_must_increase():
    with pytest.raises(ValidationError):
        RiskConfig(drawdown_caution=0.25, drawdown_defensive=0.20, drawdown_halt=0.30)


def test_valid_drawdown_ladder_accepted():
    risk = RiskConfig(drawdown_caution=0.08, drawdown_defensive=0.16, drawdown_halt=0.25)
    assert risk.drawdown_halt == 0.25


def test_kelly_fraction_cannot_exceed_full_kelly():
    with pytest.raises(ValidationError):
        RiskConfig(kelly_fraction=1.5)


def test_delta_band_must_be_ordered():
    with pytest.raises(ValidationError):
        StructurerConfig(target_delta_min=0.50, target_delta_max=0.35)


def test_iv_rank_thresholds_must_be_ordered():
    with pytest.raises(ValidationError):
        StructurerConfig(iv_rank_spread_threshold=90, iv_rank_skip_threshold=80)


def test_live_mode_requires_api_key():
    with pytest.raises(ValidationError):
        AuriferousConfig(system=SystemConfig(mode="live"))


def test_live_mode_requires_realtime_data():
    with pytest.raises(ValidationError):
        AuriferousConfig(
            system=SystemConfig(mode="live"),
            llm={"api_key": "sk-test"},
            broker={"market_data_type": 3},
        )


def test_paper_mode_allows_missing_api_key():
    config = AuriferousConfig(system=SystemConfig(mode="paper"))
    assert config.system.mode == "paper"


def test_sqlite_url_shape():
    config = AuriferousConfig()
    config.database.type = "sqlite"
    config.database.name = "test_db"
    assert config.database.url() == "sqlite+aiosqlite:///test_db.db"


def test_get_before_load_raises():
    ConfigLoader.reset()
    with pytest.raises(RuntimeError):
        ConfigLoader.get()
