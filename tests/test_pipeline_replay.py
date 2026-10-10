"""Stage 029 专项测试（一）：确定性复盘 —— 指纹覆盖 / 逐笔可复现 / 预热 / 边界。

覆盖（对应 Stage 029 验收 1、2、4、5、7、8、9）：
    指纹          input_fingerprint 覆盖全部关键输入；任一关键输入变化 → 指纹变化
    结果指纹      result_fingerprint 与逐笔结果一致；墙钟字段不污染
    逐笔复现      相同输入两次 → 逐笔信号/订单/成交/持仓/绩效完全一致
    预热          预热 K 线只喂策略、不计入绩效（无信号/成交落入预热区间）
    边界          区间末尾信号可显式用「区间之后第一根」成交；默认则如实记为未成交
    数据异常      非正价格等 → 明确失败（不静默改变交易语义）
    失败报告      策略异常向上抛出，不伪装成成功结果
    隔离          来源库只读不变

全部使用临时数据库，**不触碰 data/strategy_engine.db**。
"""

from __future__ import annotations

import itertools
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
from strategies.base import BarWindow, SignalCandidate, Strategy  # noqa: E402

MARKET = "CN"
CODE = "600519"
NAME = "贵州茅台"
CODE_B = "600000"
NAME_B = "浦发银行"

FREE_COSTS = dict(commission_bps=0.0, min_commission=0.0, stamp_tax_bps=0.0, slippage_bps=0.0)


def _bar_row(ts: str, o: float, h: float, low: float, c: float, v: float = 1000.0):
    return {
        "timestamp": ts, "open": o, "high": h, "low": low, "close": c,
        "volume": v, "amount": None,
    }


class _CodeSpyA(Strategy):
    """指纹测试用策略 A（与 B 同名但实现不同）。"""

    name: ClassVar[str] = "code_spy"

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        return None


class _CodeSpyB(Strategy):
    """指纹测试用策略 B（与 A 同名但实现不同 → 代码指纹应不同）。"""

    name: ClassVar[str] = "code_spy"

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        # 故意多一行，制造不同的源码
        _ = window.current.timestamp
        return None


class _SeenSpy(Strategy):
    """记录它看到过的全部 K 线时间戳（验证无未来数据）。"""

    name: ClassVar[str] = "seen_spy"
    seen: ClassVar[list[str]] = []

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        type(self).seen.append(window.current.timestamp)
        return None


class _BoomStrategy(Strategy):
    """在第二根 K 线上抛异常（验证失败不被伪装成成功）。"""

    name: ClassVar[str] = "boom"
    calls: ClassVar[int] = 0

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        type(self).calls += 1
        if type(self).calls >= 2:
            raise RuntimeError("策略内部错误")
        return None


class ReplayTestCase(unittest.TestCase):
    """基类：临时来源库 + 造数据工具。"""

    timeframe = "1d"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_replay_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        # 运行库放在**另一个**目录，避免污染“来源库目录不变”的断言
        self._rundir = tempfile.TemporaryDirectory(
            prefix="se_replay_run_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._rundir.cleanup)
        self._run_counter = itertools.count(1)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db_path)
        symbols = SymbolRepository(self.source_db_path)
        self.symbol_id = symbols.create_symbol(CODE, NAME, MARKET, "stock", is_active=1)
        self.symbol_id_b = symbols.create_symbol(CODE_B, NAME_B, MARKET, "stock", is_active=1)

    def seed(self, rows: list[dict[str, Any]], *, code: str = CODE, timeframe: str | None = None) -> None:
        symbol_id = self.symbol_id if code == CODE else self.symbol_id_b
        KlineRepository(self.source_db_path).bulk_upsert_klines(
            [
                {"symbol_id": symbol_id, "timeframe": timeframe or self.timeframe, **row}
                for row in rows
            ]
        )

    def spec(self, **kwargs: Any) -> RunSpec:
        base: dict[str, Any] = {
            "symbol": CODE,
            "timeframe": self.timeframe,
            "strategy_type": "close_direction",
            "quantity": 100.0,
            "costs": ExecutionCosts(**FREE_COSTS),
        }
        base.update(kwargs)
        return RunSpec(**base)

    def run_pipeline(self, spec: RunSpec, **kwargs: Any):
        # 默认把运行库写在独立目录（保留，供测试核对逐笔产物）
        kwargs.setdefault(
            "run_db_path", Path(self._rundir.name) / f"run{next(self._run_counter)}.db"
        )
        return run_pipeline(spec, source_db_path=self.source_db_path, **kwargs)

    @staticmethod
    def rising_bars(count: int = 6, day0: int = 1) -> list[dict[str, Any]]:
        return [
            _bar_row(
                f"2026-01-{day:02d} 00:00:00",
                10.0 + day,
                10.6 + day,
                9.9 + day,
                10.5 + day,
            )
            for day in range(day0, day0 + count)
        ]


