"""量化系统最小运行闭环（Stage 028）。

一句话：**真实行情 → 数据校验 → 策略信号 → 逐 K 线执行（含成本）→ 订单/成交/持仓 →
交易记录 → 绩效统计 → 可复现的结构化运行结果**。

链路与复用（**不重复实现已有能力**）
------------------------------------------------------------------
    正式数据库（只读）            本模块                复用的既有组件
    klines / symbols        ─┐                       KlineRepository / SymbolRepository
                             ├─ 复制到临时运行库        database.initialize_database
    策略（来自 RunSpec）      ─┘                       StrategyDefinitionRepository / StrategyRepository
                                逐 K 线推进            strategies.base.BarWindow（物理上不含未来）
                                策略判断               strategies.runner.build_strategy
                                信号 / 订单 / 成交      execution.ExecutionEngine（信号→订单→成交→持仓）
                                成本                   pipeline.costs.ExecutionCosts
                                绩效                   backtest.metrics.compute_metrics
                                指纹 / 报告            本模块

执行正确性（硬约束，逐条可测）
------------------------------------------------------------------
1. **无未来数据**：每根 K 线构造的 BarWindow 只含「当前 + 之前」，策略看不到未来；
   策略返回的信号时间戳必须等于当前 K 线，否则 `RunError`（拒绝把信号挂到未来）。
2. **时点分层**：`signal_timestamp`（决策，bar t 收盘）→ `submit_timestamp`（下单，同上）→
   `fill_timestamp` / `fill_price`（成交）。
   默认成交模型 `next_bar_open`：**在 t 决策、在 t+1 的开盘价成交**（不使用 t 的收盘价，
   因为 t 的收盘价在决策时尚未可知）。仅当显式选择 `signal_bar_close` 才用同根收盘价成交。
3. **成本**：滑点作用于成交价，佣金/印花税计入成交 fee（详见 pipeline.costs，费率是**假设值**）。
4. **未知状态不静默转换**：报告 `diagnostics` 里逐条标注**未模拟**的约束
   （T+1 卖出、涨跌停/一字板、停牌、历史 ST 筛选），不把它们当作已满足的交易条件。
5. **失败要明确**：标的不存在、区间内无 K 线、K 线重复/乱序、价格非正 → `RunError`，
   不会返回"看似成功的空结果"。
6. **可复现（Stage 029 起区分三个指纹）**：
   `input_fingerprint` = 「引擎版本 + 标的/周期/区间 + 实际使用行情摘要 + 策略类型/配置/代码哈希
   + 数量 + 成本 + 成交模型 + 预热/边界配置」的 sha256；
   `run_id` = 运行身份（= 'run-' + input_fingerprint 前 16 位，兼容既有格式）；
   `result_fingerprint` = 规范化运行结果（逐笔成交 / 绩效 / 净盈亏 / 回撤）的 sha256。
   相同输入 → 相同三个指纹与相同 `canonical_result()`。
7. **预热与计分分离（Stage 029）**：`warmup_bars` 根「计分区间之前」的 K 线只用于策略预热
   （喂给策略构建历史），其信号**不产生订单/成交、也不计入计分区间绩效**；
   计分区间 = `[start_timestamp, end_timestamp]`。
8. **区间边界成交（Stage 029）**：`fill_beyond_end=True` 时，计分区间最后一根产生的信号
   用「区间之后第一根 K 线」的开盘价成交（该成交仍归属本区间，标记
   `fill_beyond_scored_range=True`），避免为边界处理方便而丢弃交易；区间之后无数据时如实记为未成交。

不做（本阶段明确边界）
------------------------------------------------------------------
不做实盘、不做资金账户/杠杆/保证金、不做全市场批量回测、不做策略库/优化/AI；
正式数据库全程**只读**，所有写入落在独立临时运行库。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from backtest.metrics import compute_metrics, realized_trades
from database import (
    KlineRepository,
    PositionRepository,
    SignalRepository,
    StrategyDefinitionRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
    resolve_database_path,
)
from database.validation import require_number, require_positive_int, require_choice
from execution import ExecutionEngine
from execution.engine import SIDE_BY_DIRECTION
from strategies.base import Bar, BarWindow, bar_from_kline_row, require_direction
from strategies.runner import DEFAULT_STRATEGIES, build_strategy

from .costs import ExecutionCosts
from .performance import summarize_performance

#: 成交模型
FILL_NEXT_OPEN: Final[str] = "next_bar_open"      # 默认：t 决策、t+1 开盘成交
FILL_SIGNAL_CLOSE: Final[str] = "signal_bar_close"  # 兼容：以信号所在 K 线收盘价成交
FILL_MODELS: Final[tuple[str, ...]] = (FILL_NEXT_OPEN, FILL_SIGNAL_CLOSE)

#: 引擎版本（进入 run_id 指纹；改动执行语义时必须递增）
ENGINE_VERSION: Final[str] = "stage029.1"

DEFAULT_STRATEGY_NAME: Final[str] = "adhoc"
DEFAULT_QUANTITY: Final[float] = 100.0


class RunError(RuntimeError):
    """运行的**明确失败**（缺数据 / 数据非法 / 未知策略等）；绝不返回空成功结果。"""


@dataclass(frozen=True)
class RunSpec:
    """一次运行的全部输入（可序列化 → 可复现）。"""

    symbol: str
    timeframe: str = "1d"
    start_timestamp: str | None = None
    end_timestamp: str | None = None
    strategy_type: str = "close_direction"
    strategy_config: Mapping[str, Any] = field(default_factory=dict)
    strategy_name: str = DEFAULT_STRATEGY_NAME
    strategy_version: int = 1
    quantity: float = DEFAULT_QUANTITY
    costs: ExecutionCosts = field(default_factory=ExecutionCosts)
    fill_model: str = FILL_NEXT_OPEN
    #: 计分区间之前用于策略预热的 K 线根数（只喂给策略，不计入计分区间绩效）
    warmup_bars: int = 0
    #: 是否允许计分区间最后一根信号用「区间之后第一根」的开盘价成交（默认不允许，保持旧语义）
    fill_beyond_end: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "strategy_type": self.strategy_type,
            "strategy_config": dict(self.strategy_config),
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "quantity": self.quantity,
            "costs": self.costs.to_dict(),
            "fill_model": self.fill_model,
            "warmup_bars": self.warmup_bars,
            "fill_beyond_end": self.fill_beyond_end,
        }


@dataclass(frozen=True)
class DataDiagnostics:
    """行情校验结果（进入报告，便于诊断）。"""

    symbol_id: int
    market: str
    code: str
    name: str
    bars: int
    first_timestamp: str | None
    last_timestamp: str | None
    duplicates: int
    out_of_order: int
    ohlc_violations: int
    non_positive_prices: int
    digest: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "market": self.market,
            "code": self.code,
            "name": self.name,
            "bars": self.bars,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "ohlc_violations": self.ohlc_violations,
            "non_positive_prices": self.non_positive_prices,
            "digest": self.digest,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class RunReport:
    """一次运行的**结构化结果**（含诊断与可复现指纹）。"""

    run_id: str
    engine_version: str
    #: 完整输入指纹（所有影响结果的显式输入 + 实际使用行情内容摘要）
    input_fingerprint: str
    #: 规范化运行结果指纹（逐笔成交 / 绩效 / 净盈亏 / 回撤）
    result_fingerprint: str
    #: 策略实现代码指纹（类全名 + 源码哈希）
    strategy_code_fingerprint: str
    spec: Mapping[str, Any]
    data: DataDiagnostics
    bars_processed: int
    warmup_bars: int
    scored_bars: int
    scored_first_timestamp: str | None
    scored_last_timestamp: str | None
    signals_generated: int
    long_signals: int
    short_signals: int
    trades_executed: int
    unfilled_signals: int
    unfilled_signal_timestamps: tuple[str, ...]
    trades: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any]
    cost_totals: Mapping[str, Any]
    net_pnl: float
    slippage_total: float
    max_drawdown: float
    performance: Mapping[str, Any]
    diagnostics: Mapping[str, Any]
    run_db_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "engine_version": self.engine_version,
            "input_fingerprint": self.input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "strategy_code_fingerprint": self.strategy_code_fingerprint,
            "spec": dict(self.spec),
            "data": self.data.to_dict(),
            "bars_processed": self.bars_processed,
            "warmup_bars": self.warmup_bars,
            "scored_bars": self.scored_bars,
            "scored_first_timestamp": self.scored_first_timestamp,
            "scored_last_timestamp": self.scored_last_timestamp,
            "signals_generated": self.signals_generated,
            "long_signals": self.long_signals,
            "short_signals": self.short_signals,
            "trades_executed": self.trades_executed,
            "unfilled_signals": self.unfilled_signals,
            "unfilled_signal_timestamps": list(self.unfilled_signal_timestamps),
            "trades": [dict(t) for t in self.trades],
            "metrics": dict(self.metrics),
            "cost_totals": dict(self.cost_totals),
            "net_pnl": self.net_pnl,
            "slippage_total": self.slippage_total,
            "max_drawdown": self.max_drawdown,
            "performance": dict(self.performance),
            "diagnostics": dict(self.diagnostics),
            "run_db_path": self.run_db_path,
        }

    def canonical_result(self) -> dict[str, Any]:
        """**确定性**结果子集（用于「相同输入 → 相同结果」比对；不含路径与墙钟字段）。"""
        return {
            "run_id": self.run_id,
            "input_fingerprint": self.input_fingerprint,
            "engine_version": self.engine_version,
            "scored_first_timestamp": self.scored_first_timestamp,
            "scored_last_timestamp": self.scored_last_timestamp,
            "bars_processed": self.bars_processed,
            "signals_generated": self.signals_generated,
            "long_signals": self.long_signals,
            "short_signals": self.short_signals,
            "trades_executed": self.trades_executed,
            "unfilled_signals": self.unfilled_signals,
            "unfilled_signal_timestamps": list(self.unfilled_signal_timestamps),
            "trades": [dict(t) for t in self.trades],
            "metrics": dict(self.metrics),
            "cost_totals": dict(self.cost_totals),
            "net_pnl": self.net_pnl,
            "slippage_total": self.slippage_total,
            "max_drawdown": self.max_drawdown,
            "final_position": dict(self.diagnostics.get("final_position", {})),
        }


# --------------------------------------------------------------------------- 校验
def _canonical_lines(bars: Sequence[Any]) -> list[str]:
    return [
        f"{row['timestamp']}|{row['open']}|{row['high']}|{row['low']}|{row['close']}|{row['volume']}|{row['amount']}"
        for row in bars
    ]


def _digest(bars: Sequence[Any]) -> str:
    payload = "\n".join(_canonical_lines(bars)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_bars(bars: Sequence[Any]) -> tuple[int, int, int, int, tuple[str, ...]]:
    """返回 (duplicates, out_of_order, ohlc_violations, non_positive_prices, warnings)。"""
    duplicates = out_of_order = ohlc_violations = non_positive = 0
    warnings: list[str] = []
    previous: str | None = None
    seen: set[str] = set()
    for row in bars:
        ts = str(row["timestamp"])
        if ts in seen:
            duplicates += 1
        seen.add(ts)
        if previous is not None and ts < previous:
            out_of_order += 1
        previous = ts

        o = float(row["open"]); h = float(row["high"]); low = float(row["low"]); c = float(row["close"])
        if min(o, h, low, c) <= 0:
            non_positive += 1
        if h < max(o, c) - 1e-9 or low > min(o, c) + 1e-9 or h < low - 1e-9:
            ohlc_violations += 1

    if duplicates:
        warnings.append(f"K 线存在 {duplicates} 个重复时间戳")
    if out_of_order:
        warnings.append(f"K 线存在 {out_of_order} 处时间乱序")
    if ohlc_violations:
        warnings.append(f"K 线存在 {ohlc_violations} 处 OHLC 约束异常")
    if non_positive:
        warnings.append(f"K 线存在 {non_positive} 处非正价格")
    return duplicates, out_of_order, ohlc_violations, non_positive, tuple(warnings)


def _normalize_timestamp(value: str | None, *, end: bool) -> str | None:
    """把 'YYYY-MM-DD' 归一为完整时间戳（起始 00:00:00 / 结束 23:59:59）；None 原样返回。"""
    if value is None:
        return None
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text} {'23:59:59' if end else '00:00:00'}"
    return text


def _normalize_for_json(value: Any) -> Any:
    """把值规范化为可确定性 JSON 序列化的形式（float 定点、inf/nan 转字符串）。"""
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return round(value, 10)
    if isinstance(value, Mapping):
        return {str(k): _normalize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_for_json(v) for v in value]
    return value


def _canonical_json(value: Any) -> str:
    """确定性 JSON（键排序、无多余空白、float 定点）——所有指纹统一用它。"""
    return json.dumps(
        _normalize_for_json(value), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _strategy_code_fingerprint(strategy: Any) -> str:
    """策略实现的代码指纹（类全名 + 源码）；源码不可得时退化到类全名并显式标注。"""
    cls = type(strategy)
    parts = [f"{getattr(cls, '__module__', '?')}.{getattr(cls, '__qualname__', cls.__name__)}"]
    try:
        parts.append(inspect.getsource(cls))
    except (OSError, TypeError):
        parts.append("<source unavailable>")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _input_fingerprint(
    spec: RunSpec, strategy_code_fingerprint: str, diagnostics: DataDiagnostics
) -> str:
    """**完整输入指纹**：覆盖所有影响结果的显式输入 + 实际使用的行情内容摘要。

    注意：行情摘要只覆盖**实际使用**的 K 线（预热 + 计分 + 边界成交各一根），
    不混入区间之外的数据，否则历史运行会随新数据入库而改变指纹。
    """
    payload = {
        "engine": ENGINE_VERSION,
        "spec": spec.to_dict(),
        "strategy_code": strategy_code_fingerprint,
        "data": {
            "symbol_id": diagnostics.symbol_id,
            "market": diagnostics.market,
            "code": diagnostics.code,
            "timeframe": spec.timeframe,
            "bars_used": diagnostics.bars,
            "first_timestamp": diagnostics.first_timestamp,
            "last_timestamp": diagnostics.last_timestamp,
            "digest": diagnostics.digest,
        },
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _result_payload(report: RunReport) -> dict[str, Any]:
    """规范化运行结果（结果指纹的输入；不含路径 / 墙钟 / 运行身份）。"""
    return {
        "engine_version": report.engine_version,
        "scored_first_timestamp": report.scored_first_timestamp,
        "scored_last_timestamp": report.scored_last_timestamp,
        "bars_processed": report.bars_processed,
        "signals_generated": report.signals_generated,
        "long_signals": report.long_signals,
        "short_signals": report.short_signals,
        "trades_executed": report.trades_executed,
        "unfilled_signals": report.unfilled_signals,
        "unfilled_signal_timestamps": list(report.unfilled_signal_timestamps),
        "trades": [dict(t) for t in report.trades],
        "metrics": dict(report.metrics),
        "cost_totals": dict(report.cost_totals),
        "net_pnl": report.net_pnl,
        "slippage_total": report.slippage_total,
        "max_drawdown": report.max_drawdown,
        "final_position": dict(report.diagnostics.get("final_position", {})),
    }


def _result_fingerprint(report: RunReport) -> str:
    return "res-" + hashlib.sha256(
        _canonical_json(_result_payload(report)).encode("utf-8")
    ).hexdigest()[:16]


def _select_bars(
    all_bars: Sequence[Any],
    start_timestamp: str | None,
    end_timestamp: str | None,
    warmup_bars: int,
    fill_beyond_end: bool,
    symbol: str,
    timeframe: str,
) -> tuple[list[Any], int, str, str]:
    """按「预热 + 计分 + 边界成交」切出**实际使用**的 K 线。

    返回 (used_bars, scored_count, scored_first_timestamp, scored_last_timestamp)。
    计分区间 = [start_timestamp, end_timestamp]（闭区间；None 表示不限）。
    """
    if not all_bars:
        raise RunError(f"标的 {symbol!r} 在周期 {timeframe!r} 内没有 K 线数据")
    first_scored = last_scored = None
    for index, row in enumerate(all_bars):
        ts = str(row["timestamp"])
        if (start_timestamp is None or ts >= start_timestamp) and (
            end_timestamp is None or ts <= end_timestamp
        ):
            if first_scored is None:
                first_scored = index
            last_scored = index
    if first_scored is None or last_scored is None:
        raise RunError(
            f"标的 {symbol!r} 在周期 {timeframe!r} 与区间 "
            f"[{start_timestamp}, {end_timestamp}] 内没有 K 线数据"
        )
    warm_start = max(0, first_scored - warmup_bars)
    used_end = last_scored
    if fill_beyond_end and last_scored + 1 < len(all_bars):
        used_end = last_scored + 1
    used = list(all_bars[warm_start : used_end + 1])
    return (
        used,
        last_scored - first_scored + 1,
        str(all_bars[first_scored]["timestamp"]),
        str(all_bars[last_scored]["timestamp"]),
    )


def _calendar_diagnostics() -> str:
    """交易日历是否可用于缺口诊断（不可用就如实说明，不假装已校验）。"""
    try:
        from market import trading_calendar
    except Exception:  # noqa: BLE001
        return "不可用（无法导入 market.trading_calendar）"
    path = getattr(trading_calendar, "DEFAULT_CACHE_PATH", None)
    if path is not None and Path(path).exists():
        return f"可用（缓存 {path}）"
    return "不可用（未生成交易日历缓存）→ 未做交易日缺口校验"


# --------------------------------------------------------------------------- 主流程
def run_pipeline(
    spec: RunSpec,
    *,
    source_db_path: str | Path | None = None,
    run_db_path: str | Path | None = None,
    strategies: Mapping[str, type[Any]] | None = None,
) -> RunReport:
    """跑一次完整的运行闭环，返回结构化报告；失败抛 `RunError`。

    参数：
        spec            运行输入（标的 / 区间 / 策略 / 数量 / 成本 / 成交模型）
        source_db_path  行情来源（默认正式数据库，**只读**）
        run_db_path     运行库路径；默认自动建临时库并在返回前清理
        strategies      策略注册表（默认内置 close_direction）
    """
    if spec.fill_model not in FILL_MODELS:
        raise RunError(f"未知成交模型 {spec.fill_model!r}，可选 {list(FILL_MODELS)}")
    quantity = require_number(spec.quantity, "quantity")
    if quantity <= 0:
        raise RunError(f"成交数量必须为正数，实际为 {quantity!r}")
    warmup_bars = spec.warmup_bars
    if isinstance(warmup_bars, bool) or not isinstance(warmup_bars, int) or warmup_bars < 0:
        raise RunError(f"warmup_bars 必须是非负整数，实际为 {warmup_bars!r}")
    if not isinstance(spec.fill_beyond_end, bool):
        raise RunError(f"fill_beyond_end 必须是布尔值，实际为 {spec.fill_beyond_end!r}")
    run_spec = RunSpec(
        symbol=spec.symbol,
        timeframe=spec.timeframe,
        start_timestamp=_normalize_timestamp(spec.start_timestamp, end=False),
        end_timestamp=_normalize_timestamp(spec.end_timestamp, end=True),
        strategy_type=spec.strategy_type,
        strategy_config=dict(spec.strategy_config),
        strategy_name=spec.strategy_name,
        strategy_version=int(spec.strategy_version),
        quantity=quantity,
        costs=spec.costs,
        fill_model=spec.fill_model,
        warmup_bars=warmup_bars,
        fill_beyond_end=spec.fill_beyond_end,
    )
    config = {"type": run_spec.strategy_type, **dict(run_spec.strategy_config)}
    registry = dict(DEFAULT_STRATEGIES if strategies is None else strategies)
    strategy = build_strategy(config, registry)  # 未知策略 → ValueError（明确失败）

    source_db = resolve_database_path(source_db_path)
    symbols = SymbolRepository(source_db)
    symbol_row = None
    for market in ("CN", "HK", "US"):
        symbol_row = symbols.get_symbol(market, run_spec.symbol)
        if symbol_row is not None:
            break
    if symbol_row is None:
        raise RunError(f"正式库中不存在标的: {run_spec.symbol!r}")

    symbol_id = int(symbol_row["id"])
    # 读取该标的全部 K 线（升序）后按「预热 + 计分 + 边界成交」切片；
    # 只对**实际使用**的 K 线做校验与摘要（不把未使用的数据混进结果/指纹）。
    all_bars = KlineRepository(source_db).list_klines(symbol_id, run_spec.timeframe, None, None)
    bars, scored_count, scored_first, scored_last = _select_bars(
        all_bars,
        run_spec.start_timestamp,
        run_spec.end_timestamp,
        run_spec.warmup_bars,
        run_spec.fill_beyond_end,
        run_spec.symbol,
        run_spec.timeframe,
    )

    duplicates, out_of_order, ohlc_violations, non_positive, warnings = _validate_bars(bars)
    if duplicates:
        raise RunError(f"K 线数据非法：存在 {duplicates} 个重复时间戳")
    if out_of_order:
        raise RunError(f"K 线数据非法：存在 {out_of_order} 处时间乱序")
    if non_positive:
        raise RunError(f"K 线数据非法：存在 {non_positive} 处非正价格")

    diagnostics = DataDiagnostics(
        symbol_id=symbol_id,
        market=str(symbol_row["market"]),
        code=str(symbol_row["symbol"]),
        name=str(symbol_row["name"]),
        bars=len(bars),
        first_timestamp=str(bars[0]["timestamp"]),
        last_timestamp=str(bars[-1]["timestamp"]),
        duplicates=duplicates,
        out_of_order=out_of_order,
        ohlc_violations=ohlc_violations,
        non_positive_prices=non_positive,
        digest=_digest(bars),
        warnings=warnings,
    )
    strategy_code_fingerprint = _strategy_code_fingerprint(strategy)
    input_fingerprint = _input_fingerprint(run_spec, strategy_code_fingerprint, diagnostics)
    run_id = "run-" + input_fingerprint[:16]

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if run_db_path is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="se_pipeline_")
        run_db = Path(temp_dir.name) / "run.db"
    else:
        run_db = resolve_database_path(run_db_path)

    try:
        report = _execute(
            run_spec,
            config,
            strategy,
            symbol_row,
            bars,
            diagnostics,
            run_id,
            input_fingerprint,
            strategy_code_fingerprint,
            scored_count,
            scored_first,
            scored_last,
            run_db,
        )
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return report


def _execute(
    spec: RunSpec,
    config: Mapping[str, Any],
    strategy: Any,
    symbol_row: Any,
    source_bars: Sequence[Any],
    diagnostics: DataDiagnostics,
    run_id: str,
    input_fingerprint: str,
    strategy_code_fingerprint: str,
    scored_count: int,
    scored_first_timestamp: str | None,
    scored_last_timestamp: str | None,
    run_db: Path,
) -> RunReport:
    """在运行库里物化策略/标的/行情，然后逐根 K 线推进并落库。"""
    initialize_database(run_db)
    definitions = StrategyDefinitionRepository(run_db)
    strategies_repo = StrategyRepository(run_db)
    symbols_repo = SymbolRepository(run_db)
    klines_repo = KlineRepository(run_db)

    definition_id = definitions.ensure_definition(spec.strategy_name, description="Stage 028 adhoc run")
    existing_version = strategies_repo.get_version_by_number(definition_id, spec.strategy_version)
    version_id = (
        int(existing_version["id"])
        if existing_version is not None
        else strategies_repo.create_version(
            definition_id, config, version=spec.strategy_version, description="Stage 028 adhoc run"
        )
    )
    existing_symbol = symbols_repo.get_symbol(str(symbol_row["market"]), str(symbol_row["symbol"]))
    run_symbol_id = (
        int(existing_symbol["id"])
        if existing_symbol is not None
        else symbols_repo.create_symbol(
            str(symbol_row["symbol"]),
            str(symbol_row["name"]),
            str(symbol_row["market"]),
            str(symbol_row["asset_type"]),
            is_active=int(symbol_row["is_active"]),
        )
    )
    klines_repo.bulk_upsert_klines(
        [
            {
                "symbol_id": run_symbol_id,
                "timeframe": spec.timeframe,
                "timestamp": row["timestamp"],
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
            }
            for row in source_bars
        ]
    )

    signals = SignalRepository(run_db)
    execution = ExecutionEngine(run_db)

    trades: list[dict[str, Any]] = []
    unfilled_timestamps: list[str] = []
    history: tuple[Bar, ...] = ()
    generated = long_signals = short_signals = unfilled = 0

    for index, row in enumerate(source_bars):
        bar = bar_from_kline_row(row)
        window = BarWindow(current=bar, history=history)
        candidate = strategy.on_bar(window, config)
        history = history + (bar,)
        if candidate is None:
            continue

        if candidate.timestamp != bar.timestamp:
            raise RunError(
                f"策略返回的信号时间戳 {candidate.timestamp!r} 与当前 K 线 {bar.timestamp!r} 不一致："
                "禁止把信号挂到未来（未来数据泄漏）"
            )
        # 预热（计分区间之前）或边界成交（计分区间之后）那根上的信号不参与计分：
        # 只用于构建历史，不产生订单/成交，也不计入绩效（避免把区间外决策算进来）。
        if (scored_first_timestamp is not None and bar.timestamp < scored_first_timestamp) or (
            scored_last_timestamp is not None and bar.timestamp > scored_last_timestamp
        ):
            continue
        direction = require_direction(candidate.direction)
        side = SIDE_BY_DIRECTION[direction]
        signal_id = signals.create_signal(
            version_id,
            run_symbol_id,
            spec.timeframe,
            bar.timestamp,
            direction,
            require_number(candidate.price, "price"),
            reason=candidate.reason,
        )
        generated += 1
        long_signals += 1 if direction == "long" else 0
        short_signals += 1 if direction == "short" else 0

        # ---- 成交：默认 t 决策、t+1 开盘成交（不使用决策时不可知的 t 收盘价）
        if spec.fill_model == FILL_NEXT_OPEN:
            if index + 1 >= len(source_bars):
                unfilled += 1  # 最后一根产生的信号没有下一根可成交 → 明确记为未成交
                unfilled_timestamps.append(bar.timestamp)
                continue
            fill_timestamp = str(source_bars[index + 1]["timestamp"])
            base_price = float(source_bars[index + 1]["open"])
        else:
            fill_timestamp = bar.timestamp
            base_price = bar.close

        # 成交落在计分区间之后（fill_beyond_end 拉进来的那一根）→ 显式标记，不重复计入
        fill_beyond_scored = (
            scored_last_timestamp is not None and fill_timestamp > scored_last_timestamp
        )
        fill_price = spec.costs.fill_price(side, base_price)
        fee = spec.costs.fee(side, fill_price, spec.quantity)
        result = execution.execute_signal(
            signal_id, spec.quantity, price=fill_price, fee=fee, timestamp=fill_timestamp
        )
        trades.append(
            {
                "signal_timestamp": bar.timestamp,       # 决策 / 下单时点
                "submit_timestamp": bar.timestamp,       # 订单提交时点
                "fill_timestamp": fill_timestamp,
                "fill_beyond_scored_range": fill_beyond_scored,
                "direction": direction,
                "side": side,
                "quantity": result.quantity,
                "base_price": round(base_price, 4),
                "fill_price": result.price,
                "fee": round(fee, 2),
                "pnl": 0.0,  # 稍后按成交序列回填已实现盈亏
                "position_after": result.position_quantity,
                "signal_id": result.signal_id,
                "order_id": result.order_id,
                "trade_id": result.trade_id,
            }
        )

    trade_rows = TradeRepository(run_db).list_trades()
    metrics = compute_metrics(trade_rows)
    events = realized_trades(trade_rows)
    if len(events) != len(trades):
        raise RunError(
            f"成交记录数与明细数不一致：明细 {len(trades)}，落库 {len(events)}（拒绝伪造成功结果）"
        )
    for trade, event in zip(trades, events):
        trade["pnl"] = round(event.pnl, 6)  # 逐笔已实现盈亏（用于净值/回撤，不改动成交语义）
    fee_total = round(sum(float(t["fee"]) for t in trades), 2)
    slippage_total = round(
        sum(
            abs(float(t["fill_price"]) - float(t["base_price"])) * float(t["quantity"])
            for t in trades
        ),
        2,
    )
    net_pnl = round(metrics.total_pnl - fee_total, 6)
    performance = summarize_performance(
        trades, fee_total=fee_total, net_pnl=net_pnl, initial_capital=None
    )
    position = PositionRepository(run_db).get_position_by_symbol(run_symbol_id)

    diagnostics_block = {
        "engine_version": ENGINE_VERSION,
        "price_basis": "不复权（正式库 klines 口径）",
        "cost_source": "assumed_defaults（费率是假设值，非已核实官方费率）",
        "fill_model": spec.fill_model,
        "input_fingerprint": input_fingerprint,
        "strategy_code_fingerprint": strategy_code_fingerprint,
        "scored_range": [scored_first_timestamp, scored_last_timestamp],
        "warmup_bars": int(spec.warmup_bars),
        "fill_beyond_end": spec.fill_beyond_end,
        "order_submit_time_note": (
            "orders.created_at 由执行器写为墙钟时间；本报告的 submit/fill 时点用 K 线时间，"
            "墙钟字段不参与确定性比对"
        ),
        "trading_calendar": _calendar_diagnostics(),
        "unmodeled_constraints": [
            "T+1 卖出限制未模拟",
            "涨跌停价 / 一字板不可成交未模拟",
            "停牌（当日无 K 线）未模拟：缺失交易日不补、不插值",
            "历史 ST / 上市阶段状态未用于过滤（历史 ST 生效区间尚不可靠）",
        ],
        "unknown_states_not_converted": True,
        "final_position": {
            "quantity": float(position["quantity"]) if position is not None else 0.0,
            "avg_price": float(position["avg_price"]) if position is not None else 0.0,
        },
        "data_warnings": list(diagnostics.warnings),
        "reproducibility": (
            "相同 RunSpec + 相同行情摘要 + 相同策略代码 → 相同 input_fingerprint / run_id / "
            "result_fingerprint 与 canonical_result()"
        ),
    }

    report = RunReport(
        run_id=run_id,
        engine_version=ENGINE_VERSION,
        input_fingerprint=input_fingerprint,
        result_fingerprint="",  # 占位，按规范化结果回填（避免自引用）
        strategy_code_fingerprint=strategy_code_fingerprint,
        spec=spec.to_dict(),
        data=diagnostics,
        bars_processed=scored_count,
        warmup_bars=int(spec.warmup_bars),
        scored_bars=scored_count,
        scored_first_timestamp=scored_first_timestamp,
        scored_last_timestamp=scored_last_timestamp,
        signals_generated=generated,
        long_signals=long_signals,
        short_signals=short_signals,
        trades_executed=len(trades),
        unfilled_signals=unfilled,
        unfilled_signal_timestamps=tuple(unfilled_timestamps),
        trades=tuple(trades),
        metrics={
            "total_trades": metrics.total_trades,
            "buy_trades": metrics.buy_trades,
            "sell_trades": metrics.sell_trades,
            "winning_trades": metrics.winning_trades,
            "losing_trades": metrics.losing_trades,
            "win_rate": metrics.win_rate,
            "average_profit": metrics.average_profit,
            "average_loss": metrics.average_loss,
            "profit_factor": metrics.profit_factor,
            "gross_realized_pnl": metrics.total_pnl,
        },
        cost_totals={"fee_total": fee_total},
        net_pnl=net_pnl,
        slippage_total=slippage_total,
        max_drawdown=performance.max_drawdown,
        performance=performance.to_dict(),
        diagnostics=diagnostics_block,
        run_db_path=str(run_db),
    )
    return replace(report, result_fingerprint=_result_fingerprint(report))


__all__ = [
    "DEFAULT_QUANTITY",
    "ENGINE_VERSION",
    "FILL_MODELS",
    "FILL_NEXT_OPEN",
    "FILL_SIGNAL_CLOSE",
    "DataDiagnostics",
    "RunError",
    "RunReport",
    "RunSpec",
    "run_pipeline",
]
