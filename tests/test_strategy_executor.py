"""Stage 19：通用执行器（StrategyExecutor）+ 服务层 + API 的测试。

覆盖要求中的：
    4. Executor 可以指定 Strategy Version
    5. Executor 使用指定 Strategy Version 执行
    6. Signal 能追溯 strategy_id / strategy_version_id
    7. Trade 能追溯 strategy_id / strategy_version_id
    8. 不同 Strategy Version 可以分别执行
    9. Strategy 在 K 线 K 时不能访问 K+1（未来数据隔离）
   10. Demo Strategy 逐根执行结果稳定、可重复
    + 12 项验收：同一数据 + 同一版本重复执行结果一致；正式库不被写入
    + API：新增的 Executor / Strategy / 版本 / 追溯端点
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api import create_app  # noqa: E402
from database import (  # noqa: E402
    KlineRepository,
    SignalRepository,
    StrategyDefinitionRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
)
from strategies import (  # noqa: E402
    BarWindow,
    SignalCandidate,
    Strategy,
    StrategyExecutionResult,
    StrategyExecutor,
    StrategyService,
)

#: 确定性收盘价序列（设计成同时出现 long / short / 无信号）
CLOSES = [100.0, 101.0, 100.5, 102.0, 101.8, 101.8, 103.0, 102.0]

#: v1：min_move 默认 0 → 6 条信号（3 long / 3 short），1 根无信号
V1_CONFIG: dict[str, Any] = {"type": "close_direction"}
#: v2：min_move 1% → 只有 3 条（全部 long），与 v1 结果不同
V2_CONFIG: dict[str, Any] = {"type": "close_direction", "min_move": 0.01}

START = datetime(2026, 1, 1, 9, 30, 0)

#: 探测策略实例登记表（from_config 里按 key 注册，便于测试读取它看到过的窗口）
PROBES: dict[str, "ProbeStrategy"] = {}


def bar_timestamp(index: int) -> str:
    """第 index 根 K 线的时间戳（15 分钟周期）。"""
    return (START + timedelta(minutes=15 * index)).strftime("%Y-%m-%d %H:%M:%S")


def kline_rows(symbol_id: int, closes: list[float], timeframe: str = "15m") -> list[dict[str, Any]]:
    """构造满足 OHLC CHECK 约束的 K 线行。"""
    return [
        {
            "symbol_id": symbol_id,
            "timeframe": timeframe,
            "timestamp": bar_timestamp(index),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1000.0,
            "amount": None,
        }
        for index, close in enumerate(closes)
    ]


class ProbeStrategy(Strategy):
    """探测策略：记录每根 K 线上窗口里究竟能看到什么（不产生信号）。"""

    name: ClassVar[str] = "probe"

    def __init__(self, key: str = "default") -> None:
        self.key = key
        self.observations: list[dict[str, Any]] = []

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ProbeStrategy":
        instance = cls(str(config.get("key", "default")))
        PROBES[instance.key] = instance
        return instance

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        self.observations.append(
            {
                "index": window.index,
                "current": window.current.timestamp,
                "history_len": len(window.history),
                "seen_len": len(window.seen),
                "last_history": window.history[-1].timestamp if window.history else None,
                # 窗口上不应存在任何能取到未来数据的属性
                "future_attrs": [
                    name
                    for name in ("bars", "future", "all", "next", "series")
                    if hasattr(window, name)
                ],
            }
        )
        return None


class FutureTimestampStrategy(Strategy):
    """故意把信号挂到未来的策略（Executor 必须拒绝）。"""

    name: ClassVar[str] = "future_timestamp"

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        return SignalCandidate(
            timestamp="2099-01-01 00:00:00", direction="long", price=window.current.close
        )


class ExecutorTestCase(unittest.TestCase):
    """基类：临时正式库（symbol + klines + Strategy + 两个版本）+ 服务。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_executor_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.source_db = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db)

        self.symbol_id = SymbolRepository(self.source_db).create_symbol(
            "600519", "贵州茅台", "CN", "STOCK"
        )
        KlineRepository(self.source_db).bulk_upsert_klines(
            kline_rows(self.symbol_id, CLOSES)
        )

        self.service = StrategyService(self.source_db)
        self.strategy_id = self.service.create_strategy("Support_Rebound", description="支撑位反弹")
        self.v1 = self.service.create_version(self.strategy_id, V1_CONFIG, status="active")
        self.v2 = self.service.create_version(self.strategy_id, V2_CONFIG, status="active")

    def run_path(self, name: str) -> Path:
        return Path(self._tmpdir.name) / f"{name}.db"


