"""Configuration: secrets from the environment, parameters from config/settings.yaml.

The split is deliberate. Secrets never reach git and are never logged
(see src/core/logging.py). Parameters live in version-controlled YAML so
that any backtest run can be reproduced from its commit.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


# ---------------------------------------------------------------------------
# Secrets and connections — from the environment
# ---------------------------------------------------------------------------

class StrictModel(BaseModel):
    """Base of all config sections: an unknown key is a load error.

    By default pydantic silently drops extra keys. That is how a whole
    section was lost: the validation plan from DEC-028 was written to the
    config, looked like a preregistration — and was not loaded at all (DEC-030).
    A typo in a threshold name would behave the same way: the value from the
    file is ignored, the default applies, and nothing reports it.

    P-001: the unknown is rejected, not skipped.
    """

    model_config = ConfigDict(extra="forbid")


class Env(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_user: str = "crypto"
    postgres_password: SecretStr = SecretStr("change_me")
    postgres_db: str = "crypto_bot"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    redis_host: str = "localhost"
    redis_port: int = 6379

    # Not needed in Phase 1: all endpoints used are public.
    bybit_api_key: SecretStr = SecretStr("")
    bybit_api_secret: SecretStr = SecretStr("")
    bybit_testnet: bool = False

    master_key: SecretStr = SecretStr("")

    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    @property
    def database_url(self) -> str:
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def alembic_url(self) -> str:
        """Synchronous driver: Alembic does not work through asyncpg directly."""
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


# ---------------------------------------------------------------------------
# Parameters — from YAML
# ---------------------------------------------------------------------------
class RiskConfig(StrictModel):
    equity_usd: float = Field(gt=0)
    margin_per_trade_pct: float = Field(gt=0, le=100)
    max_loss_per_trade_pct: float = Field(gt=0, le=100)
    #: Position size cap, % of the deposit (DEC-040). Trims rather than rejects.
    max_position_pct: float | None = Field(default=None, gt=0)
    max_concurrent_positions: int = Field(ge=1)
    max_total_margin_pct: float = Field(gt=0, le=100)
    leverage_cap: float = Field(gt=1)
    min_liq_buffer_ratio: float = Field(ge=1.0)
    daily_loss_limit_pct: float = Field(gt=0, le=100)
    weekly_loss_limit_pct: float = Field(gt=0, le=100)
    max_correlated_positions: int = Field(ge=0)
    correlation_threshold: float = Field(ge=0, le=1)
    correlation_lookback_days: int = Field(ge=1)

    #: P-007. The system must survive this many losses in a row without
    #: being switched off by the weekly limit.
    SURVIVABLE_LOSS_STREAK: ClassVar[int] = 8

    @model_validator(mode="after")
    def _risk_and_limits_are_compatible(self) -> RiskConfig:
        """P-007: risk per trade and loss limits are set independently
        and must be compatible.

        The spec set 6% per trade with a 20% weekly limit: four losses in
        a row were enough to switch off the week. Each number looked
        cautious on its own.
        """
        streak = self.SURVIVABLE_LOSS_STREAK
        if self.max_loss_per_trade_pct * streak > self.weekly_loss_limit_pct:
            survives = int(
                self.weekly_loss_limit_pct / self.max_loss_per_trade_pct
            )
            raise ValueError(
                f"max_loss_per_trade_pct={self.max_loss_per_trade_pct}% "
                f"with weekly_loss_limit_pct={self.weekly_loss_limit_pct}%: "
                f"the weekly limit is breached by {survives + 1} losses in a row "
                f"while {streak} are required (P-007). At most "
                f"{self.weekly_loss_limit_pct / streak:.2f}%"
            )
        slots = self.max_concurrent_positions
        if self.max_loss_per_trade_pct * slots > self.daily_loss_limit_pct:
            raise ValueError(
                f"max_loss_per_trade_pct={self.max_loss_per_trade_pct}% "
                f"× {slots} concurrent positions exceeds the daily limit "
                f"({self.daily_loss_limit_pct}%): stops of all positions on one "
                "day — a halt after the very first bad day (P-007)"
            )
        return self

    @model_validator(mode="after")
    def _check_coherence(self) -> RiskConfig:
        if self.weekly_loss_limit_pct < self.daily_loss_limit_pct:
            raise ValueError(
                "weekly_loss_limit_pct cannot be less than daily_loss_limit_pct"
            )
        # Total margin must fit at least one trade.
        if self.max_total_margin_pct < self.margin_per_trade_pct:
            raise ValueError(
                "max_total_margin_pct is less than margin_per_trade_pct: "
                "no trade can pass"
            )
        # And all declared slots: otherwise the last slot exists only on
        # the config's paper, and MAX_MARGIN will reject it (DEC-050).
        need = self.margin_per_trade_pct * self.max_concurrent_positions
        if self.max_total_margin_pct < need:
            raise ValueError(
                f"max_total_margin_pct={self.max_total_margin_pct}% does not fit "
                f"{self.max_concurrent_positions} positions at "
                f"{self.margin_per_trade_pct}% margin (need ≥ {need}%)"
            )
        return self


class CostsConfig(StrictModel):
    maker_fee_pct: float = Field(ge=0)
    taker_fee_pct: float = Field(ge=0)
    slippage_pct: float = Field(ge=0)
    #: Mandatory Phase 2 sweep (DEC-007). Replaces a universe spread
    #: filter: protection against a thin order book lives in the cost
    #: model, not in symbol selection.
    slippage_sensitivity: list[float] = Field(min_length=2)

    @property
    def round_trip_taker_pct(self) -> float:
        return 2 * self.taker_fee_pct + 2 * self.slippage_pct

    @model_validator(mode="after")
    def _sensitivity_is_sane(self) -> CostsConfig:
        if any(v < 0 for v in self.slippage_sensitivity):
            raise ValueError("slippage_sensitivity values cannot be negative")
        if self.slippage_pct not in self.slippage_sensitivity:
            # Otherwise the base run would fall outside the sweep, and there
            # would be nothing to compare it with.
            raise ValueError(
                f"slippage_pct={self.slippage_pct} is missing from slippage_sensitivity"
            )
        return self


class UniverseConfig(StrictModel):
    quote_coin: str
    contract_type: str
    crypto_only: bool
    allowed_symbol_types: list[str] = Field(default_factory=list)
    excluded_base_coins: list[str] = Field(default_factory=list)
    top_n: int = Field(ge=1)
    top_n_sensitivity: list[int] = Field(min_length=2)
    min_position_parts: int = Field(ge=1)
    min_avg_daily_turnover_usd: float = Field(ge=0)
    min_turnover_sensitivity: list[float] = Field(min_length=2)
    min_listing_age_days: int = Field(ge=0)
    selection_lag_months: int = Field(ge=1)
    include_delisted: bool

    @model_validator(mode="after")
    def _width_knobs_are_inside_their_sweeps(self) -> UniverseConfig:
        """The base value must be part of its own sweep.

        Otherwise the sweep stops being a check: it is computed around a
        number that is not in it, and the result's deviation from the base
        run has nothing to be compared with. The same check already exists
        for slippage and coverage — universe width was the only composition
        knob without it (DEC-015).
        """
        if self.top_n not in self.top_n_sensitivity:
            raise ValueError(
                f"top_n={self.top_n} is missing from top_n_sensitivity="
                f"{self.top_n_sensitivity}"
            )
        if self.min_avg_daily_turnover_usd not in self.min_turnover_sensitivity:
            raise ValueError(
                f"min_avg_daily_turnover_usd={self.min_avg_daily_turnover_usd} "
                f"is missing from min_turnover_sensitivity="
                f"{self.min_turnover_sensitivity}"
            )
        if any(v < 1 for v in self.top_n_sensitivity):
            raise ValueError("top_n_sensitivity values must be positive")
        if any(v < 0 for v in self.min_turnover_sensitivity):
            raise ValueError("min_turnover_sensitivity values cannot be negative")
        return self


class StreamRequirements(StrictModel):
    setup_a: list[str]
    setup_b: list[str]
    setup_c: list[str]
    setup_d: list[str]
    setup_e: list[str]
    setup_f: list[str]
    setup_g: list[str]
    setup_h: list[str]
    universe_ranking: list[str]


class DataConfig(StrictModel):
    streams: list[str]
    stream_requirements: StreamRequirements
    backfill_depth_years: int = Field(ge=1)
    #: DEC-012. Fixed before the run, on a par with the acceptance thresholds.
    min_stream_coverage: float = Field(gt=0, le=1)
    #: Phase 2 sweep: the third knob of universe composition.
    coverage_sensitivity: list[float] = Field(min_length=2)

    @model_validator(mode="after")
    def _base_coverage_is_inside_the_sweep(self) -> DataConfig:
        if self.min_stream_coverage not in self.coverage_sensitivity:
            # Otherwise the base run would fall outside the sweep, and there
            # would be nothing to compare it with — same logic as for slippage.
            raise ValueError(
                f"min_stream_coverage={self.min_stream_coverage} "
                "is missing from coverage_sensitivity"
            )
        if any(not 0 < v <= 1 for v in self.coverage_sensitivity):
            raise ValueError("coverage_sensitivity values must lie in (0, 1]")
        return self

    @model_validator(mode="after")
    def _requirements_are_known_streams(self) -> DataConfig:
        known = set(self.streams)
        for setup, required in self.stream_requirements.model_dump().items():
            unknown = set(required) - known
            if unknown:
                raise ValueError(
                    f"stream_requirements.{setup} refers to unknown "
                    f"streams: {sorted(unknown)}"
                )
        return self


class ShortHistoryPolicy(StrictModel):
    require_full_in_sample_for_fitting: bool
    min_months_before_oos: int = Field(ge=0)
    log_each_inclusion: bool
    warn_if_trade_share_above_pct: float = Field(ge=0, le=100)


class CollapseFixture(StrictModel):
    symbol: str
    period: str


class WalkForwardConfig(StrictModel):
    """Walk-forward window — the third slice (DEC-011).

    A diagnostic, not a replacement for the acceptance thresholds: it tells
    overfitting from regime sensitivity where a single OOS run cannot.
    """

    enabled: bool
    train_months: int = Field(ge=1)
    test_months: int = Field(ge=1)
    step_months: int = Field(ge=1)
    min_windows: int = Field(ge=1)
    report_e_vs_liquidity_correlation: bool
    liquidity_correlation_warn_above: float = Field(ge=0, le=1)

    #: Windows overlap: neighbours share train_months − step_months months
    #: of data. The estimates are not independent, strong statistical claims
    #: cannot be built on them.
    windows_overlap: bool = True
    overlap_warning_in_report: bool = True

    @property
    def shared_months(self) -> int:
        """How many training months neighbouring windows share."""
        return max(0, self.train_months - self.step_months)


class MarginalEntrantsConfig(StrictModel):
    """Contribution of symbols added by lowering the threshold (limitation 9).

    They are not excluded in advance: they pass selection legitimately. But
    their contribution is biased into out-of-sample, so it is computed separately.
    """

    report_trade_share: bool
    report_e_without_them: bool
    path: str

    def symbols(self, root: Path | None = None) -> frozenset[str]:
        base = root or PROJECT_ROOT
        target = base / self.path
        if not target.exists():
            return frozenset()
        return frozenset(
            line.strip()
            for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )



class ValidationWindow(StrictModel):
    start: str
    end: str
    #: in_sample — may be analysed as much as you like; out_of_sample — is
    #: spent once FOR ALL SETUPS: whoever has looked at an out-of-sample
    #: period turns it into in-sample for any subsequent hypothesis.
    kind: Literal["in_sample", "out_of_sample"]

    @model_validator(mode="after")
    def _ordered(self) -> ValidationWindow:
        from datetime import date

        if date.fromisoformat(self.start) >= date.fromisoformat(self.end):
            raise ValueError(f"validation window is empty or reversed: {self.start} … {self.end}")
        return self


class ValidationPlan(StrictModel):
    """Validation plan of one setup, written before the data (DEC-028, DEC-034)."""

    frozen_at_commit: str = Field(min_length=7)
    drawdown_basis: Literal["portfolio", "population"]
    windows: dict[str, ValidationWindow]
    #: Order in which the windows are passed. The next one only after the
    #: previous one has passed.
    sequence: list[str] = Field(min_length=1)
    #: Required margin over the null hypothesis, in standard errors.
    #: None — not required (Setup C did not preregister it).
    null_advantage_min_se: float | None = None
    #: Time exit guard: the median absolute return over the holding window
    #: is not below strategy.min_move_pct (DEC-033).
    time_exit_move_floor: bool = False
    #: Code, besides the setup's own file, where its logic lives.
    #: Setup E reuses rule D: without this field an edit to setup_d.py
    #: would change E unnoticed by the freeze.
    extra_code: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sequence_is_consistent(self) -> ValidationPlan:
        from datetime import date

        unknown = [w for w in self.sequence if w not in self.windows]
        if unknown:
            raise ValueError(f"unknown windows in the sequence: {unknown}")
        if len(set(self.sequence)) != len(self.sequence):
            raise ValueError("a window appears twice in the sequence")
        ordered = [self.windows[w] for w in self.sequence]
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if (
                earlier.kind == "out_of_sample" or later.kind == "out_of_sample"
            ) and date.fromisoformat(earlier.end) > date.fromisoformat(later.start):
                raise ValueError("plan windows overlap an out-of-sample window")
        return self


class BacktestConfig(StrictModel):
    in_sample_months: int = Field(ge=1)
    out_of_sample_months: int = Field(ge=1)
    short_history: ShortHistoryPolicy
    collapse_fixtures: list[CollapseFixture]
    walk_forward: WalkForwardConfig
    marginal_entrants: MarginalEntrantsConfig
    validation: dict[str, ValidationPlan] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _walk_forward_fits_the_period(self) -> BacktestConfig:
        wf = self.walk_forward
        if not wf.enabled:
            return self
        total = self.in_sample_months + self.out_of_sample_months
        span = wf.train_months + wf.test_months
        if span > total:
            raise ValueError(
                f"walk-forward window ({span} mo) does not fit in the period "
                f"({total} mo)"
            )
        windows = (total - span) // wf.step_months + 1
        if windows < wf.min_windows:
            raise ValueError(
                f"walk-forward gives {windows} windows with a required minimum of "
                f"{wf.min_windows}: conclusions on such a sample are unreliable"
            )
        return self


class BackoffPolicy(StrictModel):
    base_seconds: float = Field(gt=0)
    max_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _max_not_below_base(self) -> BackoffPolicy:
        if self.max_seconds < self.base_seconds:
            raise ValueError("max_seconds cannot be less than base_seconds")
        return self


class BackoffConfig(StrictModel):
    network: BackoffPolicy
    rate_limit: BackoffPolicy

    @model_validator(mode="after")
    def _rate_limit_is_not_faster(self) -> BackoffConfig:
        # Retrying after a 429 faster than after a connection drop would mean
        # we hurry exactly where the exchange asked us to slow down.
        if self.rate_limit.base_seconds < self.network.base_seconds:
            raise ValueError(
                "the rate-limit base delay cannot be less than the network one"
            )
        return self


class CircuitBreakerConfig(StrictModel):
    max_consecutive_rate_limit_failures: int = Field(ge=1)


class RateLimitConfig(StrictModel):
    requests_per_second: float = Field(gt=0)
    burst: int = Field(ge=1)
    max_retries: int = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    backoff: BackoffConfig
    respect_retry_after: bool = True
    max_retry_after_seconds: float = Field(gt=0, default=300.0)
    circuit_breaker: CircuitBreakerConfig


class BootstrapCacheConfig(StrictModel):
    path: str
    ttl_hours: int = Field(ge=0)

    def resolved_path(self) -> Path:
        p = Path(self.path)
        return p if p.is_absolute() else PROJECT_ROOT / p


class BybitConfig(StrictModel):
    rest_base_url: str
    testnet_base_url: str
    public_archive_url: str
    category: str
    rate_limit: RateLimitConfig
    bootstrap_cache: BootstrapCacheConfig

    def base_url(self, *, testnet: bool) -> str:
        return self.testnet_base_url if testnet else self.rest_base_url


class FundingConfig(StrictModel):
    normalize_to_hours: float = Field(gt=0)


class Settings(StrictModel):
    """Full configuration. Secrets — separately, in `env`."""

    risk: RiskConfig
    costs: CostsConfig
    universe: UniverseConfig
    data: DataConfig
    backtest: BacktestConfig
    bybit: BybitConfig
    funding: FundingConfig
    # Strategy and AI parameters are not typed in Phase 1a: they change
    # together with the hypotheses, and a rigid schema only gets in the way
    # here. They will come in Phase 2 together with the backtest engine.
    strategy: dict[str, Any] = Field(default_factory=dict)
    ai: dict[str, Any] = Field(default_factory=dict)


def load_settings(path: Path | None = None) -> Settings:
    """Reads the YAML and validates it. Raises on the first inconsistency."""
    settings_path = path or DEFAULT_SETTINGS_PATH
    if not settings_path.exists():
        raise FileNotFoundError(f"Settings file not found: {settings_path}")
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
    return Settings.model_validate(raw)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


@functools.lru_cache(maxsize=1)
def get_env() -> Env:
    return Env()