# --------------------------------------------------------------------------- 指纹
class FingerprintTest(ReplayTestCase):
    def test_run_id_is_derived_from_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        report = self.run_pipeline(self.spec())
        self.assertEqual(report.run_id, "run-" + report.input_fingerprint[:16])
        self.assertTrue(report.result_fingerprint.startswith("res-"))
        self.assertTrue(len(report.strategy_code_fingerprint) >= 8)

    def test_same_input_same_fingerprints_and_trades(self) -> None:
        self.seed(self.rising_bars())
        first = self.run_pipeline(self.spec())
        second = self.run_pipeline(self.spec())
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertEqual(first.result_fingerprint, second.result_fingerprint)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(first.canonical_result(), second.canonical_result())

    def test_wall_clock_does_not_affect_result_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        first = self.run_pipeline(self.spec())
        second = self.run_pipeline(self.spec())
        # orders.created_at 是墙钟时间，但结果指纹只看规范化结果
        self.assertEqual(first.result_fingerprint, second.result_fingerprint)

    def test_changing_symbol_changes_input_fingerprint(self) -> None:
        rows = self.rising_bars()
        self.seed(rows)
        self.seed(rows, code=CODE_B)
        a = self.run_pipeline(self.spec(symbol=CODE))
        b = self.run_pipeline(self.spec(symbol=CODE_B))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_timeframe_changes_input_fingerprint(self) -> None:
        rows = self.rising_bars()
        self.seed(rows, timeframe="1d")
        self.seed(rows, timeframe="1h")
        a = self.run_pipeline(self.spec(timeframe="1d"))
        b = self.run_pipeline(self.spec(timeframe="1h"))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_range_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars(8))
        a = self.run_pipeline(self.spec(end_timestamp="2026-01-05"))
        b = self.run_pipeline(self.spec(end_timestamp="2026-01-08"))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_strategy_config_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        a = self.run_pipeline(self.spec(strategy_config={"min_move": 0.0}))
        b = self.run_pipeline(self.spec(strategy_config={"min_move": 0.001}))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_costs_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        a = self.run_pipeline(self.spec())
        b = self.run_pipeline(self.spec(costs=ExecutionCosts(commission_bps=1.0, min_commission=1.0)))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_fill_model_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        a = self.run_pipeline(self.spec(fill_model="next_bar_open"))
        b = self.run_pipeline(self.spec(fill_model="signal_bar_close"))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_quantity_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        a = self.run_pipeline(self.spec(quantity=100.0))
        b = self.run_pipeline(self.spec(quantity=200.0))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)

    def test_changing_warmup_and_boundary_config_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        a = self.run_pipeline(self.spec(warmup_bars=0, fill_beyond_end=False))
        b = self.run_pipeline(self.spec(warmup_bars=1, fill_beyond_end=False))
        c = self.run_pipeline(self.spec(warmup_bars=0, fill_beyond_end=True))
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)
        self.assertNotEqual(a.input_fingerprint, c.input_fingerprint)

    def test_changing_actual_bar_data_changes_input_fingerprint(self) -> None:
        bars = self.rising_bars(5)
        self.seed(bars)
        first = self.run_pipeline(self.spec())
        changed = list(bars)
        changed[2] = _bar_row("2026-01-03 00:00:00", 12.0, 99.0, 11.0, 98.0)
        self.seed(changed)
        second = self.run_pipeline(self.spec())
        self.assertNotEqual(first.input_fingerprint, second.input_fingerprint)
        self.assertNotEqual(first.result_fingerprint, second.result_fingerprint)

    def test_unused_data_does_not_change_input_fingerprint(self) -> None:
        # 只改了区间之外（end 之后）的数据 → 指纹不变
        self.seed(self.rising_bars(6))
        first = self.run_pipeline(self.spec(end_timestamp="2026-01-04"))
        self.seed([_bar_row("2026-02-20 00:00:00", 50.0, 55.0, 45.0, 52.0)])
        second = self.run_pipeline(self.spec(end_timestamp="2026-01-04"))
        self.assertEqual(first.input_fingerprint, second.input_fingerprint)

    def test_changing_strategy_code_changes_input_fingerprint(self) -> None:
        self.seed(self.rising_bars())
        a = self.run_pipeline(
            self.spec(strategy_type="code_spy"), strategies={"code_spy": _CodeSpyA}
        )
        b = self.run_pipeline(
            self.spec(strategy_type="code_spy"), strategies={"code_spy": _CodeSpyB}
        )
        self.assertNotEqual(a.strategy_code_fingerprint, b.strategy_code_fingerprint)
        self.assertNotEqual(a.input_fingerprint, b.input_fingerprint)