class StrategyVersionExecutionTest(ExecutorTestCase):
    """Executor 指定 / 使用某个策略版本执行。"""

    def test_01_executor_runs_specified_version(self) -> None:
        result = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_path("v1")
        )
        self.assertIsInstance(result, StrategyExecutionResult)
        self.assertEqual(result.strategy_id, self.strategy_id)
        self.assertEqual(result.strategy_version_id, self.v1)
        self.assertEqual(result.strategy_version, 1)
        self.assertEqual(result.strategy_name, "Support_Rebound")
        self.assertEqual(result.strategy_type, "close_direction")
        self.assertEqual(result.bars_processed, len(CLOSES))
        self.assertEqual(result.signals_generated, 6)
        self.assertEqual((result.long_signals, result.short_signals), (3, 3))
        self.assertEqual(result.trades_executed, 6, "每个信号都应有成交")
        self.assertEqual(len(result.executions), 6)
        self.assertEqual(
            [execution.side for execution in result.executions],
            ["buy", "sell", "buy", "sell", "buy", "sell"],
        )
        # 成交时间取信号所在 K 线时间（不能用当前时间）
        self.assertEqual(
            [execution.timestamp for execution in result.executions],
            [bar_timestamp(1), bar_timestamp(2), bar_timestamp(3), bar_timestamp(4), bar_timestamp(6), bar_timestamp(7)],
        )

    def test_02_signals_and_trades_land_in_run_db_only(self) -> None:
        run_db = self.run_path("v1")
        self.service.run_strategy(self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=run_db)

        signals = SignalRepository(run_db).list_signals()
        trades = TradeRepository(run_db).list_trades()
        self.assertEqual(len(signals), 6)
        self.assertEqual(len(trades), 6)
        self.assertTrue(all(row["strategy_id"] == self.v1 for row in signals))

        # 正式库（数据源）全程只读
        self.assertEqual(len(SignalRepository(self.source_db).list_signals()), 0)
        self.assertEqual(len(TradeRepository(self.source_db).list_trades()), 0)

    def test_03_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            self.service.run_strategy(999_999, self.v1, self.symbol_id, "15m")
        with self.assertRaises(ValueError):
            self.service.run_strategy(self.strategy_id, 999_999, self.symbol_id, "15m")
        with self.assertRaises(ValueError):
            self.service.run_strategy(self.strategy_id, self.v1, 999_999, "15m")

        # 版本属于另一个策略 → 拒绝（避免张冠李戴）
        other = self.service.create_strategy("other_strategy")
        other_version = self.service.create_version(other, V1_CONFIG)
        with self.assertRaises(ValueError):
            self.service.run_strategy(self.strategy_id, other_version, self.symbol_id, "15m")

        with self.assertRaises(ValueError):
            StrategyExecutor(source_db_path=self.source_db, quantity=0)

    def test_04_extended_demo_requirements(self) -> None:
        """最小 Demo Strategy：long / short / 无信号都能出现，且统计正确。"""
        result = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_path("v1")
        )
        self.assertGreater(result.long_signals, 0)
        self.assertGreater(result.short_signals, 0)
        # 第 0 根没有可比对象、第 5 根与前一根收盘相同 → 两者都不产生候选信号
        self.assertEqual(
            result.signals_generated + result.skipped, 6, "有候选信号的 K 线共 6 根"
        )
        executed_timestamps = [e.timestamp for e in result.executions]
        self.assertNotIn(bar_timestamp(0), executed_timestamps)
        self.assertNotIn(bar_timestamp(5), executed_timestamps)


