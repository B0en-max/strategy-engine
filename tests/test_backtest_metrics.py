"""Stage 15：回测绩效统计测试。

分两部分：
    A. 纯盈亏计算（人工构造成交序列，逐笔精确断言）
       全部盈利 / 全部亏损 / 混合 / 无成交 / 只有开仓未平仓 / 多头平仓 / 空头平仓 /
       部分平仓 / 反转（多翻空、空翻多）/ 平均成本口径 / 事件类型识别 / 非法输入
    B. 与 BacktestEngine 集成（人工构造 K 线，临时数据库）
       引擎跑出的成交 → 指标一致；无成交；正式库不被修改

全部使用临时数据库，**不触碰 data/strategy_engine.db**（仅只读校验其行数未变）。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest import (  # noqa: E402
    BacktestEngine,
    compute_metrics,
    realized_trades,
    run_backtest_with_metrics,
)
from backtest.metrics import (  # noqa: E402
    EVENT_ADD_LONG,
    EVENT_ADD_SHORT,
    EVENT_CLOSE_LONG,
    EVENT_CLOSE_SHORT,
    EVENT_OPEN_LONG,
    EVENT_OPEN_SHORT,
    EVENT_REDUCE_LONG,
    EVENT_REDUCE_SHORT,
    EVENT_REVERSE_LONG_TO_SHORT,
    EVENT_REVERSE_SHORT_TO_LONG,
)
from database import (  # noqa: E402
    DEFAULT_DB_PATH,
    KlineRepository,
    StrategyRepository,
    SymbolRepository,
    initialize_database,
)

T0 = "2026-01-01 09:30:00"
T1 = "2026-01-01 09:45:00"
T2 = "2026-01-01 10:00:00"
T3 = "2026-01-01 10:15:00"
T4 = "2026-01-01 10:30:00"


def trade(side: str, quantity: float, price: float, timestamp: str = T0) -> dict:
    """构造一笔成交（与 TradeRepository 返回的字段名一致）。"""
    return {"side": side, "quantity": quantity, "price": price, "timestamp": timestamp}


class PnlMathTest(unittest.TestCase):
    """A. 已实现盈亏与指标计算（纯函数）。"""

    def test_01_all_winning_trades(self) -> None:
        trades = [
            trade("buy", 1.0, 10.0, T0), trade("sell", 1.0, 12.0, T1),   # +2
            trade("buy", 1.0, 10.0, T2), trade("sell", 1.0, 15.0, T3),   # +5
        ]

        metrics = compute_metrics(trades)

        self.assertEqual(metrics.total_trades, 4)
        self.assertEqual(metrics.buy_trades, 2)
        self.assertEqual(metrics.sell_trades, 2)
        self.assertEqual(metrics.winning_trades, 2)
        self.assertEqual(metrics.losing_trades, 0)
        self.assertAlmostEqual(metrics.total_pnl, 7.0)
        self.assertAlmostEqual(metrics.win_rate, 1.0)
        self.assertAlmostEqual(metrics.average_profit, 3.5)
        self.assertAlmostEqual(metrics.average_loss, 0.0)
        self.assertTrue(math.isinf(metrics.profit_factor), "有盈利且无亏损时 profit_factor 为 inf")

    def test_02_all_losing_trades(self) -> None:
        trades = [
            trade("buy", 1.0, 10.0, T0), trade("sell", 1.0, 8.0, T1),    # -2
            trade("buy", 1.0, 10.0, T2), trade("sell", 1.0, 9.0, T3),    # -1
        ]

        metrics = compute_metrics(trades)

        self.assertEqual(metrics.winning_trades, 0)
        self.assertEqual(metrics.losing_trades, 2)
        self.assertAlmostEqual(metrics.total_pnl, -3.0)
        self.assertAlmostEqual(metrics.win_rate, 0.0)
        self.assertAlmostEqual(metrics.average_profit, 0.0)
        self.assertAlmostEqual(metrics.average_loss, -1.5, msg="平均亏损为负值")
        self.assertAlmostEqual(metrics.profit_factor, 0.0, msg="无盈利时 profit_factor 为 0")

    def test_03_mixed_profit_and_loss(self) -> None:
        trades = [
            trade("buy", 1.0, 10.0, T0), trade("sell", 1.0, 14.0, T1),   # +4
            trade("buy", 1.0, 10.0, T2), trade("sell", 1.0, 8.0, T3),    # -2
        ]

        metrics = compute_metrics(trades)

        self.assertAlmostEqual(metrics.total_pnl, 2.0)
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (1, 1))
        self.assertAlmostEqual(metrics.win_rate, 0.5)
        self.assertAlmostEqual(metrics.average_profit, 4.0)
        self.assertAlmostEqual(metrics.average_loss, -2.0)
        self.assertAlmostEqual(metrics.profit_factor, 2.0)

    def test_04_no_trades(self) -> None:
        metrics = compute_metrics([])

        self.assertEqual(metrics.total_trades, 0)
        self.assertEqual((metrics.buy_trades, metrics.sell_trades), (0, 0))
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (0, 0))
        self.assertAlmostEqual(metrics.total_pnl, 0.0)
        self.assertAlmostEqual(metrics.win_rate, 0.0)
        self.assertAlmostEqual(metrics.average_profit, 0.0)
        self.assertAlmostEqual(metrics.average_loss, 0.0)
        self.assertAlmostEqual(metrics.profit_factor, 0.0)

    def test_05_open_positions_are_not_realized(self) -> None:
        trades = [trade("buy", 2.0, 10.0, T0), trade("buy", 1.0, 12.0, T1)]

        metrics = compute_metrics(trades)
        events = realized_trades(trades)

        self.assertEqual(metrics.total_trades, 2)
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (0, 0))
        self.assertAlmostEqual(metrics.total_pnl, 0.0, msg="未平仓浮盈不计入已实现盈亏")
        self.assertEqual([event.event for event in events], [EVENT_OPEN_LONG, EVENT_ADD_LONG])
        self.assertEqual([event.closed_quantity for event in events], [0.0, 0.0])
        self.assertAlmostEqual(events[-1].position_after, 3.0, msg="成交后剩余持仓 3")

    def test_06_long_close_pnl(self) -> None:
        trades = [trade("buy", 2.0, 10.0, T0), trade("sell", 2.0, 13.0, T1)]

        events = realized_trades(trades)

        self.assertEqual(events[1].event, EVENT_CLOSE_LONG)
        self.assertAlmostEqual(events[1].closed_quantity, 2.0)
        self.assertAlmostEqual(events[1].pnl, 6.0)
        self.assertAlmostEqual(events[1].position_after, 0.0)
        self.assertAlmostEqual(compute_metrics(trades).total_pnl, 6.0)

    def test_07_short_close_pnl(self) -> None:
        trades = [trade("sell", 2.0, 20.0, T0), trade("buy", 2.0, 18.0, T1)]

        events = realized_trades(trades)

        self.assertEqual([event.event for event in events], [EVENT_OPEN_SHORT, EVENT_CLOSE_SHORT])
        self.assertAlmostEqual(events[1].pnl, 4.0, msg="空头：卖出开仓价 - 买入平仓价")
        self.assertAlmostEqual(compute_metrics(trades).total_pnl, 4.0)

    def test_08_short_losing_close(self) -> None:
        trades = [trade("sell", 1.0, 20.0, T0), trade("buy", 1.0, 25.0, T1)]

        metrics = compute_metrics(trades)

        self.assertAlmostEqual(metrics.total_pnl, -5.0)
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (0, 1))

    def test_09_partial_close_pnl(self) -> None:
        trades = [
            trade("buy", 3.0, 10.0, T0),    # 开多 3 @10
            trade("sell", 1.0, 12.0, T1),   # 部分平仓 1 → +2，剩 2 @10
            trade("sell", 2.0, 11.0, T2),   # 全部平仓 2 → +2
        ]

        events = realized_trades(trades)

        self.assertEqual(
            [event.event for event in events],
            [EVENT_OPEN_LONG, EVENT_REDUCE_LONG, EVENT_CLOSE_LONG],
        )
        self.assertAlmostEqual(events[1].closed_quantity, 1.0)
        self.assertAlmostEqual(events[1].pnl, 2.0)
        self.assertAlmostEqual(events[1].position_after, 2.0)
        self.assertAlmostEqual(events[2].pnl, 2.0, msg="部分平仓后剩余成本仍为 10")
        self.assertAlmostEqual(compute_metrics(trades).total_pnl, 4.0)

    def test_10_reverse_long_to_short(self) -> None:
        trades = [
            trade("buy", 1.0, 10.0, T0),    # 开多 1 @10
            trade("sell", 3.0, 12.0, T1),   # 平多 1（+2）+ 反手开空 2 @12
            trade("buy", 1.0, 11.0, T2),    # 部分平空 1 → (12-11)=+1
        ]

        events = realized_trades(trades)

        self.assertEqual(
            [event.event for event in events],
            [EVENT_OPEN_LONG, EVENT_REVERSE_LONG_TO_SHORT, EVENT_REDUCE_SHORT],
        )
        self.assertAlmostEqual(events[1].closed_quantity, 1.0)
        self.assertAlmostEqual(events[1].pnl, 2.0)
        self.assertAlmostEqual(events[1].position_after, -2.0)
        self.assertAlmostEqual(events[2].pnl, 1.0)
        self.assertAlmostEqual(compute_metrics(trades).total_pnl, 3.0)

    def test_11_reverse_short_to_long(self) -> None:
        trades = [
            trade("sell", 2.0, 20.0, T0),   # 开空 2 @20
            trade("buy", 3.0, 18.0, T1),    # 平空 2（(20-18)*2=+4）+ 反手开多 1 @18
        ]

        events = realized_trades(trades)

        self.assertEqual(
            [event.event for event in events], [EVENT_OPEN_SHORT, EVENT_REVERSE_SHORT_TO_LONG]
        )
        self.assertAlmostEqual(events[1].closed_quantity, 2.0)
        self.assertAlmostEqual(events[1].pnl, 4.0)
        self.assertAlmostEqual(events[1].position_after, 1.0)
        self.assertAlmostEqual(compute_metrics(trades).total_pnl, 4.0)

    def test_12_pnl_uses_average_cost_not_fifo(self) -> None:
        trades = [trade("buy", 1.0, 10.0, T0), trade("buy", 1.0, 20.0, T1), trade("sell", 1.0, 18.0, T2)]

        metrics = compute_metrics(trades)

        self.assertAlmostEqual(metrics.total_pnl, 3.0, msg="平均成本 15 → (18-15)*1 = +3（FIFO 会算成 +8）")

        short_trades = [
            trade("sell", 1.0, 20.0, T0), trade("sell", 1.0, 10.0, T1), trade("buy", 1.0, 12.0, T2),
        ]
        self.assertAlmostEqual(compute_metrics(short_trades).total_pnl, 3.0, msg="空头平均成本 15 → +3")

    def test_13_all_event_types_are_identified(self) -> None:
        trades = [
            trade("buy", 1.0, 10.0, T0),    # 开多
            trade("buy", 1.0, 12.0, T1),    # 加多（平均成本 11）
            trade("sell", 1.0, 15.0, T2),   # 部分平多 → +4
            trade("sell", 1.0, 20.0, T3),   # 全平多 → +9
            trade("sell", 1.0, 30.0, T4),   # 开空
            trade("sell", 1.0, 25.0, T4),   # 加空（平均成本 27.5）
            trade("buy", 1.0, 20.0, T4),    # 部分平空 → +7.5
        ]

        events = realized_trades(trades)

        self.assertEqual(
            [event.event for event in events],
            [
                EVENT_OPEN_LONG,
                EVENT_ADD_LONG,
                EVENT_REDUCE_LONG,
                EVENT_CLOSE_LONG,
                EVENT_OPEN_SHORT,
                EVENT_ADD_SHORT,
                EVENT_REDUCE_SHORT,
            ],
        )
        self.assertAlmostEqual([event.pnl for event in events][2], 4.0)
        self.assertAlmostEqual([event.pnl for event in events][3], 9.0)
        self.assertAlmostEqual([event.pnl for event in events][6], 7.5)
        self.assertAlmostEqual(compute_metrics(trades).total_pnl, 20.5)

    def test_14_win_rate_formula(self) -> None:
        trades = [
            trade("buy", 1.0, 10.0, T0), trade("sell", 1.0, 20.0, T1),   # +10
            trade("buy", 1.0, 10.0, T2), trade("sell", 1.0, 7.0, T3),    # -3
            trade("buy", 1.0, 10.0, T4), trade("sell", 1.0, 8.0, T4),    # -2
        ]

        metrics = compute_metrics(trades)

        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (1, 2))
        self.assertAlmostEqual(metrics.win_rate, 1 / 3)
        self.assertAlmostEqual(metrics.profit_factor, 2.0, msg="10 / (3+2)")

    def test_15_zero_pnl_close_is_neither_win_nor_loss(self) -> None:
        trades = [trade("buy", 1.0, 10.0, T0), trade("sell", 1.0, 10.0, T1)]

        metrics = compute_metrics(trades)

        self.assertEqual(metrics.total_trades, 2)
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (0, 0))
        self.assertAlmostEqual(metrics.total_pnl, 0.0)
        self.assertAlmostEqual(metrics.win_rate, 0.0)

    def test_16_invalid_input_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compute_metrics([trade("long", 1.0, 10.0)])
        with self.assertRaises(ValueError):
            compute_metrics([trade("buy", 0.0, 10.0)])
        with self.assertRaises(ValueError):
            compute_metrics([trade("buy", -1.0, 10.0)])


class MetricsFromEngineTest(unittest.TestCase):
    """B. 与 BacktestEngine 集成（人工构造 K 线 + 临时数据库）。"""

    timeframe = "15m"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_metrics_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db_path)

        self.symbol_id = SymbolRepository(self.source_db_path).create_symbol(
            "600519", "贵州茅台", "CN", "STOCK"
        )
        self.strategy_id = StrategyRepository(self.source_db_path).create_strategy(
            "close_direction_v1", {"type": "close_direction"}
        )
        self.klines = KlineRepository(self.source_db_path)

    def add_closes(self, closes: dict[str, float]) -> None:
        for timestamp, close in closes.items():
            open_price = close - 1.0
            self.klines.create_kline(
                self.symbol_id,
                self.timeframe,
                timestamp,
                open=open_price,
                high=max(open_price, close) + 1.0,
                low=min(open_price, close) - 1.0,
                close=close,
                volume=100.0,
            )

    def new_engine(self) -> BacktestEngine:
        engine = BacktestEngine(source_db_path=self.source_db_path)
        self.addCleanup(engine.close)
        return engine

    def test_01_only_opening_trades(self) -> None:
        # 一路上涨：每根 K 线都产生 long → 全是买入开仓，没有平仓
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0, T3: 130.0})
        engine = self.new_engine()

        result, metrics = run_backtest_with_metrics(engine, self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.trades_executed, 3)
        self.assertEqual(metrics.total_trades, 3)
        self.assertEqual(metrics.buy_trades, 3)
        self.assertEqual(metrics.sell_trades, 0)
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (0, 0))
        self.assertAlmostEqual(metrics.total_pnl, 0.0, msg="只有开仓 → 没有已实现盈亏")

    def test_02_losing_round_trip(self) -> None:
        # 100 → 110（买入 @110）→ 105（卖出 @105 平多）→ 115（买入 @115 开多）
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0, T3: 115.0})
        engine = self.new_engine()

        result, metrics = run_backtest_with_metrics(engine, self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.trades_executed, 3)
        self.assertEqual(metrics.total_trades, 3)
        self.assertEqual(metrics.buy_trades, 2)
        self.assertEqual(metrics.sell_trades, 1)
        self.assertEqual(metrics.losing_trades, 1)
        self.assertEqual(metrics.winning_trades, 0)
        self.assertAlmostEqual(metrics.total_pnl, -5.0, msg="(105 - 110) * 1")

    def test_03_winning_round_trip(self) -> None:
        # 下跌开空：110 → 100（卖 @100）→ 90（加空）→ 85（加空，均价 275/3）→ 91（买 @91 平空）
        self.add_closes({T0: 110.0, T1: 100.0, T2: 90.0, T3: 85.0, T4: 91.0})
        engine = self.new_engine()

        result, metrics = run_backtest_with_metrics(engine, self.strategy_id, self.symbol_id, self.timeframe)

        expected = 275.0 / 3.0 - 91.0  # 平均成本 91.6667 → 平仓价 91 → +0.6667
        self.assertEqual(result.trades_executed, 4)
        self.assertEqual((metrics.winning_trades, metrics.losing_trades), (1, 0))
        self.assertAlmostEqual(metrics.total_pnl, expected, places=9)
        self.assertAlmostEqual(metrics.average_profit, expected, places=9)
        self.assertAlmostEqual(metrics.win_rate, 1.0)
        self.assertTrue(math.isinf(metrics.profit_factor))

    def test_04_no_trades_gives_zero_metrics(self) -> None:
        engine = self.new_engine()

        result, metrics = run_backtest_with_metrics(engine, self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(result.bars_processed, 0)
        self.assertEqual(metrics.total_trades, 0)
        self.assertAlmostEqual(metrics.total_pnl, 0.0)
        self.assertAlmostEqual(metrics.profit_factor, 0.0)

    def test_05_metrics_trade_count_matches_backtest_result(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0, T3: 99.0, T4: 108.0})
        engine = self.new_engine()

        result, metrics = run_backtest_with_metrics(engine, self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(metrics.total_trades, result.trades_executed)
        self.assertEqual(
            metrics.buy_trades + metrics.sell_trades, result.trades_executed
        )

    def test_06_metrics_use_temporary_databases_only(self) -> None:
        """绩效统计不触碰正式库（正式库只读，前后行数一致）。"""
        production = KlineRepository(DEFAULT_DB_PATH)

        def snapshot() -> dict[str, int]:
            return {
                table: production.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
                for table in ("symbols", "klines", "strategies", "signals", "orders", "trades", "positions")
            }

        before = snapshot()
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0})
        engine = self.new_engine()

        self.assertNotEqual(engine.source_db_path, DEFAULT_DB_PATH)
        run_backtest_with_metrics(engine, self.strategy_id, self.symbol_id, self.timeframe)

        self.assertEqual(snapshot(), before, "绩效统计过程不得修改正式库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
