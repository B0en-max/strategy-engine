"""Stage 027′ 后续 Step 1：交易日历缓存模块测试（market.trading_calendar）。

全部使用临时缓存文件与注入的假数据源，**不打真实网络、不碰正式数据库**。
关键不变式：
    - 超出覆盖范围 → out_of_range，**不得**返回 False 冒充"非交易日"
    - sessions_since 与 limits.resolve_prev_close 的"跨缺失交易日"判定一致
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_source.base import DataSourceError  # noqa: E402
from market.rules import STATUS_OK, STATUS_UNKNOWN, resolve_prev_close  # noqa: E402
from market.trading_calendar import (  # noqa: E402
    CAL_NON_TRADING,
    CAL_OUT_OF_RANGE,
    CAL_TRADING,
    CALENDAR_SCHEMA_VERSION,
    DEFAULT_CACHE_PATH,
    DEFAULT_INDEX_SYMBOL,
    TradingCalendar,
    calendar_from_bars,
    calendar_from_dates,
    fetch_calendar,
    load_cache,
    normalize_date,
    refresh_cache,
)

# 合成日历：09-28(一) 09-29 09-30 | 国庆休市 10-01~10-08 | 10-09(五) 10-12 10-13
DAYS = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-09", "2026-10-12", "2026-10-13")


class FakeBar:
    def __init__(self, timestamp: str) -> None:
        self.timestamp = timestamp


class FakeKlineSource:
    """假数据源：只实现 fetch_klines / name，测试不打网络。"""

    name = "fake"

    def __init__(self, bars: list[FakeBar] | None = None) -> None:
        self._bars = bars if bars is not None else [FakeBar(f"{d} 00:00:00") for d in DAYS]
        self.calls: list[tuple[str, str]] = []

    def fetch_klines(self, *, symbol: str, timeframe: str, start_timestamp=None, end_timestamp=None):
        self.calls.append((symbol, timeframe))
        return list(self._bars)


class NormalizeDateTest(unittest.TestCase):
    def test_variants(self) -> None:
        self.assertEqual(normalize_date("2026-10-09"), "2026-10-09")
        self.assertEqual(normalize_date("2026-10-09 00:00:00"), "2026-10-09")
        self.assertEqual(normalize_date("2026-10-09T14:30:00"), "2026-10-09")

    def test_invalid_raises(self) -> None:
        for bad in ("", "2026-13-01", "not-a-date", "2026/10/09"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_date(bad)

    def test_single_digit_is_zero_padded(self) -> None:
        # 非补零输入必须规范化，否则排序/比较会不一致
        self.assertEqual(normalize_date("2026-10-9"), "2026-10-09")
        self.assertEqual(normalize_date("2026-1-9"), "2026-01-09")


class CalendarQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cal = calendar_from_dates(DAYS, source="test")

    def test_sorted_unique_and_bounds(self) -> None:
        cal = calendar_from_dates(["2026-09-30", "2026-09-28", "2026-09-30"])
        self.assertEqual(cal.days, ("2026-09-28", "2026-09-30"))
        self.assertEqual(len(cal), 2)
        self.assertEqual(self.cal.earliest, "2026-09-28")
        self.assertEqual(self.cal.latest, "2026-10-13")

    def test_duplicate_and_out_of_order_combined(self) -> None:
        cal = calendar_from_dates(["2026-10-09", "2026-09-28", "2026-10-09", "2026-9-30", "2026-09-28"])
        self.assertEqual(cal.days, ("2026-09-28", "2026-09-30", "2026-10-09"))

    def test_invalid_date_in_construction_raises(self) -> None:
        with self.assertRaises(ValueError):
            calendar_from_dates(["2026-13-40"])

    def test_classify_distinguishes_holiday_from_out_of_range(self) -> None:
        self.assertEqual(self.cal.classify("2026-09-29"), CAL_TRADING)
        self.assertEqual(self.cal.classify("2026-10-01"), CAL_NON_TRADING)  # 国庆假期，仍在覆盖范围内
        self.assertEqual(self.cal.classify("2026-09-27"), CAL_OUT_OF_RANGE)  # 早于覆盖起点
        self.assertEqual(self.cal.classify("2026-10-14"), CAL_OUT_OF_RANGE)  # 晚于覆盖终点
        # 关键：超出范围不能冒充非交易日
        self.assertFalse(self.cal.is_trading_day("2026-10-14"))
        self.assertNotEqual(self.cal.classify("2026-10-14"), CAL_NON_TRADING)

    def test_empty_calendar_is_out_of_range(self) -> None:
        empty = calendar_from_dates([])
        self.assertEqual(empty.classify("2026-10-09"), CAL_OUT_OF_RANGE)
        self.assertIsNone(empty.earliest)
        self.assertIsNone(empty.next_trading_day("2026-10-09"))
        self.assertIsNone(empty.prev_trading_day("2026-10-09"))

    def test_index_of(self) -> None:
        self.assertEqual(self.cal.index_of("2026-09-28"), 0)
        self.assertEqual(self.cal.index_of("2026-10-09"), 3)
        self.assertIsNone(self.cal.index_of("2026-10-01"))

    def test_next_and_prev_trading_day(self) -> None:
        # 跨国庆：09-30 的下一个交易日是 10-09（不是 10-01）
        self.assertEqual(self.cal.next_trading_day("2026-09-30"), "2026-10-09")
        self.assertEqual(self.cal.prev_trading_day("2026-10-09"), "2026-09-30")
        self.assertEqual(self.cal.next_trading_day("2026-09-30", inclusive=True), "2026-09-30")
        self.assertEqual(self.cal.prev_trading_day("2026-10-09", inclusive=True), "2026-10-09")
        self.assertEqual(self.cal.next_trading_day("2026-10-01"), "2026-10-09")  # 假日 → 下一个交易日
        self.assertIsNone(self.cal.next_trading_day("2026-10-13"))
        self.assertIsNone(self.cal.prev_trading_day("2026-09-28"))

    def test_ranges_and_sessions(self) -> None:
        self.assertEqual(self.cal.trading_days("2026-09-28", "2026-09-30"), DAYS[:3])
        self.assertEqual(self.cal.sessions_between("2026-09-28", "2026-09-30"), 3)
        self.assertEqual(self.cal.sessions_between("2026-09-30", "2026-09-28"), 0)
        self.assertEqual(self.cal.sessions_between("2026-09-30", "2026-10-09"), 2)

    def test_sessions_since(self) -> None:
        self.assertEqual(self.cal.sessions_since("2026-09-28", "2026-09-29"), 1)  # 相邻
        self.assertEqual(self.cal.sessions_since("2026-09-28", "2026-09-30"), 2)  # 中间隔 1 天
        self.assertEqual(self.cal.sessions_since("2026-09-30", "2026-10-09"), 1)  # 跨假期仍相邻
        self.assertIsNone(self.cal.sessions_since("2026-10-01", "2026-10-09"))  # 起点非交易日
        self.assertIsNone(self.cal.sessions_since("2026-09-28", "2026-10-20"))  # 终点超出范围

    def test_missing_from(self) -> None:
        self.assertEqual(self.cal.missing_from(["2026-09-29", "2026-10-01", "2026-10-20"]), ("2026-10-01",))


class CacheRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cache" / "trading_calendar.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_load_round_trip(self) -> None:
        cal = calendar_from_dates(DAYS, source="test", generated_at="2026-10-10 00:00:00")
        saved = cal.save(self.path)
        self.assertEqual(saved, self.path)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())  # 原子替换后无 .tmp 残留
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], CALENDAR_SCHEMA_VERSION)
        self.assertEqual(payload["count"], len(DAYS))
        loaded = load_cache(self.path)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.days, cal.days)
        self.assertEqual(loaded.index_symbol, DEFAULT_INDEX_SYMBOL)
        self.assertEqual(loaded.source, "test")

    def test_load_cache_missing_returns_none(self) -> None:
        self.assertIsNone(load_cache(Path(self._tmp.name) / "nope.json"))

    def test_corrupt_non_json_cache_raises(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("<html>这不是 JSON</html>", encoding="utf-8")
        with self.assertRaises(ValueError):
            TradingCalendar.load(self.path)

    def test_invalid_date_in_cache_raises(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"days": ["2026-13-40"]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            TradingCalendar.load(self.path)

    def test_writes_never_touch_default_cache_path(self) -> None:
        before = (DEFAULT_CACHE_PATH.exists(),
                  DEFAULT_CACHE_PATH.stat().st_mtime if DEFAULT_CACHE_PATH.exists() else None)
        calendar_from_dates(DAYS, source="test").save(self.path)
        refresh_src = FakeKlineSource()
        refresh_cache(self.path, source=refresh_src)
        after = (DEFAULT_CACHE_PATH.exists(),
                 DEFAULT_CACHE_PATH.stat().st_mtime if DEFAULT_CACHE_PATH.exists() else None)
        self.assertEqual(before, after)  # 测试绝不写项目默认缓存
        self.assertNotEqual(os.fspath(self.path), os.fspath(DEFAULT_CACHE_PATH))

    def test_load_rejects_bad_payload(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema_version": 999, "days": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            TradingCalendar.load(self.path)
        self.path.write_text(json.dumps({"no_days": True}), encoding="utf-8")
        with self.assertRaises(ValueError):
            TradingCalendar.load(self.path)


class FetchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "trading_calendar.json"
        self.source = FakeKlineSource()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_calendar_from_bars(self) -> None:
        cal = calendar_from_bars(self.source.fetch_klines(symbol="sh000001", timeframe="1d"), source="fake")
        self.assertEqual(cal.days, tuple(sorted(DAYS)))
        self.assertEqual(cal.source, "fake")

    def test_fetch_calendar_uses_one_request(self) -> None:
        cal = fetch_calendar(source=self.source, index_symbol="sh000001")
        self.assertEqual(self.source.calls, [("sh000001", "1d")])  # 只 1 个请求
        self.assertEqual(len(cal), len(DAYS))
        self.assertEqual(cal.index_symbol, "sh000001")
        self.assertIsNotNone(cal.generated_at)

    def test_fetch_calendar_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            fetch_calendar(source=FakeKlineSource(bars=[]))

    def test_source_exception_propagates(self) -> None:
        class Boom:
            name = "boom"

            def fetch_klines(self, **kwargs):
                raise DataSourceError("网络不可用")

        with self.assertRaises(DataSourceError):
            fetch_calendar(source=Boom())

    def test_refresh_cache_writes_file_only(self) -> None:
        cal = refresh_cache(self.path, source=self.source)
        self.assertTrue(self.path.exists())
        self.assertEqual(load_cache(self.path).days, cal.days)  # type: ignore[union-attr]


class IntegrationWithLimitsTest(unittest.TestCase):
    """日历 sessions_since 与 limits.resolve_prev_close 的衔接（跨缺失交易日 → unknown）。"""

    def setUp(self) -> None:
        self.cal = calendar_from_dates(DAYS, source="test")

    def test_adjacent_sessions_allow_prev_close(self) -> None:
        gap = self.cal.sessions_since("2026-09-29", "2026-09-30")
        self.assertEqual(gap, 1)
        self.assertEqual(resolve_prev_close("10.00", sessions_since_prev_bar=gap).status, STATUS_OK)

    def test_missing_middle_session_forces_unknown(self) -> None:
        # 09-28 与 09-30 之间缺 09-29
        gap = self.cal.sessions_since("2026-09-28", "2026-09-30")
        self.assertEqual(gap, 2)
        result = resolve_prev_close("10.00", sessions_since_prev_bar=gap)
        self.assertEqual(result.status, STATUS_UNKNOWN)

    def test_out_of_range_gap_is_none(self) -> None:
        self.assertIsNone(self.cal.sessions_since("2025-01-01", "2026-09-30"))


class NoDatabaseDependencyTest(unittest.TestCase):
    def test_module_does_not_import_database(self) -> None:
        source = (PROJECT_ROOT / "market" / "trading_calendar.py").read_text(encoding="utf-8")
        self.assertNotIn("import database", source)
        self.assertNotIn("from database", source)


if __name__ == "__main__":
    unittest.main()
