"""Stage 032.1 专项测试：全历史 / 多周期行情覆盖。

覆盖（对应 Stage 032.1 验收）：
    §2 五个交易对 × 九个周期 = 45 个组合，且 9 个周期均有 OKX 原生 bar
    §3 周期映射与**严格派生**（不假定所有周期共用同一 bar 格式；不完整分组整组丢弃）
    §4 全历史回补：分页到最早记录、批次提交、断点续传、幂等、冲突先报告不覆盖
    §5 时间完整性：最早/最晚、获取/有效/重复/拒绝/未完结、分页请求数、缺失区间明细、
       覆盖天数、完成状态
    §6 正式数据库未被测试代码修改

全部使用**合成 API 响应** + 临时数据库，不访问网络。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence
from urllib.error import URLError
from urllib.parse import parse_qsl, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import (  # noqa: E402
    KlineRepository,
    SymbolRepository,
    get_database_path,
    initialize_database,
)
from data_source import (  # noqa: E402
    DataSourceError,
    KlineBar,
    OKX_INSTRUMENTS,
    REQUIRED_TIMEFRAMES,
    aggregate_bars,
    backfill_matrix,
    backfill_okx_klines,
    collect_okx_klines,
    timeframe_plan,
)
from data_source import okx as okx_module  # noqa: E402

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
START_MS = 1_735_689_600_000  # 2025-01-01 00:00:00 UTC


def ts_of(index: int, step: int = HOUR_MS) -> str:
    return okx_module.milliseconds_to_timestamp(START_MS + index * step)


def candle(ts_ms: int, o: float, h: float, low: float, c: float,
           vol: float = 2.0, confirm: str = "1") -> list[str]:
    return [str(ts_ms), str(o), str(h), str(low), str(c), str(vol),
            str(c * vol), str(c * vol), confirm]


def series(count: int, *, step: int = HOUR_MS, base: float = 100.0,
           start_ms: int = START_MS, confirm_last: str = "1",
           drop: Sequence[int] = ()) -> list[list[str]]:
    rows = []
    for index in range(count):
        if index in drop:
            continue
        o = base + index
        c = o + 0.5
        rows.append(candle(start_ms + index * step, o, c + 1.0, o - 1.0, c))
    if confirm_last != "1" and rows:
        rows[-1][8] = confirm_last
    return rows


class FakeOkxServer:
    def __init__(self, candles: Sequence[Sequence[str]], *,
                 raise_on_calls: Sequence[int] = ()) -> None:
        self.candles = sorted(candles, key=lambda row: int(row[0]))
        self.raise_on_calls = set(raise_on_calls)
        self.calls = 0
        self.requested: list[dict[str, str]] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> bytes:
        self.calls += 1
        query = dict(parse_qsl(urlsplit(url).query))
        self.requested.append(query)
        if self.calls in self.raise_on_calls:
            raise URLError("network down")
        after, limit = int(query["after"]), int(query["limit"])
        older = [row for row in self.candles if int(row[0]) < after]
        older.sort(key=lambda row: -int(row[0]))
        return json.dumps({"code": "0", "msg": "", "data": older[:limit]}).encode("utf-8")


def make_source(candles: Sequence[Sequence[str]], *, page_limit: int = 100,
                max_pages: int = 200, max_retries: int = 0,
                raise_on_calls: Sequence[int] = ()):
    return okx_module.OkxKlineSource(
        opener=FakeOkxServer(candles, raise_on_calls=raise_on_calls),
        sleeper=lambda _seconds: None,
        min_request_interval=0.0,
        page_limit=page_limit,
        max_pages=max_pages,
        max_retries=max_retries,
    )


class BackfillTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_okx_bf_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "bf.db"
        initialize_database(self.db_path)

    def backfill(self, candles, *, symbol="BTC-USDT", timeframe="1h",
                 source_kwargs: dict[str, Any] | None = None, **kwargs):
        source = kwargs.pop("source", None) or make_source(candles, **(source_kwargs or {}))
        return backfill_okx_klines(symbol=symbol, timeframe=timeframe,
                                   db_path=self.db_path, source=source, **kwargs)


# --------------------------------------------------------------------------- §2 映射
class TimeframeMatrixTest(BackfillTestCase):
    def test_nine_required_timeframes_and_bars(self) -> None:
        self.assertEqual(
            list(REQUIRED_TIMEFRAMES),
            ["5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"],
        )
        expected = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H",
                    "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D"}
        for timeframe, bar in expected.items():
            plan = timeframe_plan(timeframe)
            self.assertEqual(plan.mode, "native", timeframe)
            self.assertEqual(plan.bar, bar, timeframe)
            self.assertIsNone(plan.derived_from)

    def test_unknown_timeframe_is_unsupported_not_assumed(self) -> None:
        self.assertEqual(timeframe_plan("7m").mode, "unsupported")
        with self.assertRaises(DataSourceError):
            self.backfill(series(3), timeframe="7m")

    def test_matrix_is_5_pairs_by_9_timeframes(self) -> None:
        combos = [(code, tf) for code in OKX_INSTRUMENTS for tf in REQUIRED_TIMEFRAMES]
        self.assertEqual(len(combos), 45)
        self.assertEqual(sorted(OKX_INSTRUMENTS),
                         ["BTC-USDT", "DOGE-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"])


# --------------------------------------------------------------------------- §3 派生
class AggregationTest(BackfillTestCase):
    @staticmethod
    def _hourly(count: int, *, start_index: int = 0) -> list[KlineBar]:
        bars = []
        for index in range(count):
            o = 100.0 + index
            bars.append(KlineBar("BTC-USDT", "1h", ts_of(start_index + index),
                                 o, o + 2.0, o - 1.0, o + 1.0, 3.0, 300.0))
        return bars

    def test_aggregate_4h_from_1h_is_exact(self) -> None:
        aggregated, skipped = aggregate_bars(self._hourly(4), "1h", "4h")
        self.assertEqual(skipped, 0)
        self.assertEqual(len(aggregated), 1)
        bar = aggregated[0]
        self.assertEqual(bar.timeframe, "4h")
        self.assertEqual(bar.timestamp, ts_of(0))
        self.assertEqual(bar.open, 100.0)    # 首根
        self.assertEqual(bar.close, 104.0)   # 末根
        self.assertEqual(bar.high, 105.0)    # max
        self.assertEqual(bar.low, 99.0)      # min
        self.assertEqual(bar.volume, 12.0)   # 求和
        self.assertEqual(bar.amount, 1200.0)

    def test_incomplete_group_is_dropped_not_partial(self) -> None:
        aggregated, skipped = aggregate_bars(self._hourly(3), "1h", "4h")
        self.assertEqual(aggregated, [])
        self.assertEqual(skipped, 1)

    def test_unaligned_bars_do_not_produce_bars(self) -> None:
        # 01:00 起：跨两个 4h 分组，都不完整 → 不生成任何派生 K 线
        aggregated, skipped = aggregate_bars(self._hourly(4, start_index=1), "1h", "4h")
        self.assertEqual(aggregated, [])
        self.assertEqual(skipped, 2)

    def test_aggregate_rejects_bad_ratio(self) -> None:
        with self.assertRaises(DataSourceError):
            aggregate_bars(self._hourly(4), "4h", "1h")
        with self.assertRaises(DataSourceError):
            aggregate_bars(self._hourly(4), "1h", "1h")

    def test_derived_backfill_records_provenance(self) -> None:
        okx_module.DERIVED_TIMEFRAME_BASE["3h"] = "1h"
        try:
            report = self.backfill(series(6), timeframe="3h", start_timestamp=ts_of(0),
                                   end_timestamp=ts_of(5))
        finally:
            del okx_module.DERIVED_TIMEFRAME_BASE["3h"]
        self.assertEqual(report.mode, "derived")
        self.assertEqual(report.derived_from, "1h")
        self.assertEqual(report.fetch_timeframe, "1h")
        self.assertEqual(report.total_in_db, 2)      # 6 根 1h → 2 个完整 3h 分组
        self.assertEqual(report.skipped_groups, 0)
        self.assertTrue(report.complete)
        self.assertIn("derived", report.stop_reason or "")
        rows = KlineRepository(self.db_path).list_klines(report.symbol_id, "3h")
        self.assertEqual([row["timestamp"] for row in rows], [ts_of(0), ts_of(3)])

    def test_derived_drops_incomplete_group_and_reports(self) -> None:
        okx_module.DERIVED_TIMEFRAME_BASE["3h"] = "1h"
        try:
            report = self.backfill(series(4), timeframe="3h", start_timestamp=ts_of(0),
                                   end_timestamp=ts_of(3))
        finally:
            del okx_module.DERIVED_TIMEFRAME_BASE["3h"]
        self.assertEqual(report.total_in_db, 1)
        self.assertEqual(report.skipped_groups, 1)
        self.assertFalse(report.complete)
        self.assertTrue(any("整组丢弃" in w for w in report.warnings))


# --------------------------------------------------------------------------- §4/§5 全历史
class FullHistoryTest(BackfillTestCase):
    def test_pages_until_no_more_data(self) -> None:
        report = self.backfill(series(250), source_kwargs={"page_limit": 100})
        self.assertEqual(report.total_in_db, 250)
        self.assertEqual(report.pages, 3)
        self.assertEqual(report.requests, 3)
        self.assertEqual(report.stop_reason, "no_more_data")
        self.assertTrue(report.complete)
        self.assertEqual(report.earliest, ts_of(0))
        self.assertEqual(report.latest, ts_of(249))
        self.assertAlmostEqual(report.coverage_days, 249 / 24, places=4)
        self.assertEqual(report.fetched, 250)
        self.assertEqual(report.valid, 250)
        self.assertEqual(report.rejected, 0)
        self.assertEqual(report.duplicates, 0)
        self.assertIsNone(report.resumed_from)

    def test_max_pages_marks_incomplete(self) -> None:
        report = self.backfill(series(250), source_kwargs={"page_limit": 100, "max_pages": 1})
        self.assertEqual(report.pages, 1)
        self.assertEqual(report.stop_reason, "max_pages")
        self.assertFalse(report.complete)
        self.assertTrue(any("回补未完成" in w for w in report.warnings))

    def test_unfinished_candle_is_filtered(self) -> None:
        report = self.backfill(series(6, confirm_last="0"))
        self.assertEqual(report.fetched, 6)
        self.assertEqual(report.unfinished, 1)
        self.assertEqual(report.total_in_db, 5)

    def test_missing_ranges_are_reported_with_lengths(self) -> None:
        report = self.backfill(series(24, drop=(5, 6, 7)))
        self.assertEqual(report.missing_intervals, 3)
        self.assertEqual(len(report.gap_ranges), 1)
        start, end, count = report.gap_ranges[0]
        self.assertEqual(count, 3)
        self.assertEqual(start, ts_of(5))
        self.assertEqual(end, ts_of(7))
        self.assertEqual(report.total_in_db, 21)
        self.assertTrue(any("缺失" in w for w in report.warnings))

    def test_rerun_after_complete_backfill_is_noop(self) -> None:
        first = self.backfill(series(24))
        self.assertEqual(first.total_in_db, 24)
        second = self.backfill(series(24))
        self.assertEqual(second.resumed_from, ts_of(0))
        self.assertEqual(second.new_records, 0)
        self.assertEqual(second.total_in_db, 24)
        self.assertTrue(second.complete)

    def test_incremental_after_backfill_is_idempotent(self) -> None:
        self.backfill(series(24))
        again = collect_okx_klines(
            symbol="BTC-USDT", timeframe="1h", db_path=self.db_path,
            source=make_source(series(24)),
            start_timestamp=ts_of(0), end_timestamp=ts_of(23),
        )
        self.assertEqual(again.new_records, 0)
        self.assertEqual(again.total_in_db, 24)

    def test_resume_after_failure_keeps_progress_without_redownload(self) -> None:
        failing = make_source(series(250), page_limit=100, raise_on_calls=(3,))
        with self.assertRaises(DataSourceError):
            backfill_okx_klines(symbol="BTC-USDT", timeframe="1h",
                                db_path=self.db_path, source=failing)
        # 已成功的前两页必须保留（批次提交，不从头再来）
        self.assertEqual(KlineRepository(self.db_path).count_klines(1, "1h"), 200)

        second = self.backfill(series(250), source_kwargs={"page_limit": 100})
        self.assertEqual(second.resumed_from, ts_of(50))
        self.assertEqual(second.total_in_db, 250)
        self.assertEqual(second.new_records, 50)
        self.assertLess(second.fetched, 60)   # 只补更早的部分
        self.assertTrue(second.complete)


# --------------------------------------------------------------------------- §4 冲突
class ConflictTest(BackfillTestCase):
    def _seed(self, close: float) -> None:
        symbol_id = SymbolRepository(self.db_path).create_symbol(
            "BTC-USDT", "Bitcoin / TetherUS", "CRYPTO", "SPOT"
        )
        KlineRepository(self.db_path).bulk_upsert_klines([
            {"symbol_id": symbol_id, "timeframe": "1h", "timestamp": ts_of(0),
             "open": 200.0, "high": 205.0, "low": 199.0, "close": close,
             "volume": 1.0, "amount": None},
        ])

    def test_conflict_is_reported_and_not_overwritten(self) -> None:
        self._seed(204.0)
        report = self.backfill(series(3), start_timestamp=ts_of(0), end_timestamp=ts_of(2))
        self.assertEqual(report.conflicts, 1)
        self.assertEqual(report.conflict_samples[0]["timestamp"], ts_of(0))
        self.assertEqual(report.conflict_samples[0]["existing"]["close"], 204.0)
        self.assertEqual(report.conflict_samples[0]["incoming"]["close"], 100.5)
        self.assertTrue(any("不覆盖" in w for w in report.warnings))
        rows = KlineRepository(self.db_path).list_klines(report.symbol_id, "1h")
        self.assertEqual(len(rows), 3)
        self.assertEqual(float(rows[0]["close"]), 204.0)   # 既有有效数据未被覆盖

    def test_overwrite_policy_updates_conflicting_row(self) -> None:
        self._seed(204.0)
        report = self.backfill(series(3), start_timestamp=ts_of(0), end_timestamp=ts_of(2),
                               conflict_policy="overwrite")
        self.assertEqual(report.conflicts, 1)
        rows = KlineRepository(self.db_path).list_klines(report.symbol_id, "1h")
        self.assertEqual(float(rows[0]["close"]), 100.5)

    def test_unknown_conflict_policy_is_rejected(self) -> None:
        with self.assertRaises(DataSourceError):
            self.backfill(series(3), conflict_policy="magic")


# --------------------------------------------------------------------------- §6 矩阵
class MatrixTest(BackfillTestCase):
    def test_matrix_runs_combinations_and_continues_on_error(self) -> None:
        report = backfill_matrix(
            symbols=["BTC-USDT", "FOO-USDT"],
            timeframes=["1h", "1d"],
            db_path=self.db_path,
            source_factory=lambda: make_source(series(3)),
            now_timestamp="2025-01-02 00:00:00",
        )
        self.assertEqual(report.total, 4)
        self.assertEqual(report.ok, 2)
        self.assertEqual(report.failed, 2)
        offender = [combo for combo in report.combos if not combo.ok]
        self.assertTrue(all("未登记" in (combo.error or "") for combo in offender))

    def test_matrix_defaults_cover_45_combos(self) -> None:
        report = backfill_matrix(
            db_path=self.db_path,
            source_factory=lambda: make_source([]),   # 空响应：快速跑完 45 个组合
            now_timestamp="2025-01-02 00:00:00",
        )
        self.assertEqual(report.total, 45)
        self.assertEqual(report.ok, 45)
        self.assertEqual(report.failed, 0)
        self.assertEqual({combo.symbol for combo in report.combos}, set(OKX_INSTRUMENTS))
        self.assertEqual({combo.timeframe for combo in report.combos}, set(REQUIRED_TIMEFRAMES))

    def test_matrix_report_is_serializable(self) -> None:
        report = backfill_matrix(
            symbols=["BTC-USDT"], timeframes=["1h"], db_path=self.db_path,
            source_factory=lambda: make_source(series(5)),
            now_timestamp="2025-01-02 00:00:00",
        )
        payload = json.loads(json.dumps(report.to_dict(), ensure_ascii=False))
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["combos"][0]["report"]["total_in_db"], 5)
        self.assertEqual(payload["combos"][0]["report"]["mode"], "native")


# --------------------------------------------------------------------------- 安全
class FormalDbSafetyTest(BackfillTestCase):
    def test_formal_database_is_not_touched(self) -> None:
        formal = get_database_path()
        before = (formal.stat().st_size, formal.stat().st_mtime_ns) if formal.exists() else None
        self.backfill(series(6))
        after = (formal.stat().st_size, formal.stat().st_mtime_ns) if formal.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
