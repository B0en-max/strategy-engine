"""Stage 029 专项测试（二）：时间序列样本外验证（训练 / 验证 / 测试）。

覆盖（对应 Stage 029 验收 3、4、5、6、7、9）：
    时间切分     训练/验证/测试目标区间互不重叠、按时间升序；重叠直接报错
    预热         预热 K 线不重复计入区间绩效
    边界         区间末尾信号归属本区间（成交可落在区间之后，标记清楚）
    测试冻结     同一测试区间在参数变更后被重复评估 → 明确告警
    证据质量     样本/区间不足 → insufficient（不为「收益为正」背书）
    收益率口径   未提供初始资金 → 不算收益率（不用不一致分母）
    无未来数据   单区间运行时策略看不到该区间之前之外的未来
    隔离         来源库只读不变

全部使用临时数据库，**不触碰 data/strategy_engine.db**。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from typing import Any, ClassVar, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import KlineRepository, SymbolRepository, initialize_database  # noqa: E402
from pipeline import (  # noqa: E402
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_OK,
    ExecutionCosts,
    RunSpec,
    Segment,
    WalkForwardSpec,
    reset_test_access_log,
    run_walk_forward,
)
from strategies.base import BarWindow, SignalCandidate, Strategy  # noqa: E402

MARKET = "CN"
CODE = "600519"
NAME = "贵州茅台"

FREE_COSTS = ExecutionCosts(commission_bps=0.0, min_commission=0.0, stamp_tax_bps=0.0, slippage_bps=0.0)


def _bar_row(ts: str, o: float, h: float, low: float, c: float, v: float = 1000.0):
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": v, "amount": None}


def daily_bars(count: int, start: date = date(2026, 1, 1)) -> list[dict[str, Any]]:
    """交替涨跌的日线（几乎每根都产生 close_direction 信号）。"""
    rows: list[dict[str, Any]] = []
    day = start
    price = 10.0
    for index in range(count):
        close = price + (0.30 if index % 2 == 0 else -0.20)
        rows.append(
            _bar_row(
                f"{day.isoformat()} 00:00:00",
                price,
                max(price, close) + 0.10,
                min(price, close) - 0.10,
                close,
            )
        )
        price = close
        day += timedelta(days=1)
    return rows


class _SegSpy(Strategy):
    """记录它看到过的 K 线时间戳。"""

    name: ClassVar[str] = "seg_spy"
    seen: ClassVar[list[str]] = []

    def on_bar(self, window: BarWindow, config: Mapping[str, Any]) -> SignalCandidate | None:
        type(self).seen.append(window.current.timestamp)
        return None


class WalkForwardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_test_access_log()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_wf_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.source_db_path = Path(self._tmpdir.name) / "source.db"
        initialize_database(self.source_db_path)
        self.symbol_id = SymbolRepository(self.source_db_path).create_symbol(
            CODE, NAME, MARKET, "stock", is_active=1
        )
        self.bars = daily_bars(120)  # 2026-01-01 .. 2026-04-30
        KlineRepository(self.source_db_path).bulk_upsert_klines(
            [{"symbol_id": self.symbol_id, "timeframe": "1d", **row} for row in self.bars]
        )

    def base_spec(self, **kwargs: Any) -> RunSpec:
        base: dict[str, Any] = {
            "symbol": CODE,
            "timeframe": "1d",
            "strategy_type": "close_direction",
            "quantity": 100.0,
            "costs": FREE_COSTS,
        }
        base.update(kwargs)
        return RunSpec(**base)

    def wf(self, **kwargs: Any) -> WalkForwardSpec:
        base: dict[str, Any] = {
            "base_spec": self.base_spec(),
            "segments": (
                Segment("train", "2026-01-05", "2026-02-28"),
                Segment("validation", "2026-03-01", "2026-04-10"),
                Segment("test", "2026-04-11", "2026-04-30"),
            ),
            "warmup_bars": 3,
            "initial_capital": 1_000_000.0,
            "min_scored_bars": 10,
            "min_trades": 5,
        }
        base.update(kwargs)
        return WalkForwardSpec(**base)

    def run_wf(self, spec: WalkForwardSpec):
        return run_walk_forward(spec, source_db_path=self.source_db_path)


class SplitTest(WalkForwardTestCase):
    def test_three_segments_run_in_order_with_disjoint_ranges(self) -> None:
        report = self.run_wf(self.wf())
        names = [segment.segment.name for segment in report.segments]
        self.assertEqual(names, ["train", "validation", "test"])
        ranges = [
            (segment.report.scored_first_timestamp, segment.report.scored_last_timestamp)
            for segment in report.segments
        ]
        for left, right in zip(ranges, ranges[1:]):
            self.assertLess(left[1], right[0])  # 计分区间严格不重叠
        self.assertEqual(report.evidence_quality, EVIDENCE_OK)
        self.assertIn("train", report.comparison["by_segment"])
        self.assertIn("test", report.comparison["by_segment"])

    def test_overlapping_segments_are_rejected(self) -> None:
        spec = self.wf(
            segments=(
                Segment("train", "2026-01-05", "2026-03-15"),
                Segment("test", "2026-03-01", "2026-04-30"),
            )
        )
        with self.assertRaises(ValueError) as ctx:
            self.run_wf(spec)
        self.assertIn("重叠", str(ctx.exception))

    def test_duplicate_segment_names_are_rejected(self) -> None:
        spec = self.wf(
            segments=(
                Segment("train", "2026-01-05", "2026-02-28"),
                Segment("train", "2026-03-01", "2026-04-30"),
            )
        )
        with self.assertRaises(ValueError) as ctx:
            self.run_wf(spec)
        self.assertIn("重复", str(ctx.exception))


class WarmupAndBoundaryTest(WalkForwardTestCase):
    def test_warmup_bars_not_counted_in_segment_performance(self) -> None:
        report = self.run_wf(self.wf())
        for segment in report.segments:
            self.assertEqual(segment.report.warmup_bars, 3)
            self.assertGreater(segment.report.data.bars, segment.report.scored_bars)
            for trade in segment.report.trades:
                self.assertGreaterEqual(
                    trade["signal_timestamp"], segment.report.scored_first_timestamp
                )
                self.assertLessEqual(
                    trade["signal_timestamp"], segment.report.scored_last_timestamp
                )

    def test_boundary_signal_belongs_to_its_segment(self) -> None:
        report = self.run_wf(self.wf())
        by_name = {segment.segment.name: segment for segment in report.segments}
        for name in ("train", "validation"):
            beyond = [
                trade
                for trade in by_name[name].report.trades
                if trade["fill_beyond_scored_range"]
            ]
            self.assertEqual(len(beyond), 1)
            self.assertEqual(
                beyond[0]["signal_timestamp"], by_name[name].report.scored_last_timestamp
            )
            self.assertGreater(
                beyond[0]["fill_timestamp"], by_name[name].report.scored_last_timestamp
            )
        # 最后一个区间之后没有数据 → 不再有边界成交（如实记为未成交）
        test_beyond = [
            trade for trade in by_name["test"].report.trades if trade["fill_beyond_scored_range"]
        ]
        self.assertEqual(test_beyond, [])

    def test_no_cross_segment_lookahead(self) -> None:
        from strategies.runner import DEFAULT_STRATEGIES

        _SegSpy.seen = []
        # 只跑 test 区间：策略不得看到 test 之前的未来（train/validation 的 K 线）
        spec = self.wf(
            base_spec=self.base_spec(strategy_type="seg_spy"),
            segments=(Segment("test", "2026-04-11", "2026-04-30"),),
        )
        DEFAULT_STRATEGIES["seg_spy"] = _SegSpy
        try:
            report = run_walk_forward(spec, source_db_path=self.source_db_path)
        finally:
            DEFAULT_STRATEGIES.pop("seg_spy", None)
        timestamps = [row["timestamp"] for row in self.bars]
        scored_first = report.segments[0].report.scored_first_timestamp
        first_scored_index = next(i for i, ts in enumerate(timestamps) if ts >= scored_first)
        expected_start = timestamps[first_scored_index - 3]  # 预热 3 根
        self.assertEqual(min(_SegSpy.seen), expected_start)
        self.assertTrue(_SegSpy.seen == sorted(_SegSpy.seen))


class FreezeAndEvidenceTest(WalkForwardTestCase):
    def test_test_segment_freeze_warning_on_param_change(self) -> None:
        first = self.run_wf(self.wf())
        self.assertEqual(first.test_freeze["evaluations"][0]["distinct_param_sets"], 1)
        self.assertFalse(
            first.test_freeze["evaluations"][0]["params_changed_after_first_evaluation"]
        )
        # 相同参数再跑一次：仍是一个参数集，不告警
        second = self.run_wf(self.wf())
        self.assertFalse(
            second.test_freeze["evaluations"][0]["params_changed_after_first_evaluation"]
        )
        # 换参数再跑同一测试区间 → 明确告警
        changed = self.run_wf(self.wf(base_spec=self.base_spec(strategy_config={"min_move": 0.001})))
        self.assertTrue(
            changed.test_freeze["evaluations"][0]["params_changed_after_first_evaluation"]
        )
        self.assertTrue(any("样本外" in warning for warning in changed.warnings))

    def test_insufficient_samples_are_flagged(self) -> None:
        report = self.run_wf(self.wf(min_scored_bars=1000, min_trades=1000))
        self.assertEqual(report.evidence_quality, EVIDENCE_INSUFFICIENT)
        self.assertTrue(any("证据质量" in warning for warning in report.warnings))
        for segment in report.segments:
            self.assertTrue(any("过短" in warning for warning in segment.warnings))

    def test_return_pct_requires_initial_capital(self) -> None:
        without = self.run_wf(self.wf(initial_capital=None))
        for segment in without.segments:
            self.assertIsNone(segment.performance.return_pct)
            self.assertIn("unknown", segment.performance.return_basis)
        with_capital = self.run_wf(self.wf(initial_capital=1_000_000.0))
        for segment in with_capital.segments:
            self.assertIsNotNone(segment.performance.return_pct)
            self.assertAlmostEqual(
                segment.performance.return_pct,
                segment.report.net_pnl / 1_000_000.0,
                places=9,
            )

    def test_plan_fingerprint_is_stable_and_config_sensitive(self) -> None:
        first = self.run_wf(self.wf())
        second = self.run_wf(self.wf())
        self.assertEqual(first.plan_fingerprint, second.plan_fingerprint)
        changed = self.run_wf(self.wf(base_spec=self.base_spec(strategy_config={"min_move": 0.002})))
        self.assertNotEqual(first.plan_fingerprint, changed.plan_fingerprint)

    def test_source_db_is_not_modified(self) -> None:
        before = sorted(p.name for p in Path(self._tmpdir.name).iterdir())
        size_before = self.source_db_path.stat().st_size
        self.run_wf(self.wf())
        self.assertEqual(sorted(p.name for p in Path(self._tmpdir.name).iterdir()), before)
        self.assertEqual(self.source_db_path.stat().st_size, size_before)


if __name__ == "__main__":
    unittest.main()
