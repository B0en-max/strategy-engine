"""基础回测引擎（Stage 14）。

只做一件事：**K 线 → 策略信号 → 模拟执行 → 回测结果**。

    ┌─────────────── 正式数据库（只读） ───────────────┐
    │  klines / strategies / symbols                  │
    └───────────────────┬─────────────────────────────┘
                        │ 复制本次回测需要的策略 / 标的 / K 线
                        ▼
    ┌─────────────── 独立临时运行库（读写）─────────────┐
    │  signals → orders → trades → positions          │
    └───────────────────┬─────────────────────────────┘
                        ▼
                   BacktestResult

隔离保证（本阶段硬性要求）：
    - **只读**正式数据库：回测不向正式库写入任何 signals / orders / trades / positions，
      也不读正式库里的历史 signals / orders / trades / positions 当结果。
    - 每次回测都在**独立的临时数据库**里运行（默认自动创建、结束后清理；
      也可以传入固定的 run_db_path 以便事后查看）。
    - 不依赖正式 signals 表的"重复运行跳过"机制：运行库是新的，每次回测从零开始。

复用的现有组件：
    strategies.StrategyRunner  策略计算 → 写 signals
    execution.ExecutionEngine  signals → orders / trades / positions
    database.*                 各业务表的 Repository（回测引擎里不写 SQL）

本阶段**不做**：手续费 / 滑点 / 资金管理 / 杠杆 / 保证金 / 复杂绩效指标。
成交规则：long → buy、short → sell，成交价取信号对应 K 线的收盘价，每次固定数量 1。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from database import (
    KlineRepository,
    PositionRepository,
    SignalRepository,
    StrategyRepository,
    SymbolRepository,
    initialize_database,
    resolve_database_path,
)
from execution import ExecutionEngine
from strategies import StrategyRunner

#: 每次信号固定成交数量（本阶段写死为 1）
DEFAULT_BACKTEST_QUANTITY: Final[float] = 1.0


@dataclass(frozen=True)
class BacktestResult:
    """一次回测的结果。"""

    #: 策略 id（正式库中的 id）
    strategy_id: int
    #: 标的 id（正式库中的 id）
    symbol_id: int
    timeframe: str
    start_timestamp: str | None
    end_timestamp: str | None
    #: 参与回测的 K 线根数
    bars_processed: int
    #: 策略产生的信号数
    signals_generated: int
    #: 实际成交（生成 Trade）的笔数
    trades_executed: int
    #: 回测结束时的持仓数量与平均成本
    final_position_quantity: float
    final_position_avg_price: float
    #: 本次回测使用的临时运行库路径（便于事后查看）
    run_db_path: str


class BacktestEngine:
    """最小回测引擎：在独立临时库里跑一条完整链路。"""

    def __init__(
        self,
        *,
        source_db_path: str | Path | None = None,
        run_db_path: str | Path | None = None,
    ) -> None:
        # 数据来源（默认正式库，只读）
        self._source_db_path = resolve_database_path(source_db_path)
        # 回测运行库（默认临时目录，自动清理；显式指定则保留）
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        if run_db_path is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="se_backtest_")
            self._run_db_path = Path(self._temp_dir.name) / "backtest.db"
        else:
            self._run_db_path = resolve_database_path(run_db_path)

    # ------------------------------------------------------------------ 属性
    @property
    def source_db_path(self) -> Path:
        """数据来源数据库（只读）。"""
        return self._source_db_path

    @property
    def run_db_path(self) -> Path:
        """本次回测的临时运行库。"""
        return self._run_db_path

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        """清理临时运行库（显式指定 run_db_path 时不动它）；可重复调用。"""
        if self._temp_dir is not None:
            self._temp_dir.cleanup()

    def __enter__(self) -> "BacktestEngine":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 准备
    def prepare_run_db(
        self,
        strategy_id: int,
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> tuple[int, int, int]:
        """把本次回测需要的策略 / 标的 / K 线复制进临时运行库。

        返回 (运行库里的 strategy_id, 运行库里的 symbol_id, 复制的 K 线根数)。
        """
        source_strategies = StrategyRepository(self._source_db_path)
        source_symbols = SymbolRepository(self._source_db_path)
        source_klines = KlineRepository(self._source_db_path)

        strategy_row = source_strategies.get_strategy(strategy_id)
        if strategy_row is None:
            raise ValueError(f"正式库中不存在策略: strategy_id={strategy_id}")
        symbol_row = source_symbols.get_symbol_by_id(symbol_id)
        if symbol_row is None:
            raise ValueError(f"正式库中不存在标的: symbol_id={symbol_id}")

        bars = source_klines.list_klines(
            symbol_id, timeframe, start_timestamp, end_timestamp
        )

        initialize_database(self._run_db_path)
        run_strategies = StrategyRepository(self._run_db_path)
        run_symbols = SymbolRepository(self._run_db_path)
        run_klines = KlineRepository(self._run_db_path)

        run_strategy_id = self._reuse_or_create_strategy(run_strategies, strategy_row)
        run_symbol_id = self._reuse_or_create_symbol(run_symbols, symbol_row)
        if bars:
            run_klines.bulk_upsert_klines(
                [
                    {
                        "symbol_id": run_symbol_id,
                        "timeframe": timeframe,
                        "timestamp": bar["timestamp"],
                        "open": bar["open"],
                        "high": bar["high"],
                        "low": bar["low"],
                        "close": bar["close"],
                        "volume": bar["volume"],
                        "amount": bar["amount"],
                    }
                    for bar in bars
                ]
            )
        return run_strategy_id, run_symbol_id, len(bars)

    @staticmethod
    def _reuse_or_create_strategy(run_strategies: StrategyRepository, source_row: Any) -> int:
        """运行库里已有同 (name, version) 的策略时直接复用（支持同一引擎多次 run）。"""
        existing = run_strategies.get_strategy_by_name_version(
            source_row["name"], int(source_row["version"])
        )
        if existing is not None:
            return int(existing["id"])
        return run_strategies.create_strategy(
            source_row["name"],
            source_row["config"],
            description=source_row["description"],
            status=source_row["status"],
            version=int(source_row["version"]),
        )

    @staticmethod
    def _reuse_or_create_symbol(run_symbols: SymbolRepository, source_row: Any) -> int:
        """运行库里已有同 (market, symbol) 的标的时直接复用。"""
        existing = run_symbols.get_symbol(source_row["market"], source_row["symbol"])
        if existing is not None:
            return int(existing["id"])
        return run_symbols.create_symbol(
            source_row["symbol"],
            source_row["name"],
            source_row["market"],
            source_row["asset_type"],
            is_active=int(source_row["is_active"]),
        )

    # ------------------------------------------------------------------ 运行
    def run(
        self,
        strategy_id: int,
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> BacktestResult:
        """在临时运行库里完成一次回测。

        参数：
            strategy_id     策略 id（从正式库读取并复制到运行库）
            symbol_id       标的 id（同上）
            timeframe       K 线周期
            start_timestamp 回测起始时间（含）
            end_timestamp   回测结束时间（含）

        返回：
            BacktestResult

        注意：
            - 正式库全程**只读**；回测产生的 signal / order / trade / position 只写在运行库
            - 每条信号用 ExecutionEngine 单独成一个事务（策略或执行报错时抛异常，
              已完成的成交保留在运行库里，便于排查；正式库不受影响）
        """
        run_strategy_id, run_symbol_id, bars_copied = self.prepare_run_db(
            strategy_id,
            symbol_id,
            timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        # 1) 策略计算 → 写 signals（运行库）
        runner = StrategyRunner(self._run_db_path)
        run_result = runner.run(
            run_strategy_id,
            run_symbol_id,
            timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        # 2) 模拟执行 → 写 orders / trades / positions（运行库）
        #    只执行本次新产生、仍为 pending 的信号（上一条已成交的信号不会重复执行）
        execution = ExecutionEngine(self._run_db_path)
        signals = SignalRepository(self._run_db_path).list_signals(
            strategy_id=run_strategy_id,
            symbol_id=run_symbol_id,
            timeframe=timeframe,
            status="pending",
        )
        trades_executed = 0
        for signal in signals:
            # 成交时间取信号所在 K 线的时间（回测是历史模拟，不能用当前时间）
            execution.execute_signal(
                int(signal["id"]),
                DEFAULT_BACKTEST_QUANTITY,
                timestamp=signal["timestamp"],
            )
            trades_executed += 1

        # 3) 汇总结果
        position = PositionRepository(self._run_db_path).get_position_by_symbol(run_symbol_id)
        return BacktestResult(
            strategy_id=strategy_id,
            symbol_id=symbol_id,
            timeframe=timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bars_processed=bars_copied,
            signals_generated=run_result.signals,
            trades_executed=trades_executed,
            final_position_quantity=float(position["quantity"]) if position is not None else 0.0,
            final_position_avg_price=float(position["avg_price"]) if position is not None else 0.0,
            run_db_path=str(self._run_db_path),
        )