# --------------------------------------------------------------------------- 预热
class WarmupTest(ReplayTestCase):
    def test_warmup_enables_signal_on_first_scored_bar(self) -> None:
        self.seed(self.rising_bars(6))  # 01-01 .. 01-06
        no_warmup = self.run_pipeline(
            self.spec(start_timestamp="2026-01-02", end_timestamp="2026-01-06")
        )
        warmup = self.run_pipeline(
            self.spec(start_timestamp="2026-01-02", end_timestamp="2026-01-06", warmup_bars=1)
        )
        # 无预热：首根计分 K 线没有可比对象 → 不产生信号
        self.assertEqual(no_warmup.signals_generated, 4)
        self.assertEqual(no_warmup.scored_first_timestamp, "2026-01-02 00:00:00")
        # 有预热：首根计分 K 线有前一根（预热）可比 → 产生信号
        self.assertEqual(warmup.signals_generated, 5)
        self.assertEqual(warmup.warmup_bars, 1)
        self.assertEqual(warmup.scored_bars, 5)
        self.assertEqual(warmup.data.bars, 6)  # 1 预热 + 5 计分

    def test_warmup_signals_not_counted_in_performance(self) -> None:
        self.seed(self.rising_bars(6))
        report = self.run_pipeline(
            self.spec(start_timestamp="2026-01-02", end_timestamp="2026-01-06", warmup_bars=2)
        )
        # 任何成交的信号时间都不得落在计分区间之前（预热区间不产生成交）
        for trade in report.trades:
            self.assertGreaterEqual(trade["signal_timestamp"], report.scored_first_timestamp)
        # 运行库里的信号也只在计分区间
        signals = SignalRepository(report.run_db_path).list_signals()
        self.assertTrue(signals)
        for row in signals:
            self.assertGreaterEqual(row["timestamp"], report.scored_first_timestamp)


# --------------------------------------------------------------------------- 边界成交
class BoundaryFillTest(ReplayTestCase):
    def test_without_fill_beyond_end_last_signal_is_unfilled(self) -> None:
        self.seed(self.rising_bars(6))
        report = self.run_pipeline(
            self.spec(start_timestamp="2026-01-01", end_timestamp="2026-01-05")
        )
        # 计分 5 根 → 4 信号；最后一根无下一根（区间外不拉取）→ 3 成交 + 1 未成交
        self.assertEqual(report.signals_generated, 4)
        self.assertEqual(report.trades_executed, 3)
        self.assertEqual(report.unfilled_signals, 1)
        self.assertEqual(report.unfilled_signal_timestamps, ("2026-01-05 00:00:00",))

    def test_fill_beyond_end_fills_last_scored_signal(self) -> None:
        self.seed(self.rising_bars(6))
        report = self.run_pipeline(
            self.spec(
                start_timestamp="2026-01-01",
                end_timestamp="2026-01-05",
                fill_beyond_end=True,
            )
        )
        self.assertEqual(report.signals_generated, 4)
        self.assertEqual(report.trades_executed, 4)
        self.assertEqual(report.unfilled_signals, 0)
        # 恰好一笔是边界成交（信号在计分区间末尾、成交在区间之后）
        beyond = [t for t in report.trades if t["fill_beyond_scored_range"]]
        self.assertEqual(len(beyond), 1)
        self.assertEqual(beyond[0]["signal_timestamp"], "2026-01-05 00:00:00")
        self.assertEqual(beyond[0]["fill_timestamp"], "2026-01-06 00:00:00")
        self.assertEqual(beyond[0]["base_price"], 16.0)  # 01-06 开盘 = 10 + 6

    def test_beyond_end_bar_only_used_as_fill_price(self) -> None:
        # 区间之后那一根不得产生自己的信号/成交（计分区间不含它）
        self.seed(self.rising_bars(6))
        report = self.run_pipeline(
            self.spec(
                start_timestamp="2026-01-01",
                end_timestamp="2026-01-05",
                fill_beyond_end=True,
            )
        )
        signals = SignalRepository(report.run_db_path).list_signals()
        self.assertNotIn("2026-01-06 00:00:00", [row["timestamp"] for row in signals])


