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
6. **可复现**：`run_id` 为「引擎版本 + 标的 + 区间 + K 线摘要 + 策略配置 + 数量 + 成本 + 成交模型」
   的 sha256 指纹；相同输入 → 相同 run_id 与相同 `canonical_result()`。

不做（本阶段明确边界）
------------------------------------------------------------------
不做实盘、不做资金账户/杠杆/保证金、不做全市场批量回测、不做策略库/优化/AI；
正式数据库全程**只读**，所有写入落在独立临时运行库。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from backtest.metrics import compute_metrics
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

#: 成交模型
FILL_NEXT_OPEN: Final[str] = "next_bar_open"      # 默认：t 决策、t+1 开盘成交
FILL_SIGNAL_CLOSE: Final[str] = "signal_bar_close"  # 兼容：以信号所在 K 线收盘价成交
FILL_MODELS: Final[tuple[str, ...]] = (FILL_NEXT_OPEN, FILL_SIGNAL_CLOSE)

#: 引擎版本（进入 run_id 指纹；改动执行语义时必须递增）
ENGINE_VERSION: Final[str] = "stage028.1"

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
    spec: Mapping[str, Any]
    data: DataDiagnostics
    bars_processed: int
    signals_generated: int
    long_signals: int
    short_signals: int
    trades_executed: int
    unfilled_signals: int
    trades: tuple[Mapping[str, Any], ...]
    metrics: Mapping[str, Any]
    cost_totals: Mapping[str, Any]
    net_pnl: float
    diagnostics: Mapping[str, Any]
    run_db_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "engine_version": self.engine_version,
            "spec": dict(self.spec),
            "data": self.data.to_dict(),
            "bars_processed": self.bars_processed,
            "signals_generated": self.signals_generated,
            "long_signals": self.long_signals,
            "short_signals": self.short_signals,
            "trades_executed": self.trades_executed,
            "unfilled_signals": self.unfilled_signals,
            "trades": [dict(t) for t in self.trades],
            "metrics": dict(self.metrics),
            "cost_totals": dict(self.cost_totals),
            "net_pnl": self.net_pnl,
            "diagnostics": dict(self.diagnostics),
            "run_db_path": self.run_db_path,
        }

    def canonical_result(self) -> dict[str, Any]:
        """**确定性**结果子集（用于「相同输入 → 相同结果」比对；不含路径与墙钟字段）。"""
        return {
            "run_id": self.run_id,
            "bars_processed": self.bars_processed,
            "signals_generated": self.signals_generated,
            "trades_executed": self.trades_executed,
            "unfilled_signals": self.unfilled_signals,
            "trades": [dict(t) for t in self.trades],
            "metrics": dict(self.metrics),
            "cost_totals": dict(self.cost_totals),
            "net_pnl": self.net_pnl,
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


def _fingerprint(spec: RunSpec, diagnostics: DataDiagnostics) -> str:
    payload = {
        "engine": ENGINE_VERSION,
        "spec": spec.to_dict(),
        "bars": {"count": diagnostics.bars, "digest": diagnostics.digest},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "run-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
    bars = KlineRepository(source_db).list_klines(
        symbol_id, run_spec.timeframe, run_spec.start_timestamp, run_spec.end_timestamp
    )
    if not bars:
        raise RunError(
            f"标的 {run_spec.symbol!r} 在周期 {run_spec.timeframe!r} 与区间 "
            f"[{run_spec.start_timestamp}, {run_spec.end_timestamp}] 内没有 K 线数据"
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
    run_id = _fingerprint(run_spec, diagnostics)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if run_db_path is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="se_pipeline_")
        run_db = Path(temp_dir.name) / "run.db"
    else:
        run_db = resolve_database_path(run_db_path)

    try:
        report = _execute(
            run_spec, config, strategy, symbol_row, bars, diagnostics, run_id, run_db
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
                continue
            fill_timestamp = str(source_bars[index + 1]["timestamp"])
            base_price = float(source_bars[index + 1]["open"])
        else:
            fill_timestamp = bar.timestamp
            base_price = bar.close

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
                "direction": direction,
                "side": side,
                "quantity": result.quantity,
                "base_price": round(base_price, 4),
                "fill_price": result.price,
                "fee": round(fee, 2),
                "position_after": result.position_quantity,
                "signal_id": result.signal_id,
                "order_id": result.order_id,
                "trade_id": result.trade_id,
            }
        )

    trade_rows = TradeRepository(run_db).list_trades()
    metrics = compute_metrics(trade_rows)
    fee_total = round(sum(float(t["fee"]) for t in trades), 2)
    net_pnl = round(metrics.total_pnl - fee_total, 6)
    position = PositionRepository(run_db).get_position_by_symbol(run_symbol_id)

    diagnostics_block = {
        "engine_version": ENGINE_VERSION,
        "price_basis": "不复权（正式库 klines 口径）",
        "cost_source": "assumed_defaults（费率是假设值，非已核实官方费率）",
        "fill_model": spec.fill_model,
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
        "reproducibility": "相同 RunSpec + 相同行情摘要 → 相同 run_id 与 canonical_result()",
    }

    return RunReport(
        run_id=run_id,
        engine_version=ENGINE_VERSION,
        spec=spec.to_dict(),
        data=diagnostics,
        bars_processed=len(source_bars),
        signals_generated=generated,
        long_signals=long_signals,
        short_signals=short_signals,
        trades_executed=len(trades),
        unfilled_signals=unfilled,
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
        diagnostics=diagnostics_block,
        run_db_path=str(run_db),
    )


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
