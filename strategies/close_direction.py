"""示例策略：比较相邻两根 K 线的收盘价（Stage 13）。

规则（config 中未配置 min_move 时的默认行为，就是需求里写的最简单规则）：

    当前收盘价 > 前一根收盘价  → long
    当前收盘价 < 前一根收盘价  → short
    两者相等                  → 不产生信号

策略参数（来自 strategies.config，均为可选）：

    {
        "type": "close_direction",
        "min_move": 0.0        # 最小涨跌幅（比例，默认 0）：绝对值小于它的变化不产生信号
    }

注意：这只是一个**用于验证链路的最小示例策略**，不是最终交易策略；
本模块不做复杂指标、不做机器学习、不访问数据库。
"""

from __future__ import annotations

from typing import Any, ClassVar, Mapping

from .base import BarWindow, SignalCandidate, Strategy


class CloseDirectionStrategy(Strategy):
    """按相邻收盘价方向产生 long / short 信号（Stage 19 起实现增量接口 on_bar）。"""

    name: ClassVar[str] = "close_direction"

    def __init__(self, *, min_move: float = 0.0) -> None:
        if min_move < 0:
            raise ValueError(f"min_move 不能为负数，实际为 {min_move!r}")
        self._min_move = min_move

    @property
    def min_move(self) -> float:
        """最小涨跌幅（比例）。"""
        return self._min_move

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "CloseDirectionStrategy":
        """从 strategies.config 读取参数构造策略实例。"""
        raw = config.get("min_move", 0.0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"config.min_move 必须是数字，实际为 {raw!r}")
        return cls(min_move=float(raw))

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        """比较当前 K 线与上一根 K 线的收盘价（只看已发生的两根，不接触未来数据）。"""
        previous = window.previous()
        if previous is None:
            return None  # 第一根没有可比对象

        current = window.current
        if current.close > previous.close:
            direction = "long"
        elif current.close < previous.close:
            direction = "short"
        else:
            return None  # 相等：不产生信号

        if self._min_move > 0 and previous.close != 0:
            move = abs(current.close - previous.close) / abs(previous.close)
            if move < self._min_move:
                return None

        return SignalCandidate(
            timestamp=current.timestamp,
            direction=direction,
            price=current.close,
            reason=f"close {previous.close} -> {current.close}",
        )
