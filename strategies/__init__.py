"""策略模块（Stage 13）。

结构：
    base.py             Bar / SignalCandidate / Strategy 接口（与数据库解耦）
    close_direction.py  最简示例策略（相邻收盘价方向）
    runner.py           StrategyRunner：策略配置 + K 线 → signals 表

边界：
    - 只做「策略计算 → 产生信号」，**不执行交易**
    - 不创建 Order / Trade / Position，不做风控、不做回测
    - 策略参数只从 strategies.config 读取；内置实现可被注入替换
"""

from .base import Bar, SignalCandidate, Strategy, bar_from_kline_row
from .close_direction import CloseDirectionStrategy
from .runner import (
    DEFAULT_STRATEGIES,
    DEFAULT_STRATEGY_TYPE,
    STRATEGY_TYPE_KEY,
    RunResult,
    StrategyRunner,
)

__all__ = [
    "DEFAULT_STRATEGIES",
    "DEFAULT_STRATEGY_TYPE",
    "STRATEGY_TYPE_KEY",
    "Bar",
    "CloseDirectionStrategy",
    "RunResult",
    "SignalCandidate",
    "Strategy",
    "StrategyRunner",
    "bar_from_kline_row",
]
