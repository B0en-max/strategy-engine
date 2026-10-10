"""最小运行闭环（Stage 028）。

对外接口：

    RunSpec            一次运行的全部输入（标的 / 区间 / 策略 / 数量 / 成本 / 成交模型）
    run_pipeline()     跑完整闭环（行情→校验→信号→执行→成交→绩效），返回 RunReport
    RunReport          结构化运行结果（含诊断、指纹、canonical_result()）
    RunError           明确的失败类型（缺数据/数据非法/未知策略）
    ExecutionCosts     佣金 / 印花税 / 滑点

命令行入口：``python -m pipeline --help``
"""

from __future__ import annotations

from .costs import ExecutionCosts
from .loop import (
    DEFAULT_QUANTITY,
    ENGINE_VERSION,
    FILL_MODELS,
    FILL_NEXT_OPEN,
    FILL_SIGNAL_CLOSE,
    DataDiagnostics,
    RunError,
    RunReport,
    RunSpec,
    run_pipeline,
)

__all__ = [
    "DEFAULT_QUANTITY",
    "ENGINE_VERSION",
    "FILL_MODELS",
    "FILL_NEXT_OPEN",
    "FILL_SIGNAL_CLOSE",
    "DataDiagnostics",
    "ExecutionCosts",
    "RunError",
    "RunReport",
    "RunSpec",
    "run_pipeline",
]
