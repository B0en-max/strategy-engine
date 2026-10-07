"""Stage 10：模拟执行器测试。

重点验证：
    完整执行链  买入信号 → Order → Trade → Position 全链路走通
    卖出减仓    sell 会减少持仓（并遵循 avg_price 规则）
    订单状态    Order 最终为 filled
    成交记录    Trade 正确生成（数量 / 价格 / side / signal_id / fee）
    持仓更新    Position 的数量与平均成本按规则更新
    不产生半完成状态  执行中途失败必须整体回滚

另外验证：纯函数 apply_trade_to_position 的各类情形、执行前置校验、
方向映射、临时数据库隔离。

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
    OrderRepository,
    PositionRepository,
    SignalRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
)
from execution import ExecutionEngine, apply_trade_to_position  # noqa: E402

T0 = "2026-01-01 09:00:00"
T1 = "2026-01-01 10:00:00"
T2 = "2026-01-01 11:00:00"
T3 = "2026-01-01 12:00:00"


class ExecutionTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_execution_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "execution_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.strategies = StrategyRepository(self.db_path)
        self.signals = SignalRepository(self.db_path)
        self.orders = OrderRepository(self.db_path)
        self.trades = TradeRepository(self.db_path)
        self.positions = PositionRepository(self.db_path)
        self.engine = ExecutionEngine(self.db_path)

        self.symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.strategy_id = self.strategies.create_strategy("mean_reversion", {"entry": {"lookback": 20}})

    # ------------------------------------------------------------------ 工具
    def new_signal(
        self,
        direction: str = "long",
        price: float = 100.0,
        timestamp: str = T0,
    ) -> int:
        return self.signals.create_signal(
            self.strategy_id, self.symbol_id, "1d", timestamp, direction, price
        )

    def position(self):
        return self.positions.get_position_by_symbol(self.symbol_id)

    def position_values(self) -> tuple[float, float]:
        row = self.position()
        return (0.0, 0.0) if row is None else (float(row["quantity"]), float(row["avg_price"]))


class ExecutionChainTest(ExecutionTestCase):
    """完整执行链。"""

    def test_01_buy_signal_runs_full_chain(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        result = self.engine.execute_signal(signal_id, 10.0, timestamp=T1)

        # 结果对象
        self.assertEqual(result.signal_id, signal_id)
        self.assertEqual(result.symbol_id, self.symbol_id)
        self.assertEqual(result.side, "buy")
        self.assertAlmostEqual(result.quantity, 10.0)
        self.assertAlmostEqual(result.price, 100.0)
        self.assertAlmostEqual(result.position_quantity, 10.0)
        self.assertAlmostEqual(result.position_avg_price, 100.0)

        # Order 最终为 filled
        order = self.orders.get_order(result.order_id)
        self.assertEqual(order["status"], "filled")
        self.assertEqual(order["side"], "buy")
        self.assertEqual(order["order_type"], "market")
        self.assertAlmostEqual(order["quantity"], 10.0)
        self.assertAlmostEqual(order["price"], 100.0)
        self.assertEqual(order["signal_id"], signal_id)
        self.assertEqual(order["symbol_id"], self.symbol_id)

        # Trade 正确生成
        trade = self.trades.get_trade(result.trade_id)
        self.assertEqual(trade["side"], "buy")
        self.assertAlmostEqual(trade["quantity"], 10.0)
        self.assertAlmostEqual(trade["price"], 100.0)
        self.assertEqual(trade["timestamp"], T1)
        self.assertEqual(trade["signal_id"], signal_id)
        self.assertEqual(trade["symbol_id"], self.symbol_id)

        # Position 正确更新
        self.assertEqual(result.position_id, self.position()["id"])
        self.assertEqual(self.position_values(), (10.0, 100.0))

        # 信号被标记为已执行
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "executed")

        # 只产生 1 条 order / trade / position
        self.assertEqual(self.orders.count_orders(), 1)
        self.assertEqual(self.trades.count_trades(), 1)
        self.assertEqual(len(self.positions.list_positions()), 1)

    def test_02_second_buy_averages_up(self) -> None:
        self.engine.execute_signal(self.new_signal("long", 100.0), 10.0, timestamp=T1)

        signal_id = self.new_signal("long", 120.0, timestamp=T2)
        result = self.engine.execute_signal(signal_id, 10.0, timestamp=T2)

        # 加权平均：(10*100 + 10*120) / 20 = 110
        self.assertAlmostEqual(result.position_quantity, 20.0)
        self.assertAlmostEqual(result.position_avg_price, 110.0)
        self.assertEqual(self.position_values(), (20.0, 110.0))
        self.assertEqual(self.trades.count_trades(), 2)

    def test_03_sell_reduces_position_and_keeps_avg_price(self) -> None:
        self.engine.execute_signal(self.new_signal("long", 100.0), 10.0, timestamp=T1)

        signal_id = self.new_signal("short", 110.0, timestamp=T2)
        result = self.engine.execute_signal(signal_id, 4.0, timestamp=T2)

        self.assertEqual(result.side, "sell")
        self.assertAlmostEqual(result.position_quantity, 6.0)
        self.assertAlmostEqual(result.position_avg_price, 100.0, msg="减仓时剩余持仓的平均成本不变")
        self.assertEqual(self.position_values(), (6.0, 100.0))

        trade = self.trades.get_trade(result.trade_id)
        self.assertEqual(trade["side"], "sell")

    def test_04_sell_everything_closes_position(self) -> None:
        self.engine.execute_signal(self.new_signal("long", 100.0), 10.0, timestamp=T1)

        result = self.engine.execute_signal(self.new_signal("short", 120.0, timestamp=T2), 10.0, timestamp=T2)

        self.assertAlmostEqual(result.position_quantity, 0.0)
        self.assertAlmostEqual(result.position_avg_price, 0.0)
        self.assertEqual(self.position_values(), (0.0, 0.0))

    def test_05_sell_beyond_position_flips_to_short(self) -> None:
        self.engine.execute_signal(self.new_signal("long", 100.0), 10.0, timestamp=T1)

        result = self.engine.execute_signal(self.new_signal("short", 90.0, timestamp=T2), 25.0, timestamp=T2)

        # 反手：剩余 -15 以本次成交价 90 作为平均成本
        self.assertAlmostEqual(result.position_quantity, -15.0)
        self.assertAlmostEqual(result.position_avg_price, 90.0)

    def test_06_short_signal_opens_short_position_without_existing_position(self) -> None:
        result = self.engine.execute_signal(self.new_signal("short", 50.0), 8.0, timestamp=T1)

        self.assertEqual(result.side, "sell")
        self.assertAlmostEqual(result.position_quantity, -8.0)
        self.assertAlmostEqual(result.position_avg_price, 50.0)
        self.assertEqual(self.orders.get_order(result.order_id)["status"], "filled")

    def test_07_fee_is_recorded_but_not_applied_to_position(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        result = self.engine.execute_signal(signal_id, 10.0, fee=-0.5, timestamp=T1)

        self.assertAlmostEqual(self.trades.get_trade(result.trade_id)["fee"], -0.5)
        # 手续费不参与持仓计算
        self.assertEqual(self.position_values(), (10.0, 100.0))


class ExecutionGuardTest(ExecutionTestCase):
    """执行前置校验。"""

    def test_01_unknown_signal_is_rejected_without_writes(self) -> None:
        with self.assertRaises(ValueError):
            self.engine.execute_signal(999999, 10.0)
        self.assertEqual(self.orders.count_orders(), 0)
        self.assertEqual(self.trades.count_trades(), 0)
        self.assertEqual(self.positions.list_positions(), [])

    def test_02_already_executed_signal_is_rejected(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        self.engine.execute_signal(signal_id, 10.0, timestamp=T1)

        with self.assertRaises(ValueError):
            self.engine.execute_signal(signal_id, 10.0, timestamp=T1)

        self.assertEqual(self.orders.count_orders(), 1, "重复执行不应产生第二个订单")
        self.assertEqual(self.trades.count_trades(), 1)

    def test_03_cancelled_signal_is_rejected(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        self.signals.update_signal_status(signal_id, "cancelled")

        with self.assertRaises(ValueError):
            self.engine.execute_signal(signal_id, 10.0)
        self.assertEqual(self.orders.count_orders(), 0)

    def test_04_quantity_must_be_positive(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        for bad in (0.0, -5.0):
            with self.subTest(quantity=bad):
                with self.assertRaises(ValueError):
                    self.engine.execute_signal(signal_id, bad)
        self.assertEqual(self.orders.count_orders(), 0)
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "pending")

    def test_05_invalid_price_and_timestamp_are_rejected(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        with self.assertRaises(ValueError):
            self.engine.execute_signal(signal_id, 10.0, price="100")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.engine.execute_signal(signal_id, 10.0, timestamp="2026-01-01")
        self.assertEqual(self.orders.count_orders(), 0)


class ExecutionAtomicityTest(ExecutionTestCase):
    """执行中途失败不得留下半完成状态。"""

    def test_01_failure_while_updating_position_rolls_back_everything(self) -> None:
        # 先建立基线：一次成功的买入
        self.engine.execute_signal(self.new_signal("long", 100.0), 10.0, timestamp=T1)
        baseline = self.position_values()
        self.assertEqual(baseline, (10.0, 100.0))

        second_signal = self.new_signal("long", 120.0, timestamp=T2)
        with mock.patch.object(
            self.engine.positions, "update_position", side_effect=RuntimeError("模拟持仓更新失败")
        ):
            with self.assertRaises(RuntimeError):
                self.engine.execute_signal(second_signal, 5.0, timestamp=T2)

        # 没有半完成状态：持仓、订单、成交记录全部保持基线
        self.assertEqual(self.position_values(), baseline, "持仓不应被改动")
        self.assertEqual(self.orders.count_orders(), 1, "失败的那次订单必须回滚")
        self.assertEqual(self.trades.count_trades(), 1, "失败的那次成交必须回滚")
        self.assertEqual(
            self.signals.get_signal(second_signal)["status"],
            "pending",
            "失败的信号应保持 pending，不能变成 executed",
        )

    def test_02_failure_while_creating_trade_rolls_back_the_order(self) -> None:
        self.engine.execute_signal(self.new_signal("long", 100.0), 10.0, timestamp=T1)
        baseline = self.position_values()

        second_signal = self.new_signal("long", 120.0, timestamp=T2)
        with mock.patch.object(
            self.engine.trades, "create_trade", side_effect=RuntimeError("模拟成交记录失败")
        ):
            with self.assertRaises(RuntimeError):
                self.engine.execute_signal(second_signal, 5.0, timestamp=T2)

        self.assertEqual(self.orders.count_orders(), 1, "已经插入的订单必须随事务回滚")
        self.assertEqual(self.trades.count_trades(), 1)
        self.assertEqual(self.position_values(), baseline)
        self.assertEqual(self.signals.get_signal(second_signal)["status"], "pending")

    def test_03_engine_and_repositories_share_the_same_database(self) -> None:
        signal_id = self.new_signal("long", 100.0)
        result = self.engine.execute_signal(signal_id, 10.0, timestamp=T1)

        # 用独立 Repository 也能看到同一次执行的结果
        self.assertEqual(OrderRepository(self.db_path).get_order(result.order_id)["status"], "filled")
        self.assertEqual(TradeRepository(self.db_path).count_trades(), 1)
        self.assertEqual(PositionRepository(self.db_path).get_position(result.position_id)["symbol_id"], self.symbol_id)


class ApplyTradeToPositionTest(unittest.TestCase):
    """纯函数：持仓计算规则。"""

    def test_01_all_cases(self) -> None:
        cases = {
            "建仓": ((0.0, 0.0), "buy", 10.0, 100.0, (10.0, 100.0)),
            "加仓加权": ((10.0, 100.0), "buy", 10.0, 120.0, (20.0, 110.0)),
            "减仓成本不变": ((10.0, 100.0), "sell", 4.0, 130.0, (6.0, 100.0)),
            "清仓": ((10.0, 100.0), "sell", 10.0, 130.0, (0.0, 0.0)),
            "反手转空": ((10.0, 100.0), "sell", 25.0, 90.0, (-15.0, 90.0)),
            "做空建仓": ((0.0, 0.0), "sell", 5.0, 80.0, (-5.0, 80.0)),
            "空头加仓加权": ((-10.0, 100.0), "sell", 10.0, 80.0, (-20.0, 90.0)),
            "空头回补成本不变": ((-10.0, 100.0), "buy", 4.0, 60.0, (-6.0, 100.0)),
        }
        for label, (start, side, quantity, price, expected) in cases.items():
            with self.subTest(case=label):
                result = apply_trade_to_position(start[0], start[1], side, quantity, price)
                self.assertAlmostEqual(result[0], expected[0])
                self.assertAlmostEqual(result[1], expected[1])

    def test_02_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_trade_to_position(0.0, 0.0, "hold", 1.0, 100.0)
        with self.assertRaises(ValueError):
            apply_trade_to_position(0.0, 0.0, "buy", 0.0, 100.0)
        with self.assertRaises(ValueError):
            apply_trade_to_position(0.0, 0.0, "buy", -1.0, 100.0)


class ExecutionIsolationTest(ExecutionTestCase):
    """不污染正式数据库。"""

    def test_01_engine_uses_given_path(self) -> None:
        self.assertNotEqual(self.engine.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.engine.db_path, self.db_path)
        for repo in (self.engine.signals, self.engine.orders, self.engine.trades, self.engine.positions):
            self.assertEqual(repo.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
