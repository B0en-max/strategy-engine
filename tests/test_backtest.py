"""Stage 14：基础回测引擎测试。

覆盖（按要求精简）：
    结果字段      十项必需字段 + 运行库路径
    K 线读取      指定时间范围、时间顺序、无数据
    策略信号      信号数 = 相邻 K 线比较次数
    long → buy    订单 / 成交 / 持仓
    short → sell  同上（含做空为负持仓）
    最终持仓      数量与平均成本（含买入后卖出回到 0）
    隔离          正式（来源）库只读；连续多次回测互相隔离；临时库会被清理
    错误处理      策略 / 标的不存在、策略类型未知

全部使用临时数据库，**不触碰 data/strategy_engine.db**。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import BacktestEngine  # noqa: E402
from database import (  # noqa: E402
    DEFAULT_DB_PATH,
    KlineRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
)
from database.models import SCHEMA_VERSION  # noqa: E402

T0 = "2026-01-01 09:30:00"
T1 = "2026-01-01 09:45:00"
T2 = "2026-01-01 10:00:00"
T3 = "2026-01-01 10:15:00"
T4 = "2026-01-01 10:30:00"


class BacktestTestCase(unittest.TestCase):
    """基类：临时“正式库”（只读来源）+ 各类工具。"""

    timeframe = "15m"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_backtest_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        self.run_dir = Path(self._tmpdir.name) / "runs"
        self.run_dir.mkdir()
        initialize_database(self.source_db_path)

        self.symbols = SymbolRepository(self.source_db_path)
        self.klines = KlineRepository(self.source_db_path)
        self.strategies = StrategyRepository(self.source_db_path)

        self.symbol_id = self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        self.strategy_id = self.strategies.create_strategy(
            "close_direction_v1", {"type": "close_direction"}
        )

    # ------------------------------------------------------------------ 来源库
    def add_bar(self, timestamp: str, close: float) -> int:
        """写入一根合法 K 线（high/low 围绕 open/close 构造，满足 OHLC 约束）。"""
        open_price = close - 1.0
        return self.klines.create_kline(
            self.symbol_id,
            self.timeframe,
            timestamp,
            open=open_price,
            high=max(open_price, close) + 1.0,
            low=min(open_price, close) - 1.0,
            close=close,
            volume=100.0,
        )

    def add_closes(self, closes: dict[str, float]) -> None:
        for timestamp, close in closes.items():
            self.add_bar(timestamp, close)

    # ------------------------------------------------------------------ 引擎
    def new_engine(self, **kwargs) -> BacktestEngine:
        """创建回测引擎（默认使用临时运行库），测试结束后自动清理。"""
        engine = BacktestEngine(source_db_path=self.source_db_path, **kwargs)
        self.addCleanup(engine.close)
        return engine

    def run_repos(self, engine: BacktestEngine) -> dict[str, object]:
        """打开运行库上的各 Repository（便于断言运行库内容）。"""
        path = engine.run_db_path
        return {
            "signals": SignalRepository(path),
            "orders": OrderRepository(path),
            "trades": TradeRepository(path),
            "positions": PositionRepository(path),
            "symbols": SymbolRepository(path),
            "strategies": StrategyRepository(path),
            "klines": KlineRepository(path),
        }

    def source_counts(self) -> dict[str, int]:
        """来源库中交易类数据的行数（回测后应保持 0）。"""
        return {
            "signals": self.klines.query_one("SELECT COUNT(*) AS n FROM signals")["n"],
            "orders": self.klines.query_one("SELECT COUNT(*) AS n FROM orders")["n"],
            "trades": self.klines.query_one("SELECT COUNT(*) AS n FROM trades")["n"],
            "positions": self.klines.query_one("SELECT COUNT(*) AS n FROM positions")["n"],
            "klines": self.klines.query_one("SELECT COUNT(*) AS n FROM klines")["n"],
        }


class BacktestResultTest(BacktestTestCase):
    """回测结果字段与计数。"""

    def test_01_result_fields_are_complete(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.strategy_id, self.strategy_id)
        self.assertEqual(result.symbol_id, self.symbol_id)
        self.assertEqual(result.timeframe, self.timeframe)
        self.assertIsNone(result.start_timestamp)
        self.assertIsNone(result.end_timestamp)
        self.assertEqual(result.bars_processed, 3)
        self.assertEqual(result.signals_generated, 2)
        self.assertEqual(result.trades_executed, 2)
        self.assertAlmostEqual(result.final_position_quantity, 2.0)
        self.assertAlmostEqual(result.final_position_avg_price, 115.0)
        self.assertEqual(result.run_db_path, str(engine.run_db_path))
        self.assertNotEqual(result.run_db_path, str(self.source_db_path))
        self.assertTrue(engine.run_db_path.exists())

    def test_02_time_range_is_applied_and_echoed(self) -> None:
        self.add_closes({T0: 100.0, T1: 101.0, T2: 102.0, T3: 103.0})
        engine = self.new_engine()

        result = engine.run(
            self.strategy_id, self.symbol_id, self.timeframe,
            start_timestamp=T1, end_timestamp=T2,
        )

        self.assertEqual(result.start_timestamp, T1)
        self.assertEqual(result.end_timestamp, T2)
        self.assertEqual(result.bars_processed, 2)
        self.assertEqual(result.signals_generated, 1)
        self.assertEqual(result.trades_executed, 1)

    def test_03_no_klines_gives_empty_result(self) -> None:
        engine = self.new_engine()
        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual((result.bars_processed, result.signals_generated, result.trades_executed), (0, 0, 0))
        self.assertAlmostEqual(result.final_position_quantity, 0.0)
        self.assertAlmostEqual(result.final_position_avg_price, 0.0)

    def test_04_signal_count_equals_adjacent_bar_comparisons(self) -> None:
        self.add_closes({T0: 100.0, T1: 101.0, T2: 102.0, T3: 103.0, T4: 104.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.bars_processed, 5)
        self.assertEqual(result.signals_generated, 4, "5 根 K 线产生 4 次相邻比较")
        self.assertEqual(result.trades_executed, 4)
        self.assertAlmostEqual(result.final_position_quantity, 4.0)


class BacktestDirectionTest(BacktestTestCase):
    """long → buy，short → sell，以及订单 / 成交 / 持仓内容。"""

    def test_01_long_creates_buy_order_trade_and_position(self) -> None:
        self.add_closes({T0: 100.0, T1: 111.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        repos = self.run_repos(engine)
        run_symbol_id = repos["symbols"].get_symbol("CN", "600519")["id"]

        orders = repos["orders"].list_orders()
        trades = repos["trades"].list_trades()
        position = repos["positions"].get_position_by_symbol(run_symbol_id)

        self.assertEqual(result.trades_executed, 1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["side"], "buy")
        self.assertEqual(orders[0]["order_type"], "market")
        self.assertEqual(orders[0]["status"], "filled")
        self.assertAlmostEqual(orders[0]["quantity"], 1.0)
        self.assertAlmostEqual(orders[0]["price"], 111.0)

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "buy")
        self.assertAlmostEqual(trades[0]["quantity"], 1.0)
        self.assertAlmostEqual(trades[0]["price"], 111.0, msg="成交价取信号所在 K 线的 close")
        self.assertAlmostEqual(trades[0]["fee"], 0.0)
        self.assertEqual(trades[0]["timestamp"], T1, "成交时间取信号所在 K 线时间")

        self.assertIsNotNone(position)
        self.assertAlmostEqual(position["quantity"], 1.0)
        self.assertAlmostEqual(position["avg_price"], 111.0)

    def test_02_short_creates_sell_trade_and_negative_position(self) -> None:
        self.add_closes({T0: 100.0, T1: 90.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.trades_executed, 1)
        self.assertAlmostEqual(result.final_position_quantity, -1.0)
        self.assertAlmostEqual(result.final_position_avg_price, 90.0)
        trades = self.run_repos(engine)["trades"].list_trades()
        self.assertEqual(trades[0]["side"], "sell")
        self.assertAlmostEqual(trades[0]["price"], 90.0)

    def test_03_short_then_buy_covers_position(self) -> None:
        # 100 → 90（short，卖 1）→ 95（long，买 1）→ 回到 0
        self.add_closes({T0: 100.0, T1: 90.0, T2: 95.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.signals_generated, 2)
        self.assertAlmostEqual(result.final_position_quantity, 0.0)
        self.assertAlmostEqual(result.final_position_avg_price, 0.0)

    def test_04_buy_then_sell_returns_to_zero(self) -> None:
        # 100 → 110（long，买 1）→ 105（short，卖 1）→ 回到 0
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertAlmostEqual(result.final_position_quantity, 0.0)
        self.assertAlmostEqual(result.final_position_avg_price, 0.0)

    def test_05_mixed_sequence_position_is_correct(self) -> None:
        # 100 → 110（买 1，qty=1 avg=110）→ 105（卖 1，qty=0）→ 115（买 1，qty=1 avg=115）
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0, T3: 115.0})
        engine = self.new_engine()

        result = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.trades_executed, 3)
        self.assertAlmostEqual(result.final_position_quantity, 1.0)
        self.assertAlmostEqual(result.final_position_avg_price, 115.0)

    def test_06_klines_are_processed_in_time_order(self) -> None:
        # 故意乱序写入来源库
        self.add_closes({T2: 102.0, T0: 100.0, T1: 101.0, T3: 99.0})
        engine = self.new_engine()

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        repos = self.run_repos(engine)

        trade_times = [row["timestamp"] for row in repos["trades"].list_trades()]
        signal_times = [row["timestamp"] for row in repos["signals"].list_signals()]
        # 时间顺序：T0=100 → T1=101（long）→ T2=102（long）→ T3=99（short）
        self.assertEqual(signal_times, [T1, T2, T3], "信号按 K 线时间升序")
        self.assertEqual(trade_times, [T1, T2, T3], "成交按同一顺序完成")
        self.assertEqual(trade_times, sorted(trade_times))

    def test_07_all_signals_are_executed_once(self) -> None:
        self.add_closes({T0: 100.0, T1: 105.0, T2: 103.0, T3: 108.0})
        engine = self.new_engine()

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        repos = self.run_repos(engine)
        signals = repos["signals"].list_signals()
        trades = repos["trades"].list_trades()

        self.assertTrue(all(row["status"] == "executed" for row in signals))
        self.assertEqual(len(trades), len(signals))
        self.assertEqual({row["signal_id"] for row in trades}, {row["id"] for row in signals})


class BacktestIsolationTest(BacktestTestCase):
    """隔离：来源库只读、多次回测互不影响、临时库清理。"""

    def test_01_source_database_is_not_modified(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0})
        before = self.source_counts()
        engine = self.new_engine()

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(self.source_counts(), before, "回测不得修改来源库")
        self.assertEqual(before["signals"], 0)
        self.assertEqual(before["orders"], 0)
        self.assertEqual(before["trades"], 0)
        self.assertEqual(before["positions"], 0)
        self.assertEqual(before["klines"], 3, "来源库 K 线数量不变")

    def test_02_run_database_is_separate_and_holds_the_backtest_data(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        engine = self.new_engine()

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        repos = self.run_repos(engine)

        self.assertNotEqual(engine.run_db_path, self.source_db_path)
        run_symbol_id = repos["symbols"].get_symbol("CN", "600519")["id"]
        self.assertEqual(
            repos["klines"].count_klines(run_symbol_id, self.timeframe), 2, "运行库里有本次回测的 K 线"
        )
        self.assertEqual(repos["trades"].count_trades(), 1)
        self.assertEqual(len(repos["positions"].list_positions()), 1)
        self.assertEqual(len(repos["strategies"].list_strategies()), 1)
        # 来源库依然是空的
        self.assertEqual(self.source_counts()["trades"], 0)

    def test_03_consecutive_backtests_are_isolated(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0})
        first = self.new_engine()
        second = self.new_engine()

        first_result = first.run(self.strategy_id, self.symbol_id, self.timeframe)
        second_result = second.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertNotEqual(first.run_db_path, second.run_db_path)
        self.assertEqual(first_result.trades_executed, 2)
        self.assertEqual(second_result.trades_executed, 2, "第二次回测不受第一次影响")
        self.assertEqual(
            self.run_repos(first)["trades"].count_trades(), 2, "各自运行库只含自己的成交"
        )
        self.assertEqual(self.run_repos(second)["trades"].count_trades(), 2)
        self.assertAlmostEqual(first_result.final_position_quantity, 2.0)
        self.assertAlmostEqual(second_result.final_position_quantity, 2.0)

    def test_04_same_engine_second_run_does_not_duplicate_trades(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0})
        engine = self.new_engine()

        first = engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        second = engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(first.trades_executed, 2)
        self.assertEqual(second.signals_generated, 0, "同一运行库内已有信号会被跳过")
        self.assertEqual(second.trades_executed, 0)
        self.assertEqual(self.run_repos(engine)["trades"].count_trades(), 2)
        self.assertAlmostEqual(second.final_position_quantity, 2.0)
        self.assertEqual(len(self.run_repos(engine)["strategies"].list_strategies()), 1)

    def test_05_explicit_run_db_path_is_kept(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        run_path = self.run_dir / "explicit_run.db"
        engine = self.new_engine(run_db_path=run_path)

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        engine.close()

        self.assertEqual(engine.run_db_path, run_path)
        self.assertTrue(run_path.exists(), "显式指定的运行库不会被删除")
        self.assertEqual(TradeRepository(run_path).count_trades(), 1)

    def test_06_default_run_db_is_cleaned_up(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        engine = self.new_engine()

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)
        run_path = engine.run_db_path
        self.assertTrue(run_path.exists())

        engine.close()

        self.assertFalse(run_path.exists(), "默认临时运行库在 close() 后应被清理")

    def test_07_context_manager_closes_and_cleans(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        with BacktestEngine(source_db_path=self.source_db_path) as engine:
            engine.run(self.strategy_id, self.symbol_id, self.timeframe)
            run_path = engine.run_db_path
            self.assertTrue(run_path.exists())

        self.assertFalse(run_path.exists())

    def test_08_run_db_uses_current_schema(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        engine = self.new_engine()

        engine.run(self.strategy_id, self.symbol_id, self.timeframe)

        version = self.run_repos(engine)["signals"].query_one("PRAGMA user_version")[0]
        self.assertEqual(version, SCHEMA_VERSION)

    def test_09_default_source_is_the_project_database(self) -> None:
        engine = BacktestEngine()
        self.addCleanup(engine.close)

        self.assertEqual(engine.source_db_path, DEFAULT_DB_PATH)
        self.assertNotEqual(engine.run_db_path, DEFAULT_DB_PATH, "回测结果不会写进正式库")


class BacktestErrorTest(BacktestTestCase):
    """错误处理：不存在的策略 / 标的 / 策略类型。"""

    def test_01_unknown_strategy_is_rejected(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        engine = self.new_engine()

        with self.assertRaises(ValueError):
            engine.run(999999, self.symbol_id, self.timeframe)

        self.assertFalse(engine.run_db_path.exists(), "校验失败不应创建运行库")

    def test_02_unknown_symbol_is_rejected(self) -> None:
        engine = self.new_engine()
        with self.assertRaises(ValueError):
            engine.run(self.strategy_id, 999999, self.timeframe)
        self.assertFalse(engine.run_db_path.exists())

    def test_03_unknown_strategy_type_is_rejected(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        bad_strategy_id = self.strategies.create_strategy("bad_type_v1", {"type": "no_such_type"})
        engine = self.new_engine()

        with self.assertRaises(ValueError):
            engine.run(bad_strategy_id, self.symbol_id, self.timeframe)

        repos = self.run_repos(engine)
        self.assertEqual(repos["signals"].list_signals(), [])
        self.assertEqual(repos["trades"].count_trades(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
