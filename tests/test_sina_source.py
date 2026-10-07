"""Stage 12：真实数据源（新浪财经）接入测试。

**不进行真实网络请求**：所有响应都由 mock opener 提供，但使用的数据结构与真实接口一致
（字段名、字符串型数字、时间标签含义）。

覆盖：
    预设配置      地址 / 参数名 / 周期映射 / 字段映射 / 时间语义 / datalen 校验
    请求构造      symbol=sh600519、scale=15、datalen、且**不下发**起止时间参数
    数据转换      字符串型数字、字段映射、15m 时间标签回退（结束 → 开始）、日线不回退
    时间范围      数据源不支持范围参数 → 由 import_klines 本地裁切
    写入          通过 KlineRepository 写入 klines 表；重复导入不产生重复记录
    错误处理      非法 datalen / 不支持的周期 / 响应形状异常

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import ENV_BASE_URL, ENV_TIMEOUT, ConfigError  # noqa: E402
from data_source import (  # noqa: E402
    DataSourceError,
    HttpKlineDataSource,
    SINA_KLINE_URL,
    SINA_TIMEFRAME_MAPPING,
    import_klines,
    sina_cn_kline_config,
)
from database import KlineRepository, SymbolRepository, initialize_database  # noqa: E402

# 真实接口返回的 15 分钟 K 线（字段与取值形式与线上一致）
SINA_15M_ROWS = [
    {"day": "2026-09-29 09:45:00", "open": "1261.440", "high": "1268.000", "low": "1260.880", "close": "1263.210", "volume": "512600"},
    {"day": "2026-09-29 10:00:00", "open": "1264.620", "high": "1265.500", "low": "1262.000", "close": "1264.600", "volume": "249480"},
    {"day": "2026-09-29 13:15:00", "open": "1255.000", "high": "1260.000", "low": "1250.000", "close": "1258.000", "volume": "300000"},
    {"day": "2026-09-29 15:00:00", "open": "1258.190", "high": "1258.620", "low": "1255.860", "close": "1258.620", "volume": "291636"},
]

# 真实接口返回的日线（day 只有日期）
SINA_DAY_ROWS = [
    {"day": "2026-09-28", "open": "1236.000", "high": "1244.010", "low": "1228.100", "close": "1243.880", "volume": "2821800"},
    {"day": "2026-09-29", "open": "1244.600", "high": "1256.130", "low": "1231.050", "close": "1237.000", "volume": "3123935"},
]


class RecordingOpener:
    """假的 opener：记录请求，返回预设响应（不打真实网络）。"""

    def __init__(self, payload=None, *, error: Exception | None = None, raw: bytes | None = None) -> None:
        self.payload = payload
        self.raw = raw
        self.error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers, timeout: float) -> bytes:
        self.calls.append((url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return json.dumps(self.payload).encode("utf-8")

    @property
    def last_url(self) -> str:
        return self.calls[-1][0]


def make_source(rows, *, datalen: int = 320, opener=None, **config_overrides):
    opener = opener or RecordingOpener(rows)
    config = sina_cn_kline_config(datalen=datalen, **config_overrides)
    return HttpKlineDataSource(config, opener=opener), opener


class SinaPresetConfigTest(unittest.TestCase):
    """预设配置。"""

    def test_01_defaults(self) -> None:
        config = sina_cn_kline_config(datalen=100)
        self.assertEqual(config.base_url, SINA_KLINE_URL)
        self.assertIsNone(config.api_key, "该接口无需认证，配置里不应有任何密钥")
        self.assertEqual(config.symbol_param, "symbol")
        self.assertEqual(config.timeframe_param, "scale")
        self.assertIsNone(config.start_param, "该接口不支持起止时间参数")
        self.assertIsNone(config.end_param)
        self.assertEqual(config.extra_params, {"ma": "no", "datalen": "100"})
        self.assertEqual(config.timestamp_semantics, "end")
        self.assertEqual(config.response_timestamp_format, "datetime")
        self.assertIsNone(config.response_path)
        self.assertEqual(config.field_mapping["timestamp"], "day")
        self.assertNotIn("amount", config.field_mapping, "该接口无成交额字段")
        self.assertEqual(config.map_timeframe("15m"), "15")
        self.assertEqual(config.map_timeframe("1d"), "240")
        self.assertEqual(set(SINA_TIMEFRAME_MAPPING), {"5m", "15m", "30m", "60m", "1h", "1d"})

    def test_02_env_overrides_url_and_timeout(self) -> None:
        config = sina_cn_kline_config(env={ENV_BASE_URL: "https://mirror.test/klines", ENV_TIMEOUT: "3"})
        self.assertEqual(config.base_url, "https://mirror.test/klines")
        self.assertAlmostEqual(config.timeout, 3.0)

    def test_03_invalid_datalen_is_rejected(self) -> None:
        for bad in (0, -1, 1024, 1.5, True, "320"):
            with self.subTest(value=bad):
                with self.assertRaises(ConfigError):
                    sina_cn_kline_config(datalen=bad)  # type: ignore[arg-type]

    def test_04_unsupported_timeframe_is_rejected(self) -> None:
        config = sina_cn_kline_config()
        with self.assertRaises(ConfigError):
            config.map_timeframe("1m")
        with self.assertRaises(ConfigError):
            config.map_timeframe("4h")


class SinaRequestTest(unittest.TestCase):
    """请求构造。"""

    def test_01_url_contains_sina_params_and_no_time_range(self) -> None:
        source, opener = make_source(SINA_15M_ROWS, datalen=320)
        source.fetch_klines(
            symbol="sh600519", timeframe="15m",
            start_timestamp="2026-09-29 00:00:00", end_timestamp="2026-09-30 00:00:00",
        )
        url = opener.last_url
        self.assertIn("symbol=sh600519", url)
        self.assertIn("scale=15", url)
        self.assertIn("ma=no", url)
        self.assertIn("datalen=320", url)
        self.assertNotIn("startTime", url, "该接口不支持起止时间参数，不应下发")
        self.assertNotIn("endTime", url)

    def test_02_daily_uses_scale_240(self) -> None:
        source, opener = make_source(SINA_DAY_ROWS)
        source.fetch_klines(symbol="sh600519", timeframe="1d")
        self.assertIn("scale=240", opener.last_url)


class SinaParsingTest(unittest.TestCase):
    """数据转换。"""

    def test_01_intraday_label_is_shifted_to_bar_start(self) -> None:
        source, _ = make_source(SINA_15M_ROWS)
        bars = source.fetch_klines(symbol="sh600519", timeframe="15m")

        self.assertEqual(len(bars), 4)
        # 新浪的日内时间标签是 K 线结束时间：09:45 → 09:30
        self.assertEqual(
            [bar.timestamp for bar in bars],
            ["2026-09-29 09:30:00", "2026-09-29 09:45:00", "2026-09-29 13:00:00", "2026-09-29 14:45:00"],
        )
        first = bars[0]
        self.assertEqual(first.symbol, "sh600519")
        self.assertEqual(first.timeframe, "15m")
        self.assertAlmostEqual(first.open, 1261.440, places=3)
        self.assertAlmostEqual(first.high, 1268.000, places=3)
        self.assertAlmostEqual(first.low, 1260.880, places=3)
        self.assertAlmostEqual(first.close, 1263.210, places=3)
        self.assertAlmostEqual(first.volume, 512600.0)
        self.assertIsNone(first.amount, "该接口不返回成交额")

    def test_02_daily_label_is_a_date_and_not_shifted(self) -> None:
        source, _ = make_source(SINA_DAY_ROWS)
        bars = source.fetch_klines(symbol="sh600519", timeframe="1d")
        self.assertEqual([bar.timestamp for bar in bars], ["2026-09-28 00:00:00", "2026-09-29 00:00:00"])

    def test_03_bad_payloads_raise_source_error(self) -> None:
        cases = {
            "响应不是数组": ({"data": []}, None),
            "缺字段": ([{"day": "2026-09-29 09:45:00", "open": "1"}], None),
            "价格不是数字": ([{**SINA_15M_ROWS[0], "close": "abc"}], None),
            "时间格式异常": ([{**SINA_15M_ROWS[0], "day": "29/09/2026"}], None),
            "非 JSON": (None, b"<html>"),
        }
        for label, (payload, raw) in cases.items():
            with self.subTest(case=label):
                opener = RecordingOpener(payload, raw=raw)
                config = sina_cn_kline_config()
                with self.assertRaises(DataSourceError):
                    HttpKlineDataSource(config, opener=opener).fetch_klines(
                        symbol="sh600519", timeframe="15m"
                    )


class SinaImportTestCase(unittest.TestCase):
    """基类（无测试方法）：临时数据库 + 预置标的。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_sina_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "sina_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)
        self.symbol_id = self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")