class TraceabilityTest(ExecutorTestCase):
    """Signal / Trade 追溯策略与版本。"""

    def setUp(self) -> None:
        super().setUp()
        self.run_db = self.run_path("trace")
        self.result = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_db
        )
        # 追溯在「运行库」上验证（执行结果落在运行库）
        self.run_service = StrategyService(self.run_db)

    def test_01_signal_keeps_version_reference(self) -> None:
        signal = SignalRepository(self.run_db).list_signals()[0]
        self.assertEqual(signal["strategy_id"], self.v1, "signals.strategy_id 指向版本记录")

        trace = self.run_service.trace_signal(int(signal["id"]))
        self.assertEqual(trace["signal_id"], signal["id"])
        self.assertEqual(trace["strategy_id"], self.strategy_id)
        self.assertEqual(trace["strategy_version_id"], self.v1)
        self.assertEqual(trace["strategy_version"], 1)
        self.assertEqual(trace["strategy_name"], "Support_Rebound")
        self.assertEqual(trace["strategy_type"], "close_direction")

    def test_02_trade_traceable_through_signal(self) -> None:
        trade = TradeRepository(self.run_db).list_trades()[0]
        trace = self.run_service.trace_trade(int(trade["id"]))
        self.assertEqual(trace["trade_id"], trade["id"])
        self.assertEqual(trace["strategy_id"], self.strategy_id)
        self.assertEqual(trace["strategy_version_id"], self.v1)
        self.assertEqual(trace["strategy_version"], 1)

    def test_03_unknown_ids_return_none(self) -> None:
        self.assertIsNone(self.run_service.trace_signal(999_999))
        self.assertIsNone(self.run_service.trace_trade(999_999))


class MultipleVersionsTest(ExecutorTestCase):
    """不同版本分别执行、互不干扰。"""

    def test_01_versions_run_independently(self) -> None:
        v1_result = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_path("v1")
        )
        v2_result = self.service.run_strategy(
            self.strategy_id, self.v2, self.symbol_id, "15m", run_db_path=self.run_path("v2")
        )

        self.assertEqual(v1_result.signals_generated, 6)
        self.assertEqual(v2_result.signals_generated, 3)
        self.assertEqual(v2_result.long_signals, 3)
        self.assertEqual(v2_result.short_signals, 0)
        self.assertNotEqual(v1_result.executions, v2_result.executions)

        # 每个运行库里只应有自己版本的信号（运行库是镜像库，按库内版本 id 比对）
        v1_run_db, v2_run_db = self.run_path("v1"), self.run_path("v2")
        v1_signals = SignalRepository(v1_run_db).list_signals()
        v2_signals = SignalRepository(v2_run_db).list_signals()
        v1_run_version_id = self._run_version_id(v1_run_db, 1)
        v2_run_version_id = self._run_version_id(v2_run_db, 2)
        self.assertTrue(all(row["strategy_id"] == v1_run_version_id for row in v1_signals))
        self.assertTrue(all(row["strategy_id"] == v2_run_version_id for row in v2_signals))

        # 追溯能区分两个版本
        v2_signal = v2_signals[0]
        trace = StrategyService(v2_run_db).trace_signal(int(v2_signal["id"]))
        self.assertEqual(trace["strategy_version_id"], v2_run_version_id)
        self.assertEqual(trace["strategy_version"], 2)
        self.assertEqual(trace["strategy_name"], "Support_Rebound")

    def _run_version_id(self, run_db: Path, version: int) -> int:
        """取镜像库里的版本记录 id（镜像库有自己的一套 id）。"""
        definition = StrategyDefinitionRepository(run_db).get_definition_by_name("Support_Rebound")
        self.assertIsNotNone(definition)
        row = StrategyRepository(run_db).get_version_by_number(int(definition["id"]), version)
        self.assertIsNotNone(row)
        return int(row["id"])


