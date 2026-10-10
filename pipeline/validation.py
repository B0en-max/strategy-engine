"""时间序列样本外验证（Stage 029）：训练 / 验证 / 测试切分 + 策略冻结记录。

定位
------------------------------------------------------------------
- 在 Stage 028 的最小闭环（`pipeline.loop`）之上，按**时间顺序**切出训练 / 验证 / 测试区间，
  逐区间跑**同一个** RunSpec（策略、参数、成本、成交模型完全一致），产出可对比的报告。
- **不做参数搜索 / 不做自动优化 / 不训练 AI / 不做多标的大规模回测**：
  本模块只负责「按区间跑 → 汇总 → 明确报警」。

切分与边界（硬约束）
------------------------------------------------------------------
- 训练 / 验证 / 测试的目标 K 线区间**互不重叠**且按时间升序；区间重叠直接报错，不静默调整。
- 预热：每个区间允许读取其起点之前的 `warmup_bars` 根 K 线用于策略预热；预热区间的信号
  **不产生订单/成交、不计入该区间绩效**（由 `pipeline.loop` 保证，见其 `_select_bars`）。
- 边界成交：`fill_beyond_end=True` 时，区间最后一根的信号用「区间之后第一根」成交，
  成交归属本区间但标记 `fill_beyond_scored_range=True`；**不丢弃、不重复计入**。
- 任何时点只看得到当时已可获得的数据（`BarWindow` 物理隔离，Stage 019/028 已保证）。
- 测试区间只应在策略与参数**冻结后**用于最终评估；本模块用**进程内**访问日志做
  「同一测试区间被不同参数重复评估」的尽力提醒（不是安全边界，也不落盘）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Mapping

from .loop import RunReport, RunSpec, _canonical_json, run_pipeline
from .performance import PerformanceSummary, summarize_performance

PLAN_VERSION: str = "stage029.1"

SEGMENT_TRAIN: str = "train"
SEGMENT_VALIDATION: str = "validation"
SEGMENT_TEST: str = "test"
SEGMENT_NAMES: tuple[str, ...] = (SEGMENT_TRAIN, SEGMENT_VALIDATION, SEGMENT_TEST)

DEFAULT_MIN_SCORED_BARS: int = 30
DEFAULT_MIN_TRADES: int = 20

EVIDENCE_OK: str = "ok"
EVIDENCE_LIMITED: str = "limited"
EVIDENCE_INSUFFICIENT: str = "insufficient"

#: 进程内「测试区间访问日志」：key=(symbol, timeframe, start, end) → 评估身份集合
_TEST_ACCESS_LOG: dict[tuple[str, str, str, str], set[str]] = {}


def reset_test_access_log() -> None:
    """清空进程内测试区间访问日志（仅测试使用）。"""
    _TEST_ACCESS_LOG.clear()


def _full_timestamp(value: str, *, end: bool) -> str:
    """把 'YYYY-MM-DD' 归一为完整时间戳（起始 00:00:00 / 结束 23:59:59）。"""
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text} {'23:59:59' if end else '00:00:00'}"
    return text


@dataclass(frozen=True)
class Segment:
    """一个评估区间（按 K 线时间，闭区间）。"""

    name: str
    start_timestamp: str
    end_timestamp: str

    def __post_init__(self) -> None:
        if self.name not in SEGMENT_NAMES:
            raise ValueError(
                f"segment.name 必须是 {list(SEGMENT_NAMES)} 之一，实际为 {self.name!r}"
            )
        start = _full_timestamp(self.start_timestamp, end=False)
        end = _full_timestamp(self.end_timestamp, end=True)
        if start > end:
            raise ValueError(f"segment 起点晚于终点：{start!r} > {end!r}")
        object.__setattr__(self, "start_timestamp", start)
        object.__setattr__(self, "end_timestamp", end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
        }


@dataclass(frozen=True)
class WalkForwardSpec:
    """一次样本外验证的全部输入（可序列化 → 可冻结）。"""

    #: 基准运行输入（标的 / 周期 / 策略 / 参数 / 成本 / 数量 / 成交模型）；区间按 segment 覆盖
    base_spec: RunSpec
    segments: tuple[Segment, ...]
    #: 每个区间允许预热的历史 K 线根数（只喂策略，不计入绩效）
    warmup_bars: int = 0
    #: 是否允许区间末尾信号用「区间之后第一根」成交（默认允许，避免边界丢交易）
    fill_beyond_end: bool = True
    #: 初始资金；提供时才计算收益率与回撤百分比
    initial_capital: float | None = None
    #: 样本量门槛：低于则标记「证据不足」
    min_scored_bars: int = DEFAULT_MIN_SCORED_BARS
    min_trades: int = DEFAULT_MIN_TRADES
    plan_version: str = PLAN_VERSION


@dataclass(frozen=True)
class SegmentResult:
    """单个区间的结果（运行报告 + 补充绩效 + 告警）。"""

    segment: Segment
    report: RunReport
    performance: PerformanceSummary
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        report = self.report
        return {
            "name": self.segment.name,
            "start_timestamp": self.segment.start_timestamp,
            "end_timestamp": self.segment.end_timestamp,
            "input_fingerprint": report.input_fingerprint,
            "result_fingerprint": report.result_fingerprint,
            "scored_first_timestamp": report.scored_first_timestamp,
            "scored_last_timestamp": report.scored_last_timestamp,
            "scored_bars": report.bars_processed,
            "warmup_bars": report.warmup_bars,
            "signals_generated": report.signals_generated,
            "trades_executed": report.trades_executed,
            "unfilled_signals": report.unfilled_signals,
            "net_pnl": report.net_pnl,
            "return_pct": self.performance.return_pct,
            "max_drawdown": self.performance.max_drawdown,
            "win_rate": report.metrics.get("win_rate"),
            "profit_factor": report.metrics.get("profit_factor"),
            "data_warnings": list(report.data.warnings),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ValidationReport:
    """一次样本外验证的完整报告。"""

    plan_version: str
    plan_fingerprint: str
    symbol: str
    timeframe: str
    strategy_code_fingerprint: str
    segments: tuple[SegmentResult, ...]
    comparison: Mapping[str, Any]
    #: 证据质量：ok / limited / insufficient（收益为正 ≠ 策略有效）
    evidence_quality: str
    warnings: tuple[str, ...]
    test_freeze: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "plan_fingerprint": self.plan_fingerprint,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "strategy_code_fingerprint": self.strategy_code_fingerprint,
            "segments": [segment.to_dict() for segment in self.segments],
            "comparison": dict(self.comparison),
            "evidence_quality": self.evidence_quality,
            "warnings": list(self.warnings),
            "test_freeze": dict(self.test_freeze),
        }


# --------------------------------------------------------------------------- 校验 / 工具
def _ordered_segments(segments: tuple[Segment, ...]) -> tuple[Segment, ...]:
    if not segments:
        raise ValueError("至少需要一个 segment")
    names = [segment.name for segment in segments]
    if len(set(names)) != len(names):
        raise ValueError(f"segment 名称重复：{names}")
    ordered = tuple(sorted(segments, key=lambda segment: segment.start_timestamp))
    for left, right in zip(ordered, ordered[1:]):
        if right.start_timestamp <= left.end_timestamp:
            raise ValueError(
                f"segment 区间重叠：{left.name} 到 {left.end_timestamp} 与 "
                f"{right.name} 从 {right.start_timestamp}（训练/验证/测试不得重叠）"
            )
    return ordered


def _config_fingerprint(spec: RunSpec) -> str:
    """策略参数 + 执行/成本配置指纹（用于测试冻结提醒）。"""
    payload = {
        "strategy_type": spec.strategy_type,
        "strategy_config": dict(spec.strategy_config),
        "fill_model": spec.fill_model,
        "costs": spec.costs.to_dict(),
        "quantity": spec.quantity,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]


def _record_test_access(
    symbol: str, timeframe: str, segment: Segment, identity: str
) -> dict[str, Any]:
    """记录一次测试区间评估，返回是否在**参数变更后**重复评估同一区间。"""
    key = (symbol, timeframe, segment.start_timestamp, segment.end_timestamp)
    bucket = _TEST_ACCESS_LOG.setdefault(key, set())
    changed = bool(bucket) and identity not in bucket
    bucket.add(identity)
    return {
        "test_range": [segment.start_timestamp, segment.end_timestamp],
        "distinct_param_sets": len(bucket),
        "params_changed_after_first_evaluation": changed,
    }


def _segment_warnings(
    report: RunReport,
    performance: PerformanceSummary,
    *,
    min_scored_bars: int,
    min_trades: int,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if report.bars_processed < min_scored_bars:
        warnings.append(
            f"计分区间过短：{report.bars_processed} 根 < {min_scored_bars} 根，统计不稳健"
        )
    if report.trades_executed < min_trades:
        warnings.append(
            f"交易样本不足：{report.trades_executed} 笔 < {min_trades} 笔，绩效不具代表性"
        )
    if report.data.warnings:
        warnings.append("数据质量告警：" + "；".join(report.data.warnings))
    if performance.return_pct is None:
        warnings.append("未提供初始资金 → 不计算收益率（避免用不一致的分母）")
    if report.unfilled_signals:
        warnings.append(
            f"有 {report.unfilled_signals} 条区间末尾信号无下一根可成交（已如实记为未成交）"
        )
    return tuple(warnings)


# --------------------------------------------------------------------------- 主流程
def run_walk_forward(
    spec: WalkForwardSpec,
    *,
    source_db_path: str | None = None,
    run_db_path: str | None = None,
) -> ValidationReport:
    """按时间顺序逐区间运行并汇总；区间重叠 / 输入非法直接抛 ValueError。

    参数：
        spec            验证输入（基准 RunSpec + 区间 + 预热 / 边界 / 初始资金 / 样本门槛）
        source_db_path  行情来源（默认正式库，只读）
        run_db_path     仅用于单区间调试；默认每区间各用独立临时库（自动清理）
    """
    segments = _ordered_segments(spec.segments)
    if spec.initial_capital is not None and float(spec.initial_capital) <= 0:
        raise ValueError(f"initial_capital 必须为正数，实际为 {spec.initial_capital!r}")
    if spec.warmup_bars < 0:
        raise ValueError(f"warmup_bars 不能为负数，实际为 {spec.warmup_bars!r}")

    base = spec.base_spec
    config_fingerprint = _config_fingerprint(base)

    results: list[SegmentResult] = []
    scored_ranges: list[tuple[str, str]] = []
    global_warnings: list[str] = []
    test_freeze: dict[str, Any] = {"evaluations": []}
    strategy_code_fingerprint = ""

    for segment in segments:
        segment_spec = replace(
            base,
            start_timestamp=segment.start_timestamp,
            end_timestamp=segment.end_timestamp,
            warmup_bars=spec.warmup_bars,
            fill_beyond_end=spec.fill_beyond_end,
        )
        report = run_pipeline(segment_spec, source_db_path=source_db_path, run_db_path=run_db_path)
        strategy_code_fingerprint = report.strategy_code_fingerprint
        performance = summarize_performance(
            list(report.trades),
            fee_total=float(report.cost_totals["fee_total"]),
            net_pnl=report.net_pnl,
            initial_capital=spec.initial_capital,
        )
        warnings = _segment_warnings(
            report,
            performance,
            min_scored_bars=spec.min_scored_bars,
            min_trades=spec.min_trades,
        )
        results.append(SegmentResult(segment, report, performance, warnings))
        scored_ranges.append(
            (str(report.scored_first_timestamp), str(report.scored_last_timestamp))
        )
        if segment.name == SEGMENT_TEST:
            identity = f"{strategy_code_fingerprint}:{config_fingerprint}"
            entry = _record_test_access(base.symbol, base.timeframe, segment, identity)
            test_freeze["evaluations"].append(entry)
            if entry["params_changed_after_first_evaluation"]:
                global_warnings.append(
                    "同一测试区间在**参数/配置变更后**被重复评估 → 该结果不再构成独立样本外测试"
                )

    # 防御性校验：实际计分区间不得重叠（按时间字符串比较）
    for i in range(len(scored_ranges)):
        for j in range(i + 1, len(scored_ranges)):
            a, b = scored_ranges[i], scored_ranges[j]
            if not (a[1] < b[0] or b[1] < a[0]):
                raise ValueError(f"实际计分区间重叠：{a} 与 {b}")

    insufficient = any(
        segment_result.report.bars_processed < spec.min_scored_bars
        or segment_result.report.trades_executed < spec.min_trades
        for segment_result in results
    )
    any_data_warning = any(segment_result.report.data.warnings for segment_result in results)
    if insufficient:
        evidence_quality = EVIDENCE_INSUFFICIENT
    elif any_data_warning or len(results) < 2:
        evidence_quality = EVIDENCE_LIMITED
    else:
        evidence_quality = EVIDENCE_OK

    if evidence_quality != EVIDENCE_OK:
        global_warnings.append(
            "证据质量 " + evidence_quality + "：样本/区间不足或数据有告警，**不得仅凭收益为正判定策略有效**"
        )
    if not any(segment_result.segment.name == SEGMENT_TEST for segment_result in results):
        global_warnings.append("未包含 test 区间 → 未做最终样本外评估")

    comparison = _build_comparison(results, spec)
    plan_fingerprint = _plan_fingerprint(spec, segments, strategy_code_fingerprint)

    return ValidationReport(
        plan_version=spec.plan_version,
        plan_fingerprint=plan_fingerprint,
        symbol=base.symbol,
        timeframe=base.timeframe,
        strategy_code_fingerprint=strategy_code_fingerprint,
        segments=tuple(results),
        comparison=comparison,
        evidence_quality=evidence_quality,
        warnings=tuple(global_warnings),
        test_freeze=test_freeze,
    )


def _build_comparison(
    results: list[SegmentResult], spec: WalkForwardSpec
) -> dict[str, Any]:
    """训练 / 验证 / 测试之间的结果对比。"""
    by_segment = {
        result.segment.name: {
            "scored_bars": result.report.bars_processed,
            "trades": result.report.trades_executed,
            "net_pnl": result.report.net_pnl,
            "return_pct": result.performance.return_pct,
            "max_drawdown": result.performance.max_drawdown,
            "win_rate": result.report.metrics.get("win_rate"),
            "profit_factor": result.report.metrics.get("profit_factor"),
        }
        for result in results
    }
    train = by_segment.get(SEGMENT_TRAIN)
    test = by_segment.get(SEGMENT_TEST)
    note = "区间之间结果差异不构成因果证据；样本量/区间不足时以证据质量为准"
    if train is not None and test is not None:
        note = (
            f"训练净盈亏 {train['net_pnl']} vs 测试净盈亏 {test['net_pnl']}；"
            "两者差异不构成因果证据"
        )
    return {
        "by_segment": by_segment,
        "note": note,
        "min_scored_bars": spec.min_scored_bars,
        "min_trades": spec.min_trades,
    }


def _plan_fingerprint(
    spec: WalkForwardSpec, segments: tuple[Segment, ...], strategy_code_fingerprint: str
) -> str:
    """验证方案冻结指纹（策略 / 参数 / 成本 / 执行 / 区间 / 方案版本）。"""
    base = spec.base_spec
    payload = {
        "plan_version": spec.plan_version,
        "symbol": base.symbol,
        "timeframe": base.timeframe,
        "strategy_type": base.strategy_type,
        "strategy_config": dict(base.strategy_config),
        "strategy_code_fingerprint": strategy_code_fingerprint,
        "costs": base.costs.to_dict(),
        "fill_model": base.fill_model,
        "quantity": base.quantity,
        "warmup_bars": spec.warmup_bars,
        "fill_beyond_end": spec.fill_beyond_end,
        "initial_capital": spec.initial_capital,
        "segments": [segment.to_dict() for segment in segments],
    }
    return "plan-" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "DEFAULT_MIN_SCORED_BARS",
    "DEFAULT_MIN_TRADES",
    "EVIDENCE_INSUFFICIENT",
    "EVIDENCE_LIMITED",
    "EVIDENCE_OK",
    "PLAN_VERSION",
    "SEGMENT_NAMES",
    "SEGMENT_TEST",
    "SEGMENT_TRAIN",
    "SEGMENT_VALIDATION",
    "Segment",
    "SegmentResult",
    "ValidationReport",
    "WalkForwardSpec",
    "reset_test_access_log",
    "run_walk_forward",
]
