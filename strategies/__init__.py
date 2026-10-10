"""策略模块（Stage 13 建立；Stage 19 拆出 Strategy / Strategy Version 并加入执行器）。

结构：
    base.py             Bar / BarWindow / SignalCandidate / Strategy 接口（与数据库解耦）
    close_direction.py  最简示例策略（相邻收盘价方向）；本阶段的 Demo Strategy
    runner.py           StrategyRunner：策略配置 + K 线 → signals 表（批量，Stage 13）
    executor.py         StrategyExecutor：按「策略版本」逐根执行 → Signal → Trade（Stage 19）
    service.py          StrategyService：Strategy / 版本 / 追溯 / 执行入口 的门面

分工（Stage 19）：
    strategy_definitions  Strategy 身份（StrategyService.create_strategy）
    strategies            策略版本（StrategyService.create_version，参数在 config 里）
    StrategyExecutor      逐根 K 线调用 Strategy.on_bar（只看历史），写 Signal 并触发执行
    StrategyService       API / 复盘系统 / 策略系统的统一入口

边界：
    - 策略只做计算；Executor 负责时间推进、落库与执行；两者都不含对方的职责
    - 不创建实盘订单、不做风控、不做资金管理；策略参数只从版本 config 读取
"""

from .base import Bar, BarWindow, SignalCandidate, Strategy, bar_from_kline_row
from .close_direction import CloseDirectionStrategy
from .executor import (
    DEFAULT_EXECUTION_QUANTITY,
    ExecutedSignal,
    StrategyExecutionResult,
    StrategyExecutor,
)
from .runner import (
    DEFAULT_STRATEGIES,
    DEFAULT_STRATEGY_TYPE,
    STRATEGY_TYPE_KEY,
    RunResult,
    StrategyRunner,
    build_strategy,
    strategy_type_of,
)
from .service import StrategyService

__all__ = [
    "DEFAULT_EXECUTION_QUANTITY",
    "DEFAULT_STRATEGIES",
    "DEFAULT_STRATEGY_TYPE",
    "STRATEGY_TYPE_KEY",
    "Bar",
    "BarWindow",
    "CloseDirectionStrategy",
    "ExecutedSignal",
    "RunResult",
    "SignalCandidate",
    "Strategy",
    "StrategyExecutionResult",
    "StrategyExecutor",
    "StrategyRunner",
    "StrategyService",
    "bar_from_kline_row",
    "build_strategy",
    "strategy_type_of",
]
