"""Stage 030 专项测试：多标的共享资金组合闭环。

覆盖（对应 Stage 030 验收 1–13）：
    1  两个标的共享同一笔初始资金
    2  同时买入不发生超额使用资金
    3  同时下单时执行顺序确定、结果可复现（与标的排列顺序无关）
    4  一个标的卖出释放的资金能被后续订单按规则使用
    5  一个标的的持仓变化不会覆盖另一个标的的持仓
    6/7 行情时间错位不读未来；缺失 K 线不被静默视为可成交
    8  成本 / 滑点 / 成交时点正确作用于组合账本
    9  组合净值、已实现盈亏与未实现盈亏口径一致
    10 任一影响结果的输入变化都会改变输入指纹
    11 相同完整输入重复运行 → 逐笔成交与结果指纹一致
    12 运行失败不遗留部分成功的状态
    13 单标的既有语义保持不变

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

from database import KlineRepository, SymbolRepository, initialize_database  # noqa: E402
from pipeline import (  # noqa: E402
    ExecutionCosts,
    PortfolioPolicy,
    PortfolioSpec,
    RunError,
    RunSpec,
    SymbolSpec,
    run_pipeline,
    run_portfolio,
)
from pipeline.loop import ENGINE_VERSION  # noqa: E402
from strategies.base import BarWindow, SignalCandidate, Strategy  # noqa: E402

MARKET = "CN"
CODE_A = "AAA001"
CODE_B = "BBB001"
NAME_A = "标的A"
NAME_B = "标的B"

ZERO_COSTS = ExecutionCosts(
    commission_bps=0.0, min_commission=0.0, stamp_tax_bps=0.0, slippage_bps=0.0
)


class ScriptedStrategy(Strategy):
    """按 config['plan']（时间戳 → long/short）精确产出信号，便于确定性测试。"""

    name: ClassVar[str] = "scripted"

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        direction = dict(config.get("plan", {})).get(window.current.timestamp)
        if direction is None:
            return None
        return SignalCandidate(
            timestamp=window.current.timestamp, direction=direction, price=window.current.close
        )


class SeenStrategy(Strategy):
    """记录它看到过的全部 K 线时间戳（验证无未来数据）。"""

    name: ClassVar[str] = "seen"
    seen: ClassVar[list[str]] = []

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        type(self).seen.append(window.current.timestamp)
        return None


def ts(day: int) -> str:
    return f"2026-01-{day:02d} 00:00:00"


def flat_bars(days: list[int], price: float) -> list[dict[str, Any]]:
    """开盘=收盘=price 的平盘 K 线（成交价可精确断言）。"""
    return [
        {
            "timestamp": ts(day), "open": price, "high": price * 1.01,
            "low": price * 0.99, "close": price, "volume": 1000.0, "amount": None,
        }
        for day in days
    ]


class PortfolioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_pf_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self._rundir = tempfile.TemporaryDirectory(prefix="se_pf_run_", ignore_cleanup_errors=True)
        self.addCleanup(self._rundir.cleanup)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db_path)
        self.symbols = SymbolRepository(self.source_db_path)
        self.symbols.create_symbol(CODE_A, NAME_A, MARKET, "stock", is_active=1)
        self.symbols.create_symbol(CODE_B, NAME_B, MARKET, "stock", is_active=1)

    def seed(self, code: str, bars: list[dict[str, Any]], timeframe: str = "1d") -> None:
        row = self.symbols.get_symbol(MARKET, code)
        KlineRepository(self.source_db_path).bulk_upsert_klines(
            [{"symbol_id": int(row["id"]), "timeframe": timeframe, **bar} for bar in bars]
        )

    def symbol(self, code: str, plan: Mapping[str, str], *, quantity: float = 10.0, **kwargs: Any):
        base: dict[str, Any] = {
            "symbol": code,
            "timeframe": "1d",
            "strategy_type": "scripted",
            "strategy_config": {"plan": dict(plan)},
            "quantity": quantity,
            "start_timestamp": "2026-01-01",
            "end_timestamp": "2026-01-31",
        }
        base.update(kwargs)
        return SymbolSpec(**base)

    def spec(self, symbols, *, capital: float = 1000.0, costs: ExecutionCosts = ZERO_COSTS, **policy: Any):
        return PortfolioSpec(
            symbols=tuple(symbols),
            initial_capital=capital,
            costs=costs,
            policy=PortfolioPolicy(**policy),
        )

    def run_pf(self, spec: PortfolioSpec):
        return run_portfolio(spec, source_db_path=self.source_db_path,
                             strategies={"scripted": ScriptedStrategy})

    def run_seen(self, spec: PortfolioSpec):
        return run_portfolio(spec, source_db_path=self.source_db_path,
                             strategies={"seen": SeenStrategy})


class SharedCapitalTest(PortfolioTestCase):
    def test_two_symbols_share_one_capital_pool(self) -> None:
        bars = flat_bars([1, 2, 3, 4], 100.0)
        self.seed(CODE_A, bars)
        self.seed(CODE_B, bars)
        plan = {ts(2): "long"}
        # 初始资金 1000，单价 100 × 10 股 = 1000 → 只够一个标的买入
        report = self.run_pf(self.spec([self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)]))
        self.assertEqual(len(report.trades), 1)
        self.assertEqual(report.trades[0]["symbol"], CODE_A)  # 代码升序先到先得
        self.assertEqual(len(report.rejected_orders), 1)
        self.assertEqual(report.rejected_orders[0]["reason"], "insufficient_cash")
        self.assertEqual(report.rejected_orders[0]["symbol"], CODE_B)
        self.assertEqual(report.summary["cash_available"], 0.0)  # 资金只被用了一次

    def test_no_over_allocation_across_symbols(self) -> None:
        bars = flat_bars([1, 2, 3, 4], 100.0)
        self.seed(CODE_A, bars)
        self.seed(CODE_B, bars)
        plan = {ts(2): "long"}
        report = self.run_pf(self.spec([self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)]))
        self.assertGreaterEqual(report.summary["cash_available"], 0.0)
        spent = sum(t["fill_price"] * t["quantity"] + t["fee"] for t in report.trades)
        self.assertLessEqual(spent, 1000.0 + 1e-9)

    def test_execution_order_is_by_code_not_spec_order(self) -> None:
        bars = flat_bars([1, 2, 3, 4], 100.0)
        self.seed(CODE_A, bars)
        self.seed(CODE_B, bars)
        plan = {ts(2): "long"}
        normal = self.run_pf(self.spec([self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)]))
        reversed_spec = self.run_pf(self.spec([self.symbol(CODE_B, plan), self.symbol(CODE_A, plan)]))
        # 排列顺序不同 → 结果完全一致（排序规则固定）
        self.assertEqual(normal.canonical_result(), reversed_spec.canonical_result())
        self.assertEqual(normal.input_fingerprint, reversed_spec.input_fingerprint)
        self.assertEqual(normal.trades[0]["symbol"], CODE_A)

    def test_sell_releases_cash_for_later_orders(self) -> None:
        bars = flat_bars([1, 2, 3, 4, 5], 100.0)
        self.seed(CODE_A, bars)
        self.seed(CODE_B, bars)
        # A 在 01-02 买入（01-03 成交），01-03 卖出（01-04 成交）；B 在 01-03 买入（01-04 成交）
        spec = self.spec([
            self.symbol(CODE_A, {ts(2): "long", ts(3): "short"}),
            self.symbol(CODE_B, {ts(3): "long"}),
        ])
        report = self.run_pf(spec)
        self.assertEqual(len(report.rejected_orders), 0)
        self.assertEqual([t["symbol"] for t in report.trades], [CODE_A, CODE_A, CODE_B])
        # A 卖出回收的 1000 元被 B 的买入使用
        self.assertEqual(report.per_symbol[CODE_A]["position_quantity"], 0.0)
        self.assertEqual(report.per_symbol[CODE_B]["position_quantity"], 10.0)
        self.assertEqual(report.summary["cash_available"], 0.0)


class PositionIsolationTest(PortfolioTestCase):
    def test_positions_are_not_overwritten_across_symbols(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 2, 3], 50.0))
        plan = {ts(2): "long"}
        report = self.run_pf(self.spec(
            [self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)], capital=3000.0
        ))
        self.assertEqual(len(report.trades), 2)
        self.assertEqual(report.per_symbol[CODE_A]["position_quantity"], 10.0)
        self.assertEqual(report.per_symbol[CODE_B]["position_quantity"], 10.0)
        self.assertEqual(report.per_symbol[CODE_A]["position_value"], 1000.0)
        self.assertEqual(report.per_symbol[CODE_B]["position_value"], 500.0)
        self.assertEqual(report.summary["positions_value"], 1500.0)

    def test_naked_short_rejected_by_default_and_allowed_when_configured(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        plan = {ts(2): "short"}
        default = self.run_pf(self.spec([self.symbol(CODE_A, plan)]))
        self.assertEqual(len(default.trades), 0)
        self.assertEqual(default.rejected_orders[0]["reason"], "insufficient_position")
        allowed = self.run_pf(self.spec([self.symbol(CODE_A, plan)], allow_short=True))
        self.assertEqual(len(allowed.trades), 1)
        self.assertEqual(allowed.per_symbol[CODE_A]["position_quantity"], -10.0)


class TimeAlignmentTest(PortfolioTestCase):
    def test_missing_bar_is_not_tradable_and_no_future_fill(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 3], 100.0))  # 缺 01-02
        plan = {ts(2): "long"}
        report = self.run_pf(self.spec(
            [self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)], capital=10_000.0
        ))
        # B 在 01-02 没有行情 → 不产生信号、不被当作可成交；只有 A 成交
        self.assertEqual([t["symbol"] for t in report.trades], [CODE_A])
        # 成交价必须来自「该标的自己的下一根 K 线」，且不早于信号
        for trade in report.trades:
            self.assertGreater(trade["fill_timestamp"], trade["signal_timestamp"])
            self.assertEqual(trade["fill_timestamp"], ts(3))

    def test_strategy_sees_each_symbol_bars_only_once_ascending(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 3], 100.0))
        SeenStrategy.seen = []
        report = self.run_seen(self.spec(
            [
                SymbolSpec(symbol=CODE_A, timeframe="1d", strategy_type="seen", start_timestamp="2026-01-01", end_timestamp="2026-01-31"),
                SymbolSpec(symbol=CODE_B, timeframe="1d", strategy_type="seen", start_timestamp="2026-01-01", end_timestamp="2026-01-31"),
            ],
            capital=10_000.0,
        ))
        self.assertEqual(SeenStrategy.seen, sorted(SeenStrategy.seen))
        self.assertEqual(len(SeenStrategy.seen), 5)  # 3 + 2 根，各喂一次
        self.assertEqual(report.summary["equity_points"], 3)  # 时间轴并集 = {01-01,01-02,01-03}


class LedgerAndValuationTest(PortfolioTestCase):
    def test_costs_and_slippage_flow_into_portfolio_ledger(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 2, 3], 100.0))
        costs = ExecutionCosts(commission_bps=2.5, min_commission=5.0, stamp_tax_bps=5.0, slippage_bps=10.0)
        plan = {ts(2): "long"}
        report = self.run_pf(self.spec(
            [self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)], capital=10_000.0, costs=costs
        ))
        for trade in report.trades:
            expected_fee = costs.fee(trade["side"], trade["fill_price"], trade["quantity"])
            self.assertAlmostEqual(trade["fee"], expected_fee, places=2)
            self.assertEqual(trade["base_price"], 100.0)
            self.assertAlmostEqual(trade["fill_price"], 100.1, places=4)  # 买入上浮 10bps
        self.assertAlmostEqual(
            report.summary["fee_total"],
            sum(t["fee"] for t in report.trades),
            places=2,
        )
        self.assertAlmostEqual(
            report.summary["slippage_total"],
            sum(abs(t["fill_price"] - t["base_price"]) * t["quantity"] for t in report.trades),
            places=2,
        )

    def test_equity_realized_unrealized_are_consistent(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 2, 3], 50.0))
        plan = {ts(2): "long"}
        report = self.run_pf(self.spec(
            [self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)], capital=3000.0
        ))
        summary = report.summary
        expected = (
            summary["initial_capital"]
            + summary["realized_pnl"]
            - summary["fee_total"]
            + summary["unrealized_pnl"]
        )
        self.assertAlmostEqual(summary["final_equity"], expected, places=4)
        self.assertAlmostEqual(
            summary["final_equity"],
            summary["cash_available"] + summary["positions_value"],
            places=4,
        )
        self.assertAlmostEqual(
            summary["net_pnl"], summary["final_equity"] - summary["initial_capital"], places=4
        )


class FingerprintAndReproducibilityTest(PortfolioTestCase):
    def _baseline(self):
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 2, 3], 100.0))
        plan = {ts(2): "long"}
        return self.spec(
            [self.symbol(CODE_A, plan), self.symbol(CODE_B, plan)], capital=10_000.0
        )

    def test_same_input_same_ledger_and_fingerprints(self) -> None:
        spec = self._baseline()
        first = self.run_pf(spec)
        second = self.run_pf(spec)
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.result_fingerprint, second.result_fingerprint)
        self.assertEqual(first.run_id, second.run_id)
        self.assertTrue(first.run_id.startswith("pf-"))
        self.assertEqual(first.canonical_result(), second.canonical_result())

    def test_input_changes_move_input_fingerprint(self) -> None:
        spec = self._baseline()
        base = self.run_pf(spec).input_fingerprint
        variants = {
            "capital": self.spec(spec.symbols, capital=20_000.0),
            "costs": self.spec(spec.symbols, capital=10_000.0,
                               costs=ExecutionCosts(commission_bps=1.0, min_commission=1.0)),
            "policy": self.spec(spec.symbols, capital=10_000.0, allow_short=True),
            "fill_model": self.spec(spec.symbols, capital=10_000.0, fill_model="signal_bar_close"),
            "quantity": PortfolioSpec(
                symbols=(self.symbol(CODE_A, {ts(2): "long"}, quantity=5.0),
                         self.symbol(CODE_B, {ts(2): "long"})),
                initial_capital=10_000.0, costs=ZERO_COSTS, policy=PortfolioPolicy(),
            ),
            "range": PortfolioSpec(
                symbols=(self.symbol(CODE_A, {ts(2): "long"}, end_timestamp="2026-01-02"),
                         self.symbol(CODE_B, {ts(2): "long"})),
                initial_capital=10_000.0, costs=ZERO_COSTS, policy=PortfolioPolicy(),
            ),
        }
        for name, variant in variants.items():
            with self.subTest(name=name):
                self.assertNotEqual(base, self.run_pf(variant).input_fingerprint)

    def test_changing_bar_data_moves_fingerprints(self) -> None:
        spec = self._baseline()
        first = self.run_pf(spec)
        self.seed(CODE_A, flat_bars([1, 2, 3], 101.0))
        second = self.run_pf(spec)
        self.assertNotEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertNotEqual(first.result_fingerprint, second.result_fingerprint)

    def test_data_outside_used_range_does_not_move_fingerprint(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 2, 3], 100.0))
        plan = {ts(2): "long"}
        short_range = PortfolioSpec(
            symbols=(self.symbol(CODE_A, plan, end_timestamp="2026-01-03"),
                     self.symbol(CODE_B, plan, end_timestamp="2026-01-03")),
            initial_capital=10_000.0, costs=ZERO_COSTS, policy=PortfolioPolicy(),
        )
        first = self.run_pf(short_range).input_fingerprint
        self.seed(CODE_A, flat_bars([20], 999.0))  # 区间之后的行情
        self.assertEqual(first, self.run_pf(short_range).input_fingerprint)


class FailureAndIsolationTest(PortfolioTestCase):
    def test_unknown_symbol_fails_explicitly(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        with self.assertRaises(RunError) as ctx:
            self.run_pf(self.spec([self.symbol("999999", {ts(2): "long"})]))
        self.assertIn("不存在标的", str(ctx.exception))

    def test_duplicate_symbol_fails_explicitly(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        with self.assertRaises(RunError) as ctx:
            self.run_pf(self.spec([self.symbol(CODE_A, {}), self.symbol(CODE_A, {})]))
        self.assertIn("重复标的", str(ctx.exception))

    def test_non_positive_capital_fails_explicitly(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        with self.assertRaises(RunError):
            self.run_pf(self.spec([self.symbol(CODE_A, {})], capital=0.0))

    def test_failure_leaves_source_db_untouched(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        before = sorted((p.name, p.stat().st_size) for p in Path(self._tmpdir.name).iterdir())
        with self.assertRaises(RunError):
            self.run_pf(self.spec([self.symbol("999999", {})]))
        after = sorted((p.name, p.stat().st_size) for p in Path(self._tmpdir.name).iterdir())
        self.assertEqual(before, after)

    def test_success_leaves_source_db_untouched(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3], 100.0))
        self.seed(CODE_B, flat_bars([1, 2, 3], 100.0))
        before = sorted((p.name, p.stat().st_size) for p in Path(self._tmpdir.name).iterdir())
        self.run_pf(self.spec([self.symbol(CODE_A, {ts(2): "long"}),
                            self.symbol(CODE_B, {ts(2): "long"})], capital=10_000.0))
        after = sorted((p.name, p.stat().st_size) for p in Path(self._tmpdir.name).iterdir())
        self.assertEqual(before, after)


class SingleSymbolCompatibilityTest(PortfolioTestCase):
    def test_single_symbol_pipeline_semantics_unchanged(self) -> None:
        self.seed(CODE_A, flat_bars([1, 2, 3, 4], 100.0))
        self.assertEqual(ENGINE_VERSION, "stage029.1")
        first = run_pipeline(
            RunSpec(symbol=CODE_A, timeframe="1d", quantity=10.0, strategy_type="scripted",
                    strategy_config={"plan": {ts(2): "long"}}),
            source_db_path=self.source_db_path,
            strategies={"scripted": ScriptedStrategy},
        )
        second = run_pipeline(
            RunSpec(symbol=CODE_A, timeframe="1d", quantity=10.0, strategy_type="scripted",
                    strategy_config={"plan": {ts(2): "long"}}),
            source_db_path=self.source_db_path,
            strategies={"scripted": ScriptedStrategy},
        )
        self.assertTrue(first.run_id.startswith("run-"))
        self.assertEqual(first.canonical_result(), second.canonical_result())
        self.assertEqual(first.signals_generated, 1)
        self.assertEqual(first.trades_executed, 1)


if __name__ == "__main__":
    unittest.main()
