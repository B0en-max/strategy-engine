"""策略接口与数据结构（Stage 13；增量接口与未来数据隔离于 Stage 19 加固）。

只保留策略运行所需的最小抽象：

    Bar              策略输入的一根 K 线（与数据库解耦，便于单独测试策略）
    BarWindow        增量接口看到的数据（只有当前 K 线 + 它之前的 K 线，**不含未来**）
    SignalCandidate  策略输出的一条候选信号（由 Runner / Executor 负责落库）
    Strategy         策略接口：on_bar（增量，推荐）/ generate（批量，兼容旧用法）
    bar_from_kline_row()  klines 表行 → Bar

未来数据隔离（本项目硬约束）：
    - 增量入口 on_bar 拿到的 BarWindow 由 Executor 按「已发生的 K 线」构造，
      物理上不存在未来 K 线，策略无法提前看到 high/low/close
    - 批量入口 generate 由基类逐根推进实现，语义与增量一致，不会提前看到未来

设计原则：
    - 策略是**纯计算**：输入 K 线 + 配置，输出候选信号；不碰数据库、不碰执行器。
    - 策略参数只从 strategies.config / 策略版本 config 读取，不写死在代码里。
    - 不在这里做指标库、不做回测、不做风控。
"""

from __future__ import annotations

from abc import ABC
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


@dataclass(frozen=True)
class BarWindow:
    """增量策略在某一根 K 线上能看到的**全部**数据。

    只有当前 K 线 + 它之前的 K 线（升序）；**构造时就物理上不含未来 K 线**，
    所以策略无法越界读到未来价格。

    （history 为不可变元组，规模等于当前数据量，本项目数据量下开销可接受。）
    """

    current: Bar
    history: tuple[Bar, ...] = ()

    @property
    def index(self) -> int:
        """当前是第几根（从 0 开始）。"""
        return len(self.history)

    @property
    def seen(self) -> tuple[Bar, ...]:
        """到目前为止已发生的 K 线（含当前）。"""
        return self.history + (self.current,)

    def previous(self) -> Bar | None:
        """上一根 K 线；没有历史时返回 None。"""
        return self.history[-1] if self.history else None

    def lookback(self, count: int) -> tuple[Bar, ...]:
        """最近 count 根**历史** K 线（不含当前）；不足时返回已有部分。"""
        if count <= 0:
            return ()
        return self.history[-count:]


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
    """策略接口。

    两个入口（实现其一即可，推荐实现增量入口 on_bar）：
        on_bar(window, config)   逐根 K 线调用：只看得到当前及历史（Executor 用这个）
        generate(bars, config)   批量调用：默认由 on_bar 逐根推导（旧调用方式继续可用）
    """

    #: 策略类型名（strategies.config 里的 "type" 用它来选实现）
    name: ClassVar[str] = ""

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Strategy":
        """从 strategies.config 构造策略实例（默认实现不使用配置）。

        需要参数的策略覆写本方法，并把校验后的参数传给自己的构造函数。
        """
        return cls()

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        """增量入口：处理当前 K 线，返回候选信号（无信号时返回 None）。

        参数：
            window  只包含「当前 K 线 + 之前的 K 线」的窗口（见 BarWindow）
            config  策略版本的 config（参数来源）

        约定：
            - 只做计算，不访问数据库
            - 不产生 Order / Trade / Position
            - 不能读取未来 K 线（BarWindow 里根本没有未来数据）
        """
        raise NotImplementedError(
            f"{type(self).__name__} 既没有实现 on_bar（推荐），也没有实现 generate"
        )

    def generate(
        self,
        bars: Sequence[Bar],
        config: Mapping[str, Any],
    ) -> list[SignalCandidate]:
        """批量入口（与旧接口兼容）：由 on_bar 逐根推导，保证两种入口结果一致。

        参数：
            bars    按 timestamp 升序排列的 K 线
            config  策略版本的 config（策略参数来源）

        返回：
            SignalCandidate 列表（按时间顺序）；不产生信号时返回空列表。
        """
        signals: list[SignalCandidate] = []
        history: tuple[Bar, ...] = ()
        for bar in bars:
            candidate = self.on_bar(BarWindow(current=bar, history=history), config)
            if candidate is not None:
                signals.append(candidate)
            history = history + (bar,)
        return signals
