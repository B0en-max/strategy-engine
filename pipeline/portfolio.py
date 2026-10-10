"""多标的共享资金组合执行闭环（Stage 030）。

定位
------------------------------------------------------------------
在 Stage 028/029 的**单标的**闭环之上，实现**单账户、单策略族、多标的**的组合事件循环：

    多标的行情 → 同步决策 → 统一订单管理 → 共享资金与持仓 → 成交与成本
    → 组合净值（含未实现盈亏）→ 组合绩效 → 确定性复盘

**不是**「循环跑多次单标的再相加」：所有标的共用**同一笔初始资金**，同一时点统一检查
资金与持仓、按固定规则排序成交，组合收益从统一账户的**权益变化**计算。

复用（不复制执行器、不另建平行交易账本）
------------------------------------------------------------------
    strategies.base.BarWindow / bar_from_kline_row    无未来数据窗口（物理隔离）
    strategies.runner.build_strategy                  按 config 构造策略
    execution.ExecutionEngine                         信号 → 订单 → 成交 → 持仓（落库）
    execution.engine.apply_trade_to_position           持仓数量 / 平均成本规则
    backtest.metrics.realized_trades / compute_metrics 逐笔已实现盈亏与绩效
    pipeline.costs.ExecutionCosts                      佣金 / 印花税 / 滑点
    pipeline.performance.summarize_performance         净值 / 回撤 / 收益率（已实现口径）
    pipeline.loop（_select_bars / _validate_bars / _digest / _canonical_json / _strategy_code_fingerprint）
    database.*（Kline/Symbol/Strategy/Signal/Trade Repository）

组合语义（硬约束，逐条可测）
------------------------------------------------------------------
1. **共享账户**：单一 `initial_capital`；每笔买入检查 `可用资金 >= 成交额 + 手续费`，
   绝不允许「每个标的各用全部初始资金」。卖出回收资金立即可被后续订单使用。
2. **同步决策 + 确定性执行**：按时间推进；同一时点先收齐各标的信号，再按**固定的资金
   分配规则**统一检查资金与持仓后成交（规则见 `ALLOCATION_RULE`，写入报告与指纹）。
3. **行情时间对齐**：时间轴 = 各标的**实际使用 K 线**时间戳的并集；某标的在某个时点
   没有 K 线 → 视为**没有行情**（不补齐、不插值、不当作价格不变可成交）。
   成交只用「该标的自己的下一根 K 线」，禁止使用未来行情。
4. **市场规则不硬编码**：成交限制 / 手续费 / 滑点 / 资金占用由显式配置
   （`ExecutionCosts` / `PortfolioPolicy`）决定；不把 A 股 T+1 / 涨跌停写死为通用规则。
   默认 `allow_short=False`（现金账户），超出持仓的卖出**明确拒绝**。
5. **未实现盈亏口径**：持有标的按「当前时点之前最近一根已发生 K 线的收盘价」估值
   （不使用未来价格、不插值）；该价格不是当前时点的 K 线时记为**陈旧估值告警**。
6. **失败要明确**：标的不存在、区间无行情、行情非法、标的重复、初始资金非正 → `RunError`；
   账实不一致同样抛 `RunError`，不返回伪成功的组合结果。

不做（本阶段边界）
------------------------------------------------------------------
不做多策略竞争 / 参数优化 / AI / 实盘；不做保证金、杠杆、融券、T+1、涨跌停、
停牌过滤；不做跨标的资产配置优化。正式数据库全程**只读**。
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Mapping

from backtest.metrics import BacktestMetrics, compute_metrics, realized_trades
from database import (
    KlineRepository,
    OrderRepository,
    SignalRepository,
    StrategyDefinitionRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
    resolve_database_path,
)
from execution import ExecutionEngine
from execution.engine import (
    ORDER_TYPE_MARKET,
    SIDE_BY_DIRECTION,
    apply_trade_to_position,
)
from strategies.base import Bar, BarWindow, bar_from_kline_row, require_direction
from strategies.runner import DEFAULT_STRATEGIES, build_strategy

from .costs import ExecutionCosts
from .loop import (
    DEFAULT_QUANTITY,
    ENGINE_VERSION,
    FILL_MODELS,
    FILL_NEXT_OPEN,
    DataDiagnostics,
    RunError,
    _canonical_json,
    _digest,
    _normalize_timestamp,
    _select_bars,
    _strategy_code_fingerprint,
    _validate_bars,
)
from .performance import summarize_performance

#: 组合引擎版本（改动组合执行语义时必须递增）
PORTFOLIO_ENGINE_VERSION: Final[str] = "stage030.1"

#: 资金分配规则（写入报告与指纹，保证「遍历顺序不同 → 结果不同」可解释）
ALLOCATION_RULE: Final[str] = (
    "同步决策：同一时点先收齐各标的信号，再按「标的代码升序」依次检查资金与持仓后成交；"
    "先到先得，可用资金/持仓不足即拒绝（不超额下单、不拆分成交）。"
)

QUANTITY_EPSILON: Final[float] = 1e-9

#: 拒绝原因
REJECT_INSUFFICIENT_CASH: Final[str] = "insufficient_cash"
REJECT_INSUFFICIENT_POSITION: Final[str] = "insufficient_position"
REJECT_POSITION_LIMIT: Final[str] = "position_limit"
UNFILLED_NO_NEXT_BAR: Final[str] = "no_next_bar"

#: 拒单对应的订单状态（orders 表既有取值；拒单必须落库，不能只改信号状态）
ORDER_STATUS_REJECTED: Final[str] = "rejected"


@dataclass(frozen=True)
class SymbolSpec:
    """组合中一个标的的全部输入（各自的数据区间 / 策略 / 执行参数）。"""

    symbol: str
    timeframe: str = "1d"
    start_timestamp: str | None = None
    end_timestamp: str | None = None
    #: 计分区间之前用于策略预热的 K 线根数（不计入绩效）
    warmup_bars: int = 0
    strategy_type: str = "close_direction"
    strategy_config: Mapping[str, Any] = field(default_factory=dict)
    strategy_name: str = "adhoc"
    strategy_version: int = 1
    #: 该标的每次成交数量
    quantity: float = DEFAULT_QUANTITY

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "warmup_bars": self.warmup_bars,
            "strategy_type": self.strategy_type,
            "strategy_config": dict(self.strategy_config),
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "quantity": self.quantity,
        }


@dataclass(frozen=True)
class PortfolioPolicy:
    """组合执行的显式策略（市场规则由配置决定，不硬编码）。"""

    #: 成交模型：next_bar_open（默认，t 决策 t+1 开盘）/ signal_bar_close
    fill_model: str = FILL_NEXT_OPEN
    #: 是否允许做空（默认现金账户：不允许裸卖空；超出持仓的卖出会被拒绝）
    allow_short: bool = False
    #: 是否允许计分区间末尾信号用「区间之后第一根」成交
    fill_beyond_end: bool = False
    #: 单一标的持仓市值上限（None = 不限）
    max_position_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_model": self.fill_model,
            "allow_short": self.allow_short,
            "fill_beyond_end": self.fill_beyond_end,
            "max_position_value": self.max_position_value,
        }


@dataclass(frozen=True)
class PortfolioSpec:
    """一次组合运行的全部输入。"""

    symbols: tuple[SymbolSpec, ...]
    initial_capital: float
    costs: ExecutionCosts = field(default_factory=ExecutionCosts)
    policy: PortfolioPolicy = field(default_factory=PortfolioPolicy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols": [s.to_dict() for s in self.symbols],
            "initial_capital": self.initial_capital,
            "costs": self.costs.to_dict(),
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True)
class PortfolioReport:
    """一次组合运行的结构化结果。"""

    run_id: str
    engine_version: str
    single_engine_version: str
    input_fingerprint: str
    result_fingerprint: str
    spec: Mapping[str, Any]
    data: Mapping[str, Any]
    summary: Mapping[str, Any]
    metrics: Mapping[str, Any]
    per_symbol: Mapping[str, Any]
    trades: tuple[Mapping[str, Any], ...]
    rejected_orders: tuple[Mapping[str, Any], ...]
    unfilled_signals: tuple[Mapping[str, Any], ...]
    equity_curve: tuple[tuple[str, float], ...]
    diagnostics: Mapping[str, Any]
    run_db_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "engine_version": self.engine_version,
            "single_engine_version": self.single_engine_version,
            "input_fingerprint": self.input_fingerprint,
            "result_fingerprint": self.result_fingerprint,
            "spec": dict(self.spec),
            "data": {k: dict(v) for k, v in self.data.items()},
            "summary": dict(self.summary),
            "metrics": dict(self.metrics),
            "per_symbol": {k: dict(v) for k, v in self.per_symbol.items()},
            "trades": [dict(t) for t in self.trades],
            "rejected_orders": [dict(t) for t in self.rejected_orders],
            "unfilled_signals": [dict(t) for t in self.unfilled_signals],
            "equity_curve": [[ts, eq] for ts, eq in self.equity_curve],
            "diagnostics": dict(self.diagnostics),
            "run_db_path": self.run_db_path,
        }

    def canonical_result(self) -> dict[str, Any]:
        """**确定性**结果子集（不含路径 / 墙钟字段）——用于「相同输入 → 相同结果」比对。"""
        return {
            "run_id": self.run_id,
            "input_fingerprint": self.input_fingerprint,
            "engine_version": self.engine_version,
            "summary": dict(self.summary),
            "metrics": dict(self.metrics),
            "per_symbol": {k: dict(v) for k, v in self.per_symbol.items()},
            "trades": [dict(t) for t in self.trades],
            "rejected_orders": [dict(t) for t in self.rejected_orders],
            "unfilled_signals": [dict(t) for t in self.unfilled_signals],
            "equity_curve": [[ts, eq] for ts, eq in self.equity_curve],
        }


# --------------------------------------------------------------------------- 内部结构
@dataclass
class _SymbolState:
    """单个标的在组合循环中的运行状态。"""

    spec: SymbolSpec
    symbol_row: Any
    config: Mapping[str, Any]
    run_symbol_id: int
    version_id: int
    bars: list[Any]
    bar_by_timestamp: dict[str, Any]
    bar_index: dict[str, int]
    scored_first: str
    scored_last: str
    strategy: Any
    history: tuple[Bar, ...]
    data: DataDiagnostics


@dataclass
class _PendingOrder:
    """等待成交的订单（延迟到「该标的的下一根 K 线」成交）。"""

    symbol: str
    side: str
    signal_id: int
    signal_timestamp: str
    fill_timestamp: str
    base_price: float
    quantity: float
    sequence: int


# --------------------------------------------------------------------------- 校验
def _require_positive_capital(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunError(f"initial_capital 必须是数字，实际为 {value!r}")
    capital = float(value)
    if capital <= 0:
        raise RunError(f"initial_capital 必须为正数，实际为 {capital!r}")
    return capital


def _validate_spec(spec: PortfolioSpec) -> PortfolioSpec:
    if spec.policy.fill_model not in FILL_MODELS:
        raise RunError(f"未知成交模型 {spec.policy.fill_model!r}，可选 {list(FILL_MODELS)}")
    if not spec.symbols:
        raise RunError("组合至少需要一个标的")
    codes = [s.symbol for s in spec.symbols]
    if len(set(codes)) != len(codes):
        raise RunError(f"组合中存在重复标的：{codes}")
    for symbol_spec in spec.symbols:
        if symbol_spec.warmup_bars < 0:
            raise RunError(f"[{symbol_spec.symbol}] warmup_bars 不能为负数")
        if isinstance(symbol_spec.quantity, bool) or not isinstance(
            symbol_spec.quantity, (int, float)
        ):
            raise RunError(f"[{symbol_spec.symbol}] quantity 必须是数字")
        if float(symbol_spec.quantity) <= 0:
            raise RunError(f"[{symbol_spec.symbol}] quantity 必须为正数")
    max_value = spec.policy.max_position_value
    if max_value is not None and float(max_value) <= 0:
        raise RunError(f"max_position_value 必须为正数，实际为 {max_value!r}")
    return PortfolioSpec(
        symbols=tuple(spec.symbols),
        initial_capital=_require_positive_capital(spec.initial_capital),
        costs=spec.costs,
        policy=spec.policy,
    )


def _resolve_symbol(source_db: Any, code: str) -> Any:
    symbols = SymbolRepository(source_db)
    for market in ("CN", "HK", "US"):
        row = symbols.get_symbol(market, code)
        if row is not None:
            return row
    raise RunError(f"正式库中不存在标的: {code!r}")


def _load_symbol_state(
    spec: SymbolSpec,
    source_db: Any,
    registry: Mapping[str, type[Any]],
    fill_beyond_end: bool,
) -> tuple[_SymbolState, str]:
    """读取某标的行情、构造策略、返回运行状态与策略代码指纹。"""
    symbol_row = _resolve_symbol(source_db, spec.symbol)
    symbol_id = int(symbol_row["id"])
    all_bars = KlineRepository(source_db).list_klines(symbol_id, spec.timeframe, None, None)
    start = _normalize_timestamp(spec.start_timestamp, end=False)
    end = _normalize_timestamp(spec.end_timestamp, end=True)
    bars, _scored_count, scored_first, scored_last = _select_bars(
        all_bars, start, end, spec.warmup_bars, fill_beyond_end, spec.symbol, spec.timeframe
    )
    duplicates, out_of_order, ohlc_violations, non_positive, warnings = _validate_bars(bars)
    if duplicates:
        raise RunError(f"[{spec.symbol}] K 线数据非法：存在 {duplicates} 个重复时间戳")
    if out_of_order:
        raise RunError(f"[{spec.symbol}] K 线数据非法：存在 {out_of_order} 处时间乱序")
    if non_positive:
        raise RunError(f"[{spec.symbol}] K 线数据非法：存在 {non_positive} 处非正价格")
    data = DataDiagnostics(
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
    config: Mapping[str, Any] = {"type": spec.strategy_type, **dict(spec.strategy_config)}
    strategy = build_strategy(config, registry)  # 未知策略 → ValueError（明确失败）
    state = _SymbolState(
        spec=spec,
        symbol_row=symbol_row,
        config=config,
        run_symbol_id=0,
        version_id=0,
        bars=list(bars),
        bar_by_timestamp={str(row["timestamp"]): row for row in bars},
        bar_index={str(row["timestamp"]): index for index, row in enumerate(bars)},
        scored_first=scored_first,
        scored_last=scored_last,
        strategy=strategy,
        history=(),
        data=data,
    )
    return state, _strategy_code_fingerprint(strategy)


def _input_fingerprint(spec: PortfolioSpec, symbol_payloads: list[Mapping[str, Any]]) -> str:
    payload = {
        "engine": PORTFOLIO_ENGINE_VERSION,
        "single_engine": ENGINE_VERSION,
        "initial_capital": spec.initial_capital,
        "costs": spec.costs.to_dict(),
        "policy": spec.policy.to_dict(),
        "allocation_rule": ALLOCATION_RULE,
        "symbols": symbol_payloads,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _result_fingerprint(report: PortfolioReport) -> str:
    return "pres-" + hashlib.sha256(
        _canonical_json(report.canonical_result()).encode("utf-8")
    ).hexdigest()[:16]


# --------------------------------------------------------------------------- 主流程
def run_portfolio(
    spec: PortfolioSpec,
    *,
    source_db_path: str | None = None,
    run_db_path: str | None = None,
    strategies: Mapping[str, type[Any]] | None = None,
) -> PortfolioReport:
    """跑一次多标的共享资金组合闭环，返回结构化报告；失败抛 `RunError`。

    参数：
        spec            组合输入（标的列表 / 初始资金 / 成本 / 执行策略）
        source_db_path  行情来源（默认正式库，**只读**）
        run_db_path     运行库路径（默认临时库，跑完清理）
        strategies      策略注册表（默认内置 close_direction）
    """
    spec = _validate_spec(spec)
    registry = dict(DEFAULT_STRATEGIES if strategies is None else strategies)
    source_db = resolve_database_path(source_db_path)

    states: dict[str, _SymbolState] = {}
    symbol_payloads: list[dict[str, Any]] = []
    for symbol_spec in spec.symbols:
        state, code_fingerprint = _load_symbol_state(
            symbol_spec, source_db, registry, spec.policy.fill_beyond_end
        )
        states[symbol_spec.symbol] = state
        payload = dict(symbol_spec.to_dict())
        payload["strategy_code_fingerprint"] = code_fingerprint
        payload["data"] = {
            "symbol_id": state.data.symbol_id,
            "market": state.data.market,
            "code": state.data.code,
            "timeframe": symbol_spec.timeframe,
            "bars_used": state.data.bars,
            "first_timestamp": state.data.first_timestamp,
            "last_timestamp": state.data.last_timestamp,
            "scored_first_timestamp": state.scored_first,
            "scored_last_timestamp": state.scored_last,
            "digest": state.data.digest,
        }
        symbol_payloads.append(payload)
    symbol_payloads.sort(key=lambda item: item["symbol"])
    input_fingerprint = _input_fingerprint(spec, symbol_payloads)
    run_id = "pf-" + input_fingerprint[:16]

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if run_db_path is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="se_portfolio_")
        run_db = Path(temp_dir.name) / "portfolio.db"
    else:
        run_db = resolve_database_path(run_db_path)

    try:
        report = _execute(spec, states, symbol_payloads, input_fingerprint, run_id, run_db)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    return report


def _materialize(run_db: Any, state: _SymbolState) -> None:
    """把该标的的策略 / 版本 / 标的 / K 线复制进运行库（复用既有 Repository）。"""
    definitions = StrategyDefinitionRepository(run_db)
    strategies_repo = StrategyRepository(run_db)
    symbols_repo = SymbolRepository(run_db)
    klines_repo = KlineRepository(run_db)

    # 同一 run 内不同标的的 (name, version) 必须唯一 → 名称带上标的代码
    definition_name = f"{state.spec.strategy_name}:{state.spec.symbol}"
    definition_id = definitions.ensure_definition(
        definition_name, description="Stage 030 portfolio"
    )
    existing_version = strategies_repo.get_version_by_number(
        definition_id, state.spec.strategy_version
    )
    state.version_id = (
        int(existing_version["id"])
        if existing_version is not None
        else strategies_repo.create_version(
            definition_id,
            state.config,
            version=state.spec.strategy_version,
            description="Stage 030 portfolio",
        )
    )
    row = state.symbol_row
    existing_symbol = symbols_repo.get_symbol(str(row["market"]), str(row["symbol"]))
    state.run_symbol_id = (
        int(existing_symbol["id"])
        if existing_symbol is not None
        else symbols_repo.create_symbol(
            str(row["symbol"]),
            str(row["name"]),
            str(row["market"]),
            str(row["asset_type"]),
            is_active=int(row["is_active"]),
        )
    )
    klines_repo.bulk_upsert_klines(
        [
            {
                "symbol_id": state.run_symbol_id,
                "timeframe": state.spec.timeframe,
                "timestamp": bar["timestamp"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
                "amount": bar["amount"],
            }
            for bar in state.bars
        ]
    )


def _execute(
    spec: PortfolioSpec,
    states: dict[str, _SymbolState],
    symbol_payloads: list[Mapping[str, Any]],
    input_fingerprint: str,
    run_id: str,
    run_db: Any,
) -> PortfolioReport:
    initialize_database(run_db)
    for state in states.values():
        _materialize(run_db, state)

    signals = SignalRepository(run_db)
    trades_repo = TradeRepository(run_db)
    orders = OrderRepository(run_db)
    execution = ExecutionEngine(run_db)

    cash = float(spec.initial_capital)
    positions: dict[str, float] = {code: 0.0 for code in states}
    avg_prices: dict[str, float] = {code: 0.0 for code in states}
    last_close: dict[str, float] = {}
    last_close_ts: dict[str, str] = {}

    trades: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unfilled: list[dict[str, Any]] = []
    equity_curve: list[tuple[str, float]] = []
    stale_mark_events = 0
    sequence = 0
    pending: list[_PendingOrder] = []

    # 组合时间轴 = 各标的**实际使用** K 线时间戳的并集（升序）
    timeline = sorted({str(row["timestamp"]) for st in states.values() for row in st.bars})

    def _fill(order: _PendingOrder) -> None:
        """统一资金/持仓检查后成交（或被拒绝）。"""
        nonlocal cash
        code = order.symbol
        hold = positions[code]
        fill_price = spec.costs.fill_price(order.side, order.base_price)
        fee = spec.costs.fee(order.side, fill_price, order.quantity)

        def reject(reason: str, **extra: Any) -> None:
            """拒单：落一条 status='rejected' 的订单（现金/持仓不变、不产生成交）。"""
            order_id = orders.create_order(
                states[code].run_symbol_id,
                order.side,
                order.quantity,
                order_type=ORDER_TYPE_MARKET,
                price=fill_price,
                signal_id=order.signal_id,
                status=ORDER_STATUS_REJECTED,
            )
            signals.update_signal_status(order.signal_id, "cancelled")
            rejected.append(
                {
                    "symbol": code,
                    "signal_timestamp": order.signal_timestamp,
                    "side": order.side,
                    "quantity": order.quantity,
                    "order_id": order_id,
                    "reason": reason,
                    **extra,
                }
            )

        if order.side == "buy":
            cost = fill_price * order.quantity
            if cost + fee > cash + QUANTITY_EPSILON:
                reject(
                    REJECT_INSUFFICIENT_CASH,
                    required_cash=round(cost + fee, 6),
                    available_cash=round(cash, 6),
                )
                return
            max_value = spec.policy.max_position_value
            if max_value is not None and (hold + order.quantity) * fill_price > max_value + QUANTITY_EPSILON:
                reject(
                    REJECT_POSITION_LIMIT,
                    position_value_after=round((hold + order.quantity) * fill_price, 6),
                    max_position_value=float(max_value),
                )
                return
        else:
            if not spec.policy.allow_short and order.quantity > hold + QUANTITY_EPSILON:
                reject(REJECT_INSUFFICIENT_POSITION, held_quantity=round(hold, 6))
                return

        result = execution.execute_signal(
            order.signal_id,
            order.quantity,
            price=fill_price,
            fee=fee,
            timestamp=order.fill_timestamp,
        )
        new_qty, new_avg = apply_trade_to_position(
            hold, avg_prices[code], order.side, order.quantity, fill_price
        )
        positions[code] = new_qty
        avg_prices[code] = new_avg
        if order.side == "buy":
            cash -= fill_price * order.quantity + fee
        else:
            cash += fill_price * order.quantity - fee
        trades.append(
            {
                "symbol": code,
                "signal_timestamp": order.signal_timestamp,
                "submit_timestamp": order.signal_timestamp,
                "fill_timestamp": order.fill_timestamp,
                "fill_beyond_scored_range": order.fill_timestamp > states[code].scored_last,
                "side": order.side,
                "quantity": result.quantity,
                "base_price": round(order.base_price, 4),
                "fill_price": result.price,
                "fee": round(fee, 2),
                "pnl": 0.0,
                "position_after": new_qty,
                "cash_after": round(cash, 6),
                "signal_id": result.signal_id,
                "order_id": result.order_id,
                "trade_id": result.trade_id,
            }
        )

    def process_pending(upto: str) -> None:
        """处理 fill_timestamp <= upto 的待成交订单（按 成交时间 → 标的 → 序号 排序）。"""
        due = [order for order in pending if order.fill_timestamp <= upto]
        if not due:
            return
        due.sort(key=lambda order: (order.fill_timestamp, order.symbol, order.sequence))
        for order in due:
            pending.remove(order)
            _fill(order)

    for timestamp in timeline:
        # (1) 先处理上一时点排到本时点的成交（next_bar_open）
        process_pending(timestamp)

        # (2) 同步决策：按标的代码升序，逐标的喂当前 K 线并收集信号
        for code in sorted(states):
            state = states[code]
            row = state.bar_by_timestamp.get(timestamp)
            if row is None:
                continue  # 该标的此刻没有行情 → 不参与、不补齐
            bar = bar_from_kline_row(row)
            candidate = state.strategy.on_bar(
                BarWindow(current=bar, history=state.history), state.config
            )
            state.history = state.history + (bar,)
            if candidate is None:
                continue
            if candidate.timestamp != bar.timestamp:
                raise RunError(
                    f"[{code}] 策略返回的信号时间戳 {candidate.timestamp!r} 与当前 K 线 "
                    f"{bar.timestamp!r} 不一致：禁止把信号挂到未来"
                )
            # 只有计分区间内的信号参与组合（预热 / 边界那根只用于构建历史）
            if bar.timestamp < state.scored_first or bar.timestamp > state.scored_last:
                continue
            direction = require_direction(candidate.direction)
            side = SIDE_BY_DIRECTION[direction]
            signal_id = signals.create_signal(
                state.version_id,
                state.run_symbol_id,
                state.spec.timeframe,
                bar.timestamp,
                direction,
                float(candidate.price),
                reason=candidate.reason,
            )
            if spec.policy.fill_model == FILL_NEXT_OPEN:
                index = state.bar_index[bar.timestamp]
                if index + 1 >= len(state.bars):
                    # 与单标的闭环一致：末尾信号无下一根可成交 → 如实记为未成交
                    unfilled.append(
                        {
                            "symbol": code,
                            "signal_timestamp": bar.timestamp,
                            "direction": direction,
                            "reason": UNFILLED_NO_NEXT_BAR,
                        }
                    )
                    continue
                fill_timestamp = str(state.bars[index + 1]["timestamp"])
                base_price = float(state.bars[index + 1]["open"])
            else:
                fill_timestamp = bar.timestamp
                base_price = float(bar.close)
            sequence += 1
            pending.append(
                _PendingOrder(
                    symbol=code,
                    side=side,
                    signal_id=signal_id,
                    signal_timestamp=bar.timestamp,
                    fill_timestamp=fill_timestamp,
                    base_price=base_price,
                    quantity=float(state.spec.quantity),
                    sequence=sequence,
                )
            )

        # (3) signal_bar_close：本时点收齐信号后立刻成交
        process_pending(timestamp)

        # (4) 更新本时点有行情的标的的最近收盘价（估值用，仅历史，不含未来）
        for code in sorted(states):
            row = states[code].bar_by_timestamp.get(timestamp)
            if row is not None:
                last_close[code] = float(row["close"])
                last_close_ts[code] = timestamp

        # (5) 组合权益 = 可用资金 + Σ 持仓市值（未实现按最近已发生收盘价估值）
        equity = cash
        for code in sorted(states):
            quantity = positions[code]
            if abs(quantity) < QUANTITY_EPSILON:
                continue
            mark = last_close.get(code)
            if mark is None:
                raise RunError(f"[{code}] 有持仓但没有任何可用行情，无法估值")
            if last_close_ts.get(code) != timestamp:
                stale_mark_events += 1  # 该标的此刻无行情 → 用最近收盘价（陈旧估值告警）
            equity += quantity * mark
        equity_curve.append((timestamp, round(equity, 6)))

    if pending:
        raise RunError(f"组合循环结束后仍有 {len(pending)} 笔待成交订单未处理（内部不一致）")

    # ---- 收尾：逐标的已实现盈亏 / 绩效 / 持仓，组合级汇总
    metrics_by_symbol: dict[str, BacktestMetrics] = {}
    realized_by_symbol: dict[str, float] = {}
    for code in sorted(states):
        state = states[code]
        rows = trades_repo.list_trades(symbol_id=state.run_symbol_id)
        events = realized_trades(rows)
        symbol_trades = [t for t in trades if t["symbol"] == code]
        if len(events) != len(symbol_trades):
            raise RunError(
                f"[{code}] 成交记录数与明细数不一致：明细 {len(symbol_trades)}，落库 {len(events)}"
            )
        for trade, event in zip(symbol_trades, events):
            trade["pnl"] = round(event.pnl, 6)  # 逐笔已实现盈亏回填（不改动成交语义）
        realized_by_symbol[code] = round(sum(event.pnl for event in events), 6)
        metrics_by_symbol[code] = compute_metrics(rows)

    metrics = _aggregate_metrics(metrics_by_symbol)
    fee_total = round(sum(float(t["fee"]) for t in trades), 2)
    slippage_total = round(
        sum(
            abs(float(t["fill_price"]) - float(t["base_price"])) * float(t["quantity"])
            for t in trades
        ),
        2,
    )
    realized_total = round(sum(realized_by_symbol.values()), 6)

    positions_value = 0.0
    positions_cost = 0.0
    per_symbol: dict[str, Any] = {}
    for code in sorted(states):
        state = states[code]
        quantity = positions[code]
        average = avg_prices[code]
        mark = last_close.get(code)
        if abs(quantity) >= QUANTITY_EPSILON and mark is None:
            raise RunError(f"[{code}] 有持仓但没有任何可用行情，无法估值")
        value = 0.0 if (mark is None or abs(quantity) < QUANTITY_EPSILON) else quantity * mark
        unrealized = (
            0.0 if (mark is None or abs(quantity) < QUANTITY_EPSILON) else quantity * (mark - average)
        )
        positions_value += value
        positions_cost += quantity * average
        per_symbol[code] = {
            "trades": int(metrics_by_symbol[code].total_trades),
            "buy_trades": int(metrics_by_symbol[code].buy_trades),
            "sell_trades": int(metrics_by_symbol[code].sell_trades),
            "winning_trades": int(metrics_by_symbol[code].winning_trades),
            "losing_trades": int(metrics_by_symbol[code].losing_trades),
            "win_rate": metrics_by_symbol[code].win_rate,
            "profit_factor": metrics_by_symbol[code].profit_factor,
            "realized_pnl": realized_by_symbol[code],
            "position_quantity": round(quantity, 6),
            "position_avg_price": round(average, 6),
            "mark_price": None if mark is None else round(mark, 6),
            "mark_timestamp": last_close_ts.get(code),
            "position_value": round(value, 6),
            "unrealized_pnl": round(unrealized, 6),
            "scored_first_timestamp": state.scored_first,
            "scored_last_timestamp": state.scored_last,
            "data_warnings": list(state.data.warnings),
        }
    positions_value = round(positions_value, 6)
    positions_cost = round(positions_cost, 6)
    unrealized_total = round(sum(item["unrealized_pnl"] for item in per_symbol.values()), 6)

    final_equity = round(cash + positions_value, 6)
    net_pnl = round(final_equity - float(spec.initial_capital), 6)
    max_drawdown, max_drawdown_pct = _equity_drawdown(equity_curve)
    performance = summarize_performance(
        trades,
        fee_total=fee_total,
        net_pnl=round(realized_total - fee_total, 6),
        initial_capital=float(spec.initial_capital),
    )

    # 账实一致性核对（不静默放过）
    expected_equity = round(
        float(spec.initial_capital) + realized_total - fee_total + unrealized_total, 6
    )
    if abs(expected_equity - final_equity) > 1e-4:
        raise RunError(
            f"组合权益与已实现/未实现口径不一致：权益 {final_equity}，"
            f"初始+已实现−手续费+未实现 = {expected_equity}"
        )

    summary = {
        "initial_capital": round(float(spec.initial_capital), 6),
        "cash_available": round(cash, 6),
        "positions_cost": positions_cost,
        "positions_value": positions_value,
        "final_equity": final_equity,
        "net_pnl": net_pnl,
        "return_pct": round(net_pnl / float(spec.initial_capital), 10),
        "realized_pnl": realized_total,
        "unrealized_pnl": unrealized_total,
        "realized_return_pct": round(
            (realized_total - fee_total) / float(spec.initial_capital), 10
        ),
        "fee_total": fee_total,
        "slippage_total": slippage_total,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "equity_points": len(equity_curve),
    }

    diagnostics = {
        "engine_version": PORTFOLIO_ENGINE_VERSION,
        "single_engine_version": ENGINE_VERSION,
        "allocation_rule": ALLOCATION_RULE,
        "valuation_rule": (
            "持有标的按「当前时点之前最近一根已发生 K 线的收盘价」估值；"
            "不用未来价格、不插值；该价格不是当前时点 K 线时记为陈旧估值"
        ),
        "stale_mark_events": stale_mark_events,
        "drawdown_basis": (
            "组合最大回撤按**逐时点市值权益**计算（含未实现盈亏）；"
            "performance.max_drawdown 是已实现现金流口径（另一口径，不混用）"
        ),
        "performance_realized_only": {
            "net_pnl": performance.net_pnl,
            "max_drawdown": performance.max_drawdown,
            "return_pct": performance.return_pct,
        },
        "cost_source": "assumed_defaults（费率是假设值，非已核实官方费率）",
        "price_basis": "不复权（正式库 klines 口径）",
        "unmodeled_constraints": [
            "T+1 卖出限制未模拟（allow_short 只控制是否允许裸卖空，不限制回转交易）",
            "涨跌停价 / 一字板不可成交未模拟",
            "停牌 / 缺失 K 线：该时点不参与交易，但不做停牌过滤",
            "无保证金 / 杠杆 / 融券模型；allow_short=True 时做空不占用保证金",
            "未做跨标的资产配置优化，资金分配仅为「标的代码升序 + 先到先得」",
        ],
        "unknown_states_not_converted": True,
        "rejection_policy": (
            "拒单不改动现金/持仓，不产生成交；落一条 status='rejected' 的订单、信号置为 cancelled，"
            "并逐条记入 rejected_orders（含 order_id），与订单表对得上"
        ),
        "unfilled_policy": (
            "区间末尾信号无可成交的下一根 K 线时记入 unfilled_signals（信号保持 pending，"
            "与单标的闭环一致），不产生订单/成交"
        ),
        "reproducibility": (
            "相同 PortfolioSpec + 相同行情摘要 + 相同策略代码 → 相同 input/result 指纹与结果"
        ),
    }

    report = PortfolioReport(
        run_id=run_id,
        engine_version=PORTFOLIO_ENGINE_VERSION,
        single_engine_version=ENGINE_VERSION,
        input_fingerprint=input_fingerprint,
        result_fingerprint="",
        spec=spec.to_dict(),
        data={code: state.data.to_dict() for code, state in sorted(states.items())},
        summary=summary,
        metrics=metrics,
        per_symbol=per_symbol,
        trades=tuple(trades),
        rejected_orders=tuple(rejected),
        unfilled_signals=tuple(unfilled),
        equity_curve=tuple(equity_curve),
        diagnostics=diagnostics,
        run_db_path=str(run_db),
    )
    return replace(report, result_fingerprint=_result_fingerprint(report))


def _aggregate_metrics(metrics_by_symbol: Mapping[str, BacktestMetrics]) -> dict[str, Any]:
    """把逐标的绩效汇总为组合级绩效（口径显式，不直接相加收益率）。"""
    total_trades = sum(m.total_trades for m in metrics_by_symbol.values())
    buy_trades = sum(m.buy_trades for m in metrics_by_symbol.values())
    sell_trades = sum(m.sell_trades for m in metrics_by_symbol.values())
    winning = sum(m.winning_trades for m in metrics_by_symbol.values())
    losing = sum(m.losing_trades for m in metrics_by_symbol.values())
    total_profit = sum(m.average_profit * m.winning_trades for m in metrics_by_symbol.values())
    total_loss = sum(m.average_loss * m.losing_trades for m in metrics_by_symbol.values())
    closed = winning + losing
    if abs(total_loss) > 1e-9:
        profit_factor = total_profit / abs(total_loss)
    else:
        profit_factor = math.inf if total_profit > 1e-9 else 0.0
    return {
        "total_trades": total_trades,
        "buy_trades": buy_trades,
        "sell_trades": sell_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": winning / closed if closed else 0.0,
        "average_profit": total_profit / winning if winning else 0.0,
        "average_loss": total_loss / losing if losing else 0.0,
        "profit_factor": profit_factor,
        "gross_realized_pnl": round(total_profit + total_loss, 6),
    }


def _equity_drawdown(equity_curve: list[tuple[str, float]]) -> tuple[float, float | None]:
    """按逐时点权益计算最大回撤（货币 + 比例）。"""
    if not equity_curve:
        return 0.0, None
    peak = equity_curve[0][1]
    max_dd = 0.0
    max_dd_pct = 0.0
    for _timestamp, equity in equity_curve:
        peak = max(peak, equity)
        drawdown = peak - equity
        if drawdown > max_dd:
            max_dd = drawdown
        if peak > 0:
            max_dd_pct = max(max_dd_pct, drawdown / peak)
    return round(max_dd, 6), round(max_dd_pct, 10)


__all__ = [
    "ALLOCATION_RULE",
    "PORTFOLIO_ENGINE_VERSION",
    "PortfolioPolicy",
    "PortfolioReport",
    "PortfolioSpec",
    "SymbolSpec",
    "run_portfolio",
]