class InPlaceExecutionTest(ExecutorTestCase):
    """显式在目标库里就地执行（不建临时镜像）：追溯在同一库内闭环。"""

    def test_01_execute_in_place_and_trace(self) -> None:
        result = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.source_db
        )
        self.assertEqual(result.strategy_version_id, self.v1)

        signals = SignalRepository(self.source_db).list_signals()
        self.assertEqual(len(signals), 6)
        self.assertTrue(
            all(row["strategy_id"] == self.v1 for row in signals),
            "就地执行时信号直接引用正式库里的版本 id",
        )

        trace = self.service.trace_signal(int(signals[0]["id"]))
        self.assertEqual(trace["strategy_id"], self.strategy_id)
        self.assertEqual(trace["strategy_version_id"], self.v1)
        self.assertEqual(trace["strategy_version"], 1)

        trade = TradeRepository(self.source_db).list_trades()[0]
        trade_trace = self.service.trace_trade(int(trade["id"]))
        self.assertEqual(trade_trace["strategy_id"], self.strategy_id)
        self.assertEqual(trade_trace["strategy_version_id"], self.v1)


class NoLookAheadTest(ExecutorTestCase):
    """未来数据隔离（硬约束）。"""

    def _probe_version(self, key: str) -> int:
        return self.service.create_version(
            self.strategy_id, {"type": ProbeStrategy.name, "key": key}
        )

    def test_01_window_contains_only_past_and_current(self) -> None:
        version = self._probe_version("window")
        executor = StrategyExecutor(
            source_db_path=self.source_db,
            run_db_path=self.run_path("probe"),
            strategies={ProbeStrategy.name: ProbeStrategy},
        )
        try:
            executor.run(self.strategy_id, version, self.symbol_id, "15m")
        finally:
            executor.close()

        observations = PROBES["window"].observations
        self.assertEqual(len(observations), len(CLOSES), "策略应逐根被调用")

        for index, observation in enumerate(observations):
            with self.subTest(index=index):
                self.assertEqual(observation["index"], index)
                self.assertEqual(observation["current"], bar_timestamp(index))
                self.assertEqual(observation["history_len"], index, "历史长度应等于已发生的根数")
                self.assertEqual(observation["seen_len"], index + 1)
                if index == 0:
                    self.assertIsNone(observation["last_history"])
                else:
                    self.assertEqual(observation["last_history"], bar_timestamp(index - 1))
                self.assertEqual(observation["future_attrs"], [], "窗口不应暴露未来数据入口")

    def test_02_prefix_invariance(self) -> None:
        cutoff = bar_timestamp(4)
        full = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_path("full")
        )
        truncated = self.service.run_strategy(
            self.strategy_id,
            self.v1,
            self.symbol_id,
            "15m",
            end_timestamp=cutoff,
            run_db_path=self.run_path("truncated"),
        )

        def fingerprint(executions: Any) -> list[tuple[Any, ...]]:
            return [
                (e.timestamp, e.direction, e.price, e.reason)
                for e in executions
                if e.timestamp <= cutoff
            ]

        self.assertEqual(
            fingerprint(full.executions),
            fingerprint(truncated.executions),
            "截断未来数据后，前缀上的信号必须完全一致（否则存在未来数据泄漏）",
        )
        self.assertEqual(truncated.bars_processed, 5)
        self.assertLessEqual(truncated.trades_executed, full.trades_executed)

    def test_03_strategy_cannot_stamp_signal_in_the_future(self) -> None:
        version = self.service.create_version(
            self.strategy_id, {"type": FutureTimestampStrategy.name}
        )
        executor = StrategyExecutor(
            source_db_path=self.source_db,
            run_db_path=self.run_path("future"),
            strategies={FutureTimestampStrategy.name: FutureTimestampStrategy},
        )
        try:
            with self.assertRaises(ValueError) as context:
                executor.run(self.strategy_id, version, self.symbol_id, "15m")
            self.assertIn("未来", str(context.exception))
            # 拒绝时不应落任何信号
            self.assertEqual(len(SignalRepository(self.run_path("future")).list_signals()), 0)
        finally:
            executor.close()


