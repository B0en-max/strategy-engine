"""最小运行闭环（Stage 028）+ 确定性复盘与样本外验证（Stage 029）。

对外接口：

    RunSpec            一次运行的全部输入（标的 / 区间 / 策略 / 数量 / 成本 / 成交模型 / 预热 / 边界）
    run_pipeline()     跑完整闭环（行情→校验→信号→执行→成交→绩效），返回 RunReport
    RunReport          结构化运行结果（含 input/result 指纹、canonical_result()）
    RunError           明确的失败类型（缺数据/数据非法/未知策略）
    ExecutionCosts     佣金 / 印花税 / 滑点

    PerformanceSummary 补充绩效（净值曲线 / 最大回撤 / 收益率，口径显式）
    summarize_performance()
    Segment            评估区间（train / validation / test）
    WalkForwardSpec    样本外验证输入（基准 RunSpec + 区间 + 预热 / 边界 / 初始资金 / 样本门槛）
    run_walk_forward() 按时间顺序逐区间运行并汇总，返回 ValidationReport
    ValidationReport   样本外验证报告（逐区间 + 对比 + 证据质量 + 冻结提醒）

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
from .performance import PerformanceSummary, summarize_performance
from .validation import (
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_LIMITED,
    EVIDENCE_OK,
    PLAN_VERSION,
    SEGMENT_NAMES,
    SEGMENT_TEST,
    SEGMENT_TRAIN,
    SEGMENT_VALIDATION,
    Segment,
    SegmentResult,
    ValidationReport,
    WalkForwardSpec,
    reset_test_access_log,
    run_walk_forward,
)

__all__ = [
    "DEFAULT_QUANTITY",
    "ENGINE_VERSION",
    "EVIDENCE_INSUFFICIENT",
    "EVIDENCE_LIMITED",
    "EVIDENCE_OK",
    "FILL_MODELS",
    "FILL_NEXT_OPEN",
    "FILL_SIGNAL_CLOSE",
    "PLAN_VERSION",
    "SEGMENT_NAMES",
    "SEGMENT_TEST",
    "SEGMENT_TRAIN",
    "SEGMENT_VALIDATION",
    "DataDiagnostics",
    "ExecutionCosts",
    "PerformanceSummary",
    "RunError",
    "RunReport",
    "RunSpec",
    "Segment",
    "SegmentResult",
    "ValidationReport",
    "WalkForwardSpec",
    "reset_test_access_log",
    "run_pipeline",
    "run_walk_forward",
    "summarize_performance",
]
