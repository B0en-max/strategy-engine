"""最小运行闭环（Stage 028）+ 确定性复盘与样本外验证（Stage 029）+ 多标的组合（Stage 030）。

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

    SymbolSpec         组合中单个标的的输入（区间 / 策略 / 执行参数）
    PortfolioPolicy    组合执行策略（成交模型 / 是否允许做空 / 持仓上限）
    PortfolioSpec      组合输入（标的列表 + 初始资金 + 成本 + 策略）
    run_portfolio()    多标的共享资金组合闭环，返回 PortfolioReport
    PortfolioReport    组合报告（共享账户权益 / 逐标的 / 逐笔 / 权益曲线 / 指纹）

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
from .portfolio import (
    ALLOCATION_RULE,
    PORTFOLIO_ENGINE_VERSION,
    PortfolioPolicy,
    PortfolioReport,
    PortfolioSpec,
    SymbolSpec,
    run_portfolio,
)
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
    "PORTFOLIO_ENGINE_VERSION",
    "SEGMENT_NAMES",
    "SEGMENT_TEST",
    "SEGMENT_TRAIN",
    "SEGMENT_VALIDATION",
    "DataDiagnostics",
    "ExecutionCosts",
    "PerformanceSummary",
    "PortfolioPolicy",
    "PortfolioReport",
    "PortfolioSpec",
    "RunError",
    "RunReport",
    "RunSpec",
    "Segment",
    "SegmentResult",
    "SymbolSpec",
    "ValidationReport",
    "WalkForwardSpec",
    "reset_test_access_log",
    "run_pipeline",
    "run_portfolio",
    "run_walk_forward",
    "summarize_performance",
]
