"""Stage 21 Step 1：新浪 Adapter 测试（market/data/sina.py）。

**不进行真实网络请求**：所有响应都由 mock opener 提供，但字段名与取值形式与真实接口一致
（列表：symbol/code/name 且 symbol 带 sh/sz/bj 前缀；日 K：day/open/high/low/close/volume 且为字符串）。

覆盖：
    列表分页   请求 URL（page / num / sort / asc / node）、单页上限校验、空页
    容错       伪 JSON（键无引号）、名称为空 → 用代码兜底、非法代码 → 进 skipped
    错误       HTTP 错误 / 响应形状异常 → DataSourceError
    总数       '"5571"' → 5571；异常响应 → None
    按代码查名 GBK 响应、命中 / 未命中 / 空结果
    日 K 配置  与 Stage 12 同一份地址与字段映射；datalen 上限放宽到实测值；端到端转换
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import ConfigError  # noqa: E402
from data_source import DataSourceError, HttpKlineDataSource  # noqa: E402
from data_source.presets import SINA_KLINE_URL  # noqa: E402
from market.data.sina import (  # noqa: E402
    SINA_A_SHARE_NODE,
    SINA_MAX_DAILY_BARS,
    SINA_MAX_PAGE_SIZE,
    SinaAStockListSource,
    decode_text,
    loads_tolerant,
    sina_daily_kline_config,
)

#: 真实接口返回的列表行（字段名与线上一致；symbol 带交易所前缀）
LIST_ROWS = [
    {"symbol": "bj920000", "code": "920000", "name": "XD安徽凤", "trade": "14.330", "amount": "1234567"},
    {"symbol": "sh600519", "code": "600519", "name": "贵州茅台", "trade": "1258.620"},
    {"symbol": "sz000001", "code": "000001", "name": "平安银行", "trade": "11.000"},
]

#: 真实接口返回的日 K（day 只有日期、数字为字符串）
DAY_ROWS = [
    {"day": "2026-09-28", "open": "1236.000", "high": "1244.010", "low": "1228.100", "close": "1243.880", "volume": "2821800"},
    {"day": "2026-09-29", "open": "1244.600", "high": "1256.130", "low": "1231.050", "close": "1237.000", "volume": "3123935"},
]

#: 真实接口返回的 suggest 响应（GBK 编码）
SUGGEST_TEXT = 'var suggestvalue="sh600519,11,600519,sh600519,贵州茅台,,贵州茅台,99,1,ESG,,";'


class RecordingOpener:
    """假的 opener：记录请求，返回预设响应（不打真实网络）。"""

    def __init__(self, *, raw: bytes | None = None, payload=None, error: Exception | None = None) -> None:
        self.raw = raw
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers, timeout: float) -> bytes:
        self.calls.append((url, dict(headers), timeout))
        if self.error is not None:
            raise self.error
        if self.raw is not None:
            return self.raw
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

    @property
    def last_url(self) -> str:
        return self.calls[-1][0]


class ListSourceTest(unittest.TestCase):
    """A 股列表 Adapter。"""

    def _source(self, opener: RecordingOpener) -> SinaAStockListSource:
        return SinaAStockListSource(opener=opener, timeout=7.0)

    def test_fetch_page_builds_url_and_standardizes_rows(self) -> None:
        opener = RecordingOpener(payload=LIST_ROWS)
        page = self._source(opener).fetch_page(page=2, page_size=100)

        url = opener.last_url
        self.assertIn("page=2", url)
        self.assertIn(f"num={SINA_MAX_PAGE_SIZE}", url)
        self.assertIn(f"node={SINA_A_SHARE_NODE}", url)
        self.assertIn("sort=symbol", url)
        self.assertIn("asc=1", url)
        self.assertEqual(opener.calls[0][2], 7.0)

        self.assertEqual(len(page.items), 3)
        self.assertEqual(page.page, 2)
        self.assertEqual(page.total, None)  # 列表接口不给总数，不猜测
        first = page.items[0]
        self.assertEqual((first.symbol, first.exchange, first.name, first.market), ("920000", "BJ", "XD安徽凤", "CN"))
        self.assertEqual(first.asset_type, "STOCK")
        self.assertEqual(first.source, "sina")
        self.assertEqual(page.skipped, ())

    def test_tolerant_parse_of_bare_key_json(self) -> None:
        raw = (
            '[{symbol: "sh600519", code: "600519", name: "贵州茅台"},'
            ' {symbol: "sz000001", code: "000001", name: "平安银行"}]'
        ).encode("utf-8")
        page = self._source(RecordingOpener(raw=raw)).fetch_page(page=1, page_size=100)
        self.assertEqual([item.symbol for item in page.items], ["600519", "000001"])

    def test_empty_name_falls_back_to_code(self) -> None:
        rows = [{"symbol": "sh600519", "code": "600519", "name": "   "}]
        page = self._source(RecordingOpener(payload=rows)).fetch_page(page=1, page_size=100)
        self.assertEqual(page.items[0].name, "600519")

    def test_invalid_rows_are_reported_not_dropped_silently(self) -> None:
        rows = [
            {"symbol": "sh600519", "code": "600519", "name": "贵州茅台"},
            {"symbol": "hk00700", "code": "00700", "name": "腾讯"},  # 非 A 股代码
            "not-a-dict",
        ]
        page = self._source(RecordingOpener(payload=rows)).fetch_page(page=1, page_size=100)
        self.assertEqual(len(page.items), 1)
        self.assertEqual(len(page.skipped), 2)
        self.assertTrue(any("非对象行" in note for note in page.skipped))

    def test_empty_page(self) -> None:
        page = self._source(RecordingOpener(payload=[])).fetch_page(page=99, page_size=100)
        self.assertTrue(page.is_empty)

    def test_page_arguments_are_validated(self) -> None:
        source = self._source(RecordingOpener(payload=[]))
        for kwargs in ({"page": 0, "page_size": 100}, {"page": 1, "page_size": 0}, {"page": 1, "page_size": 101}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    source.fetch_page(**kwargs)

    def test_http_error_becomes_data_source_error(self) -> None:
        opener = RecordingOpener(error=HTTPError("http://x", 503, "boom", {}, None))
        with self.assertRaises(DataSourceError):
            self._source(opener).fetch_page(page=1, page_size=100)

    def test_non_list_payload_raises(self) -> None:
        with self.assertRaises(DataSourceError):
            self._source(RecordingOpener(payload={"data": []})).fetch_page(page=1, page_size=100)

    def test_count_parses_quoted_number(self) -> None:
        opener = RecordingOpener(raw='"5571"'.encode("utf-8"))
        count = SinaAStockListSource(opener=opener).count()
        self.assertEqual(count, 5571)
        self.assertIn("node=hs_a", opener.last_url)

    def test_count_returns_none_when_unavailable(self) -> None:
        self.assertIsNone(SinaAStockListSource(opener=RecordingOpener(raw=b"oops")).count())
        self.assertIsNone(SinaAStockListSource(opener=RecordingOpener(error=OSError("down"))).count())

    def test_lookup_name_decodes_gbk_response(self) -> None:
        opener = RecordingOpener(raw=SUGGEST_TEXT.encode("gbk"))
        name = SinaAStockListSource(opener=opener).lookup_name("600519")
        self.assertEqual(name, "贵州茅台")
        self.assertIn("key=600519", opener.last_url)

    def test_lookup_name_returns_none_when_not_found(self) -> None:
        self.assertIsNone(SinaAStockListSource(opener=RecordingOpener(raw=b'var suggestvalue="";')).lookup_name("600519"))
        other = 'var suggestvalue="sh600519,11,600519,sh600519,贵州茅台,,";'
        self.assertIsNone(SinaAStockListSource(opener=RecordingOpener(raw=other.encode("gbk"))).lookup_name("000001"))


class DecodeAndParseTest(unittest.TestCase):
    """编码与 JSON 容错工具。"""

    def test_decode_text_prefers_utf8_then_gbk(self) -> None:
        self.assertEqual(decode_text("已经是字符串"), "已经是字符串")
        self.assertEqual(decode_text("贵州茅台".encode("utf-8")), "贵州茅台")
        self.assertEqual(decode_text("贵州茅台".encode("gbk")), "贵州茅台")

    def test_loads_tolerant_strict_and_repaired(self) -> None:
        self.assertEqual(loads_tolerant('{"a": 1}'), {"a": 1})
        self.assertEqual(loads_tolerant("[{a: 1, b: \"x\"}]"), [{"a": 1, "b": "x"}])

    def test_loads_tolerant_raises_data_source_error(self) -> None:
        with self.assertRaises(DataSourceError):
            loads_tolerant("<html>not json</html>")


class DailyKlineConfigTest(unittest.TestCase):
    """新浪日 K 配置（复用 Stage 12 的地址与字段映射）。"""

    def test_config_reuses_sina_pieces(self) -> None:
        config = sina_daily_kline_config(datalen=320, timeout=5.0)
        self.assertEqual(config.base_url, SINA_KLINE_URL)
        self.assertIsNone(config.api_key)  # 无需 Key
        self.assertEqual(config.extra_params, {"ma": "no", "datalen": "320"})
        self.assertEqual(config.symbol_param, "symbol")
        self.assertEqual(config.timeframe_param, "scale")
        self.assertIsNone(config.start_param)  # 接口不支持时间范围参数
        self.assertIsNone(config.end_param)
        self.assertEqual(config.map_timeframe("1d"), "240")
        self.assertEqual(config.timestamp_semantics, "end")
        self.assertNotIn("amount", config.field_mapping)  # 该接口不返回成交额
        self.assertEqual(config.timeout, 5.0)

    def test_datalen_bounds(self) -> None:
        self.assertEqual(sina_daily_kline_config(datalen=SINA_MAX_DAILY_BARS).extra_params["datalen"], "6000")
        for bad in (0, -1, SINA_MAX_DAILY_BARS + 1, True, "320"):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    sina_daily_kline_config(datalen=bad)  # type: ignore[arg-type]

    def test_end_to_end_daily_bars_convert(self) -> None:
        opener = RecordingOpener(payload=DAY_ROWS)
        source = HttpKlineDataSource(sina_daily_kline_config(datalen=2), opener=opener)
        bars = source.fetch_klines(symbol="sh600519", timeframe="1d")

        self.assertIn("scale=240", opener.last_url)
        self.assertIn("symbol=sh600519", opener.last_url)
        self.assertNotIn("startTime", opener.last_url)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].timestamp, "2026-09-28 00:00:00")  # 日线标签是日期，不回退
        self.assertEqual(bars[0].close, 1243.88)
        self.assertIsNone(bars[0].amount)  # 无成交额字段 → None（不补 0）


if __name__ == "__main__":
    unittest.main()