class SinaImportTest(SinaImportTestCase):
    """采集写入数据库（mock 数据源）。"""

    def test_01_imports_real_shaped_payload(self) -> None:
        source, opener = make_source(SINA_15M_ROWS)
        result = import_klines(
            source, self.klines, self.symbol_id, symbol="sh600519", timeframe="15m"
        )

        self.assertEqual((result.fetched, result.written), (4, 4))
        rows = self.klines.list_klines(self.symbol_id, "15m")
        self.assertEqual([r["timestamp"] for r in rows], [
            "2026-09-29 09:30:00", "2026-09-29 09:45:00", "2026-09-29 13:00:00", "2026-09-29 14:45:00",
        ])
        self.assertAlmostEqual(rows[0]["close"], 1263.210, places=3)
        self.assertIsNone(rows[0]["amount"])
        self.assertIn("symbol=sh600519", opener.last_url)

    def test_02_repeated_import_keeps_row_count(self) -> None:
        import_klines(make_source(SINA_15M_ROWS)[0], self.klines, self.symbol_id, symbol="sh600519", timeframe="15m")
        count = self.klines.count_klines(self.symbol_id, "15m")

        import_klines(make_source(SINA_15M_ROWS)[0], self.klines, self.symbol_id, symbol="sh600519", timeframe="15m")

        self.assertEqual(self.klines.count_klines(self.symbol_id, "15m"), count, "重复导入不应新增记录")

    def test_03_local_trimming_to_requested_range(self) -> None:
        source, _ = make_source(SINA_15M_ROWS)
        result = import_klines(
            source, self.klines, self.symbol_id, symbol="sh600519", timeframe="15m",
            start_timestamp="2026-09-29 09:30:00", end_timestamp="2026-09-29 09:45:00",
        )

        self.assertEqual(result.fetched, 4, "数据源返回 4 根（接口不支持范围参数）")
        self.assertEqual(result.written, 2, "只有区间内的 2 根被写入")
        self.assertEqual(
            [r["timestamp"] for r in self.klines.list_klines(self.symbol_id, "15m")],
            ["2026-09-29 09:30:00", "2026-09-29 09:45:00"],
        )

    def test_04_tests_never_hit_the_network(self) -> None:
        # 用会抛错的 opener 替代网络：若代码真去联网就会失败
        def forbidden(url: str, headers, timeout: float) -> bytes:
            raise AssertionError("自动测试不应发起真实网络请求")

        source = HttpKlineDataSource(sina_cn_kline_config(), opener=forbidden)
        with self.assertRaises(AssertionError):
            source.fetch_klines(symbol="sh600519", timeframe="15m")


