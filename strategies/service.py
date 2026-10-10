"""策略服务层（Stage 19）：Strategy / Strategy Version / 执行入口 的统一门面。

分层：

    API / 前端 / 复盘系统 / 策略系统
                ↓
          StrategyService          本文件：只做编排，不写 SQL
                ↓
    StrategyDefinitionRepository / StrategyRepository / SignalRepository / TradeRepository
                ↓
          StrategyExecutor         逐根 K 线 → Signal → Trade（复用 ExecutionEngine）

职责：
    1. 创建 / 查询 Strategy（身份）与 Strategy Version（版本 + config）
    2. **追溯**任意 Signal / Trade 来自哪个策略、哪个版本
       （Signal → strategies(版本) → strategy_definitions(策略)；Trade → Signal → …）
    3. 按「策略 + 版本 + 标的 + 周期 + 时间范围」调用执行器（Executor 调用入口）

为后续系统预留：
    - 复盘系统：用 trace_signal / trace_trade 读到策略与版本，再按版本聚合执行结果
    - 策略系统：用 create_strategy / create_version 产出新版本，交给 run_strategy 验证
    - 两条路径都只依赖本门面，不直接依赖 Executor 内部细节
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from database import (
    SignalRepository,
    StrategyDefinitionRepository,
    StrategyRepository,
    TradeRepository,
    resolve_database_path,
)
from database.validation import require_positive_int

from .executor import StrategyExecutionResult, StrategyExecutor
from .runner import DEFAULT_STRATEGY_TYPE, STRATEGY_TYPE_KEY


class StrategyService:
    """Strategy / Strategy Version / 执行入口 的门面。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = resolve_database_path(db_path)
        self.definitions = StrategyDefinitionRepository(self._db_path)
        self.versions = StrategyRepository(self._db_path)
        self.signals = SignalRepository(self._db_path)
        self.trades = TradeRepository(self._db_path)

    # ------------------------------------------------------------------ 属性
    @property
    def db_path(self) -> Path:
        """服务使用的数据库路径。"""
        return self._db_path

    # ------------------------------------------------------- Strategy（身份）
    def create_strategy(
        self,
        name: str,
        *,
        description: str | None = None,
        status: str | None = None,
    ) -> int:
        """创建一个 Strategy（身份），返回 strategy_id（name 唯一，不覆盖同名策略）。"""
        if status is None:
            return self.definitions.create_definition(name, description=description)
        return self.definitions.create_definition(
            name, description=description, status=status
        )

    def get_strategy(self, strategy_id: int) -> dict[str, Any] | None:
        """按 id 查询 Strategy。"""
        return self.definitions.get_definition(strategy_id)

    def list_strategies(self, *, status: str | None = None) -> list[dict[str, Any]]:
        """列出 Strategy（身份）。"""
        return self.definitions.list_definitions(status=status)

    # ------------------------------------------------- Strategy Version（版本）
    def create_version(
        self,
        strategy_id: int,
        config: Mapping[str, Any],
        *,
        version: int | None = None,
        status: str | None = None,
    ) -> int:
        """给指定 Strategy 新建一个版本，返回 strategy_version_id（strategies.id）。

        version 不传时自动取「当前最大版本 + 1」；参数全部放在 config（结构化 JSON）里，
        不需要为每个参数改数据库结构。
        """
        if status is None:
            return self.versions.create_version(
                strategy_id, config, version=version
            )
        return self.versions.create_version(
            strategy_id, config, version=version, status=status
        )

    def get_version(self, strategy_version_id: int) -> dict[str, Any] | None:
        """按版本 id 查询版本。"""
        return self.versions.get_version(strategy_version_id)

    def list_versions(self, strategy_id: int) -> list[dict[str, Any]]:
        """列出某个 Strategy 的全部版本（按版本号升序）。"""
        return self.versions.list_versions(strategy_id)

    # ------------------------------------------------------------------ 追溯
    def trace_signal(self, signal_id: int) -> dict[str, Any] | None:
        """追溯一个 Signal 来自哪个策略、哪个版本（不存在时返回 None）。"""
        signal = self.signals.get_signal(require_positive_int(signal_id, "signal_id"))
        if signal is None:
            return None
        trace = self._trace_version(int(signal["strategy_id"]))
        if trace is None:
            return None
        return {"signal_id": int(signal["id"]), **trace}

    def trace_trade(self, trade_id: int) -> dict[str, Any] | None:
        """追溯一笔 Trade 来自哪个策略、哪个版本（无对应 Signal 时返回 None）。"""
        trade = self.trades.get_trade(require_positive_int(trade_id, "trade_id"))
        if trade is None:
            return None
        signal_id = trade["signal_id"]
        if signal_id is None:
            return None  # 手工成交没有 Signal，无法追溯策略版本
        trace = self.trace_signal(int(signal_id))
        if trace is None:
            return None
        return {"trade_id": int(trade["id"]), "signal_id": int(signal_id), **{
            key: value for key, value in trace.items() if key != "signal_id"
        }}

    def _trace_version(self, strategy_version_id: int) -> dict[str, Any] | None:
        """版本 id → 策略 + 版本信息（Signal / Trade 追溯的公共部分）。"""
        version = self.versions.get_version(strategy_version_id)
        if version is None:
            return None
        definition = (
            self.definitions.get_definition(int(version["strategy_id"]))
            if version["strategy_id"] is not None
            else None
        )
        config = version["config"] if isinstance(version["config"], Mapping) else {}
        return {
            "strategy_id": int(definition["id"]) if definition is not None else None,
            "strategy_name": definition["name"] if definition is not None else version["name"],
            "strategy_version_id": int(version["id"]),
            "strategy_version": int(version["version"]),
            "strategy_type": str(config.get(STRATEGY_TYPE_KEY, DEFAULT_STRATEGY_TYPE)),
        }

    # ------------------------------------------------------------------ 执行
    def run_strategy(
        self,
        strategy_id: int,
        strategy_version_id: int,
        symbol_id: int,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
        run_db_path: str | Path | None = None,
    ) -> StrategyExecutionResult:
        """Executor 调用入口：按指定策略版本执行一段时间范围的 K 线。

        默认在临时运行库里执行并自动清理（正式库只读）；
        传入 run_db_path 可保留运行库以便事后查看。
        """
        executor = StrategyExecutor(
            source_db_path=self._db_path, run_db_path=run_db_path
        )
        try:
            return executor.run(
                strategy_id,
                strategy_version_id,
                symbol_id,
                timeframe,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        finally:
            executor.close()


__all__ = ["StrategyService"]
