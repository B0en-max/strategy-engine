"""回测模块（Stage 14 ~ Stage 15）。

包含：
    engine.py   基础回测引擎
        BacktestEngine.run(strategy_id, symbol_id, timeframe, start, end) -> BacktestResult
    metrics.py  绩效统计
        compute_metrics() / realized_trades() / run_backtest_with_metrics()

链路：K 线 → 策略信号（StrategyRunner）→ 模拟执行（ExecutionEngine）→
      订单/成交/持仓 → 回测结果 → 绩效指标。

隔离原则：
    - 正式数据库**只读**：只从中复制本次回测需要的策略 / 标的 / K 线
    - 一次回测使用一个**独立临时数据库**，所有 signal / order / trade / position 都写在那里
    - 不使用正式库的历史信号/订单/成交/持仓当回测结果

本阶段不做：手续费、滑点、资金管理、杠杆、保证金、
复杂绩效指标（Sharpe / 最大回撤 / 资金曲线）、AI。
"""

from .engine import DEFAULT_BACKTEST_QUANTITY, BacktestEngine, BacktestResult
from .metrics import (
    EVENT_TYPES,
    BacktestMetrics,
    RealizedTrade,
    compute_metrics,
    realized_trades,
    run_backtest_with_metrics,
)

__all__ = [
    "DEFAULT_BACKTEST_QUANTITY",
    "EVENT_TYPES",
    "BacktestEngine",
    "BacktestMetrics",
    "BacktestResult",
    "RealizedTrade",
    "compute_metrics",
    "realized_trades",
    "run_backtest_with_metrics",
]
