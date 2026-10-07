"""策略接口与数据结构（Stage 13）。

只保留策略运行所需的最小抽象：

    Bar              策略输入的一根 K 线（与数据库解耦，便于单独测试策略）
    SignalCandidate  策略输出的一条候选信号（由 Runner 负责落库）
    Strategy         策略接口：generate(bars, config) → 候选信号列表
    bar_from_kline_row()  klines 表行 → Bar

设计原则：
    - 策略是**纯计算**：输入 K 线 + 配置，输出候选信号；不碰数据库、不碰执行器。
    - 策略参数只从 strategies.config 读取，不写死在代码里。
    - 不在这里做指标库、不做回测、不做风控。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Mapping, Sequence

from database import models
from database.validation import require_choice, require_number, require_timestamp


@dataclass(frozen=True)
class Bar:
    """策略使用的一根 K 线（timestamp 为 K 线**开始**时间，UTC）。"""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None


@dataclass(frozen=True)
class SignalCandidate:
    """策略产出的一条候选信号（尚未落库）。"""

    timestamp: str
    direction: str
    price: float
    reason: str | None = None


def bar_from_kline_row(row: Mapping[str, Any]) -> Bar:
    """把 klines 表的一行转换成 Bar。"""
    return Bar(
        timestamp=require_timestamp(row["timestamp"]),
        open=require_number(row["open"], "open"),
        high=require_number(row["high"], "high"),
        low=require_number(row["low"], "low"),
        close=require_number(row["close"], "close"),
        volume=require_number(row["volume"], "volume"),
        amount=None if row["amount"] is None else require_number(row["amount"], "amount"),
    )


def require_direction(value: object) -> str:
    """候选信号的 direction 必须是 long / short。"""
    return require_choice(value, "direction", models.SIGNAL_DIRECTIONS)


class Strategy(ABC):
    """策略接口：把 K 线序列转换成候选信号。"""

    #: 策略类型名（strategies.config 里的 "type" 用它来选实现）
    name: ClassVar[str] = ""

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Strategy":
        """从 strategies.config 构造策略实例（默认实现不使用配置）。

        需要参数的策略覆写本方法，并把校验后的参数传给自己的构造函数。
        """
        return cls()

    @abstractmethod
    def generate(
        self,
        bars: Sequence[Bar],
        config: Mapping[str, Any],
    ) -> list[SignalCandidate]:
        """按时间顺序计算候选信号。

        参数：
            bars    按 timestamp 升序排列的 K 线
            config  strategies.config（策略参数来源）

        返回：
            SignalCandidate 列表，按时间顺序排列；不产生信号时返回空列表。

        约定：
            - 只做计算，不访问数据库
            - 不产生 Order / Trade / Position
        """
        raise NotImplementedError
