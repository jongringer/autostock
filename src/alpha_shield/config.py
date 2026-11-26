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


class ConfigError(ValueError):
    """설정 파일 오류."""


def _require(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{path}: 필수 키 '{key}' 가 없습니다")
    return mapping[key]


def _num(mapping: Mapping[str, Any], key: str, path: str) -> Any:
    return dec(_require(mapping, key, path))


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
class CapitalConfig:
    slots: int
    threshold_usd: dec
    small_entry_usd: dec
    small_symbol_cap_usd: dec
    large_entry_usd: dec
    large_symbol_cap_usd: dec
    cash_buffer_pct: dec
    cash_buffer_min_usd: dec
    min_order_usd: dec

    def tier(self, equity: dec) -> tuple[dec, dec]:
        """(1회 진입 금액, 종목 상한)."""
        if equity < self.threshold_usd:
            return self.small_entry_usd, self.small_symbol_cap_usd
        return self.large_entry_usd, self.large_symbol_cap_usd

    def cash_buffer(self, equity: dec) -> dec:
        return max(equity * self.cash_buffer_pct, self.cash_buffer_min_usd)


@dataclass(frozen=True)
class EntryConfig:
    atr_period: int
    atr_drawdown_multiple: dec
    rsi_period: int
    rsi_max: dec
    chase_rsi_block: dec
    sma_period: int
    sma_floor_ratio: dec
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
    rsi_max: dec
    max_adds: int


@dataclass(frozen=True)
class ExitConfig:
    stop_loss_pct: dec
    stop_loss_added_pct: dec
    take_profit_vix_low: dec
    take_profit_low_vix_pct: dec
    take_profit_high_vix_pct: dec
    time_exit_trading_days: int
    time_exit_calendar_days: int
    cooldown_seconds: int
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
    capital = CapitalConfig(
        slots=int(_require(cap, "slots", f"{where}:capital")),
        threshold_usd=_num(cap, "threshold_usd", f"{where}:capital"),
        small_entry_usd=_num(cap, "small_entry_usd", f"{where}:capital"),
        small_symbol_cap_usd=_num(cap, "small_symbol_cap_usd", f"{where}:capital"),
        large_entry_usd=_num(cap, "large_entry_usd", f"{where}:capital"),
        large_symbol_cap_usd=_num(cap, "large_symbol_cap_usd", f"{where}:capital"),
        cash_buffer_pct=_num(cap, "cash_buffer_pct", f"{where}:capital"),
        cash_buffer_min_usd=_num(cap, "cash_buffer_min_usd", f"{where}:capital"),
        min_order_usd=_num(cap, "min_order_usd", f"{where}:capital"),
    )
    if capital.slots != 2:
        raise ConfigError(f"{where}: v3.2 는 2슬롯 고정입니다 (slots={capital.slots})")
    if capital.small_entry_usd < capital.min_order_usd:
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
        atr_fallback_pct=fallback,
        fallback_default_pct=fallback_default,
        signal_ttl_seconds=int(_require(entry_raw, "signal_ttl_seconds", f"{where}:entry")),
        signal_redelivery_rsi_max=_num(entry_raw, "signal_redelivery_rsi_max", f"{where}:entry"),
        hod_outlier_filter=bool(entry_raw.get("hod_outlier_filter", True)),
    )
    if entry.rsi_max >= entry.chase_rsi_block:
        raise ConfigError(f"{where}: rsi_max 는 chase_rsi_block 보다 작아야 합니다")
    if entry.signal_ttl_seconds != 900:
        raise ConfigError(f"{where}: v3.2 BUY 신호 만료는 900초입니다")

    add_raw = _require(raw, "add", where)
    add = AddConfig(
        trigger_pct=_num(add_raw, "trigger_pct", f"{where}:add"),
        rsi_max=_num(add_raw, "rsi_max", f"{where}:add"),
        max_adds=int(_require(add_raw, "max_adds", f"{where}:add")),
    )
    if add.max_adds != 1:
        raise ConfigError(f"{where}: v3.2 물타기는 종목당 1회입니다")

    exit_raw = _require(raw, "exit", where)
    exit_cfg = ExitConfig(
        stop_loss_pct=_num(exit_raw, "stop_loss_pct", f"{where}:exit"),
        stop_loss_added_pct=_num(exit_raw, "stop_loss_added_pct", f"{where}:exit"),
        take_profit_vix_low=_num(exit_raw, "take_profit_vix_low", f"{where}:exit"),
        take_profit_low_vix_pct=_num(exit_raw, "take_profit_low_vix_pct", f"{where}:exit"),
        take_profit_high_vix_pct=_num(exit_raw, "take_profit_high_vix_pct", f"{where}:exit"),
        time_exit_trading_days=int(_require(exit_raw, "time_exit_trading_days", f"{where}:exit")),
        time_exit_calendar_days=int(_require(exit_raw, "time_exit_calendar_days", f"{where}:exit")),
        cooldown_seconds=int(_require(exit_raw, "cooldown_seconds", f"{where}:exit")),
        force_exit_buffer_min=int(_require(exit_raw, "force_exit_buffer_min", f"{where}:exit")),
    )
    if exit_cfg.stop_loss_added_pct >= exit_cfg.stop_loss_pct:
        raise ConfigError(f"{where}: 물타기 손절은 단일 손절보다 타이트해야 합니다")

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
        fail_closed=bool(market_raw.get("fail_closed", True)),
    )

    session_raw = _require(raw, "session", where)
    session = SessionGuardConfig(
        entry_open_delay_min=int(_require(session_raw, "entry_open_delay_min", f"{where}:session")),
        entry_close_buffer_min=int(_require(session_raw, "entry_close_buffer_min", f"{where}:session")),
        fractional_close_buffer_min=int(
            _require(session_raw, "fractional_close_buffer_min", f"{where}:session")
        ),
    )

    events_raw = _require(raw, "events", where)
    events = EventsGuardConfig(
        earnings_block_before_days=int(
            _require(events_raw, "earnings_block_before_days", f"{where}:events")
        ),
        earnings_fail_closed=bool(events_raw.get("earnings_fail_closed", True)),
        earnings_cache_seconds=int(_require(events_raw, "earnings_cache_seconds", f"{where}:events")),
    )

    filings_raw = _require(raw, "filings", where)
    keywords = tuple(str(item).lower() for item in _require(filings_raw, "block_keywords", f"{where}:filings"))
    if not keywords:
        raise ConfigError(f"{where}: filings.block_keywords 가 비어 있습니다")
    cik_raw = _require(filings_raw, "cik", f"{where}:filings")
    filings = FilingsGuardConfig(
        lookback_days=int(_require(filings_raw, "lookback_days", f"{where}:filings")),
        fail_closed=bool(filings_raw.get("fail_closed", False)),
        block_on_error=bool(filings_raw.get("block_on_error", True)),
        block_keywords=keywords,
        sec_user_agent=str(_require(filings_raw, "sec_user_agent", f"{where}:filings")),
        cik={str(k).upper(): str(v) for k, v in cik_raw.items() if v},
    )

    news_raw = _require(raw, "news", where)
    news = NewsGuardConfig(
        enabled=bool(news_raw.get("enabled", True)),
        fail_closed=bool(news_raw.get("fail_closed", True)),
        lookback_items=int(news_raw.get("lookback_items", 20)),
        feed_url=str(_require(news_raw, "feed_url", f"{where}:news")),
        block_keywords=tuple(str(item).lower() for item in _require(news_raw, "block_keywords", f"{where}:news")),
    )

    macro_raw = _require(raw, "macro", where)
    macro = MacroGuardConfig(
        calendar_file=str(_require(macro_raw, "calendar_file", f"{where}:macro")),
        drawdown_boost_pct=_num(macro_raw, "drawdown_boost_pct", f"{where}:macro"),
        fail_closed=bool(macro_raw.get("fail_closed", True)),
    )

    data_raw = _require(raw, "data", where)
    data = DataGuardConfig(
        max_quote_age_seconds=int(_require(data_raw, "max_quote_age_seconds", f"{where}:data")),
        max_daily_bar_age_days=int(_require(data_raw, "max_daily_bar_age_days", f"{where}:data")),
        require_sma200=bool(data_raw.get("require_sma200", True)),
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
            "SEC_USER_AGENT", "alpha-shield/3.2 (https://github.com/jongringer/autostock)"
        ).strip(),
        alert_webhook_url=env.get("ALERT_WEBHOOK_URL", "").strip(),
        paper_starting_cash=paper_cash,
    )


def load_all(
    root: Path | None = None, mode: str | None = None, data_dir: str | None = None
) -> tuple[Settings, StrategyConfig, GuardsConfig]:
    root = (root or DEFAULT_ROOT).resolve()
    settings = load_settings(root=root, mode=mode, data_dir=data_dir)
    strategy = load_strategy(root / "config" / "strategy.yaml")
    guards = load_guards(root / "config" / "guards.yaml")
    return settings, strategy, guards
