"""Stage 13：策略运行器测试。

覆盖（按要求精简）：
    读取策略配置    config 的 type 选择实现、缺省默认、未知类型报错、参数生效
    读取 K 线       按 symbol_id + timeframe，支持时间范围，升序参与计算
    long / short    涨 → long，跌 → short，相等 → 不产生信号
    信号落库        strategy_id / symbol_id / timeframe / timestamp / direction / price / reason
    时间顺序        信号按时间升序写入
    不产生交易对象  不创建 Order / Trade / Position
    重复运行        已有信号会被跳过（可重复运行）
    隔离            全部使用临时数据库，不污染 data/strategy_engine.db

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
from strategies import (  # noqa: E402
    Bar,
    CloseDirectionStrategy,
    Strategy,
    StrategyRunner,
)

T0 = "2026-01-01 09:30:00"
T1 = "2026-01-01 09:45:00"
T2 = "2026-01-01 10:00:00"
T3 = "2026-01-01 10:15:00"
T4 = "2026-01-01 10:30:00"


class AlwaysLongStrategy(Strategy):
    """测试用策略：每根 K 线（除第一根）都产生 long 信号。"""

    name = "always_long"

    def generate(self, bars, config):
        from strategies import SignalCandidate

        return [
            SignalCandidate(timestamp=bar.timestamp, direction="long", price=bar.close, reason="test")
            for bar in bars[1:]
        ]


class StrategyRunnerTestCase(unittest.TestCase):
    """基类：临时数据库 + 标的 + K 线/策略工具。"""

    timeframe = "15m"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_runner_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "runner_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)
        self.strategies = StrategyRepository(self.db_path)
        self.signals = SignalRepository(self.db_path)
        self.runner = StrategyRunner(self.db_path)

        self.symbol_id = self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        self.other_symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")

    # ------------------------------------------------------------------ 工具
    def new_strategy(self, config: dict | None = None, name: str = "close_direction_v1") -> int:
        return self.strategies.create_strategy(name, {"type": "close_direction"} if config is None else config)

    def add_bar(self, timestamp: str, close: float, *, symbol_id: int | None = None, timeframe: str | None = None) -> int:
        """写入一根合法的 K 线（high/low 围绕 open/close 构造，满足 OHLC 约束）。"""
        open_price = close - 1.0
        return self.klines.create_kline(
            self.symbol_id if symbol_id is None else symbol_id,
            timeframe or self.timeframe,
            timestamp,
            open=open_price,
            high=max(open_price, close) + 1.0,
            low=min(open_price, close) - 1.0,
            close=close,
            volume=100.0,
        )

    def add_closes(self, closes: dict[str, float], **kwargs) -> None:
        for timestamp, close in closes.items():
            self.add_bar(timestamp, close, **kwargs)

    def signals_of(self, strategy_id: int, symbol_id: int | None = None) -> list:
        return self.signals.list_signals(
            strategy_id=strategy_id,
            symbol_id=self.symbol_id if symbol_id is None else symbol_id,
            timeframe=self.timeframe,
        )


class RunConfigTest(StrategyRunnerTestCase):
    """读取策略配置。"""

    def test_01_config_type_selects_implementation(self) -> None:
        self.add_closes({T0: 100.0, T1: 101.0})
        strategy_id = self.new_strategy({"type": "close_direction"})

        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.strategy_type, "close_direction")
        self.assertEqual(result.strategy_id, strategy_id)
        self.assertEqual(result.strategy_name, "close_direction_v1")
        self.assertEqual(result.strategy_version, 1)
        self.assertEqual(result.symbol_id, self.symbol_id)
        self.assertEqual(result.timeframe, self.timeframe)

    def test_02_missing_type_falls_back_to_default(self) -> None:
        self.add_closes({T0: 100.0, T1: 101.0})
        strategy_id = self.new_strategy({})
        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)
        self.assertEqual(result.strategy_type, "close_direction")
        self.assertEqual(result.signals, 1)

    def test_03_unknown_type_is_rejected_without_writing(self) -> None:
        self.add_closes({T0: 100.0, T1: 101.0})
        strategy_id = self.new_strategy({"type": "does_not_exist"})

        with self.assertRaises(ValueError):
            self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(self.signals_of(strategy_id), [])

    def test_04_missing_strategy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.runner.run(999999, self.symbol_id, self.timeframe)

    def test_05_config_parameters_are_applied(self) -> None:
        # 涨跌幅：100→100.5（0.5%）、100.5→101（0.5%）、101→200（98%）
        self.add_closes({T0: 100.0, T1: 100.5, T2: 101.0, T3: 200.0})

        loose = self.new_strategy({"type": "close_direction"}, name="loose")
        strict = self.new_strategy({"type": "close_direction", "min_move": 0.05}, name="strict")

        loose_result = self.runner.run(loose, self.symbol_id, self.timeframe)
        strict_result = self.runner.run(strict, self.symbol_id, self.timeframe)

        self.assertEqual(loose_result.signals, 3)
        self.assertEqual(strict_result.signals, 1, "min_move=5% 时只有最后一根大幅上涨留下信号")
        self.assertEqual([s["timestamp"] for s in self.signals_of(strict)], [T3])

    def test_06_invalid_config_parameters_are_rejected(self) -> None:
        self.add_closes({T0: 100.0, T1: 101.0})
        for bad in ("abc", -1, True, [1]):
            with self.subTest(min_move=bad):
                strategy_id = self.new_strategy({"type": "close_direction", "min_move": bad}, name=f"bad_{bad}")
                with self.assertRaises(ValueError):
                    self.runner.run(strategy_id, self.symbol_id, self.timeframe)

    def test_07_registry_can_be_injected(self) -> None:
        # 策略实现不在 Runner 里写死：可以注入自己的策略
        self.add_closes({T0: 100.0, T1: 99.0, T2: 99.5})
        strategy_id = self.new_strategy({"type": "always_long"})
        runner = StrategyRunner(self.db_path, strategies={"always_long": AlwaysLongStrategy})

        result = runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.strategy_type, "always_long")
        self.assertEqual(result.signals, 2)
        self.assertEqual(result.long_signals, 2)
        self.assertEqual(result.short_signals, 0)
        self.assertIn("always_long", runner.available_strategy_types)


class RunBarsTest(StrategyRunnerTestCase):
    """读取 K 线。"""

    def test_01_reads_klines_by_symbol_and_timeframe(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 101.0})
        # 干扰数据：其他标的 / 其他周期
        self.add_bar(T0, 999.0, symbol_id=self.other_symbol_id)
        self.add_bar(T0, 888.0, timeframe="1d")

        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.bars, 2)
        self.assertEqual(result.signals, 1)
        self.assertEqual([s["timestamp"] for s in self.signals_of(strategy_id)], [T1])

    def test_02_time_range_filters_bars(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 101.0, T2: 102.0, T3: 103.0})

        result = self.runner.run(
            strategy_id, self.symbol_id, self.timeframe, start_timestamp=T1, end_timestamp=T2
        )

        self.assertEqual(result.bars, 2)
        self.assertEqual(result.first_bar_timestamp, T1)
        self.assertEqual(result.last_bar_timestamp, T2)
        self.assertEqual([s["timestamp"] for s in self.signals_of(strategy_id)], [T2])

    def test_03_computes_in_time_order(self) -> None:
        strategy_id = self.new_strategy()
        # 故意乱序写入，读取时应按 timestamp 升序
        self.add_bar(T2, 102.0)
        self.add_bar(T0, 100.0)
        self.add_bar(T1, 101.0)

        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(
            [s["timestamp"] for s in self.signals_of(strategy_id)], [T1, T2], "信号应按时间升序写入"
        )
        self.assertEqual([s["direction"] for s in self.signals_of(strategy_id)], ["long", "long"])
        self.assertEqual((result.first_bar_timestamp, result.last_bar_timestamp), (T0, T2))

    def test_04_no_klines_means_no_signals(self) -> None:
        strategy_id = self.new_strategy()
        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)
        self.assertEqual((result.bars, result.signals), (0, 0))
        self.assertIsNone(result.first_bar_timestamp)
        self.assertIsNone(result.last_bar_timestamp)


class DirectionTest(StrategyRunnerTestCase):
    """long / short / 无信号 + 信号字段。"""

    def test_01_long_signal(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 105.0})

        self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        signals = self.signals_of(strategy_id)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["direction"], "long")
        self.assertEqual(signals[0]["timestamp"], T1, "信号时间取当前 K 线的时间")
        self.assertAlmostEqual(signals[0]["price"], 105.0, "信号价格取当前 K 线收盘价")

    def test_02_short_signal(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 95.0, T2: 90.0})

        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        signals = self.signals_of(strategy_id)
        self.assertEqual([s["direction"] for s in signals], ["short", "short"])
        self.assertEqual(result.short_signals, 2)
        self.assertEqual(result.long_signals, 0)

    def test_03_equal_closes_produce_no_signal(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 100.0, T2: 100.0})

        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.signals, 0)
        self.assertEqual(self.signals_of(strategy_id), [])

    def test_04_signal_fields_are_written(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 106.0})

        self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        signal = self.signals_of(strategy_id)[0]
        self.assertEqual(signal["strategy_id"], strategy_id)
        self.assertEqual(signal["symbol_id"], self.symbol_id)
        self.assertEqual(signal["timeframe"], self.timeframe)
        self.assertEqual(signal["timestamp"], T1)
        self.assertEqual(signal["direction"], "long")
        self.assertAlmostEqual(signal["price"], 106.0)
        self.assertIn("100.0", signal["reason"])
        self.assertIn("106.0", signal["reason"])
        self.assertEqual(signal["status"], "pending", "运行器只产生信号，不执行")
        self.assertIsNotNone(signal["created_at"])

    def test_05_signals_persist(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 106.0})
        self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        del self.runner
        reopened = SignalRepository(self.db_path)
        self.assertEqual(reopened.count_signals(strategy_id=strategy_id), 1)


class RunIsolationTest(StrategyRunnerTestCase):
    """不产生交易对象、可重复运行、原子性、数据库隔离。"""

    def test_01_runner_creates_no_order_trade_position(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 106.0, T2: 99.0})

        self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(OrderRepository(self.db_path).count_orders(), 0)
        self.assertEqual(TradeRepository(self.db_path).count_trades(), 0)
        self.assertEqual(PositionRepository(self.db_path).list_positions(), [])
        self.assertEqual(len(self.signals_of(strategy_id)), 2)

    def test_02_repeat_run_skips_existing_signals(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 106.0, T2: 99.0})

        first = self.runner.run(strategy_id, self.symbol_id, self.timeframe)
        second = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(first.signals, 2)
        self.assertEqual(second.signals, 0)
        self.assertEqual(second.skipped, 2)
        self.assertEqual(len(self.signals_of(strategy_id)), 2, "重复运行不应产生重复信号")

    def test_03_rerun_after_new_bars_only_writes_new_signals(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 106.0})
        self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.add_bar(T2, 120.0)
        result = self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual((result.signals, result.skipped), (1, 1))
        self.assertEqual([s["timestamp"] for s in self.signals_of(strategy_id)], [T1, T2])

    def test_04_failure_while_writing_signals_rolls_back(self) -> None:
        strategy_id = self.new_strategy()
        self.add_closes({T0: 100.0, T1: 106.0, T2: 120.0})

        with mock.patch.object(self.runner.signals, "create_signal", side_effect=RuntimeError("模拟写入失败")):
            with self.assertRaises(RuntimeError):
                self.runner.run(strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(self.signals_of(strategy_id), [], "写入失败必须整批回滚，不留半完成状态")

    def test_05_runner_uses_given_db_path(self) -> None:
        self.assertNotEqual(self.runner.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.runner.db_path, self.db_path)
        for repo in (self.runner.strategies, self.runner.klines, self.runner.signals):
            self.assertEqual(repo.db_path, self.db_path)


class PureStrategyTest(unittest.TestCase):
    """策略本身是纯计算：不碰数据库。"""

    def make_bar(self, timestamp: str, close: float) -> Bar:
        return Bar(timestamp=timestamp, open=close - 1.0, high=close + 1.0, low=close - 2.0, close=close, volume=10.0)

    def test_01_generate_over_plain_bars(self) -> None:
        strategy = CloseDirectionStrategy()
        bars = [
            self.make_bar(T0, 100.0),
            self.make_bar(T1, 101.0),
            self.make_bar(T2, 101.0),
            self.make_bar(T3, 99.0),
        ]

        signals = strategy.generate(bars, {})

        self.assertEqual([s.timestamp for s in signals], [T1, T3])
        self.assertEqual([s.direction for s in signals], ["long", "short"])
        self.assertAlmostEqual(signals[0].price, 101.0)

    def test_02_from_config_reads_parameters(self) -> None:
        strategy = CloseDirectionStrategy.from_config({"min_move": 0.02})
        self.assertAlmostEqual(strategy.min_move, 0.02)
        self.assertAlmostEqual(CloseDirectionStrategy.from_config({}).min_move, 0.0)

        with self.assertRaises(ValueError):
            CloseDirectionStrategy.from_config({"min_move": -0.5})

    def test_03_empty_or_single_bar_produces_nothing(self) -> None:
        strategy = CloseDirectionStrategy()
        self.assertEqual(strategy.generate([], {}), [])
        self.assertEqual(strategy.generate([self.make_bar(T0, 100.0)], {}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