class ReproducibilityTest(ExecutorTestCase):
    """同一份数据 + 同一个版本重复执行：结果一致、可重复。"""

    def test_01_two_runs_are_identical(self) -> None:
        first = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_path("run1")
        )
        second = self.service.run_strategy(
            self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=self.run_path("run2")
        )

        self.assertEqual(first.executions, second.executions)
        self.assertEqual(
            (first.signals_generated, first.long_signals, first.short_signals, first.trades_executed),
            (second.signals_generated, second.long_signals, second.short_signals, second.trades_executed),
        )
        self.assertEqual(
            (first.final_position_quantity, first.final_position_avg_price),
            (second.final_position_quantity, second.final_position_avg_price),
        )

        def signal_rows(path: Path) -> list[tuple[Any, ...]]:
            return [
                (row["strategy_id"], row["timestamp"], row["direction"], row["price"], row["reason"], row["status"])
                for row in SignalRepository(path).list_signals()
            ]

        def trade_rows(path: Path) -> list[tuple[Any, ...]]:
            return [
                (row["side"], row["quantity"], row["price"], row["timestamp"], row["fee"])
                for row in TradeRepository(path).list_trades()
            ]

        self.assertEqual(signal_rows(self.run_path("run1")), signal_rows(self.run_path("run2")))
        self.assertEqual(trade_rows(self.run_path("run1")), trade_rows(self.run_path("run2")))

    def test_02_rerun_into_same_run_db_is_idempotent(self) -> None:
        run_db = self.run_path("same")
        first = self.service.run_strategy(self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=run_db)
        second = self.service.run_strategy(self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=run_db)

        self.assertEqual(first.signals_generated, 6)
        self.assertEqual(second.signals_generated, 0)
        self.assertEqual(second.skipped, 6)
        self.assertEqual(second.trades_executed, 0)
        self.assertEqual(len(SignalRepository(run_db).list_signals()), 6, "不应重复写入信号")
        self.assertEqual(len(TradeRepository(run_db).list_trades()), 6, "不应重复成交")

    def test_03_source_database_stays_clean(self) -> None:
        self.service.run_strategy(self.strategy_id, self.v1, self.symbol_id, "15m")
        self.assertEqual(len(KlineRepository(self.source_db).list_klines(self.symbol_id, "15m")), len(CLOSES))
        self.assertEqual(len(SignalRepository(self.source_db).list_signals()), 0)
        self.assertEqual(len(TradeRepository(self.source_db).list_trades()), 0)


