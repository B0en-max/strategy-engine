"""Stage 11：K 线采集层测试。

覆盖：
    配置        环境变量读取、缺失/非法配置、API Key 只放请求头
    请求构造    查询参数名、额外参数、时间格式（秒/毫秒/datetime）
    数据转换    response_path、字段映射、时间格式、amount 可缺失
    错误处理    HTTP 错误、连接错误、非法 JSON、缺字段、格式错误（且不泄漏密钥）
    写入        通过 KlineRepository 写入 klines 表
    重复数据    同一时间段重复采集不会产生重复记录
    时间范围    起止时间会传给数据源
    隔离        全部使用临时数据库，不污染 data/strategy_engine.db

**全部使用 mock opener，不进行真实网络请求。**

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import ENV_API_KEY, ENV_BASE_URL, ENV_TIMEOUT, ConfigError, DataSourceConfig  # noqa: E402
from data_source import (  # noqa: E402
    DataSourceError,
    HttpKlineDataSource,
    KlineBar,
    KlineDataSource,
    import_klines,
)
from data_source.http_source import (  # noqa: E402
    format_request_timestamp,
    normalize_datetime,
    parse_response_timestamp,
)
from data_source.base import timeframe_to_seconds  # noqa: E402
from database import DEFAULT_DB_PATH, KlineRepository, SymbolRepository, initialize_database  # noqa: E402

BASE_URL = "https://api.example.test/klines"
API_KEY = "secret-test-key"

T0 = "2026-01-01 00:00:00"
T1 = "2026-01-01 01:00:00"
T2 = "2026-01-01 02:00:00"


class FakeOpener:
    """可注入的假 opener：记录调用并按需返回数据或抛错（不打真实网络）。"""

    def __init__(self, payload=None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers, timeout: float) -> bytes:
        self.calls.append((url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        return json.dumps(self.payload).encode("utf-8")

    @property
    def last_url(self) -> str:
        return self.calls[-1][0]

    @property
    def last_headers(self) -> dict[str, str]:
        return self.calls[-1][1]


def make_config(**overrides) -> DataSourceConfig:
    params = {"base_url": BASE_URL, "api_key": API_KEY}
    params.update(overrides)
    return DataSourceConfig(**params)  # type: ignore[arg-type]


def bar_payload(timestamp: int = 1767225600, **overrides) -> dict:
    row = {
        "timestamp": timestamp,
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 12.5,
        "amount": 1300.0,
    }
    row.update(overrides)
    return row


class ConfigTest(unittest.TestCase):
    """配置读取与校验。"""

    def test_01_from_env_reads_url_key_and_timeout(self) -> None:
        env = {ENV_BASE_URL: BASE_URL, ENV_API_KEY: API_KEY, ENV_TIMEOUT: "3.5"}
        config = DataSourceConfig.from_env(env)
        self.assertEqual(config.base_url, BASE_URL)
        self.assertEqual(config.api_key, API_KEY)
        self.assertAlmostEqual(config.timeout, 3.5)

    def test_02_missing_url_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            DataSourceConfig.from_env({})
        with self.assertRaises(ConfigError):
            DataSourceConfig(base_url="")

    def test_03_invalid_values_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            DataSourceConfig.from_env({ENV_BASE_URL: BASE_URL, ENV_TIMEOUT: "abc"})
        with self.assertRaises(ConfigError):
            make_config(timeout=0)
        with self.assertRaises(ConfigError):
            make_config(response_timestamp_format="minutes")
        with self.assertRaises(ConfigError):
            make_config(field_mapping={"timestamp": "t", "open": "o"})

    def test_04_api_key_only_in_headers_and_optional(self) -> None:
        headers = make_config().headers()
        self.assertEqual(headers["X-API-Key"], API_KEY)

        anonymous = make_config(api_key=None).headers()
        self.assertNotIn("X-API-Key", anonymous)

        custom = make_config(api_key_header="Authorization").headers()
        self.assertEqual(custom["Authorization"], API_KEY)


class RequestBuildingTest(unittest.TestCase):
    """请求参数与时间格式。"""

    def test_01_query_params_use_configured_names(self) -> None:
        opener = FakeOpener([])
        source = HttpKlineDataSource(
            make_config(extra_params={"limit": "100"}), opener=opener
        )
        source.fetch_klines(symbol="BTCUSDT", timeframe="1h", start_timestamp=T0, end_timestamp=T1)

        url = opener.last_url
        self.assertTrue(url.startswith(BASE_URL + "?"))
        self.assertIn("symbol=BTCUSDT", url)
        self.assertIn("interval=1h", url)
        self.assertIn("limit=100", url)
        expected_start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertIn(f"startTime={expected_start}", url)
        self.assertIn(f"endTime={expected_start + 3600}", url)

    def test_02_custom_param_names_are_used(self) -> None:
        opener = FakeOpener([])
        source = HttpKlineDataSource(
            make_config(
                symbol_param="s", timeframe_param="tf", start_param="from", end_param="to",
                request_timestamp_format="milliseconds",
            ),
            opener=opener,
        )
        source.fetch_klines(symbol="AAPL", timeframe="1d", start_timestamp=T0)
        url = opener.last_url
        self.assertIn("s=AAPL", url)
        self.assertIn("tf=1d", url)
        self.assertIn("from=1767225600000", url)

    def test_03_api_key_is_sent_as_header_never_in_url(self) -> None:
        opener = FakeOpener([])
        HttpKlineDataSource(make_config(), opener=opener).fetch_klines(symbol="X", timeframe="1d")
        self.assertEqual(opener.last_headers["X-API-Key"], API_KEY)
        self.assertNotIn(API_KEY, opener.last_url)

    def test_04_request_timestamp_formats(self) -> None:
        self.assertEqual(format_request_timestamp(T0, "seconds"), 1767225600)
        self.assertEqual(format_request_timestamp(T0, "milliseconds"), 1767225600000)
        self.assertEqual(format_request_timestamp(T0, "datetime"), T0)

    def test_05_response_timestamp_formats(self) -> None:
        self.assertEqual(parse_response_timestamp(1767225600, "seconds"), "2026-01-01 00:00:00")
        self.assertEqual(parse_response_timestamp(1767225600000, "milliseconds"), "2026-01-01 00:00:00")
        self.assertEqual(parse_response_timestamp("2026-01-01 00:00:00", "datetime"), "2026-01-01 00:00:00")
        with self.assertRaises(ValueError):
            parse_response_timestamp(object(), "seconds")


class ResponseParsingTest(unittest.TestCase):
    """响应解析与字段转换。"""

    def test_01_plain_list_payload(self) -> None:
        opener = FakeOpener([bar_payload()])
        bars = HttpKlineDataSource(make_config(), opener=opener).fetch_klines(
            symbol="BTCUSDT", timeframe="1h"
        )
        self.assertEqual(len(bars), 1)
        bar = bars[0]
        self.assertIsInstance(bar, KlineBar)
        self.assertEqual(bar.symbol, "BTCUSDT")
        self.assertEqual(bar.timeframe, "1h")
        self.assertEqual(bar.timestamp, T0)
        self.assertAlmostEqual(bar.open, 100.0)
        self.assertAlmostEqual(bar.amount, 1300.0)

    def test_02_response_path_is_used(self) -> None:
        opener = FakeOpener({"data": {"klines": [bar_payload()]}})
        source = HttpKlineDataSource(make_config(response_path="data.klines"), opener=opener)
        bars = source.fetch_klines(symbol="X", timeframe="1d")
        self.assertEqual(len(bars), 1)

    def test_03_custom_field_mapping(self) -> None:
        opener = FakeOpener([{"t": 1767225600, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}])
        source = HttpKlineDataSource(
            make_config(field_mapping={"timestamp": "t", "open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}),
            opener=opener,
        )
        bar = source.fetch_klines(symbol="X", timeframe="1d")[0]
        self.assertEqual(bar.timestamp, T0)
        self.assertAlmostEqual(bar.high, 2.0)
        self.assertIsNone(bar.amount, "未映射的 amount 应为 None")

    def test_04_amount_may_be_missing_or_null(self) -> None:
        opener = FakeOpener([bar_payload(amount=None)])
        bar = HttpKlineDataSource(make_config(), opener=opener).fetch_klines(symbol="X", timeframe="1d")[0]
        self.assertIsNone(bar.amount)

    def test_05_bad_payloads_raise_source_error(self) -> None:
        cases = {
            "数组不是列表": ({"timestamp": 1}, None),
            "缺少 response_path": ({"data": [bar_payload()]}, "missing.path"),
            "缺少必需字段": ([{"timestamp": 1}], None),
            "价格不是数字": ([bar_payload(close="abc")], None),
            "元素不是对象": ([[1, 2, 3]], None),
        }
        for label, (payload, path) in cases.items():
            with self.subTest(case=label):
                opener = FakeOpener(payload)
                source = HttpKlineDataSource(make_config(response_path=path), opener=opener)
                with self.assertRaises(DataSourceError):
                    source.fetch_klines(symbol="X", timeframe="1d")


class ErrorHandlingTest(unittest.TestCase):
    """API 错误处理。"""

    def test_01_http_error_becomes_source_error(self) -> None:
        error = HTTPError(BASE_URL, 503, "Service Unavailable", hdrs=None, fp=None)
        opener = FakeOpener(error=error)
        with self.assertRaises(DataSourceError) as ctx:
            HttpKlineDataSource(make_config(), opener=opener).fetch_klines(symbol="X", timeframe="1d")
        self.assertIn("503", str(ctx.exception))

    def test_02_connection_error_becomes_source_error(self) -> None:
        opener = FakeOpener(error=URLError("connection refused"))
        with self.assertRaises(DataSourceError):
            HttpKlineDataSource(make_config(), opener=opener).fetch_klines(symbol="X", timeframe="1d")

    def test_03_invalid_json_becomes_source_error(self) -> None:
        def bad_opener(url, headers, timeout):
            return b"<html>not json</html>"

        with self.assertRaises(DataSourceError):
            HttpKlineDataSource(make_config(), opener=bad_opener).fetch_klines(symbol="X", timeframe="1d")

    def test_04_errors_never_leak_the_api_key(self) -> None:
        error = HTTPError(BASE_URL, 500, "Server Error", hdrs=None, fp=None)
        opener = FakeOpener(error=error)
        with self.assertRaises(DataSourceError) as ctx:
            HttpKlineDataSource(make_config(), opener=opener).fetch_klines(symbol="X", timeframe="1d")
        self.assertNotIn(API_KEY, str(ctx.exception))

    def test_05_invalid_symbol_and_timeframe_are_rejected(self) -> None:
        opener = FakeOpener([])
        source = HttpKlineDataSource(make_config(), opener=opener)
        with self.assertRaises(ValueError):
            source.fetch_klines(symbol="  ", timeframe="1d")
        with self.assertRaises(ValueError):
            source.fetch_klines(symbol="X", timeframe="")
        with self.assertRaises(ValueError):
            source.fetch_klines(symbol="X", timeframe="1d", start_timestamp="2026-01-01")
        self.assertEqual(opener.calls, [], "参数非法时不应发起请求")


class FakeSource(KlineDataSource):
    """测试用的数据源：直接返回预设 K 线并记录调用参数。"""

    def __init__(self, bars: list[KlineBar]) -> None:
        self.bars = bars
        self.calls: list[dict[str, object]] = []

    def fetch_klines(self, *, symbol, timeframe, start_timestamp=None, end_timestamp=None):
        self.calls.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
            }
        )
        return list(self.bars)


def make_bar(timestamp: str = T0, close: float = 105.0, **overrides) -> KlineBar:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "timestamp": timestamp,
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": close,
        "volume": 12.5,
        "amount": None,
    }
    values.update(overrides)
    return KlineBar(**values)  # type: ignore[arg-type]


class ImporterTestCase(unittest.TestCase):
    """基类（无测试方法）：每个用例使用独立的临时数据库。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_datasource_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "datasource_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)
        self.symbol_id = self.symbols.create_symbol("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO")


