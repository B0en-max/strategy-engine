"""回测绩效统计（Stage 15）。

只做「回测结果 → 绩效指标」，**不改变 Stage 14 的回测执行逻辑**，
也不重复实现策略计算 / 信号生成 / 模拟成交：指标统一从回测产生的**成交序列**算出。

盈亏口径（保持最小化）：
    - 只算**已实现**盈亏（开仓但未平仓的部分不计入）
    - 不考虑手续费、滑点、资金、杠杆、保证金
    - 多头：卖出平仓价 - 买入成本价；空头：卖出开仓价 - 买入平仓价
    - 成本价用**持仓平均成本**（与执行器的持仓规则一致），部分平仓只结算对应部分

识别方式（第 5 条要求）：
    不把 buy / sell 两两相减，而是维护一个带符号的持仓状态，
    逐笔识别 **开多 / 加多 / 平多 / 减多 / 开空 / 加空 / 平空 / 减空 / 反转**，
    只有"平掉的那部分"才结算盈亏。可用 realized_trades() 审计每一笔。

用法：
    from backtest import run_backtest_with_metrics
    with BacktestEngine(source_db_path=...) as engine:
        result, metrics = run_backtest_with_metrics(engine, strategy_id, symbol_id, "15m")
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from database import TradeRepository
from database import models
from database.validation import require_choice, require_number

from .engine import BacktestEngine, BacktestResult

#: 数量/盈亏比较容差（浮点）
EPSILON: float = 1e-9

# --- 事件类型：仅用于审计"这笔成交属于哪种动作"（第 5 条要求能识别这些情形） ---
EVENT_OPEN_LONG: str = "open_long"      # 开多
EVENT_ADD_LONG: str = "add_long"        # 加多
EVENT_CLOSE_LONG: str = "close_long"    # 平多（全部）
EVENT_REDUCE_LONG: str = "reduce_long"  # 部分平多
EVENT_OPEN_SHORT: str = "open_short"    # 开空
EVENT_ADD_SHORT: str = "add_short"      # 加空
EVENT_CLOSE_SHORT: str = "close_short"  # 平空（全部）
EVENT_REDUCE_SHORT: str = "reduce_short"  # 部分平空
EVENT_REVERSE_LONG_TO_SHORT: str = "reverse_long_to_short"  # 多翻空
EVENT_REVERSE_SHORT_TO_LONG: str = "reverse_short_to_long"  # 空翻多

EVENT_TYPES: tuple[str, ...] = (
    EVENT_OPEN_LONG,
    EVENT_ADD_LONG,
    EVENT_CLOSE_LONG,
    EVENT_REDUCE_LONG,
    EVENT_OPEN_SHORT,
    EVENT_ADD_SHORT,
    EVENT_CLOSE_SHORT,
    EVENT_REDUCE_SHORT,
    EVENT_REVERSE_LONG_TO_SHORT,
    EVENT_REVERSE_SHORT_TO_LONG,
)


@dataclass(frozen=True)
class RealizedTrade:
    """一笔成交对盈亏的影响（审计用）。"""

    timestamp: str | None
    side: str
    event: str
    #: 本笔成交数量（正数）
    quantity: float
    #: 成交价
    price: float
    #: 本笔成交中，平掉旧持仓的数量（开仓/加仓为 0）
    closed_quantity: float
    #: 本笔成交结算的已实现盈亏（开仓/加仓为 0）
    pnl: float
    #: 成交后剩余持仓（带符号）
    position_after: float


@dataclass(frozen=True)
class BacktestMetrics:
    """一次回测的基础绩效指标。"""

    total_trades: int
    buy_trades: int
    sell_trades: int
    #: 有已实现盈亏的平仓成交中，盈利 / 亏损的笔数（只开仓的成交不计入）
    winning_trades: int
    losing_trades: int
    #: 已实现盈亏合计（未平仓浮盈不计）
    total_pnl: float
    #: 胜率 = winning_trades / (winning_trades + losing_trades)，没有平仓成交时为 0
    win_rate: float
    #: 平均盈利（盈利成交的均值），无盈利成交时为 0
    average_profit: float
    #: 平均亏损（**负值**，亏损成交的均值），无亏损成交时为 0
    average_loss: float
    #: 总盈利 / |总亏损|；有盈利且无亏损时为 inf，无盈利时为 0
    profit_factor: float


def _value(trade: Any, key: str) -> Any:
    """按字段名取值（兼容 sqlite3.Row 与 dict）。"""
    return trade[key]


def _optional_timestamp(trade: Any) -> str | None:
    try:
        value = trade["timestamp"]
    except (KeyError, IndexError):
        return None
    return None if value is None else str(value)


def realized_trades(trades: Iterable[Any]) -> list[RealizedTrade]:
    """逐笔识别交易动作并结算已实现盈亏。

    参数：
        trades 按时间升序的成交序列；每笔需可按 side / quantity / price 取值
               （TradeRepository.list_trades() 返回的 sqlite3.Row 可以直接传入）

    返回：
        RealizedTrade 列表（与输入一一对应）
    """
    events: list[RealizedTrade] = []
    position = 0.0  # 带符号持仓
    average = 0.0   # 当前持仓平均成本

    for trade in trades:
        side = require_choice(_value(trade, "side"), "side", models.TRADE_SIDES)
        quantity = require_number(_value(trade, "quantity"), "quantity")
        price = require_number(_value(trade, "price"), "price")
        if quantity <= 0:
            raise ValueError(f"成交数量必须为正数，实际为 {quantity!r}")

        signed = quantity if side == "buy" else -quantity
        timestamp = _optional_timestamp(trade)
        closed = 0.0
        pnl = 0.0

        if position == 0.0 or (position > 0) == (signed > 0):
            # 开仓 / 加仓：不结算盈亏，只更新平均成本
            if position == 0.0:
                event = EVENT_OPEN_LONG if signed > 0 else EVENT_OPEN_SHORT
            else:
                event = EVENT_ADD_LONG if signed > 0 else EVENT_ADD_SHORT
            new_position = position + signed
            average = (position * average + signed * price) / new_position
        else:
            # 反向成交：先平掉能平的部分
            closed = min(abs(signed), abs(position))
            pnl = (price - average) * closed if position > 0 else (average - price) * closed
            new_position = position + signed

            if abs(new_position) < EPSILON:
                # 全部平仓
                new_position = 0.0
                average = 0.0
                event = EVENT_CLOSE_LONG if position > 0 else EVENT_CLOSE_SHORT
            elif (new_position > 0) == (position > 0):
                # 部分平仓（同向缩小）：剩余持仓平均成本不变
                event = EVENT_REDUCE_LONG if position > 0 else EVENT_REDUCE_SHORT
            else:
                # 反转（持仓符号翻转，无论剩余仓位大小）：剩余部分以本次成交价为成本
                average = price
                event = (
                    EVENT_REVERSE_LONG_TO_SHORT if position > 0 else EVENT_REVERSE_SHORT_TO_LONG
                )

        position = new_position
        events.append(
            RealizedTrade(
                timestamp=timestamp,
                side=side,
                event=event,
                quantity=quantity,
                price=price,
                closed_quantity=closed,
                pnl=pnl,
                position_after=position,
            )
        )

    return events


def compute_metrics(trades: Iterable[Any]) -> BacktestMetrics:
    """从成交序列计算基础绩效指标（纯计算，不访问数据库）。"""
    events = realized_trades(trades)

    total_trades = len(events)
    buy_trades = sum(1 for event in events if event.side == "buy")
    sell_trades = total_trades - buy_trades

    profits = [event.pnl for event in events if event.closed_quantity > EPSILON and event.pnl > EPSILON]
    losses = [event.pnl for event in events if event.closed_quantity > EPSILON and event.pnl < -EPSILON]

    winning_trades = len(profits)
    losing_trades = len(losses)
    total_profit = sum(profits)
    total_loss = sum(losses)  # 负值
    total_pnl = sum(event.pnl for event in events)

    closed_trades = winning_trades + losing_trades
    win_rate = winning_trades / closed_trades if closed_trades else 0.0
    average_profit = total_profit / winning_trades if winning_trades else 0.0
    average_loss = total_loss / losing_trades if losing_trades else 0.0
    if abs(total_loss) > EPSILON:
        profit_factor = total_profit / abs(total_loss)
    else:
        profit_factor = math.inf if total_profit > EPSILON else 0.0

    return BacktestMetrics(
        total_trades=total_trades,
        buy_trades=buy_trades,
        sell_trades=sell_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        total_pnl=total_pnl,
        win_rate=win_rate,
        average_profit=average_profit,
        average_loss=average_loss,
        profit_factor=profit_factor,
    )


def run_backtest_with_metrics(
    engine: BacktestEngine,
    strategy_id: int,
    symbol_id: int,
    timeframe: str,
    *,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> tuple[BacktestResult, BacktestMetrics]:
    """跑一次回测（复用 BacktestEngine）并给出绩效指标。

    指标统计的是**该引擎运行库中的全部成交**：一个引擎实例对应一个运行库，
    因此也就是本次回测产生的成交（Stage 14 已保证正式库只读）。
    """
    result = engine.run(
        strategy_id,
        symbol_id,
        timeframe,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    trades = TradeRepository(engine.run_db_path).list_trades()
    return result, compute_metrics(trades)
