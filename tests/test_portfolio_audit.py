"""Stage 031：组合账本与风险指标审计测试。

目的：用最小合成行情**逐项核对** Stage 030 组合账本的口径，只在有确凿证据时修复。

覆盖（对应 Stage 031 验收 1–9）：
    1  买入后权益不因「资产换成持仓」而凭空增加
    2  卖出与手续费不会重复计入盈亏（逐笔手算核对现金）
    3  部分平仓 / 反向交易的账实一致性
    4  浮亏正确反映在组合权益与最大回撤中
    5  缺失行情不产生「虚假的平稳净值」（明确告警，不用未来价格）
    6  做空与仓位上限符合明确的配置语义
    7  资金不足 / 拒单不破坏账本（现金、持仓、订单状态一致）
    8  相同输入产生相同的组合结果
    9  单标的 Stage 029 语义不变

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
from pipeline import (  # noqa: E402
    ExecutionCosts,
    PortfolioPolicy,
    PortfolioSpec,
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

ZERO = ExecutionCosts(commission_bps=0.0, min_commission=0.0, stamp_tax_bps=0.0, slippage_bps=0.0)
MIN_FEE_5 = ExecutionCosts(commission_bps=0.0, min_commission=5.0, stamp_tax_bps=0.0, slippage_bps=0.0)


class Scripted(Strategy):
    name: ClassVar[str] = "scripted"

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        direction = dict(config.get("plan", {})).get(window.current.timestamp)
        if direction is None:
            return None
        return SignalCandidate(window.current.timestamp, direction, window.current.close)


def ts(day: int) -> str:
    return f"2026-01-{day:02d} 00:00:00"


def bars_of(prices: Mapping[int, float]) -> list[dict[str, Any]]:
    """{日: 价格} → 平盘 K 线（open=close=价格），便于精确手算。"""
    return [
        {"timestamp": ts(day), "open": price, "high": price * 1.01, "low": price * 0.99,
         "close": price, "volume": 1000.0, "amount": None}
        for day, price in sorted(prices.items())
    ]


class AuditBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_audit_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self._rundir = tempfile.TemporaryDirectory(prefix="se_audit_run_", ignore_cleanup_errors=True)
        self.addCleanup(self._rundir.cleanup)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db_path)
        self.symbols = SymbolRepository(self.source_db_path)
        for code in (CODE_A, CODE_B):
            self.symbols.create_symbol(code, code, MARKET, "stock", is_active=1)

    def seed(self, code: str, rows: list[dict[str, Any]]) -> None:
        row = self.symbols.get_symbol(MARKET, code)
        KlineRepository(self.source_db_path).bulk_upsert_klines(
            [{"symbol_id": int(row["id"]), "timeframe": "1d", **r} for r in rows]
        )

    def symbol(self, code: str, plan: Mapping[str, str], quantity: float = 10.0) -> SymbolSpec:
        return SymbolSpec(
            symbol=code, timeframe="1d", strategy_type="scripted",
            strategy_config={"plan": dict(plan)}, quantity=quantity,
            start_timestamp="2026-01-01", end_timestamp="2026-01-31",
        )

    def run_pf(self, symbols, *, capital: float = 1000.0, costs: ExecutionCosts = ZERO, **policy: Any):
        return run_portfolio(
            PortfolioSpec(tuple(symbols), capital, costs, PortfolioPolicy(**policy)),
            source_db_path=self.source_db_path,
            strategies={"scripted": Scripted},
        )

    def run_pf_db(self, symbols, *, capital: float = 1000.0, costs: ExecutionCosts = ZERO, **policy: Any):
        """保留运行库（用于核对订单 / 信号 / 成交落库状态）。"""
        run_db = Path(self._rundir.name) / f"run{len(list(Path(self._rundir.name).iterdir()))}.db"
        report = run_portfolio(
            PortfolioSpec(tuple(symbols), capital, costs, PortfolioPolicy(**policy)),
            source_db_path=self.source_db_path,
            run_db_path=run_db,
            strategies={"scripted": Scripted},
        )
        return report, run_db


# --------------------------------------------------------------------------- 1) 权益不凭空增加
class EquityIntegrityTest(AuditBase):
    def test_buy_does_not_create_phantom_equity(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0}))
        report = self.run_pf([self.symbol(CODE_A, {ts(2): "long"})])
        self.assertEqual(len(report.trades), 1)
        # 资产从现金换成持仓，权益必须恒等于初始资金（零成本、平盘）
        for timestamp, equity in report.equity_curve:
            self.assertAlmostEqual(equity, 1000.0, places=6, msg=timestamp)
        self.assertAlmostEqual(report.summary["net_pnl"], 0.0, places=6)
        self.assertAlmostEqual(report.summary["positions_cost"], 1000.0, places=6)
        self.assertAlmostEqual(report.summary["positions_value"], 1000.0, places=6)
        self.assertAlmostEqual(report.summary["cash_available"], 0.0, places=6)

    def test_cash_moves_by_exactly_notional_plus_fee(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0, 5: 100.0}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long", ts(4): "short"})],
            capital=2000.0, costs=MIN_FEE_5,
        )
        self.assertEqual(len(report.trades), 2)
        self.assertAlmostEqual(report.summary["cash_available"], 2000.0 - 5.0 - 5.0, places=6)
        self.assertEqual([t["cash_after"] for t in report.trades], [995.0, 1990.0])


# --------------------------------------------------------------------------- 2) 手续费只扣一次
class FeeAccountingTest(AuditBase):
    def test_fee_is_not_double_charged(self) -> None:
        # 买 100 → 卖 110（各 10 股，单笔最低佣金 5 元）
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 110.0, 5: 110.0}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long", ts(4): "short"})],
            capital=2000.0, costs=MIN_FEE_5,
        )
        summary = report.summary
        self.assertAlmostEqual(summary["realized_pnl"], 100.0, places=6)  # 毛已实现
        self.assertAlmostEqual(summary["fee_total"], 10.0, places=2)
        self.assertAlmostEqual(summary["unrealized_pnl"], 0.0, places=6)
        # 净收益 = 毛已实现 − 手续费（手续费恰好一次）
        self.assertAlmostEqual(summary["net_pnl"], 90.0, places=6)
        self.assertAlmostEqual(summary["cash_available"], 2090.0, places=6)
        self.assertAlmostEqual(summary["final_equity"], 2090.0, places=6)

    def test_fee_not_inside_metrics_gross_pnl(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 110.0, 5: 110.0}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long", ts(4): "short"})],
            capital=2000.0, costs=MIN_FEE_5,
        )
        self.assertAlmostEqual(report.metrics["gross_realized_pnl"], 100.0, places=6)
        self.assertAlmostEqual(report.metrics["profit_factor"], float("inf"), places=6)


# --------------------------------------------------------------------------- 3) 部分平仓 / 反向
class PartialAndReverseTest(AuditBase):
    def test_partial_close_keeps_cost_basis_and_ledger(self) -> None:
        # 买 10@100（01-03）→ 再买 10@110（01-04，持仓 20，均价 105）
        # → 卖 10@110（01-05，部分平仓：实现 10×(110−105)=50，剩余均价仍为 105）
        self.seed(
            CODE_A,
            bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 110.0, 5: 110.0, 6: 120.0, 7: 120.0}),
        )
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long", ts(3): "long", ts(4): "short"}, quantity=10.0)],
            capital=5000.0, costs=ZERO,
        )
        self.assertEqual([t["side"] for t in report.trades], ["buy", "buy", "sell"])
        self.assertAlmostEqual(report.summary["realized_pnl"], 50.0, places=6)
        info = report.per_symbol[CODE_A]
        self.assertAlmostEqual(info["position_quantity"], 10.0, places=6)
        self.assertAlmostEqual(info["position_avg_price"], 105.0, places=6)
        self.assertAlmostEqual(info["unrealized_pnl"], 150.0, places=6)  # 10×(120−105)
        self.assertAlmostEqual(report.summary["cash_available"], 4000.0, places=6)
        self.assertAlmostEqual(report.summary["final_equity"], 5200.0, places=6)
        summary = report.summary
        self.assertAlmostEqual(
            summary["final_equity"],
            summary["initial_capital"] + summary["realized_pnl"]
            - summary["fee_total"] + summary["unrealized_pnl"],
            places=4,
        )

    def test_full_close_then_open_short_position_math(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 110.0, 5: 110.0, 6: 120.0, 7: 120.0}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long", ts(4): "short", ts(6): "short"}, quantity=10.0)],
            capital=5000.0, costs=ZERO, allow_short=True,
        )
        # 买 10@100 → 卖 10@110（清仓，实现 +100）→ 再卖 10@120（开空 10）
        self.assertEqual([t["side"] for t in report.trades], ["buy", "sell", "sell"])
        self.assertAlmostEqual(report.summary["realized_pnl"], 100.0, places=6)
        self.assertEqual(report.per_symbol[CODE_A]["position_quantity"], -10.0)
        # 以 120 开空、末价仍为 120 → 浮动 0；权益 = 5000 + 100
        self.assertAlmostEqual(report.summary["final_equity"], 5100.0, places=6)
        summary = report.summary
        self.assertAlmostEqual(
            summary["final_equity"],
            summary["initial_capital"] + summary["realized_pnl"]
            - summary["fee_total"] + summary["unrealized_pnl"],
            places=4,
        )


# --------------------------------------------------------------------------- 4) 浮亏进入权益与回撤
class MarkToMarketTest(AuditBase):
    def test_unrealized_loss_hits_equity_and_drawdown(self) -> None:
        # 买 10@100（01-03 成交），随后价格跌到 90（未平仓）
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 90.0, 5: 90.0}))
        report = self.run_pf([self.symbol(CODE_A, {ts(2): "long"})])
        self.assertAlmostEqual(report.summary["unrealized_pnl"], -100.0, places=6)
        self.assertAlmostEqual(report.summary["realized_pnl"], 0.0, places=6)
        self.assertAlmostEqual(report.summary["final_equity"], 900.0, places=6)
        self.assertAlmostEqual(report.summary["net_pnl"], -100.0, places=6)
        # 最大回撤必须包含浮亏（>= 100），只算已实现会得到 0
        self.assertAlmostEqual(report.summary["max_drawdown"], 100.0, places=6)
        self.assertAlmostEqual(report.summary["max_drawdown_pct"], 0.1, places=9)
        self.assertAlmostEqual(report.summary["equity_points"], len(report.equity_curve), places=6)

    def test_drawdown_uses_full_equity_curve_not_realized_only(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 80.0, 5: 100.0}))
        report = self.run_pf([self.symbol(CODE_A, {ts(2): "long"})])
        # 已实现为 0，但权益曲线 1000 → 800 → 1000，回撤 200
        self.assertAlmostEqual(report.summary["realized_pnl"], 0.0, places=6)
        self.assertAlmostEqual(report.summary["max_drawdown"], 200.0, places=6)


# --------------------------------------------------------------------------- 5) 缺失行情估值
class MissingQuoteValuationTest(AuditBase):
    def test_missing_quote_is_flagged_and_no_future_price_used(self) -> None:
        # A 在 01-03 买入后停牌（缺 01-04~01-06），01-07 复牌大跌到 80
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 7: 80.0}))
        self.seed(CODE_B, bars_of({d: 100.0 for d in range(1, 8)}))  # 提供完整时间轴
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long"}), self.symbol(CODE_B, {})],
            capital=2000.0,
        )
        curve = dict(report.equity_curve)
        # 缺口期间不得使用 01-07 的未来价格 → 仍按最近已发生收盘价 100 估值（并被标记为陈旧）
        for day in (4, 5, 6):
            self.assertAlmostEqual(curve[ts(day)], 2000.0, places=6)
        self.assertAlmostEqual(curve[ts(7)], 1800.0, places=6)  # 复牌后才反映 80
        self.assertGreaterEqual(report.diagnostics["stale_mark_events"], 3)
        self.assertIn("估值", report.diagnostics["valuation_rule"])

    def test_flat_equity_from_missing_quotes_is_explicitly_flagged(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 7: 80.0}))
        self.seed(CODE_B, bars_of({d: 100.0 for d in range(1, 8)}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long"}), self.symbol(CODE_B, {})],
            capital=2000.0,
        )
        # 不静默：缺口被计入 stale_mark_events，且 diagnostic 明确说明估值口径
        self.assertGreater(report.diagnostics["stale_mark_events"], 0)
        self.assertTrue(report.diagnostics["unknown_states_not_converted"])


# --------------------------------------------------------------------------- 6) 仓位上限 / 做空
class RiskConstraintTest(AuditBase):
    def test_max_position_value_blocks_oversized_buy(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long"}, quantity=10.0)],
            capital=10_000.0, max_position_value=500.0,  # 10×100 = 1000 > 500
        )
        self.assertEqual(len(report.trades), 0)
        self.assertEqual(len(report.rejected_orders), 1)
        self.assertEqual(report.rejected_orders[0]["reason"], "position_limit")
        self.assertAlmostEqual(report.summary["cash_available"], 10_000.0, places=6)
        self.assertEqual(report.per_symbol[CODE_A]["position_quantity"], 0.0)

    def test_max_position_value_allows_within_limit(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0}))
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long"}, quantity=10.0)],
            capital=10_000.0, max_position_value=1000.0,
        )
        self.assertEqual(len(report.trades), 1)

    def test_short_semantics_are_explicit(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0}))
        default = self.run_pf([self.symbol(CODE_A, {ts(2): "short"})])
        self.assertEqual(len(default.trades), 0)
        self.assertEqual(default.rejected_orders[0]["reason"], "insufficient_position")
        allowed = self.run_pf([self.symbol(CODE_A, {ts(2): "short"})], allow_short=True)
        self.assertEqual(len(allowed.trades), 1)
        self.assertEqual(allowed.per_symbol[CODE_A]["position_quantity"], -10.0)
        # 无保证金模型必须显式声明
        self.assertTrue(
            any("保证金" in item for item in allowed.diagnostics["unmodeled_constraints"])
        )


# --------------------------------------------------------------------------- 7) 拒单不破坏账本
class RejectionLedgerTest(AuditBase):
    def test_rejected_order_is_recorded_and_ledger_stays_consistent(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0}))
        report, run_db = self.run_pf_db(
            [self.symbol(CODE_A, {ts(2): "long"}, quantity=10.0)],
            capital=500.0,  # 需要 1000 > 500 → 资金不足
        )
        self.assertEqual(len(report.trades), 0)
        self.assertEqual(len(report.rejected_orders), 1)
        self.assertEqual(report.rejected_orders[0]["reason"], "insufficient_cash")
        # 现金与持仓不变
        self.assertAlmostEqual(report.summary["cash_available"], 500.0, places=6)
        self.assertEqual(report.per_symbol[CODE_A]["position_quantity"], 0.0)
        # 无成交
        self.assertEqual(len(TradeRepository(run_db).list_trades()), 0)
        # 订单状态必须落库且一致（拒单 → rejected），不能没有订单记录
        orders = OrderRepository(run_db).list_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "rejected")
        # 信号状态为 cancelled（不遗留 pending 假象）
        signals = SignalRepository(run_db).list_signals()
        self.assertEqual([s["status"] for s in signals], ["cancelled"])
        # 报告里的拒单必须能对应到落库订单
        self.assertEqual(report.rejected_orders[0].get("order_id"), int(orders[0]["id"]))

    def test_no_over_allocation_when_two_symbols_compete(self) -> None:
        rows = bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0})
        self.seed(CODE_A, rows)
        self.seed(CODE_B, rows)
        report = self.run_pf(
            [self.symbol(CODE_A, {ts(2): "long"}), self.symbol(CODE_B, {ts(2): "long"})],
            capital=1000.0,
        )
        self.assertEqual(len(report.trades), 1)
        self.assertAlmostEqual(report.summary["cash_available"], 0.0, places=6)
        first = report.trades[0]
        self.assertLessEqual(first["fill_price"] * first["quantity"] + first["fee"], 1000.0 + 1e-9)


# --------------------------------------------------------------------------- 8) 可复现
class ReproducibilityTest(AuditBase):
    def test_same_input_same_portfolio_result(self) -> None:
        rows = bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 110.0, 5: 110.0})
        self.seed(CODE_A, rows)
        self.seed(CODE_B, rows)
        symbols = [self.symbol(CODE_A, {ts(2): "long"}), self.symbol(CODE_B, {ts(2): "long", ts(4): "short"})]
        first = self.run_pf(symbols, capital=3000.0, costs=MIN_FEE_5)
        second = self.run_pf(symbols, capital=3000.0, costs=MIN_FEE_5)
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.result_fingerprint, second.result_fingerprint)
        self.assertEqual(first.canonical_result(), second.canonical_result())


# --------------------------------------------------------------------------- 9) 单标的语义不变
class SingleSymbolRegressionTest(AuditBase):
    def test_stage029_single_symbol_semantics_unchanged(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 101.0, 3: 102.0, 4: 103.0}))
        self.assertEqual(ENGINE_VERSION, "stage029.1")
        spec = RunSpec(symbol=CODE_A, timeframe="1d", quantity=10.0,
                       strategy_type="scripted", strategy_config={"plan": {ts(2): "long"}})
        first = run_pipeline(spec, source_db_path=self.source_db_path,
                             strategies={"scripted": Scripted})
        second = run_pipeline(spec, source_db_path=self.source_db_path,
                              strategies={"scripted": Scripted})
        self.assertTrue(first.run_id.startswith("run-"))
        self.assertEqual(first.canonical_result(), second.canonical_result())
        self.assertEqual(first.signals_generated, 1)
        self.assertEqual(first.trades_executed, 1)

    def test_source_db_is_untouched(self) -> None:
        self.seed(CODE_A, bars_of({1: 100.0, 2: 100.0, 3: 100.0, 4: 100.0}))
        before = sorted((p.name, p.stat().st_size) for p in Path(self._tmpdir.name).iterdir())
        self.run_pf([self.symbol(CODE_A, {ts(2): "long"})])
        after = sorted((p.name, p.stat().st_size) for p in Path(self._tmpdir.name).iterdir())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
