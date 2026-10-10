"""策略运行器（Stage 13）。

职责：读策略配置 → 读 K 线 → 按时间顺序计算 → 把候选信号写入 signals 表。

    strategies.config ─┐
                       ├─→ Strategy.generate() ─→ signals 表
    klines (symbol+tf) ─┘

边界（本阶段明确不做）：
    - 不创建 Order / Trade / Position（运行器里根本没有这三个 Repository）
    - 不做风控、不做回测、不做 AI、不自动交易
    - 策略实现不在本文件里：通过 strategies.config 的 "type" 选择，
      内置实现见 strategies/close_direction.py，可在构造 Runner 时注入自己的注册表

一致性：
    - K 线一律按 timestamp 升序参与计算（由 KlineRepository.list_klines 保证）
    - 落库使用 database.unit_of_work()，整批信号在同一个事务里（要么全写要么全不写）
    - 同一 (strategy_id, symbol_id, timeframe, timestamp) 已有信号时会跳过（可重复运行）
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from database import (
    KlineRepository,
    SignalRepository,
    StrategyRepository,
    resolve_database_path,
    unit_of_work,
)

from .base import Bar, SignalCandidate, Strategy, bar_from_kline_row, require_direction
from .close_direction import CloseDirectionStrategy

#: strategies.config 中用于选择策略实现的键
STRATEGY_TYPE_KEY: str = "type"

#: config 未指定 type 时使用的默认实现
DEFAULT_STRATEGY_TYPE: str = CloseDirectionStrategy.name

#: 内置策略注册表（类型名 → 策略类）
DEFAULT_STRATEGIES: dict[str, type[Strategy]] = {
    CloseDirectionStrategy.name: CloseDirectionStrategy,
}


def strategy_type_of(config: Mapping[str, Any]) -> str:
    """读取 config['type']（缺省用默认类型），非法时抛 ValueError。"""
    type_name = config.get(STRATEGY_TYPE_KEY, DEFAULT_STRATEGY_TYPE)
    if not isinstance(type_name, str) or not type_name.strip():
        raise ValueError(
            f"config[{STRATEGY_TYPE_KEY!r}] 必须是非空字符串，实际为 {type_name!r}"
        )
    return type_name.strip()


def build_strategy(
    config: Mapping[str, Any],
    registry: Mapping[str, type[Strategy]],
) -> Strategy:
    """按 config['type'] 从注册表构造策略实例（StrategyRunner 与 StrategyExecutor 共用）。"""
    type_name = strategy_type_of(config)
    strategy_class = registry.get(type_name)
    if strategy_class is None:
        raise ValueError(f"未知策略类型 {type_name!r}，已注册: {sorted(registry)}")
    return strategy_class.from_config(config)


@dataclass(frozen=True)
class RunResult:
    """一次策略运行的结果（只读汇总）。"""

    strategy_id: int
    strategy_name: str
    strategy_version: int
    strategy_type: str
    symbol_id: int
    timeframe: str
    #: 参与计算的 K 线根数
    bars: int
    #: 本次写入的信号数
    signals: int
    long_signals: int
    short_signals: int
    #: 因该 (strategy, symbol, timeframe, timestamp) 已有信号而跳过的条数
    skipped: int
    #: 参与计算的 K 线时间范围
    first_bar_timestamp: str | None
    last_bar_timestamp: str | None


class StrategyRunner:
    """最小策略运行器。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        strategies: Mapping[str, type[Strategy]] | None = None,
    ) -> None:
        self._db_path = resolve_database_path(db_path)
        self._registry: dict[str, type[Strategy]] = dict(
            DEFAULT_STRATEGIES if strategies is None else strategies
        )
        # 只用这三个 Repository：不引入 Order / Trade / Position
        self.strategies = StrategyRepository(self._db_path)
        self.klines = KlineRepository(self._db_path)
        self.signals = SignalRepository(self._db_path)

    # ------------------------------------------------------------------ 属性
    @property
    def db_path(self) -> Path:
        """运行器使用的数据库路径。"""
        return self._db_path

    @property
    def available_strategy_types(self) -> tuple[str, ...]:
        """已注册的策略类型名。"""
        return tuple(sorted(self._registry))

    # ------------------------------------------------------------------ 准备
    def resolve_strategy(self, config: Mapping[str, Any]) -> Strategy:
        """根据 strategies.config 选择并构造策略实例。"""
        return build_strategy(config, self._registry)

    def load_bars(
        self,
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> list[Bar]:
        """读取 K 线并转换成策略输入（升序）。"""
        rows = self.klines.list_klines(symbol_id, timeframe, start_timestamp, end_timestamp)
        return [bar_from_kline_row(row) for row in rows]

    # ------------------------------------------------------------------ 运行
    def run(
        self,
        strategy_id: int,
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> RunResult:
        """运行策略并把信号写入 signals 表。

        参数：
            strategy_id     策略 id（strategies 表）
            symbol_id       标的 id（klines / signals 表）
            timeframe       K 线周期
            start_timestamp 可选的起始时间（含）
            end_timestamp   可选的结束时间（含）

        返回：
            RunResult

        失败时：
            策略不存在、策略类型未知、策略参数非法时抛 ValueError（不写任何信号）；
            落库过程中出错则整批回滚。
        """
        strategy_row = self.strategies.get_strategy(strategy_id)
        if strategy_row is None:
            raise ValueError(f"策略不存在: {strategy_id}")
        config = strategy_row["config"]
        strategy = self.resolve_strategy(config)
        strategy_type = config.get(STRATEGY_TYPE_KEY, DEFAULT_STRATEGY_TYPE)

        bars = self.load_bars(
            symbol_id,
            timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        candidates: list[SignalCandidate] = strategy.generate(bars, config)

        # 已有信号（按 (strategy, symbol, timeframe, timestamp) 去重，便于重复运行）
        existing = {
            row["timestamp"]
            for row in self.signals.list_signals(
                strategy_id=strategy_id, symbol_id=symbol_id, timeframe=timeframe
            )
        }

        written = 0
        skipped = 0
        long_signals = 0
        short_signals = 0
        with unit_of_work(self.signals):
            for candidate in candidates:
                timestamp = candidate.timestamp
                if timestamp in existing:
                    skipped += 1
                    continue
                direction = require_direction(candidate.direction)
                self.signals.create_signal(
                    strategy_id,
                    symbol_id,
                    timeframe,
                    timestamp,
                    direction,
                    candidate.price,
                    reason=candidate.reason,
                )
                existing.add(timestamp)
                written += 1
                if direction == "long":
                    long_signals += 1
                else:
                    short_signals += 1

        return RunResult(
            strategy_id=strategy_id,
            strategy_name=strategy_row["name"],
            strategy_version=int(strategy_row["version"]),
            strategy_type=str(strategy_type),
            symbol_id=symbol_id,
            timeframe=timeframe,
            bars=len(bars),
            signals=written,
            long_signals=long_signals,
            short_signals=short_signals,
            skipped=skipped,
            first_bar_timestamp=bars[0].timestamp if bars else None,
            last_bar_timestamp=bars[-1].timestamp if bars else None,
        )
