"""策略执行器（Stage 19）：把「策略版本」逐根 K 线跑成 Signal / Trade。

链路（本阶段的核心关系）：

    Strategy（身份，strategy_definitions）
        ↓
    Strategy Version（版本 + config，strategies 表的一行）
        ↓
    StrategyExecutor（本文件：时间推进 / 提供窗口 / 记录信号 / 触发执行 / 统计）
        ↓
    Strategy.on_bar(BarWindow)   ← 只看到当前及历史
        ↓
    Signal（signals.strategy_id = 版本 id）
        ↓
    ExecutionEngine（复用 Stage 10）→ Order / Trade / Position
        ↓
    StrategyExecutionResult（可追溯、可重复比对）

职责划分：
    Executor   推进时间、提供 K 线与窗口、写入 Signal、触发执行、统计结果
    Strategy   只做判断（long / short / 无信号），参数来自**版本**的 config；
               Executor 里没有任何具体策略规则

未来数据隔离（硬约束）：
    - 每根 K 线构造的 BarWindow 只含「当前 + 之前已发生的」K 线，物理上不含未来
    - 策略返回的信号时间戳必须等于当前 K 线时间戳，否则抛 ValueError
      （拒绝把信号挂到未来；不做静默改写）
    - 成交时间取信号所在 K 线的时间（历史模拟，不用当前时间）

隔离与可重复性：
    - 默认在**独立临时运行库**里执行（复制策略版本 / 标的 / K 线），正式库全程只读
    - 同一份数据 + 同一版本重复执行结果一致：按 (版本, 标的, 周期, timestamp) 去重，
      已存在的信号计为 skipped，不会重复成交
    - 每条信号用 ExecutionEngine 单独成一个事务（与回测引擎一致，便于排查）
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Mapping

from database import (
    DEFAULT_DB_PATH,
    KlineRepository,
    PositionRepository,
    SignalRepository,
    StrategyDefinitionRepository,
    StrategyRepository,
    SymbolRepository,
    initialize_database,
    resolve_database_path,
)
from database.validation import require_number, require_positive_int

from execution import ExecutionEngine

from .base import Bar, BarWindow, bar_from_kline_row, require_direction
from .runner import DEFAULT_STRATEGIES, DEFAULT_STRATEGY_TYPE, STRATEGY_TYPE_KEY, build_strategy

#: 每个信号固定成交数量（与回测引擎保持一致；资金管理属于后续阶段）
DEFAULT_EXECUTION_QUANTITY: Final[float] = 1.0


@dataclass(frozen=True)
class ExecutedSignal:
    """一条被真正执行过的信号（含策略版本追溯信息）。"""

    signal_id: int
    trade_id: int
    order_id: int
    timestamp: str
    direction: str
    side: str
    price: float
    quantity: float
    reason: str | None
    position_quantity: float
    position_avg_price: float


@dataclass(frozen=True)
class StrategyExecutionResult:
    """一次策略执行的结果（只读，便于复盘/策略系统引用与重复比对）。"""

    #: Strategy 身份（strategy_definitions.id）
    strategy_id: int
    strategy_name: str
    #: 策略版本记录 id（strategies.id；signals.strategy_id 指向它）
    strategy_version_id: int
    strategy_version: int
    strategy_type: str
    symbol_id: int
    timeframe: str
    start_timestamp: str | None
    end_timestamp: str | None
    #: 参与推进的 K 线根数
    bars_processed: int
    #: 本次新写入的信号数
    signals_generated: int
    long_signals: int
    short_signals: int
    #: 因已有同 (版本, 标的, 周期, timestamp) 信号而跳过的条数
    skipped: int
    #: 实际成交笔数
    trades_executed: int
    #: 逐条执行明细（按时间顺序）
    executions: tuple[ExecutedSignal, ...]
    final_position_quantity: float
    final_position_avg_price: float
    #: 本次执行使用的运行库路径
    run_db_path: str


class StrategyExecutor:
    """通用策略执行器：不写死任何具体策略，按「策略 + 版本」执行。"""

    def __init__(
        self,
        *,
        source_db_path: str | Path | None = None,
        run_db_path: str | Path | None = None,
        quantity: float = DEFAULT_EXECUTION_QUANTITY,
        strategies: Mapping[str, type[Any]] | None = None,
    ) -> None:
        # 数据来源（默认正式库，只读）
        self._source_db_path = resolve_database_path(source_db_path)
        # 运行库（默认临时目录，自动清理；显式指定则保留）
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if run_db_path is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="se_executor_")
            self._run_db_path = Path(self._temp_dir.name) / "executor.db"
        else:
            self._run_db_path = resolve_database_path(run_db_path)

        quantity = require_number(quantity, "quantity")
        if quantity <= 0:
            raise ValueError(f"成交数量必须为正数，实际为 {quantity!r}")
        self._quantity = quantity
        self._registry = dict(DEFAULT_STRATEGIES if strategies is None else strategies)

    # ------------------------------------------------------------------ 属性
    @property
    def source_db_path(self) -> Path:
        """数据来源数据库（只读）。"""
        return self._source_db_path

    @property
    def run_db_path(self) -> Path:
        """本次执行使用的运行库。"""
        return self._run_db_path

    @property
    def quantity(self) -> float:
        """每个信号的固定成交数量。"""
        return self._quantity

    # -------------------------------------------------------------- 生命周期
    def close(self) -> None:
        """清理临时运行库（显式指定 run_db_path 时不动它）；可重复调用。"""
        if self._temp_dir is not None:
            self._temp_dir.cleanup()

    def __enter__(self) -> "StrategyExecutor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 准备
    def prepare_run_db(
        self,
        version_row: Mapping[str, Any],
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> tuple[int, int, list[Bar]]:
        """把本次执行需要的策略版本 / 标的 / K 线复制进运行库。

        返回 (运行库里的版本 id, 运行库里的标的 id, 参与推进的 K 线)。
        """
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        symbol_row = SymbolRepository(self._source_db_path).get_symbol_by_id(symbol_id)
        if symbol_row is None:
            raise ValueError(f"正式库中不存在标的: symbol_id={symbol_id}")

        klines = KlineRepository(self._source_db_path)
        source_bars = klines.list_klines(
            symbol_id, timeframe, start_timestamp, end_timestamp
        )

        initialize_database(self._run_db_path)
        run_definitions = StrategyDefinitionRepository(self._run_db_path)
        run_strategies = StrategyRepository(self._run_db_path)
        run_symbols = SymbolRepository(self._run_db_path)
        run_klines = KlineRepository(self._run_db_path)

        # 策略身份 + 版本（重复运行时复用，不重复插入；配置以正式库的版本为准）
        run_strategy_id = run_definitions.ensure_definition(
            str(version_row["name"]), description=version_row["description"]
        )
        run_version = run_strategies.get_version_by_number(
            run_strategy_id, int(version_row["version"])
        )
        run_version_id = (
            int(run_version["id"])
            if run_version is not None
            else run_strategies.create_version(
                run_strategy_id,
                version_row["config"],
                version=int(version_row["version"]),
                status=str(version_row["status"]),
                description=version_row["description"],
            )
        )

        existing_symbol = run_symbols.get_symbol(symbol_row["market"], symbol_row["symbol"])
        run_symbol_id = (
            int(existing_symbol["id"])
            if existing_symbol is not None
            else run_symbols.create_symbol(
                symbol_row["symbol"],
                symbol_row["name"],
                symbol_row["market"],
                symbol_row["asset_type"],
                is_active=int(symbol_row["is_active"]),
            )
        )

        if source_bars:
            run_klines.bulk_upsert_klines(
                [
                    {
                        "symbol_id": run_symbol_id,
                        "timeframe": timeframe,
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

        return run_version_id, run_symbol_id, [
            bar_from_kline_row(row) for row in source_bars
        ]

    # ------------------------------------------------------------------ 运行
    def run(
        self,
        strategy_id: int,
        strategy_version_id: int,
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> StrategyExecutionResult:
        """按指定策略版本逐根 K 线执行。

        参数：
            strategy_id          Strategy 身份 id（strategy_definitions.id）
            strategy_version_id  策略版本 id（strategies.id）——执行的是**这个版本**
            symbol_id            标的 id
            timeframe            K 线周期
            start_timestamp      起始时间（含）
            end_timestamp        结束时间（含）

        返回：
            StrategyExecutionResult

        失败时：
            策略/版本/标的不存在、版本不属于该策略、策略类型未知、参数非法 → ValueError（不写任何数据）
        """
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        strategy_version_id = require_positive_int(strategy_version_id, "strategy_version_id")

        definitions = StrategyDefinitionRepository(self._source_db_path)
        definition = definitions.get_definition(strategy_id)
        if definition is None:
            raise ValueError(f"策略不存在: {strategy_id}")

        versions = StrategyRepository(self._source_db_path)
        version_row = versions.get_version(strategy_version_id)
        if version_row is None:
            raise ValueError(f"策略版本不存在: {strategy_version_id}")
        if version_row["strategy_id"] != strategy_id:
            raise ValueError(
                f"策略版本 {strategy_version_id} 不属于策略 {strategy_id}"
                f"（该版本属于策略 {version_row['strategy_id']}）"
            )

        config = version_row["config"]
        strategy = build_strategy(config, self._registry)  # type: ignore[arg-type]
        strategy_type = str(config.get(STRATEGY_TYPE_KEY, DEFAULT_STRATEGY_TYPE))

        run_version_id, run_symbol_id, bars = self.prepare_run_db(
            version_row,
            symbol_id,
            timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        signals = SignalRepository(self._run_db_path)
        execution = ExecutionEngine(self._run_db_path)
        existing = {
            row["timestamp"]
            for row in signals.list_signals(
                strategy_id=run_version_id, symbol_id=run_symbol_id, timeframe=timeframe
            )
        }

        executions: list[ExecutedSignal] = []
        history: tuple[Bar, ...] = ()
        generated = long_signals = short_signals = skipped = 0

        for bar in bars:
            # 只把「当前 + 之前」交给策略：BarWindow 里没有未来 K 线
            window = BarWindow(current=bar, history=history)
            candidate = strategy.on_bar(window, config)  # type: ignore[attr-defined]
            history = history + (bar,)
            if candidate is None:
                continue

            if candidate.timestamp != bar.timestamp:
                raise ValueError(
                    f"策略返回的信号时间戳 {candidate.timestamp!r} 与当前 K 线 "
                    f"{bar.timestamp!r} 不一致：禁止把信号挂到未来"
                )

            direction = require_direction(candidate.direction)
            if bar.timestamp in existing:
                skipped += 1
                continue

            signal_id = signals.create_signal(
                run_version_id,
                run_symbol_id,
                timeframe,
                bar.timestamp,
                direction,
                require_number(candidate.price, "price"),
                reason=candidate.reason,
            )
            existing.add(bar.timestamp)
            generated += 1
            if direction == "long":
                long_signals += 1
            else:
                short_signals += 1

            # 成交时间取信号所在 K 线的时间（历史模拟，不能用当前时间）
            result = execution.execute_signal(
                signal_id, self._quantity, timestamp=bar.timestamp
            )
            executions.append(
                ExecutedSignal(
                    signal_id=signal_id,
                    trade_id=result.trade_id,
                    order_id=result.order_id,
                    timestamp=bar.timestamp,
                    direction=direction,
                    side=result.side,
                    price=result.price,
                    quantity=result.quantity,
                    reason=candidate.reason,
                    position_quantity=result.position_quantity,
                    position_avg_price=result.position_avg_price,
                )
            )

        position = PositionRepository(self._run_db_path).get_position_by_symbol(run_symbol_id)
        return StrategyExecutionResult(
            strategy_id=strategy_id,
            strategy_name=str(definition["name"]),
            strategy_version_id=strategy_version_id,
            strategy_version=int(version_row["version"]),
            strategy_type=strategy_type,
            symbol_id=symbol_id,
            timeframe=timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bars_processed=len(bars),
            signals_generated=generated,
            long_signals=long_signals,
            short_signals=short_signals,
            skipped=skipped,
            trades_executed=len(executions),
            executions=tuple(executions),
            final_position_quantity=float(position["quantity"]) if position is not None else 0.0,
            final_position_avg_price=float(position["avg_price"]) if position is not None else 0.0,
            run_db_path=str(self._run_db_path),
        )


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_EXECUTION_QUANTITY",
    "ExecutedSignal",
    "StrategyExecutionResult",
    "StrategyExecutor",
]