class ImporterTest(ImporterTestCase):
    """采集写入 klines 表。"""

    def test_01_imports_bars_into_klines_table(self) -> None:
        source = FakeSource([make_bar(T1), make_bar(T0)])  # 故意乱序
        result = import_klines(
            source, self.klines, self.symbol_id,
            symbol="BTCUSDT", timeframe="1h", start_timestamp=T0, end_timestamp=T1,
        )

        self.assertEqual(result.fetched, 2)
        self.assertEqual(result.written, 2)
        self.assertEqual(result.symbol_id, self.symbol_id)
        self.assertEqual(result.timeframe, "1h")
        self.assertEqual(result.start_timestamp, T0)
        self.assertEqual(result.end_timestamp, T1)

        rows = self.klines.list_klines(self.symbol_id, "1h")
        self.assertEqual([r["timestamp"] for r in rows], [T0, T1], "写入应按时间排序")
        self.assertAlmostEqual(rows[0]["close"], 105.0)
        self.assertIsNone(rows[0]["amount"])
        self.assertIsNotNone(rows[0]["created_at"])

    def test_02_requested_range_is_passed_to_source(self) -> None:
        source = FakeSource([])
        import_klines(
            source, self.klines, self.symbol_id,
            symbol="BTCUSDT", timeframe="1h", start_timestamp=T0, end_timestamp=T1,
        )
        self.assertEqual(
            source.calls,
            [{"symbol": "BTCUSDT", "timeframe": "1h", "start_timestamp": T0, "end_timestamp": T1}],
        )

    def test_03_repeated_import_does_not_duplicate_rows(self) -> None:
        bars = [make_bar(T0), make_bar(T1)]
        import_klines(FakeSource(bars), self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1h"), 2)

        # 同一时间段再采集一次，且其中一根 K 线被数据源修正（close 仍在 high/low 区间内）
        corrected = [make_bar(T0, close=95.0), make_bar(T1)]
        import_klines(FakeSource(corrected), self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")

        self.assertEqual(self.klines.count_klines(self.symbol_id, "1h"), 2, "重复采集不应产生重复记录")
        self.assertAlmostEqual(
            self.klines.get_kline(self.symbol_id, "1h", T0)["close"], 95.0, "已存在的 K 线应被更新"
        )

    def test_04_batch_with_duplicate_timestamp_keeps_single_row(self) -> None:
        # 同一批次内出现相同 timestamp，最终只应保留一条（后写入的生效）
        source = FakeSource([make_bar(T0, close=95.0), make_bar(T0, close=96.0)])
        import_klines(source, self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1h"), 1)
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1h", T0)["close"], 96.0)

    def test_05_empty_result_writes_nothing(self) -> None:
        result = import_klines(FakeSource([]), self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")
        self.assertEqual((result.fetched, result.written), (0, 0))
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1h"), 0)

    def test_06_invalid_bar_is_rejected_and_writes_nothing(self) -> None:
        # 第二根 high < close，违反数据库 OHLC 约束
        source = FakeSource([make_bar(T0), make_bar(T1, close=999.0)])
        with self.assertRaises(sqlite3.IntegrityError):
            import_klines(source, self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1h"), 0, "整批必须回滚")

    def test_07_unknown_symbol_id_is_rejected_by_database(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            import_klines(FakeSource([make_bar(T0)]), self.klines, 999999, symbol="BTCUSDT", timeframe="1h")

    def test_08_source_returning_non_list_is_rejected(self) -> None:
        class BadSource(KlineDataSource):
            def fetch_klines(self, *, symbol, timeframe, start_timestamp=None, end_timestamp=None):
                return "not-a-list"  # type: ignore[return-value]

        with self.assertRaises(DataSourceError):
            import_klines(BadSource(), self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")

    def test_09_invalid_arguments_are_rejected_before_fetching(self) -> None:
        source = FakeSource([make_bar(T0)])
        for kwargs in (
            {"symbol": "", "timeframe": "1h"},
            {"symbol": "BTCUSDT", "timeframe": ""},
            {"symbol": "BTCUSDT", "timeframe": "1h", "start_timestamp": "2026-01-01"},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    import_klines(source, self.klines, self.symbol_id, **kwargs)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            import_klines(source, self.klines, 0, symbol="BTCUSDT", timeframe="1h")
        self.assertEqual(source.calls, [])


class EndToEndWithMockHttpTest(ImporterTestCase):
    """HTTP 数据源 + 采集器（仍为 mock 网络）。"""

    def test_01_mock_http_source_flows_into_database(self) -> None:
        opener = FakeOpener(
            {
                "data": [
                    bar_payload(1767225600, close=105.0),
                    bar_payload(1767229200, close=107.0),
                ]
            }
        )
        source = HttpKlineDataSource(make_config(response_path="data"), opener=opener)
        result = import_klines(
            source, self.klines, self.symbol_id,
            symbol="BTCUSDT", timeframe="1h", start_timestamp=T0, end_timestamp=T1,
        )

        self.assertEqual((result.fetched, result.written), (2, 2))
        rows = self.klines.list_klines(self.symbol_id, "1h")
        self.assertEqual([r["timestamp"] for r in rows], [T0, T1])
        self.assertAlmostEqual(rows[1]["close"], 107.0)
        self.assertEqual(opener.last_headers["X-API-Key"], API_KEY)

    def test_02_tests_never_touch_the_production_database(self) -> None:
        self.assertNotEqual(self.klines.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.klines.db_path, self.db_path)


class GenericSourceExtensionsTest(unittest.TestCase):
    """Stage 12：为接入真实数据源所做的通用扩展。"""

    def test_01_timeframe_mapping_is_applied(self) -> None:
        opener = FakeOpener([])
        source = HttpKlineDataSource(
            make_config(timeframe_param="scale", timeframe_mapping={"15m": "15", "1d": "240"}),
            opener=opener,
        )
        source.fetch_klines(symbol="sh600519", timeframe="15m")
        self.assertIn("scale=15", opener.last_url)

        source.fetch_klines(symbol="sh600519", timeframe="1d")
        self.assertIn("scale=240", opener.last_url)

        with self.assertRaises(ConfigError):
            source.fetch_klines(symbol="sh600519", timeframe="1m")

    def test_02_time_range_params_can_be_disabled(self) -> None:
        opener = FakeOpener([])
        source = HttpKlineDataSource(make_config(start_param=None, end_param=None), opener=opener)
        source.fetch_klines(symbol="X", timeframe="1d", start_timestamp=T0, end_timestamp=T1)
        self.assertNotIn("startTime", opener.last_url)
        self.assertNotIn("endTime", opener.last_url)

        with self.assertRaises(ValueError):  # 即使不下发，也要校验格式
            source.fetch_klines(symbol="X", timeframe="1d", start_timestamp="2026-01-01")

    def test_03_numeric_strings_are_accepted(self) -> None:
        opener = FakeOpener(
            [{"timestamp": "1767225600", "open": "1.5", "high": "2", "low": "1", "close": "1.75", "volume": "100"}]
        )
        bar = HttpKlineDataSource(make_config(), opener=opener).fetch_klines(symbol="X", timeframe="1d")[0]
        self.assertEqual(bar.timestamp, T0)
        self.assertAlmostEqual(bar.open, 1.5)
        self.assertAlmostEqual(bar.close, 1.75)
        self.assertAlmostEqual(bar.volume, 100.0)
        self.assertIsNone(bar.amount)

    def test_04_datetime_values_are_normalized(self) -> None:
        cases = {
            "2026-01-01": "2026-01-01 00:00:00",
            "2026-01-01 09:45": "2026-01-01 09:45:00",
            "2026-01-01 09:45:30": "2026-01-01 09:45:30",
            "2026-01-01T09:45:30": "2026-01-01 09:45:30",
            "  2026-01-01 09:45  ": "2026-01-01 09:45:00",
        }
        for raw, expected in cases.items():
            with self.subTest(value=raw):
                self.assertEqual(normalize_datetime(raw), expected)
        for bad in ("01/01/2026", "2026-1-1", 1767225600, None):
            with self.subTest(value=bad):
                with self.assertRaises(DataSourceError):
                    normalize_datetime(bad)

    def test_05_end_semantics_shifts_only_intraday(self) -> None:
        rows = [{"timestamp": "2026-01-01 09:45:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}]
        opener = FakeOpener(rows)
        source = HttpKlineDataSource(
            make_config(response_timestamp_format="datetime", timestamp_semantics="end"),
            opener=opener,
        )
        # 15 分钟：结束时间 09:45 → 开始时间 09:30
        self.assertEqual(
            source.fetch_klines(symbol="X", timeframe="15m")[0].timestamp, "2026-01-01 09:30:00"
        )

        daily = FakeOpener([{**rows[0], "timestamp": "2026-01-01"}])
        daily_source = HttpKlineDataSource(
            make_config(response_timestamp_format="datetime", timestamp_semantics="end"),
            opener=daily,
        )
        # 日线：标签是日期，不回退
        self.assertEqual(
            daily_source.fetch_klines(symbol="X", timeframe="1d")[0].timestamp, "2026-01-01 00:00:00"
        )

        with self.assertRaises(DataSourceError):  # 无法推断周期长度时不能应用结束语义
            source.fetch_klines(symbol="X", timeframe="1M")

    def test_06_timeframe_to_seconds(self) -> None:
        self.assertEqual(timeframe_to_seconds("15m"), 900)
        self.assertEqual(timeframe_to_seconds("1h"), 3600)
        self.assertEqual(timeframe_to_seconds("4h"), 14400)
        self.assertEqual(timeframe_to_seconds("1d"), 86400)
        self.assertEqual(timeframe_to_seconds("1w"), 604800)
        for bad in ("1M", "daily", "", "10x"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    timeframe_to_seconds(bad)

    def test_07_invalid_timestamp_semantics_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            make_config(timestamp_semantics="middle")


class RangeTrimmingTest(ImporterTestCase):
    """Stage 12：数据源不支持时间范围参数时，由采集器本地裁切。"""

    def test_01_only_bars_inside_range_are_written(self) -> None:
        source = FakeSource([make_bar(T0), make_bar(T1), make_bar(T2)])
        result = import_klines(
            source, self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h",
            start_timestamp=T1, end_timestamp=T2,
        )
        self.assertEqual(result.fetched, 3)
        self.assertEqual(result.written, 2)
        self.assertEqual(
            [r["timestamp"] for r in self.klines.list_klines(self.symbol_id, "1h")], [T1, T2]
        )

    def test_02_bars_outside_range_write_nothing(self) -> None:
        source = FakeSource([make_bar(T0)])
        result = import_klines(
            source, self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h",
            start_timestamp=T2,
        )
        self.assertEqual((result.fetched, result.written), (1, 0))
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1h"), 0)

    def test_03_no_range_means_no_trimming(self) -> None:
        source = FakeSource([make_bar(T0), make_bar(T1)])
        result = import_klines(source, self.klines, self.symbol_id, symbol="BTCUSDT", timeframe="1h")
        self.assertEqual((result.fetched, result.written), (2, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
