"""Public API for the Silver Bullet research package."""

from .alpaca import (
    AlpacaProxyConfig,
    AlpacaProxyExecutor,
    ProxyOrderPreview,
    ProxyOrderResult,
)
from .data import (
    CSVBarLoader,
    CSVSchema,
    DataQualityReport,
    DataValidationError,
    LoadResult,
    MissingInterval,
)
from .calendar import CMECalendar, DEFAULT_CME_CALENDAR
from .context import (
    ContextGateConfig,
    ContextGateDecision,
    ExternalContextSnapshot,
    SignalContextGate,
)
from .analytics import TearSheetBuilder, calculate_metrics, result_frames
from .execution import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    CostModel,
    EquityPoint,
    Fill,
    FillReason,
    RiskLimits,
    RiskViolation,
    Side,
    Trade,
)
from .levels import LiquidityLevelTracker, trading_day, trading_week
from .models import (
    Bar,
    BarSource,
    Direction,
    FairValueGap,
    INSTRUMENTS,
    InstrumentSpec,
    LiquidityKind,
    LiquidityLevel,
    TradeSignal,
)
from .strategy import (
    DEFAULT_SESSIONS,
    SessionWindow,
    SilverBulletConfig,
    SilverBulletStrategy,
    StrategyStage,
)
from .unusual_whales import UnusualWhalesClient, UnusualWhalesConfig
from .optimization import (
    OptimizationRecord,
    OptimizationRunner,
    WalkForwardSplit,
    parameter_combinations,
)

__all__ = [
    "AlpacaProxyConfig",
    "AlpacaProxyExecutor",
    "Bar",
    "BarSource",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "CMECalendar",
    "ContextGateConfig",
    "ContextGateDecision",
    "CSVBarLoader",
    "CSVSchema",
    "DEFAULT_SESSIONS",
    "DataQualityReport",
    "DataValidationError",
    "DEFAULT_CME_CALENDAR",
    "Direction",
    "ExternalContextSnapshot",
    "FairValueGap",
    "Fill",
    "FillReason",
    "INSTRUMENTS",
    "InstrumentSpec",
    "LiquidityKind",
    "LiquidityLevel",
    "LiquidityLevelTracker",
    "LoadResult",
    "MissingInterval",
    "OptimizationRecord",
    "OptimizationRunner",
    "ProxyOrderPreview",
    "ProxyOrderResult",
    "CostModel",
    "EquityPoint",
    "RiskLimits",
    "RiskViolation",
    "SessionWindow",
    "SilverBulletConfig",
    "SilverBulletStrategy",
    "SignalContextGate",
    "StrategyStage",
    "Side",
    "TearSheetBuilder",
    "Trade",
    "TradeSignal",
    "UnusualWhalesClient",
    "UnusualWhalesConfig",
    "trading_day",
    "trading_week",
    "WalkForwardSplit",
    "calculate_metrics",
    "parameter_combinations",
    "result_frames",
]