# --------------------------------------------------------------------------- 复盘产物
class ReplayProductTest(ReplayTestCase):
    def test_per_trade_replay_fields_are_consistent(self) -> None:
        self.seed(self.rising_bars(6))
        report = self.run_pipeline(
            self.spec(
                start_timestamp="2026-01-01",
                end_timestamp="2026-01-06",
                costs=ExecutionCosts(commission_bps=2.5, min_commission=5.0,
                                     stamp_tax_bps=5.0, slippage_bps=1.0),
            )
        )
        self.assertTrue(report.trades)
        # 报告逐笔 vs 运行库成交/订单逐条对齐
        trades = TradeRepository(report.run_db_path).list_trades()
        self.assertEqual([float(r["price"]) for r in trades], [t["fill_price"] for t in report.trades])
        self.assertEqual([int(r["signal_id"]) for r in trades], [t["signal_id"] for t in report.trades])
        self.assertEqual(len(OrderRepository(report.run_db_path).list_orders()), len(report.trades))
        for trade in report.trades:
            for key in ("signal_timestamp", "submit_timestamp", "fill_timestamp", "base_price",
                        "fill_price", "fee", "pnl", "position_after", "order_id", "trade_id"):
                self.assertIn(key, trade)
            self.assertEqual(trade["signal_timestamp"], trade["submit_timestamp"])
            self.assertLess(trade["signal_timestamp"], trade["fill_timestamp"])

    def test_unfilled_signals_are_listed_for_replay(self) -> None:
        self.seed(self.rising_bars(5))
        report = self.run_pipeline(self.spec())
        self.assertEqual(len(report.unfilled_signal_timestamps), report.unfilled_signals)


# --------------------------------------------------------------------------- 失败 / 隔离
class FailureAndIsolationTest(ReplayTestCase):
    def test_non_positive_price_is_explicit_failure(self) -> None:
        rows = self.rising_bars(4)
        rows.append(_bar_row("2026-01-10 00:00:00", 0.0, 0.0, 0.0, 0.0))
        self.seed(rows)
        with self.assertRaises(RunError) as ctx:
            self.run_pipeline(self.spec())
        self.assertIn("非正价格", str(ctx.exception))

    def test_strategy_exception_is_not_fake_success(self) -> None:
        self.seed(self.rising_bars(5))
        _BoomStrategy.calls = 0
        with self.assertRaises(RuntimeError):
            self.run_pipeline(self.spec(strategy_type="boom"), strategies={"boom": _BoomStrategy})

    def test_invalid_warmup_bars_is_explicit_failure(self) -> None:
        self.seed(self.rising_bars(3))
        with self.assertRaises(RunError):
            self.run_pipeline(self.spec(warmup_bars=-1))

    def test_source_db_is_not_modified(self) -> None:
        self.seed(self.rising_bars(6))
        before = sorted(p.name for p in Path(self._tmpdir.name).iterdir())
        size_before = self.source_db_path.stat().st_size
        self.run_pipeline(
            self.spec(
                start_timestamp="2026-01-02",
                end_timestamp="2026-01-05",
                warmup_bars=1,
                fill_beyond_end=True,
            )
        )
        self.assertEqual(sorted(p.name for p in Path(self._tmpdir.name).iterdir()), before)
        self.assertEqual(self.source_db_path.stat().st_size, size_before)


# --------------------------------------------------------------------------- 无未来数据
class NoLookaheadTest(ReplayTestCase):
    def test_strategy_sees_warmup_then_scored_ascending(self) -> None:
        bars = self.rising_bars(6)  # 01-01 .. 01-06
        self.seed(bars)
        _SeenSpy.seen = []
        report = self.run_pipeline(
            self.spec(
                strategy_type="seen_spy",
                start_timestamp="2026-01-03",
                end_timestamp="2026-01-05",
                warmup_bars=2,
                fill_beyond_end=False,
            ),
            strategies={"seen_spy": _SeenSpy},
        )
        # 策略看到的 = 2 根预热 + 3 根计分（升序、逐根推进、无未来）
        expected = [row["timestamp"] for row in bars[:5]]
        self.assertEqual(_SeenSpy.seen, expected)
        self.assertTrue(_SeenSpy.seen == sorted(_SeenSpy.seen))
        self.assertEqual(report.scored_first_timestamp, "2026-01-03 00:00:00")
        self.assertEqual(report.warmup_bars, 2)


if __name__ == "__main__":
    unittest.main()
