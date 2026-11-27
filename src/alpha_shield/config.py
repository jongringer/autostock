"""설정 로딩·검증 (fail-fast).

YAML 은 사람이 읽고 고치는 파일이므로, 숫자는 전부 `dec()` 를 거쳐 Decimal 로 바꾼다.
잘못된 설정은 기동 시점에 `ConfigError` 로 멈춘다 — 장중에 발견하지 않도록.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .numeric import dec

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
VALID_MODES = ("dry-run", "dry_run", "paper", "live")
VALID_CAPITAL_PLANS = ("A", "B")
VALID_SETTLEMENTS = ("cash", "margin")


def _usd(value: dec) -> str:
    """$75.00 처럼 소수 2자리로 표시(정수면 소수점 생략)."""
    text = f"{value:.2f}"
    return text[:-3] if text.endswith(".00") else text


class ConfigError(ValueError):
    """설정 파일 오류."""


def _require(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{path}: 필수 키 '{key}' 가 없습니다")
    return mapping[key]


def _num(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    return dec(_require(mapping, key, path))


def _bool(mapping: Mapping[str, Any], key: str, default: bool) -> bool:
    value = mapping.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


@dataclass(frozen=True)
class Universe:
    weights: Mapping[str, dec]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(self.weights)

    def alpha(self, symbol: str) -> dec:
        return self.weights.get(symbol, dec(0))

    def __len__(self) -> int:
        return len(self.weights)


@dataclass(frozen=True)
class CapitalTier:
    """자산 구간 하나의 1회 진입 금액과 종목 상한."""

    max_equity_usd: dec | None  # None = 마지막(무한) 구간
    entry_usd: dec
    symbol_cap_usd: dec


@dataclass(frozen=True)
class CapitalConfig:
    slots: int
    plan: str
    tiers: tuple[CapitalTier, ...]
    cash_buffer_pct: dec
    cash_buffer_min_usd: dec
    min_order_usd: dec

    def tier(self, equity: dec) -> tuple[dec, dec]:
        """(1회 진입 금액, 종목 상한).

        `plan=A` 3단 사이징: $400 미만 75/150 · $400~$500 미만 85/170 · $500 이상 120/240.
        `plan=B` 부분 물타기 허용: $500 미만은 85/170 단일 구간(두 번째 종목 물타기는 가용현금으로 제한).
        """
        skip_smallest = self.plan == "B" and len(self.tiers) > 1
        for index, tier in enumerate(self.tiers):
            if skip_smallest and index == 0:
                # 대안 B 는 가장 작은 구간(75/150)을 쓰지 않고 85/170 부터 적용한다.
                if tier.max_equity_usd is None or equity < tier.max_equity_usd:
                    continue
            if tier.max_equity_usd is None or equity < tier.max_equity_usd:
                return tier.entry_usd, tier.symbol_cap_usd
        last = self.tiers[-1]
        return last.entry_usd, last.symbol_cap_usd

    def cash_buffer(self, equity: dec) -> dec:
        return max(equity * self.cash_buffer_pct, self.cash_buffer_min_usd)

    def describe_tiers(self) -> str:
        """`자산 < $400 → $75 / $150 · $400~$500 미만 → $85 / $170 …` 요약."""
        parts = []
        low: dec | None = None
        for tier in self.tiers:
            if tier.max_equity_usd is None:
                label = f"${_usd(low)} 이상" if low is not None else "전 구간"
            elif low is None:
                label = f"${_usd(tier.max_equity_usd)} 미만"
            else:
                label = f"${_usd(low)}~${_usd(tier.max_equity_usd)} 미만"
            parts.append(f"{label} → ${_usd(tier.entry_usd)} / ${_usd(tier.symbol_cap_usd)}")
            low = tier.max_equity_usd
        return " · ".join(parts)


@dataclass(frozen=True)
class EntryConfig:
    atr_period: int
    atr_drawdown_multiple: dec
    rsi_period: int
    rsi_max: dec
    chase_rsi_block: dec
    sma_period: int
    sma_floor_ratio: dec
    score_cap_multiple: dec
    crash_atr_multiple: dec
    atr_fallback_pct: Mapping[str, dec]
    fallback_default_pct: dec
    signal_ttl_seconds: int
    signal_redelivery_rsi_max: dec
    hod_outlier_filter: bool

    def fallback_threshold_pct(self, symbol: str) -> dec:
        return self.atr_fallback_pct.get(symbol, self.fallback_default_pct)


@dataclass(frozen=True)
class AddConfig:
    trigger_pct: dec
    atr_multiple: dec
    rsi_max: dec
    max_adds: int
    min_interval_hours: int


@dataclass(frozen=True)
class ExitConfig:
    stop_loss_pct: dec
    stop_loss_added_pct: dec
    preemptive_near_stop_pct: dec
    take_profit_vix_low: dec
    take_profit_low_vix_pct: dec
    take_profit_high_vix_pct: dec
    time_exit_trading_days: int
    time_exit_calendar_days: int
    trailing_on_time_exit: bool
    trailing_trigger_pct: dec
    trailing_drawdown_pct: dec
    cooldown_seconds: int
    stop_loss_eod_cooldown: bool
    force_exit_buffer_min: int


@dataclass(frozen=True)
class PaperConfig:
    starting_cash_usd: dec
    slippage_bps: dec
    commission_bps: dec


@dataclass(frozen=True)
class StrategyConfig:
    universe: Universe
    capital: CapitalConfig
    entry: EntryConfig
    add: AddConfig
    exit: ExitConfig
    paper: PaperConfig


@dataclass(frozen=True)
class MarketGuardConfig:
    nq_symbol: str
    nq_block_pct: dec
    vix_symbol: str
    vix_block: dec
    fail_closed: bool


@dataclass(frozen=True)
class SessionGuardConfig:
    entry_open_delay_min: int
    entry_close_buffer_min: int
    fractional_close_buffer_min: int
    preemptive_start_min: int
    preemptive_end_min: int
    overnight_stop_after_open_min: int
    premarket_flush_min: int


@dataclass(frozen=True)
class AccountGuardConfig:
    settlement: str
    unsettled_rebuy_block: bool
    settlement_business_days: int
    pdt_min_equity_usd: dec
    pdt_day_trade_limit: int
    pdt_window_business_days: int

    @property
    def is_cash(self) -> bool:
        return self.settlement == "cash"


@dataclass(frozen=True)
class KillSwitchConfig:
    daily_loss_pct: dec
    mdd_pct: dec
    duplicate_window_seconds: int
    api_error_threshold: int
    halt_on_mismatch: bool


@dataclass(frozen=True)
class EventsGuardConfig:
    earnings_block_before_days: int
    earnings_fail_closed: bool
    earnings_cache_seconds: int


@dataclass(frozen=True)
class FilingsGuardConfig:
    lookback_days: int
    fail_closed: bool
    block_on_error: bool
    block_keywords: tuple[str, ...]
    sec_user_agent: str
    cik: Mapping[str, str]


@dataclass(frozen=True)
class NewsGuardConfig:
    enabled: bool
    fail_closed: bool
    lookback_items: int
    feed_url: str
    block_keywords: tuple[str, ...]


@dataclass(frozen=True)
class MacroGuardConfig:
    calendar_file: str
    drawdown_boost_pct: dec
    fail_closed: bool


@dataclass(frozen=True)
class DataGuardConfig:
    max_quote_age_seconds: int
    max_daily_bar_age_days: int
    require_sma200: bool
    tick_guard_pct: dec
    tick_guard_bar_minutes: int
    tick_guard_tolerance_pct: dec
    tick_guard_fail_closed: bool


@dataclass(frozen=True)
class RiskConfig:
    daily_loss_limit_pct: dec
    max_orders_per_minute: int
    duplicate_order_window_seconds: int
    reconcile_qty_tolerance: dec


@dataclass(frozen=True)
class GuardsConfig:
    market: MarketGuardConfig
    session: SessionGuardConfig
    account: AccountGuardConfig
    kill_switch: KillSwitchConfig
    events: EventsGuardConfig
    filings: FilingsGuardConfig
    news: NewsGuardConfig
    macro: MacroGuardConfig
    data: DataGuardConfig
    risk: RiskConfig


@dataclass(frozen=True)
class Settings:
    mode: str
    root: Path
    data_dir: Path
    log_level: str
    live_orders_env: bool
    toss_client_id: str
    toss_client_secret: str
    toss_account_seq: int | None
    toss_base_url: str
    sec_user_agent: str
    alert_webhook_url: str
    paper_starting_cash: dec
    allow_fractional_premarket_sell: bool = False

    @property
    def normalized_mode(self) -> str:
        return "dry-run" if self.mode in ("dry-run", "dry_run") else self.mode

    @property
    def armed_file(self) -> Path:
        return self.data_dir / "ARMED"

    @property
    def halt_file(self) -> Path:
        return self.data_dir / "halt.flag"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "alpha_shield.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "toss_token.json"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"

    def as_safe_dict(self) -> dict[str, object]:
        """로그에 남겨도 되는 설정 요약 (비밀값 제외)."""
        return {
            "mode": self.normalized_mode,
            "data_dir": str(self.data_dir),
            "toss_client_id_set": bool(self.toss_client_id),
            "toss_secret_set": bool(self.toss_client_secret),
            "toss_account_seq": self.toss_account_seq,
            "armed_file_exists": self.armed_file.exists(),
            "live_orders_env": self.live_orders_env,
            "allow_fractional_premarket_sell": self.allow_fractional_premarket_sell,
        }


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일 없음: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 파싱 실패 {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path}: 최상위가 매핑이어야 합니다")
    return raw


def load_strategy(path: Path | None = None) -> StrategyConfig:
    path = path or DEFAULT_ROOT / "config" / "strategy.yaml"
    raw = _load_yaml(path)
    where = str(path)

    universe_raw = _require(raw, "universe", where)
    if not isinstance(universe_raw, Mapping) or not universe_raw:
        raise ConfigError(f"{where}: universe 는 비어있지 않은 매핑이어야 합니다")
    weights = {}
    for symbol, weight in universe_raw.items():
        value = dec(weight)
        if value <= 0:
            raise ConfigError(f"{where}: universe.{symbol} 가중치는 양수여야 합니다")
        weights[str(symbol).upper()] = value

    cap = _require(raw, "capital", where)
    tiers_raw = _require(cap, "tiers", f"{where}:capital")
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise ConfigError(f"{where}: capital.tiers 는 비어있지 않은 목록이어야 합니다")
    tiers: list[CapitalTier] = []
    for index, item in enumerate(tiers_raw):
        if not isinstance(item, Mapping):
            raise ConfigError(f"{where}: capital.tiers[{index}] 는 매핑이어야 합니다")
        limit_raw = item.get("max_equity_usd")
        limit = None if limit_raw is None else dec(limit_raw)
        entry_usd = _num(item, "entry_usd", f"{where}:capital.tiers[{index}]")
        symbol_cap = _num(item, "symbol_cap_usd", f"{where}:capital.tiers[{index}]")
        if symbol_cap < entry_usd:
            raise ConfigError(f"{where}: capital.tiers[{index}] 종목 상한이 1회 진입 금액보다 작습니다")
        tiers.append(CapitalTier(limit, entry_usd, symbol_cap))
    if tiers[-1].max_equity_usd is not None:
        raise ConfigError(f"{where}: capital.tiers 의 마지막 구간은 max_equity_usd: null 이어야 합니다")
    bounded = [tier.max_equity_usd for tier in tiers[:-1]]
    if any(bound is None for bound in bounded) or bounded != sorted(bounded):  # type: ignore[type-var]
        raise ConfigError(f"{where}: capital.tiers 는 자산 오름차순이어야 합니다")
    plan = str(cap.get("plan", "A")).strip().upper()
    if plan not in VALID_CAPITAL_PLANS:
        raise ConfigError(f"{where}: capital.plan 은 A 또는 B 입니다 (입력: {plan})")

    capital = CapitalConfig(
        slots=int(_require(cap, "slots", f"{where}:capital")),
        plan=plan,
        tiers=tuple(tiers),
        cash_buffer_pct=_num(cap, "cash_buffer_pct", f"{where}:capital"),
        cash_buffer_min_usd=_num(cap, "cash_buffer_min_usd", f"{where}:capital"),
        min_order_usd=_num(cap, "min_order_usd", f"{where}:capital"),
    )
    if capital.slots != 2:
        raise ConfigError(f"{where}: v3.3 는 2슬롯 고정입니다 (slots={capital.slots})")
    for tier in capital.tiers:
        if tier.entry_usd < capital.min_order_usd:
            raise ConfigError(f"{where}: 1회 진입 금액이 최소 주문 금액보다 작습니다")

    entry_raw = _require(raw, "entry", where)
    fallback_raw = _require(entry_raw, "atr_fallback_pct", f"{where}:entry")
    if not isinstance(fallback_raw, Mapping):
        raise ConfigError(f"{where}: entry.atr_fallback_pct 는 매핑이어야 합니다")
    fallback = {
        str(symbol).upper(): dec(value)
        for symbol, value in fallback_raw.items()
        if str(symbol) != "__default__"
    }
    fallback_default = dec(fallback_raw.get("__default__", 1.5))
    if fallback_default <= 0:
        raise ConfigError(f"{where}: entry.atr_fallback_pct.__default__ 는 양수여야 합니다")
    entry = EntryConfig(
        atr_period=int(_require(entry_raw, "atr_period", f"{where}:entry")),
        atr_drawdown_multiple=_num(entry_raw, "atr_drawdown_multiple", f"{where}:entry"),
        rsi_period=int(_require(entry_raw, "rsi_period", f"{where}:entry")),
        rsi_max=_num(entry_raw, "rsi_max", f"{where}:entry"),
        chase_rsi_block=_num(entry_raw, "chase_rsi_block", f"{where}:entry"),
        sma_period=int(_require(entry_raw, "sma_period", f"{where}:entry")),
        sma_floor_ratio=_num(entry_raw, "sma_floor_ratio", f"{where}:entry"),
        score_cap_multiple=_num(entry_raw, "score_cap_multiple", f"{where}:entry"),
        crash_atr_multiple=_num(entry_raw, "crash_atr_multiple", f"{where}:entry"),
        atr_fallback_pct=fallback,
        fallback_default_pct=fallback_default,
        signal_ttl_seconds=int(_require(entry_raw, "signal_ttl_seconds", f"{where}:entry")),
        signal_redelivery_rsi_max=_num(entry_raw, "signal_redelivery_rsi_max", f"{where}:entry"),
        hod_outlier_filter=_bool(entry_raw, "hod_outlier_filter", True),
    )
    if entry.rsi_max >= entry.chase_rsi_block:
        raise ConfigError(f"{where}: rsi_max 는 chase_rsi_block 보다 작아야 합니다")
    if entry.signal_ttl_seconds != 900:
        raise ConfigError(f"{where}: v3.3 BUY 신호 만료는 900초입니다")
    if entry.score_cap_multiple <= 0:
        raise ConfigError(f"{where}: entry.score_cap_multiple 는 양수여야 합니다")
    if entry.crash_atr_multiple <= 0:
        raise ConfigError(f"{where}: entry.crash_atr_multiple 는 양수여야 합니다")

    add_raw = _require(raw, "add", where)
    add = AddConfig(
        trigger_pct=_num(add_raw, "trigger_pct", f"{where}:add"),
        atr_multiple=_num(add_raw, "atr_multiple", f"{where}:add"),
        rsi_max=_num(add_raw, "rsi_max", f"{where}:add"),
        max_adds=int(_require(add_raw, "max_adds", f"{where}:add")),
        min_interval_hours=int(_require(add_raw, "min_interval_hours", f"{where}:add")),
    )
    if add.max_adds != 1:
        raise ConfigError(f"{where}: v3.3 물타기는 종목당 1회입니다")
    if add.min_interval_hours < 0:
        raise ConfigError(f"{where}: add.min_interval_hours 는 0 이상이어야 합니다")

    exit_raw = _require(raw, "exit", where)
    exit_cfg = ExitConfig(
        stop_loss_pct=_num(exit_raw, "stop_loss_pct", f"{where}:exit"),
        stop_loss_added_pct=_num(exit_raw, "stop_loss_added_pct", f"{where}:exit"),
        preemptive_near_stop_pct=_num(exit_raw, "preemptive_near_stop_pct", f"{where}:exit"),
        take_profit_vix_low=_num(exit_raw, "take_profit_vix_low", f"{where}:exit"),
        take_profit_low_vix_pct=_num(exit_raw, "take_profit_low_vix_pct", f"{where}:exit"),
        take_profit_high_vix_pct=_num(exit_raw, "take_profit_high_vix_pct", f"{where}:exit"),
        time_exit_trading_days=int(_require(exit_raw, "time_exit_trading_days", f"{where}:exit")),
        time_exit_calendar_days=int(_require(exit_raw, "time_exit_calendar_days", f"{where}:exit")),
        trailing_on_time_exit=_bool(exit_raw, "trailing_on_time_exit", True),
        trailing_trigger_pct=_num(exit_raw, "trailing_trigger_pct", f"{where}:exit"),
        trailing_drawdown_pct=_num(exit_raw, "trailing_drawdown_pct", f"{where}:exit"),
        cooldown_seconds=int(_require(exit_raw, "cooldown_seconds", f"{where}:exit")),
        stop_loss_eod_cooldown=_bool(exit_raw, "stop_loss_eod_cooldown", True),
        force_exit_buffer_min=int(_require(exit_raw, "force_exit_buffer_min", f"{where}:exit")),
    )
    if exit_cfg.stop_loss_added_pct >= exit_cfg.stop_loss_pct:
        raise ConfigError(f"{where}: 물타기 손절은 단일 손절보다 타이트해야 합니다")
    if exit_cfg.preemptive_near_stop_pct >= exit_cfg.stop_loss_added_pct:
        raise ConfigError(f"{where}: 선제 청산 근접 폭이 물타기 손절보다 작아야 합니다")

    paper_raw = _require(raw, "paper", where)
    paper = PaperConfig(
        starting_cash_usd=_num(paper_raw, "starting_cash_usd", f"{where}:paper"),
        slippage_bps=_num(paper_raw, "slippage_bps", f"{where}:paper"),
        commission_bps=_num(paper_raw, "commission_bps", f"{where}:paper"),
    )
    return StrategyConfig(universe=Universe(weights), capital=capital, entry=entry, add=add, exit=exit_cfg, paper=paper)


def load_guards(path: Path | None = None) -> GuardsConfig:
    path = path or DEFAULT_ROOT / "config" / "guards.yaml"
    raw = _load_yaml(path)
    where = str(path)

    market_raw = _require(raw, "market", where)
    market = MarketGuardConfig(
        nq_symbol=str(_require(market_raw, "nq_symbol", f"{where}:market")),
        nq_block_pct=_num(market_raw, "nq_block_pct", f"{where}:market"),
        vix_symbol=str(_require(market_raw, "vix_symbol", f"{where}:market")),
        vix_block=_num(market_raw, "vix_block", f"{where}:market"),
        fail_closed=_bool(market_raw, "fail_closed", True),
    )

    session_raw = _require(raw, "session", where)
    session = SessionGuardConfig(
        entry_open_delay_min=int(_require(session_raw, "entry_open_delay_min", f"{where}:session")),
        entry_close_buffer_min=int(_require(session_raw, "entry_close_buffer_min", f"{where}:session")),
        fractional_close_buffer_min=int(
            _require(session_raw, "fractional_close_buffer_min", f"{where}:session")
        ),
        preemptive_start_min=int(session_raw.get("preemptive_start_min", 10)),
        preemptive_end_min=int(session_raw.get("preemptive_end_min", 2)),
        overnight_stop_after_open_min=int(session_raw.get("overnight_stop_after_open_min", 2)),
        premarket_flush_min=int(session_raw.get("premarket_flush_min", 2)),
    )
    if session.preemptive_end_min >= session.preemptive_start_min:
        raise ConfigError(f"{where}: session.preemptive_end_min 은 start_min 보다 작아야 합니다")

    account_raw = _require(raw, "account", where)
    settlement = str(account_raw.get("settlement", "cash")).strip().lower()
    if settlement not in VALID_SETTLEMENTS:
        raise ConfigError(f"{where}: account.settlement 는 cash 또는 margin 입니다 (입력: {settlement})")
    account = AccountGuardConfig(
        settlement=settlement,
        unsettled_rebuy_block=_bool(account_raw, "unsettled_rebuy_block", True),
        settlement_business_days=int(account_raw.get("settlement_business_days", 1)),
        pdt_min_equity_usd=_num(account_raw, "pdt_min_equity_usd", f"{where}:account"),
        pdt_day_trade_limit=int(account_raw.get("pdt_day_trade_limit", 3)),
        pdt_window_business_days=int(account_raw.get("pdt_window_business_days", 5)),
    )

    kill_raw = _require(raw, "kill_switch", where)
    kill_switch = KillSwitchConfig(
        daily_loss_pct=_num(kill_raw, "daily_loss_pct", f"{where}:kill_switch"),
        mdd_pct=_num(kill_raw, "mdd_pct", f"{where}:kill_switch"),
        duplicate_window_seconds=int(kill_raw.get("duplicate_window_seconds", 300)),
        api_error_threshold=int(kill_raw.get("api_error_threshold", 3)),
        halt_on_mismatch=_bool(kill_raw, "halt_on_mismatch", True),
    )
    if kill_switch.api_error_threshold < 1:
        raise ConfigError(f"{where}: kill_switch.api_error_threshold 는 1 이상이어야 합니다")

    events_raw = _require(raw, "events", where)
    events = EventsGuardConfig(
        earnings_block_before_days=int(
            _require(events_raw, "earnings_block_before_days", f"{where}:events")
        ),
        earnings_fail_closed=_bool(events_raw, "earnings_fail_closed", True),
        earnings_cache_seconds=int(_require(events_raw, "earnings_cache_seconds", f"{where}:events")),
    )

    filings_raw = _require(raw, "filings", where)
    keywords = tuple(str(item).lower() for item in _require(filings_raw, "block_keywords", f"{where}:filings"))
    if not keywords:
        raise ConfigError(f"{where}: filings.block_keywords 가 비어 있습니다")
    cik_raw = _require(filings_raw, "cik", f"{where}:filings")
    filings = FilingsGuardConfig(
        lookback_days=int(_require(filings_raw, "lookback_days", f"{where}:filings")),
        fail_closed=_bool(filings_raw, "fail_closed", False),
        block_on_error=_bool(filings_raw, "block_on_error", True),
        block_keywords=keywords,
        sec_user_agent=str(_require(filings_raw, "sec_user_agent", f"{where}:filings")),
        cik={str(k).upper(): str(v) for k, v in cik_raw.items() if v},
    )

    news_raw = _require(raw, "news", where)
    news = NewsGuardConfig(
        enabled=_bool(news_raw, "enabled", True),
        fail_closed=_bool(news_raw, "fail_closed", True),
        lookback_items=int(news_raw.get("lookback_items", 20)),
        feed_url=str(_require(news_raw, "feed_url", f"{where}:news")),
        block_keywords=tuple(str(item).lower() for item in _require(news_raw, "block_keywords", f"{where}:news")),
    )

    macro_raw = _require(raw, "macro", where)
    macro = MacroGuardConfig(
        calendar_file=str(_require(macro_raw, "calendar_file", f"{where}:macro")),
        drawdown_boost_pct=_num(macro_raw, "drawdown_boost_pct", f"{where}:macro"),
        fail_closed=_bool(macro_raw, "fail_closed", True),
    )

    data_raw = _require(raw, "data", where)
    data = DataGuardConfig(
        max_quote_age_seconds=int(_require(data_raw, "max_quote_age_seconds", f"{where}:data")),
        max_daily_bar_age_days=int(_require(data_raw, "max_daily_bar_age_days", f"{where}:data")),
        require_sma200=_bool(data_raw, "require_sma200", True),
        tick_guard_pct=_num(data_raw, "tick_guard_pct", f"{where}:data") if "tick_guard_pct" in data_raw else dec("10.0"),
        tick_guard_bar_minutes=int(data_raw.get("tick_guard_bar_minutes", 5)),
        tick_guard_tolerance_pct=(
            _num(data_raw, "tick_guard_tolerance_pct", f"{where}:data")
            if "tick_guard_tolerance_pct" in data_raw
            else dec("1.5")
        ),
        tick_guard_fail_closed=_bool(data_raw, "tick_guard_fail_closed", True),
    )

    risk_raw = _require(raw, "risk", where)
    risk = RiskConfig(
        daily_loss_limit_pct=_num(risk_raw, "daily_loss_limit_pct", f"{where}:risk"),
        max_orders_per_minute=int(_require(risk_raw, "max_orders_per_minute", f"{where}:risk")),
        duplicate_order_window_seconds=int(
            _require(risk_raw, "duplicate_order_window_seconds", f"{where}:risk")
        ),
        reconcile_qty_tolerance=_num(risk_raw, "reconcile_qty_tolerance", f"{where}:risk"),
    )
    return GuardsConfig(
        market=market,
        session=session,
        account=account,
        kill_switch=kill_switch,
        events=events,
        filings=filings,
        news=news,
        macro=macro,
        data=data,
        risk=risk,
    )


def _env_bool(env: Mapping[str, str], key: str, default: bool = False) -> bool:
    value = env.get(key)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def load_settings(
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
    mode: str | None = None,
    data_dir: str | None = None,
) -> Settings:
    env = dict(os.environ if env is None else env)
    root = (root or DEFAULT_ROOT).resolve()
    resolved_mode = (mode or env.get("ALPHA_MODE", "dry-run")).strip()
    if resolved_mode not in VALID_MODES:
        raise ConfigError(f"알 수 없는 모드: {resolved_mode!r} (dry-run|paper|live)")
    if resolved_mode in ("dry-run", "dry_run"):
        resolved_mode = "dry-run"
    account_raw = env.get("TOSS_ACCOUNT_SEQ", "").strip()
    account_seq = int(account_raw) if account_raw else None
    data_path = Path(data_dir or env.get("ALPHA_DATA_DIR", "state"))
    if not data_path.is_absolute():
        data_path = (root / data_path).resolve()
    paper_cash = dec(env.get("PAPER_STARTING_CASH", "380.00"))
    return Settings(
        mode=resolved_mode,
        root=root,
        data_dir=data_path,
        log_level=env.get("ALPHA_LOG_LEVEL", "INFO").upper(),
        live_orders_env=_env_bool(env, "LIVE_ORDERS", False),
        toss_client_id=env.get("TOSS_CLIENT_ID", "").strip(),
        toss_client_secret=env.get("TOSS_CLIENT_SECRET", "").strip(),
        toss_account_seq=account_seq,
        toss_base_url=env.get("TOSS_BASE_URL", "https://openapi.tossinvest.com").rstrip("/"),
        sec_user_agent=env.get(
            "SEC_USER_AGENT", "alpha-shield/3.3 (https://github.com/jongringer/autostock)"
        ).strip(),
        alert_webhook_url=env.get("ALERT_WEBHOOK_URL", "").strip(),
        paper_starting_cash=paper_cash,
        allow_fractional_premarket_sell=_env_bool(env, "ALLOW_FRACTIONAL_PREMARKET_SELL", False),
    )


def load_all(
    root: Path | None = None, mode: str | None = None, data_dir: str | None = None
) -> tuple[Settings, StrategyConfig, GuardsConfig]:
    root = (root or DEFAULT_ROOT).resolve()
    settings = load_settings(root=root, mode=mode, data_dir=data_dir)
    strategy = load_strategy(root / "config" / "strategy.yaml")
    guards = load_guards(root / "config" / "guards.yaml")
    return settings, strategy, guards