class SinaCliImportTest(SinaImportTestCase):
    """CLI 的 run_import（注入 mock 数据源，不联网）。"""

    def test_01_run_import_creates_symbol_and_summarises(self) -> None:
        from data_source.cli import run_import

        summary = run_import(
            symbol="000001",
            api_symbol="sz000001",
            name="平安银行",
            timeframe="15m",
            db_path=self.db_path,
            source=make_source(SINA_15M_ROWS)[0],
        )

        self.assertEqual(summary["symbol"], "000001")
        self.assertEqual(summary["api_symbol"], "sz000001")
        self.assertTrue(summary["symbol_created"], "缺失的标的应被自动创建")
        self.assertEqual((summary["fetched"], summary["written"]), (4, 4))
        self.assertEqual(summary["total_in_db"], 4)
        self.assertEqual(summary["first_timestamp"], "2026-09-29 09:30:00")
        self.assertEqual(summary["last_timestamp"], "2026-09-29 14:45:00")

        # 再执行一次：不应重复创建标的，也不应重复写入
        again = run_import(
            symbol="000001", api_symbol="sz000001", timeframe="15m",
            db_path=self.db_path, source=make_source(SINA_15M_ROWS)[0],
        )
        self.assertFalse(again["symbol_created"])
        self.assertEqual(again["total_in_db"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
