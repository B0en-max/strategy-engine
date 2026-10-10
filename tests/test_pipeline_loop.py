"""Stage 028：最小运行闭环测试。

覆盖：
    闭环链路        行情 → 校验 → 信号 → 逐 K 线执行 → 订单/成交/持仓 → 交易记录 → 绩效
    执行正确性      无未来数据（窗口 + 时间戳守卫）、成交时点分层、成交价假设
    成本            滑点方向、佣金/最低佣金、卖出印花税、净盈亏 = 已实现盈亏 − 手续费
    可复现          同一 RunSpec 两次运行 → 相同 run_id 与 canonical_result()
    失败明确        标的不存在 / 区间无 K 线 / 非法输入 → RunError（不返回空成功）
    未知状态        诊断里显式标注"未模拟"的约束，不静默当作已满足条件
    隔离            来源库只读且运行后不变
    校验器          重复/乱序/OHLC 异常/非正价格（直接测校验函数）

全部使用临时数据库，**不触碰 data/strategy_engine.db**。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, ClassVar, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import (  # noqa: E402
    KlineRepository,
    OrderRepository,
    SignalRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
)
from pipeline import ExecutionCosts, RunError, RunSpec, run_pipeline  # noqa: E402
from pipeline.loop import _validate_bars  # noqa: E402
from strategies.base import BarWindow, SignalCandidate, Strategy  # noqa: E402

MARKET = "CN"
CODE = "600519"
SYMBOL_NAME = "贵州茅台"


def _bar_row(ts: str, o: float, h: float, low: float, c: float, v: float = 1000.0):
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": v, "amount": None}


class _SpyStrategy(Strategy):
    """记录它看到过的全部 K 线时间戳（用于验证无未来数据）。"""

    name: ClassVar[str] = "spy"
    seen: ClassVar[list[str]] = []

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        type(self).seen.append(window.current.timestamp)
        return None


class _FutureStrategy(Strategy):
    """故意把信号挂到未来（应被拒绝）。"""

    name: ClassVar[str] = "future"

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        return SignalCandidate(timestamp="2099-01-01 00:00:00", direction="long", price=1.0)


class PipelineTestCase(unittest.TestCase):
    """基类：临时"来源库"（只读）+ 造数据工具。"""

    timeframe = "1d"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_pipeline_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db_path)
        self.symbol_id = SymbolRepository(self.source_db_path).create_symbol(
            CODE, SYMBOL_NAME, MARKET, "stock", is_active=1
        )

    def seed(self, rows: list[dict[str, Any]]) -> None:
        KlineRepository(self.source_db_path).bulk_upsert_klines(
            [{"symbol_id": self.symbol_id, "timeframe": self.timeframe, **row} for row in rows]
        )

    def spec(self, **kwargs: Any) -> RunSpec:
        base: dict[str, Any] = {
            "symbol": CODE,
            "timeframe": self.timeframe,
            "strategy_type": "close_direction",
            "quantity": 100.0,
            "costs": ExecutionCosts(commission_bps=0.0, min_commission=0.0, stamp_tax_bps=0.0,
                                    slippage_bps=0.0),
        }
        base.update(kwargs)
        return RunSpec(**base)

    def run_pipeline(self, spec: RunSpec, **kwargs: Any):
        return run_pipeline(spec, source_db_path=self.source_db_path, **kwargs)

    @staticmethod
    def rising_bars(count: int = 5) -> list[dict[str, Any]]:
        """稳定上涨序列：每根收盘都高于前一根 → 每根（除首根）都产生 long 信号。"""
        return [
            _bar_row(f"2026-01-{day:02d} 00:00:00", 10.0 + day, 10.6 + day, 9.9 + day, 10.5 + day)
            for day in range(2, 2 + count)
        ]


class ClosedLoopTest(PipelineTestCase):
    def test_end_to_end_counts_are_consistent(self) -> None:
        self.seed(self.rising_bars(5))
        run_db = Path(self._tmpdir.name) / "run.db"
        report = self.run_pipeline(self.spec(), run_db_path=run_db)

        self.assertEqual(report.bars_processed, 5)
        # 5 根里除首根外每根都产生信号；最后一根的信号因无下一根 → 未成交
        self.assertEqual(report.signals_generated, 4)
        self.assertEqual(report.trades_executed, 3)
        self.assertEqual(report.unfilled_signals, 1)
        self.assertEqual(report.long_signals, 4)
        self.assertEqual(report.short_signals, 0)

        # 信号 / 订单 / 成交 一一对应（可在运行库里核对）
        signals = SignalRepository(report.run_db_path)
        orders = OrderRepository(report.run_db_path)
        trades = TradeRepository(report.run_db_path)
        self.assertEqual(len(signals.list_signals()), 4)
        self.assertEqual(len(orders.list_orders()), report.trades_executed)
        self.assertEqual(len(trades.list_trades()), report.trades_executed)

        # 成交明细必须能与报告逐条对上
        rows = trades.list_trades()
        self.assertEqual([float(r["price"]) for r in rows], [t["fill_price"] for t in report.trades])
        self.assertEqual([int(r["signal_id"]) for r in rows], [t["signal_id"] for t in report.trades])
        # 最终持仓 = 累计带符号数量
        self.assertEqual(report.diagnostics["final_position"]["quantity"], 300.0)
        self.assertTrue(run_db.exists())  # 显式指定运行库时保留

    def test_temporary_run_db_is_cleaned_up(self) -> None:
        self.seed(self.rising_bars(4))
        report = self.run_pipeline(self.spec())
        self.assertFalse(Path(report.run_db_path).exists())


class FillModelTest(PipelineTestCase):
    def test_next_bar_open_fill_timing_and_price(self) -> None:
        self.seed(self.rising_bars(5))
        report = self.run_pipeline(self.spec(fill_model="next_bar_open"))
        first = report.trades[0]
        # 决策在 t（第二根），成交在 t+1（第三根）的开盘价
        self.assertEqual(first["signal_timestamp"], "2026-01-03 00:00:00")
        self.assertEqual(first["fill_timestamp"], "2026-01-04 00:00:00")
        self.assertEqual(first["base_price"], 14.0)  # 第三根 open = 10+4
        self.assertEqual(first["fill_price"], 14.0)

    def test_signal_bar_close_model_uses_same_bar_close(self) -> None:
        self.seed(self.rising_bars(5))
        report = self.run_pipeline(self.spec(fill_model="signal_bar_close"))
        first = report.trades[0]
        self.assertEqual(first["fill_timestamp"], first["signal_timestamp"])
        self.assertEqual(first["base_price"], 13.5)  # 第二根 close = 10.5+3
        self.assertEqual(report.unfilled_signals, 0)  # 同根成交 → 没有未成交

    def test_unknown_fill_model_is_explicit_failure(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(RunError) as ctx:
            self.run_pipeline(self.spec(fill_model="magic"))
        self.assertIn("未知成交模型", str(ctx.exception))


class CostModelTest(PipelineTestCase):
    def test_slippage_direction_and_fee_formula(self) -> None:
        costs = ExecutionCosts(commission_bps=2.5, min_commission=5.0, stamp_tax_bps=5.0,
                               slippage_bps=10.0)
        self.assertAlmostEqual(costs.fill_price("buy", 1000.0), 1001.0)   # 买入上浮
        self.assertAlmostEqual(costs.fill_price("sell", 1000.0), 999.0)   # 卖出下压
        # 佣金：1001*100*2.5bps = 25.025 → 不低于 5 元；买入无印花税
        self.assertAlmostEqual(costs.fee("buy", 1001.0, 100.0), 25.03, places=2)
        # 卖出：佣金 + 印花税（1000*100*5bps = 50）
        self.assertAlmostEqual(costs.fee("sell", 1000.0, 100.0), 25.0 + 50.0, places=2)

    def test_net_pnl_equals_gross_minus_fees(self) -> None:
        self.seed(self.rising_bars(5))
        report = self.run_pipeline(self.spec(
            costs=ExecutionCosts(commission_bps=2.5, min_commission=5.0, stamp_tax_bps=5.0,
                                 slippage_bps=1.0)))
        self.assertGreater(report.cost_totals["fee_total"], 0.0)
        self.assertAlmostEqual(
            report.net_pnl,
            report.metrics["gross_realized_pnl"] - report.cost_totals["fee_total"],
            places=6,
        )


class NoLookaheadTest(PipelineTestCase):
    def test_strategy_only_sees_past_and_current(self) -> None:
        _SpyStrategy.seen = []
        bars = self.rising_bars(5)
        self.seed(bars)
        self.run_pipeline(self.spec(strategy_type="spy", strategy_config={}),
                          strategies={"spy": _SpyStrategy})
        timestamps = [row["timestamp"] for row in bars]
        self.assertEqual(_SpyStrategy.seen, timestamps)  # 逐根推进、顺序一致

    def test_future_timestamp_signal_is_rejected(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(RunError) as ctx:
            self.run_pipeline(self.spec(strategy_type="future", strategy_config={}),
                              strategies={"future": _FutureStrategy})
        self.assertIn("未来", str(ctx.exception))


class ReproducibilityTest(PipelineTestCase):
    def test_same_input_same_run_id_and_result(self) -> None:
        self.seed(self.rising_bars(6))
        spec = self.spec()
        first = self.run_pipeline(spec)
        second = self.run_pipeline(spec)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.canonical_result(), second.canonical_result())

    def test_different_data_changes_run_id(self) -> None:
        self.seed(self.rising_bars(5))
        first = self.run_pipeline(self.spec())
        KlineRepository(self.source_db_path).bulk_upsert_klines(
            [{"symbol_id": self.symbol_id, "timeframe": self.timeframe,
              **_bar_row("2026-01-20 00:00:00", 30.0, 31.0, 29.0, 30.5)}]
        )
        second = self.run_pipeline(self.spec())
        self.assertNotEqual(first.run_id, second.run_id)


class FailureReportingTest(PipelineTestCase):
    def test_unknown_symbol(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(RunError) as ctx:
            self.run_pipeline(self.spec(symbol="999999"))
        self.assertIn("不存在标的", str(ctx.exception))

    def test_empty_range_does_not_succeed_silently(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(RunError) as ctx:
            self.run_pipeline(self.spec(timeframe="15m"))
        self.assertIn("没有 K 线数据", str(ctx.exception))

    def test_unknown_strategy_type(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(ValueError):
            self.run_pipeline(self.spec(strategy_type="no_such_strategy"))

    def test_bad_quantity(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(RunError):
            self.run_pipeline(self.spec(quantity=0))


class DataValidationTest(unittest.TestCase):
    """直接测行情校验函数（重复时间戳在数据库里受主键约束，走不到 RunError，故单测）。"""

    def test_anomalies_are_detected(self) -> None:
        rows = [
            _bar_row("2026-01-02 00:00:00", 10.0, 11.0, 9.0, 10.5),
            _bar_row("2026-01-02 00:00:00", 10.0, 11.0, 9.0, 10.5),   # 重复
            _bar_row("2026-01-01 00:00:00", 10.0, 11.0, 9.0, 10.5),   # 乱序
            _bar_row("2026-01-03 00:00:00", 10.0, 9.0, 11.0, 10.5),   # OHLC 异常（high<low）
            _bar_row("2026-01-04 00:00:00", 0.0, 0.0, 0.0, 0.0),      # 非正价格
        ]
        duplicates, out_of_order, ohlc, non_positive, warnings = _validate_bars(rows)
        self.assertEqual(duplicates, 1)
        self.assertEqual(out_of_order, 1)
        self.assertGreaterEqual(ohlc, 1)
        self.assertEqual(non_positive, 1)
        self.assertEqual(len(warnings), 4)


class UnknownStatePolicyTest(PipelineTestCase):
    def test_unmodeled_constraints_are_declared_not_silently_converted(self) -> None:
        self.seed(self.rising_bars(4))
        report = self.run_pipeline(self.spec())
        self.assertTrue(report.diagnostics["unknown_states_not_converted"])
        joined = " ".join(report.diagnostics["unmodeled_constraints"])
        for keyword in ("T+1", "涨跌停", "停牌", "ST"):
            self.assertIn(keyword, joined)
        self.assertTrue(report.diagnostics["cost_source"].startswith("assumed_defaults"))


class IsolationTest(PipelineTestCase):
    def test_source_db_is_not_modified(self) -> None:
        self.seed(self.rising_bars(5))
        before = sorted(p.name for p in Path(self._tmpdir.name).iterdir())
        size_before = self.source_db_path.stat().st_size
        self.run_pipeline(self.spec())
        self.assertEqual(sorted(p.name for p in Path(self._tmpdir.name).iterdir()), before)
        self.assertEqual(self.source_db_path.stat().st_size, size_before)


if __name__ == "__main__":
    unittest.main()
