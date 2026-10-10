"""Stage 032 专项测试：OKX 加密资产历史 K 线采集。

覆盖（对应 Stage 032 验收 1–12）：
    1  OHLCV 与毫秒时间戳解析正确
    2  多页历史数据完整拼接
    3  时间排序与重复记录处理正确
    4  增量更新可重复执行（幂等）
    5  中途失败后可重新运行恢复
    6  限流 / 超时有限重试，绝不无限重试
    7  未完结 K 线不会被当作完整历史入库
    8  非法行情不会静默入库
    9  缺失 K 线能被发现，但不会被伪造补齐
    10 五个交易对与两个周期映射正确
    11 数据源与计量单位没有混淆
    12 正式数据库未被测试代码修改

全部使用**合成 API 响应** + 临时数据库，测试不访问网络。
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
    KlineDataSource,
    OkxKlineSource,
    analyze_klines,
    collect_okx_klines,
    deduplicate_rows,
    get_instrument,
)
from data_source.okx import (  # noqa: E402
    CANDLE_FIELDS,
    OKX_BAR_BY_TIMEFRAME,
    OKX_INSTRUMENTS,
    milliseconds_to_timestamp,
    parse_okx_candle,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
START_MS = 1_735_689_600_000  # 2025-01-01 00:00:00 UTC


def candle(
    ts_ms: int,
    o: float,
    h: float,
    low: float,
    c: float,
    vol: float = 2.0,
    vol_ccy: float = 0.0,
    vol_ccy_quote: float = 0.0,
    confirm: str = "1",
) -> list[str]:
    """构造一条 OKX 蜡烛（数字一律用字符串，逼真）。"""
    vcc = c * vol if vol_ccy == 0.0 else vol_ccy
    vcq = c * vol if vol_ccy_quote == 0.0 else vol_ccy_quote
    return [str(ts_ms), str(o), str(h), str(low), str(c), str(vol), str(vcc), str(vcq), confirm]


def series(
    count: int, *, start_ms: int = START_MS, step: int = HOUR_MS, base: float = 100.0,
    confirm_last: str = "1",
) -> list[list[str]]:
    rows: list[list[str]] = []
    for index in range(count):
        o = base + index
        c = o + 0.5
        rows.append(candle(start_ms + index * step, o, c + 1.0, o - 1.0, c, confirm="1"))
    if confirm_last != "1" and rows:
        rows[-1][8] = confirm_last  # type: ignore[index]
    return rows


class FakeOkxServer:
    """把「OKX 响应」还原成可注入的 opener（不访问网络）。"""

    def __init__(
        self,
        candles: Sequence[Sequence[str]],
        *,
        error_code: str | None = None,
        raise_on_calls: Sequence[int] = (),
    ) -> None:
        self.candles = sorted(candles, key=lambda row: int(row[0]))
        self.error_code = error_code
        self.raise_on_calls = set(raise_on_calls)
        self.calls = 0
        self.requested: list[dict[str, str]] = []

    def __call__(self, url: str, headers: Any, timeout: float) -> bytes:
        self.calls += 1
        query = dict(parse_qsl(urlsplit(url).query))
        self.requested.append(query)
        if self.calls in self.raise_on_calls:
            raise URLError("network down")
        if self.error_code is not None:
            return json.dumps(
                {"code": self.error_code, "msg": "Too Many Requests", "data": []}
            ).encode("utf-8")
        after = int(query["after"])
        limit = int(query["limit"])
        older = [row for row in self.candles if int(row[0]) < after]
        older.sort(key=lambda row: -int(row[0]))  # OKX 返回从新到旧
        return json.dumps({"code": "0", "msg": "", "data": older[:limit]}).encode("utf-8")


class StaticSource(KlineDataSource):
    """最小假数据源：直接返回给定 K 线（用于测试采集器的去重路径）。"""

    def __init__(self, bars: Sequence[KlineBar]) -> None:
        self.bars = list(bars)

    def fetch_klines(self, *, symbol, timeframe, start_timestamp=None, end_timestamp=None):
        return list(self.bars)


class OkxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_okx_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "okx.db"
        initialize_database(self.db_path)

    def source(self, server: FakeOkxServer, **kwargs: Any) -> OkxKlineSource:
        kwargs.setdefault("sleeper", lambda _seconds: None)  # 不真的等待
        kwargs.setdefault("min_request_interval", 0.0)
        return OkxKlineSource(opener=server, **kwargs)


# --------------------------------------------------------------------------- 1) 解析
class ParseTest(OkxTestCase):
    def test_timestamp_and_ohlcv_parsing(self) -> None:
        instrument = get_instrument("BTC-USDT")
        bar, confirmed = parse_okx_candle(
            instrument, "1h", candle(START_MS, 100.0, 105.0, 99.0, 104.0, vol=2.5)
        )
        self.assertEqual(bar.timestamp, "2025-01-01 00:00:00")
        self.assertEqual(bar.symbol, "BTC-USDT")
        self.assertEqual(bar.timeframe, "1h")
        self.assertEqual((bar.open, bar.high, bar.low, bar.close), (100.0, 105.0, 99.0, 104.0))
        self.assertTrue(confirmed)
        self.assertEqual(len(CANDLE_FIELDS), 9)

    def test_volume_units_are_not_confused(self) -> None:
        instrument = get_instrument("BTC-USDT")
        # 基础币 vol=2、计价币 volCcy=100000、计价币成交额 volCcyQuote=175000
        bar, _ = parse_okx_candle(
            instrument, "1h",
            candle(START_MS, 85000.0, 88000.0, 84000.0, 87500.0, vol=2.0, vol_ccy=100000.0,
                   vol_ccy_quote=175000.0),
        )
        self.assertEqual(bar.volume, 2.0)          # volume ← vol（基础币）
        self.assertEqual(bar.amount, 175000.0)     # amount ← volCcyQuote（计价币成交额）
        self.assertNotEqual(bar.volume, bar.amount)
        self.assertNotEqual(bar.amount, 100000.0)  # 不是 volCcy

    def test_malformed_candles_raise(self) -> None:
        instrument = get_instrument("BTC-USDT")
        with self.assertRaises(DataSourceError):
            parse_okx_candle(instrument, "1h", ["1", "2", "3"])  # 字段不足
        with self.assertRaises(DataSourceError):
            parse_okx_candle(instrument, "1h", candle(START_MS, 1, 2, 3, 4, confirm="2"))
        broken = candle(START_MS, 1.0, 2.0, 0.5, 1.5)
        broken[4] = "not-a-number"
        with self.assertRaises(DataSourceError):
            parse_okx_candle(instrument, "1h", broken)
        with self.assertRaises(DataSourceError):
            parse_okx_candle(instrument, "1h", "not-a-list")


# --------------------------------------------------------------------------- 10/11) 映射
class MappingTest(OkxTestCase):
    def test_five_pairs_and_two_timeframes(self) -> None:
        self.assertEqual(
            sorted(OKX_INSTRUMENTS),
            ["BTC-USDT", "DOGE-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT"],
        )
        self.assertEqual(OKX_BAR_BY_TIMEFRAME["1h"], "1H")
        self.assertEqual(OKX_BAR_BY_TIMEFRAME["1d"], "1D")
        for code, inst in OKX_INSTRUMENTS.items():
            self.assertEqual(inst.project_symbol, code)
            self.assertEqual(inst.market, "CRYPTO")
            self.assertEqual(inst.asset_type, "SPOT")
            self.assertEqual(inst.quote_ccy, "USDT")

    def test_unknown_pair_is_rejected_not_guessed(self) -> None:
        with self.assertRaises(DataSourceError) as ctx:
            get_instrument("FOO-USDT")
        self.assertIn("未登记", str(ctx.exception))

    def test_request_parameters_are_explicit(self) -> None:
        server = FakeOkxServer(series(5))
        source = self.source(server)
        source.fetch_klines(symbol="BTC-USDT", timeframe="1d",
                            start_timestamp="2025-01-01 00:00:00",
                            end_timestamp="2025-01-01 04:00:00")
        self.assertEqual(server.requested[0]["instId"], "BTC-USDT")
        self.assertEqual(server.requested[0]["bar"], "1D")
        self.assertEqual(server.requested[0]["limit"], "100")
        self.assertIn("after", server.requested[0])


# --------------------------------------------------------------------------- 2/3) 分页与排序
class PaginationTest(OkxTestCase):
    def test_multi_page_history_is_concatenated(self) -> None:
        server = FakeOkxServer(series(250))
        source = self.source(server, page_limit=100)
        result = source.fetch_candles(
            get_instrument("BTC-USDT"), "1h",
            start_timestamp="2024-12-31 00:00:00",
            end_timestamp=milliseconds_to_timestamp(START_MS + 249 * HOUR_MS),
        )
        self.assertEqual(len(result.bars), 250)
        self.assertEqual(result.stats.pages, 3)
        timestamps = [bar.timestamp for bar in result.bars]
        self.assertEqual(timestamps, sorted(timestamps))          # 升序
        self.assertEqual(len(set(timestamps)), len(timestamps))   # 无重复

    def test_boundary_end_is_inclusive_and_start_may_overshoot(self) -> None:
        server = FakeOkxServer(series(250))
        source = self.source(server, page_limit=100)
        result = source.fetch_candles(
            get_instrument("BTC-USDT"), "1h",
            start_timestamp=milliseconds_to_timestamp(START_MS + 100 * HOUR_MS),
            end_timestamp=milliseconds_to_timestamp(START_MS + 149 * HOUR_MS),
        )
        # 结束边界包含（after = end+1）；分页可能多取到 start 之前，由采集器本地裁切
        self.assertEqual(result.bars[-1].timestamp,
                         milliseconds_to_timestamp(START_MS + 149 * HOUR_MS))
        self.assertLessEqual(len(result.bars), 100)

    def test_collector_clips_to_requested_range(self) -> None:
        report = collect_okx_klines(
            symbol="BTC-USDT", timeframe="1h", db_path=self.db_path,
            source=self.source(FakeOkxServer(series(250)), page_limit=100),
            start_timestamp=milliseconds_to_timestamp(START_MS + 100 * HOUR_MS),
            end_timestamp=milliseconds_to_timestamp(START_MS + 149 * HOUR_MS),
        )
        self.assertEqual(report.total_in_db, 50)
        self.assertEqual(report.actual_start,
                         milliseconds_to_timestamp(START_MS + 100 * HOUR_MS))
        self.assertEqual(report.actual_end,
                         milliseconds_to_timestamp(START_MS + 149 * HOUR_MS))

    def test_empty_response_is_reported(self) -> None:
        server = FakeOkxServer([])
        result = self.source(server).fetch_candles(get_instrument("BTC-USDT"), "1h")
        self.assertEqual(result.bars, ())
        self.assertEqual(result.stats.empty_responses, 1)


# --------------------------------------------------------------------------- 6) 限流 / 重试
class RetryTest(OkxTestCase):
    def test_rate_limit_retries_are_bounded(self) -> None:
        server = FakeOkxServer(series(5), error_code="50011")
        source = self.source(server, max_retries=2)
        with self.assertRaises(DataSourceError) as ctx:
            source.fetch_klines(symbol="BTC-USDT", timeframe="1h")
        self.assertIn("50011", str(ctx.exception))
        self.assertEqual(server.calls, 3)  # 1 次 + 2 次重试，绝不无限重试

    def test_network_errors_are_retried_but_bounded(self) -> None:
        server = FakeOkxServer(series(5), raise_on_calls=(1, 2, 3, 4))
        source = self.source(server, max_retries=2)
        with self.assertRaises(DataSourceError) as ctx:
            source.fetch_klines(symbol="BTC-USDT", timeframe="1h")
        self.assertIn("无法连接 OKX", str(ctx.exception))
        self.assertEqual(server.calls, 3)

    def test_transient_failure_is_retried_then_succeeds(self) -> None:
        server = FakeOkxServer(series(5), raise_on_calls=(1,))
        source = self.source(server, max_retries=2)
        result = source.fetch_candles(
            get_instrument("BTC-USDT"), "1h",
            start_timestamp="2025-01-01 00:00:00", end_timestamp="2025-01-01 05:00:00",
        )
        self.assertEqual(len(result.bars), 5)
        self.assertEqual(result.stats.retries, 1)

    def test_non_retryable_error_fails_immediately(self) -> None:
        server = FakeOkxServer(series(5), error_code="51001")
        source = self.source(server, max_retries=2)
        with self.assertRaises(DataSourceError):
            source.fetch_klines(symbol="BTC-USDT", timeframe="1h")
        self.assertEqual(server.calls, 1)


# --------------------------------------------------------------------------- 9) 质量核对
class QualityTest(OkxTestCase):
    def _rows(self, count: int = 24) -> list[dict[str, Any]]:
        server = FakeOkxServer(series(count))
        source = self.source(server, page_limit=100)
        result = source.fetch_candles(
            get_instrument("BTC-USDT"), "1h",
            start_timestamp="2024-12-31 00:00:00", end_timestamp="2025-01-03 00:00:00",
        )
        from data_source.importer import prepare_kline_rows

        _, rows = prepare_kline_rows(
            source, symbol="BTC-USDT", timeframe="1h", symbol_id=1,
            start_timestamp="2024-12-31 00:00:00", end_timestamp="2025-01-03 00:00:00",
        )
        return rows

    def test_clean_series_has_no_gaps(self) -> None:
        report = analyze_klines(self._rows(24), "1h",
                                start_timestamp="2025-01-01 00:00:00",
                                end_timestamp="2025-01-01 23:00:00")
        self.assertEqual(report.missing_intervals, 0)
        self.assertEqual(report.invalid_bars, 0)
        self.assertTrue(report.is_clean)

    def test_missing_intervals_are_detected(self) -> None:
        rows = [row for row in self._rows(24) if row["timestamp"] not in (
            "2025-01-01 05:00:00", "2025-01-01 06:00:00", "2025-01-01 07:00:00")]
        report = analyze_klines(rows, "1h",
                                start_timestamp="2025-01-01 00:00:00",
                                end_timestamp="2025-01-01 23:00:00")
        self.assertEqual(report.missing_intervals, 3)
        self.assertEqual(report.bars, 21)

    def test_invalid_rows_are_detected(self) -> None:
        rows = self._rows(4)
        rows[1] = {**rows[1], "low": 0.0}       # 非正价格
        rows[2] = {**rows[2], "volume": -1.0}   # 负成交量
        report = analyze_klines(rows, "1h")
        self.assertEqual(report.non_positive_prices, 1)
        self.assertEqual(report.negative_volumes, 1)
        self.assertFalse(report.is_clean)

    def test_duplicates_are_deduplicated_deterministically(self) -> None:
        rows = self._rows(3)
        rows.append({**rows[0], "close": 999.0})
        deduped, removed = deduplicate_rows(rows)
        self.assertEqual(removed, 1)
        self.assertEqual(len(deduped), 3)
        self.assertEqual(deduped[0]["close"], 999.0)  # 保留最后一次出现
        self.assertEqual([r["timestamp"] for r in deduped], sorted(r["timestamp"] for r in deduped))


# --------------------------------------------------------------------------- 4/5/7/8/12) 采集
class CollectionTest(OkxTestCase):
    def collect(self, server: FakeOkxServer, **kwargs: Any):
        kwargs.setdefault("source", self.source(server, page_limit=100))
        kwargs.setdefault("symbol", "BTC-USDT")
        kwargs.setdefault("timeframe", "1h")
        return collect_okx_klines(db_path=self.db_path, **kwargs)

    def test_parses_and_writes_into_temp_db(self) -> None:
        report = self.collect(
            FakeOkxServer(series(24)),
            start_timestamp="2025-01-01 00:00:00", end_timestamp="2025-01-01 23:00:00",
        )
        self.assertEqual(report.fetched, 24)
        self.assertEqual(report.new_records, 24)
        self.assertEqual(report.total_in_db, 24)
        self.assertEqual(report.actual_start, "2025-01-01 00:00:00")
        self.assertEqual(report.actual_end, "2025-01-01 23:00:00")
        self.assertTrue(report.symbol_created)
        self.assertEqual(report.market, "CRYPTO")
        self.assertEqual(report.asset_type, "SPOT")
        self.assertTrue(report.last_candle_confirmed)
        # 标的登记口径
        row = SymbolRepository(self.db_path).get_symbol("CRYPTO", "BTC-USDT")
        self.assertIsNotNone(row)
        self.assertEqual(row["asset_type"], "SPOT")

    def test_incremental_update_is_idempotent(self) -> None:
        server = FakeOkxServer(series(24))
        first = self.collect(server, start_timestamp="2025-01-01 00:00:00",
                             end_timestamp="2025-01-01 23:00:00")
        self.assertEqual(first.new_records, 24)
        # 第二次不指定 start → 从库中最新一根继续（幂等，不产生重复）
        second = self.collect(FakeOkxServer(series(24)), now_timestamp="2025-01-01 23:00:00")
        self.assertEqual(second.requested_start, "2025-01-01 23:00:00")
        self.assertEqual(second.new_records, 0)
        self.assertEqual(second.total_in_db, 24)
        self.assertTrue(any("没有新增记录" in w for w in second.warnings))

    def test_incremental_continues_from_stored_latest(self) -> None:
        self.collect(FakeOkxServer(series(24)), start_timestamp="2025-01-01 00:00:00",
                     end_timestamp="2025-01-01 23:00:00")
        # 交易所又多了 24 根
        report = self.collect(FakeOkxServer(series(48)), now_timestamp="2025-01-02 23:00:00")
        self.assertEqual(report.new_records, 24)
        self.assertEqual(report.total_in_db, 48)

    def test_first_backfill_requires_explicit_start(self) -> None:
        with self.assertRaises(DataSourceError) as ctx:
            self.collect(FakeOkxServer(series(5)), now_timestamp="2025-01-02 00:00:00")
        self.assertIn("必须显式给出 start_timestamp", str(ctx.exception))

    def test_unfinished_candle_is_not_stored(self) -> None:
        report = self.collect(
            FakeOkxServer(series(6, confirm_last="0")),
            start_timestamp="2025-01-01 00:00:00", end_timestamp="2025-01-01 05:00:00",
        )
        self.assertEqual(report.fetched, 5)                 # 未完结那根被过滤
        self.assertEqual(report.total_in_db, 5)
        self.assertGreaterEqual(report.stats["unfinished_filtered"], 1)
        self.assertEqual(report.last_candle_confirm_raw, "0")  # 交易所最新一根尚未完结
        self.assertTrue(report.last_candle_confirmed)          # 入库的都是已完结
        stored = KlineRepository(self.db_path).list_klines(report.symbol_id, "1h")
        self.assertNotIn("2025-01-01 05:00:00", [row["timestamp"] for row in stored])

    def test_invalid_market_data_is_rejected_and_nothing_written(self) -> None:
        bad = series(4)
        bad[1][3] = "-5"  # low 为负 → 非法价格
        with self.assertRaises(DataSourceError) as ctx:
            self.collect(FakeOkxServer(bad), start_timestamp="2025-01-01 00:00:00",
                         end_timestamp="2025-01-01 03:00:00")
        self.assertIn("非法行情", str(ctx.exception))
        self.assertEqual(KlineRepository(self.db_path).count_klines(1, "1h"), 0)  # 一条都没写

    def test_empty_response_is_reported_not_fatal(self) -> None:
        report = self.collect(FakeOkxServer([]), start_timestamp="2025-01-01 00:00:00",
                              end_timestamp="2025-01-01 05:00:00")
        self.assertTrue(report.is_empty)
        self.assertEqual(report.new_records, 0)
        self.assertTrue(any("没有任何 K 线返回" in w for w in report.warnings))

    def test_dry_run_does_not_write(self) -> None:
        report = self.collect(FakeOkxServer(series(6)), start_timestamp="2025-01-01 00:00:00",
                              end_timestamp="2025-01-01 05:00:00", dry_run=True)
        self.assertEqual(report.written, 0)
        self.assertEqual(report.total_in_db, 0)

    def test_failure_leaves_nothing_then_rerun_recovers(self) -> None:
        failing = FakeOkxServer(series(6), raise_on_calls=(1, 2, 3, 4, 5, 6))
        with self.assertRaises(DataSourceError):
            self.collect(failing, start_timestamp="2025-01-01 00:00:00",
                         end_timestamp="2025-01-01 05:00:00")
        self.assertEqual(KlineRepository(self.db_path).count_klines(1, "1h"), 0)
        # 重新运行（网络恢复）→ 正常完成
        report = self.collect(FakeOkxServer(series(6)), start_timestamp="2025-01-01 00:00:00",
                              end_timestamp="2025-01-01 05:00:00")
        self.assertEqual(report.total_in_db, 6)

    def test_duplicate_bars_from_source_are_deduplicated(self) -> None:
        bars = [
            KlineBar("BTC-USDT", "1h", "2025-01-01 00:00:00", 10.0, 12.0, 9.0, 11.0, 1.0, 11.0),
            KlineBar("BTC-USDT", "1h", "2025-01-01 00:00:00", 10.0, 13.0, 9.0, 12.5, 2.0, 25.0),
            KlineBar("BTC-USDT", "1h", "2025-01-01 01:00:00", 12.5, 14.0, 12.0, 13.0, 1.5, 19.5),
        ]
        report = collect_okx_klines(
            symbol="BTC-USDT", timeframe="1h", db_path=self.db_path,
            source=StaticSource(bars),
            start_timestamp="2025-01-01 00:00:00", end_timestamp="2025-01-01 01:00:00",
        )
        self.assertEqual(report.deduplicated, 1)
        self.assertEqual(report.total_in_db, 2)

    def test_formal_database_is_not_touched(self) -> None:
        formal = get_database_path()
        before = formal.stat().st_size if formal.exists() else None
        self.collect(FakeOkxServer(series(6)), start_timestamp="2025-01-01 00:00:00",
                     end_timestamp="2025-01-01 05:00:00")
        after = formal.stat().st_size if formal.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