class ExecutorApiTest(ExecutorTestCase):
    """API：Strategy / 版本 / Executor 调用入口 / 追溯。"""

    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(db_path=self.source_db))

    def test_01_list_strategies_and_versions(self) -> None:
        response = self.client.get("/strategy-definitions")
        self.assertEqual(response.status_code, 200)
        names = [row["name"] for row in response.json()]
        self.assertEqual(names, ["Support_Rebound"])

        response = self.client.get(f"/strategy-definitions/{self.strategy_id}/versions")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([row["version"] for row in body], [1, 2])
        self.assertEqual([row["config"] for row in body], [V1_CONFIG, V2_CONFIG])
        self.assertEqual(self.client.get("/strategy-definitions/99999/versions").status_code, 404)

    def test_02_executor_endpoint_runs_version(self) -> None:
        response = self.client.post(
            "/executor/run",
            json={
                "strategy_id": self.strategy_id,
                "strategy_version_id": self.v2,
                "symbol_id": self.symbol_id,
                "timeframe": "15m",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["strategy_id"], self.strategy_id)
        self.assertEqual(body["strategy_version_id"], self.v2)
        self.assertEqual(body["strategy_version"], 2)
        self.assertEqual(body["signals_generated"], 3)
        self.assertEqual(body["trades_executed"], 3)
        self.assertEqual(len(body["executions"]), 3)
        # 正式库不被写入
        self.assertEqual(len(SignalRepository(self.source_db).list_signals()), 0)

    def test_03_executor_endpoint_errors(self) -> None:
        base = {
            "strategy_id": self.strategy_id,
            "strategy_version_id": self.v1,
            "symbol_id": self.symbol_id,
            "timeframe": "15m",
        }
        missing_strategy = self.client.post("/executor/run", json={**base, "strategy_id": 99999})
        self.assertEqual(missing_strategy.status_code, 404)

        missing_version = self.client.post("/executor/run", json={**base, "strategy_version_id": 99999})
        self.assertEqual(missing_version.status_code, 404)

        unknown_symbol = self.client.post("/executor/run", json={**base, "symbol_id": 99999})
        self.assertEqual(unknown_symbol.status_code, 404)

        other = self.service.create_strategy("other_strategy")
        other_version = self.service.create_version(other, V1_CONFIG)
        mismatch = self.client.post(
            "/executor/run", json={**base, "strategy_version_id": other_version}
        )
        self.assertEqual(mismatch.status_code, 409)

        bad_timestamp = self.client.post(
            "/executor/run", json={**base, "start_timestamp": "2026/01/01"}
        )
        self.assertEqual(bad_timestamp.status_code, 400)

        bad_body = self.client.post("/executor/run", json={**base, "timeframe": ""})
        self.assertEqual(bad_body.status_code, 422)

    def test_04_trace_endpoints(self) -> None:
        run_db = self.run_path("api_trace")
        self.service.run_strategy(self.strategy_id, self.v1, self.symbol_id, "15m", run_db_path=run_db)
        client = TestClient(create_app(db_path=run_db))

        signal_id = int(SignalRepository(run_db).list_signals()[0]["id"])
        trade_id = int(TradeRepository(run_db).list_trades()[0]["id"])

        signal_trace = client.get(f"/signals/{signal_id}/trace")
        self.assertEqual(signal_trace.status_code, 200, signal_trace.text)
        self.assertEqual(signal_trace.json()["strategy_id"], self.strategy_id)
        self.assertEqual(signal_trace.json()["strategy_version_id"], self.v1)

        trade_trace = client.get(f"/trades/{trade_id}/trace")
        self.assertEqual(trade_trace.status_code, 200, trade_trace.text)
        self.assertEqual(trade_trace.json()["strategy_version_id"], self.v1)
        self.assertEqual(trade_trace.json()["strategy_version"], 1)

        self.assertEqual(client.get("/signals/999999/trace").status_code, 404)
        self.assertEqual(client.get("/trades/999999/trace").status_code, 404)

    def test_05_existing_endpoints_still_work(self) -> None:
        """Stage 16/18 用到的既有端点必须保持不变。"""
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/strategies").status_code, 200)
        self.assertEqual(len(self.client.get("/strategies").json()), 2)
        self.assertEqual(self.client.get("/symbols").json()[0]["symbol"], "600519")
        self.assertEqual(
            self.client.get(
                "/klines", params={"symbol_id": self.symbol_id, "timeframe": "15m"}
            ).status_code,
            200,
        )
        run = self.client.post(
            f"/strategies/{self.v1}/run",
            json={"symbol_id": self.symbol_id, "timeframe": "15m"},
        )
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["signals"], 6)


if __name__ == "__main__":
    unittest.main()
