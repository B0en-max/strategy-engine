"""模拟执行器（Stage 10）。

把已有的数据层串成一条固定的执行链：

    Signal → Order → Trade → Position

定位与边界：
    - **只做模拟执行**：不接实时行情、不接交易所 / 券商 API、不做实盘交易。
    - 执行器**不做市场判断**：side 由 Signal 的 direction 决定
      （long → buy，short → sell），数量由调用方给出。
    - 不计算盈亏，不处理资金账户、杠杆、手续费结算
      （fee 只是原样记入 Trade，不参与持仓计算）。
    - 所有数据库操作都走现有 Repository，执行器里**不写 SQL**。

原子性：
    整条链（建单 → 成交 → 建成交记录 → 改单状态 → 更新持仓 → 标记信号已执行）
    在 `unit_of_work()` 的**同一个事务**里完成：
    任何一步失败都会整体回滚，不会留下半完成状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from database import (
    DEFAULT_DB_PATH,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    TradeRepository,
    resolve_database_path,
    unit_of_work,
)
from database import models
from database.validation import require_number, require_positive_int, require_timestamp

#: Signal.direction → Order/Trade.side 的固定映射
SIDE_BY_DIRECTION: Final[dict[str, str]] = {
    "long": "buy",
    "short": "sell",
}

#: 模拟执行使用市价单
ORDER_TYPE_MARKET: Final[str] = "market"

#: 下单后立即模拟成交，所以订单先进入 submitted，再置为 filled
ORDER_STATUS_SUBMITTED: Final[str] = "submitted"
ORDER_STATUS_FILLED: Final[str] = "filled"

#: 信号执行完成后置为 executed
SIGNAL_STATUS_PENDING: Final[str] = "pending"
SIGNAL_STATUS_EXECUTED: Final[str] = "executed"

#: 数量比较的容差（浮点运算后判断“是否已清仓”）
QUANTITY_EPSILON: Final[float] = 1e-12


@dataclass(frozen=True)
class ExecutionResult:
    """一次模拟执行的结果（只读，便于测试与后续复盘引用）。"""

    signal_id: int
    order_id: int
    trade_id: int
    position_id: int
    symbol_id: int
    side: str
    quantity: float
    price: float
    position_quantity: float
    position_avg_price: float


def apply_trade_to_position(
    position_quantity: float,
    position_avg_price: float,
    side: str,
    quantity: float,
    price: float,
) -> tuple[float, float]:
    """按一笔成交计算持仓的新 (quantity, avg_price)。

    规则（简单平均成本法，不做盈亏）：
        - 加仓 / 建仓（同方向增加，或原本无持仓）：加权平均
        - 减仓（同方向减少）：剩余持仓的平均成本不变
        - 反手（穿过 0）：剩余部分以本次成交价作为平均成本
        - 恰好清仓（数量为 0）：数量与平均成本都归零
    """
    if side not in models.TRADE_SIDES:
        raise ValueError(f"side 必须是 {list(models.TRADE_SIDES)} 之一，实际为 {side!r}")
    if quantity <= 0:
        raise ValueError(f"成交数量必须为正数，实际为 {quantity!r}")

    signed_quantity = quantity if side == "buy" else -quantity
    new_quantity = position_quantity + signed_quantity

    if abs(new_quantity) < QUANTITY_EPSILON:
        return 0.0, 0.0

    adding = position_quantity == 0 or (position_quantity > 0) == (signed_quantity > 0)
    if adding:
        # 加权平均
        new_avg_price = (
            position_quantity * position_avg_price + signed_quantity * price
        ) / new_quantity
    elif abs(new_quantity) < abs(position_quantity):
        # 减仓：剩余部分的平均成本不变
        new_avg_price = position_avg_price
    else:
        # 反手：剩余部分以本次成交价作为平均成本
        new_avg_price = price

    return new_quantity, new_avg_price


class ExecutionEngine:
    """最小模拟执行器：把 Signal 落成 Order / Trade / Position。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = resolve_database_path(db_path)
        self.signals = SignalRepository(self._db_path)
        self.orders = OrderRepository(self._db_path)
        self.trades = TradeRepository(self._db_path)
        self.positions = PositionRepository(self._db_path)

    # ------------------------------------------------------------------ 属性
    @property
    def db_path(self) -> Path:
        """执行器使用的数据库路径。"""
        return self._db_path

    # ------------------------------------------------------------------ 执行
    def execute_signal(
        self,
        signal_id: int,
        quantity: float,
        *,
        price: float | None = None,
        fee: float = 0.0,
        timestamp: str | None = None,
    ) -> ExecutionResult:
        """执行一个 Signal，完成 Signal → Order → Trade → Position 全链路。

        参数：
            signal_id  要执行的信号 id（必须是 pending 状态）
            quantity   成交数量（必须为正数；由调用方给出，不从策略配置推断）
            price      成交价格；不传则用 Signal 自带的 price
            fee       手续费，原样记入 Trade（不参与持仓计算，允许为负）
            timestamp  成交时间（UTC）；不传则用当前 UTC 时间

        返回：
            ExecutionResult（包含 order / trade / position 的 id 与持仓结果）

        失败时：
            只抛出异常，**不产生任何写入**（整条链在同一事务内，失败整体回滚）。
        """
        quantity = require_number(quantity, "quantity")
        if quantity <= 0:
            raise ValueError(f"成交数量必须为正数，实际为 {quantity!r}")
        fee = require_number(fee, "fee")

        # 1) 读取并校验 Signal（只读，尚未产生任何写入）
        signal = self.signals.get_signal(require_positive_int(signal_id, "signal_id"))
        if signal is None:
            raise ValueError(f"signal 不存在: {signal_id!r}")
        if signal["status"] != SIGNAL_STATUS_PENDING:
            raise ValueError(
                f"只有 {SIGNAL_STATUS_PENDING} 状态的 signal 可以执行，"
                f"当前状态为 {signal['status']!r}"
            )

        side = SIDE_BY_DIRECTION.get(signal["direction"])
        if side is None:  # pragma: no cover - direction 由数据库 CHECK 限定
            raise ValueError(f"无法识别的 signal.direction: {signal['direction']!r}")

        symbol_id = int(signal["symbol_id"])
        fill_price = signal["price"] if price is None else require_number(price, "price")
        executed_at = models.utc_now_str() if timestamp is None else require_timestamp(timestamp)

        # 2) 整条链放在同一个事务里：要么全部成功，要么全部回滚
        with unit_of_work(self.signals, self.orders, self.trades, self.positions):
            order_id = self.orders.create_order(
                symbol_id,
                side,
                quantity,
                order_type=ORDER_TYPE_MARKET,
                price=fill_price,
                signal_id=signal_id,
                status=ORDER_STATUS_SUBMITTED,
            )

            # 模拟成交：市价单立即以 fill_price 全部成交
            trade_id = self.trades.create_trade(
                symbol_id,
                side,
                quantity,
                fill_price,
                executed_at,
                signal_id=signal_id,
                fee=fee,
            )
            self.orders.update_order_status(order_id, ORDER_STATUS_FILLED)

            # 根据成交更新持仓
            position = self.positions.get_position_by_symbol(symbol_id)
            position_quantity, position_avg_price = apply_trade_to_position(
                float(position["quantity"]) if position is not None else 0.0,
                float(position["avg_price"]) if position is not None else 0.0,
                side,
                quantity,
                fill_price,
            )
            if position is None:
                position_id = self.positions.create_position(
                    symbol_id, position_quantity, position_avg_price
                )
            else:
                position_id = int(position["id"])
                self.positions.update_position(
                    position_id,
                    quantity=position_quantity,
                    avg_price=position_avg_price,
                )

            # 标记信号已执行（避免同一信号被重复执行）
            self.signals.update_signal_status(signal_id, SIGNAL_STATUS_EXECUTED)

        return ExecutionResult(
            signal_id=signal_id,
            order_id=order_id,
            trade_id=trade_id,
            position_id=position_id,
            symbol_id=symbol_id,
            side=side,
            quantity=quantity,
            price=fill_price,
            position_quantity=position_quantity,
            position_avg_price=position_avg_price,
        )


__all__ = [
    "DEFAULT_DB_PATH",
    "ExecutionEngine",
    "ExecutionResult",
    "ORDER_TYPE_MARKET",
    "SIDE_BY_DIRECTION",
    "apply_trade_to_position",
]
