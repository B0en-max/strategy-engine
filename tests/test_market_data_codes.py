"""Stage 21 Step 1：A 股代码标准化测试（market/data/codes.py）。

覆盖：
    前缀形式 'sh600519' / 'sz000001' / 'bj920000'
    后缀形式 '600519.SH' / '000001.sz'（大小写不敏感）
    裸代码    '600519' → 按代码段规则推导交易所
    交易所规则 600/601/603/605/688 → SH，000/001/002/003/300/301 → SZ，430/83/87/88/920 → BJ
    与展示代码一致：AStockCode.display_code 必须等于 market.service.display_code
    非法输入：空、非 6 位数字、规则也判断不出的代码 → ValueError（带原始值）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market.data.codes import (  # noqa: E402
    A_SHARE_MARKET,
    AStockCode,
    exchange_from_rules,
    is_a_share_code,
    normalize_a_share_code,
    strip_exchange,
)
from market.service import display_code  # noqa: E402


class NormalizeCodeTest(unittest.TestCase):
    """代码标准化与交易所映射。"""

    def test_prefix_form_uses_source_exchange(self) -> None:
        for raw, symbol, exchange in (
            ("sh600519", "600519", "SH"),
            ("sz000001", "000001", "SZ"),
            ("bj920000", "920000", "BJ"),
            ("SH600519", "600519", "SH"),  # 前缀大小写不敏感
        ):
            with self.subTest(raw=raw):
                code = normalize_a_share_code(raw)
                self.assertEqual(code.symbol, symbol)
                self.assertEqual(code.exchange, exchange)
                self.assertEqual(code.exchange_source, "source")
                self.assertEqual(code.market, A_SHARE_MARKET)
                self.assertEqual(code.source_symbol, f"{exchange.lower()}{symbol}")

    def test_suffix_form_uses_source_exchange(self) -> None:
        for raw, symbol, exchange in (
            ("600519.SH", "600519", "SH"),
            ("000001.sz", "000001", "SZ"),
            ("830799.BJ", "830799", "BJ"),
        ):
            with self.subTest(raw=raw):
                code = normalize_a_share_code(raw)
                self.assertEqual((code.symbol, code.exchange, code.exchange_source), (symbol, exchange, "source"))

    def test_bare_code_falls_back_to_rule(self) -> None:
        cases = {
            "600519": "SH",
            "601398": "SH",
            "603288": "SH",
            "605499": "SH",
            "688111": "SH",
            "900901": "SH",
            "000001": "SZ",
            "001979": "SZ",
            "002594": "SZ",
            "003816": "SZ",
            "300750": "SZ",
            "301269": "SZ",
            "200011": "SZ",
            "430047": "BJ",
            "830799": "BJ",
            "870204": "BJ",
            "920000": "BJ",
        }
        for raw, exchange in cases.items():
            with self.subTest(raw=raw):
                code = normalize_a_share_code(raw)
                self.assertEqual(code.symbol, raw)
                self.assertEqual(code.exchange, exchange)
                self.assertEqual(code.exchange_source, "rule")

    def test_display_code_matches_service_rule(self) -> None:
        """展示代码必须与后端 API 用的 display_code 完全一致（不出现两套规则）。"""
        for raw in ("sh600519", "sz000001", "bj920000", "688111", "300750", "830799"):
            with self.subTest(raw=raw):
                code = normalize_a_share_code(raw)
                self.assertEqual(code.display_code, display_code(A_SHARE_MARKET, code.symbol))

    def test_exchange_from_rules_unknown(self) -> None:
        self.assertIsNone(exchange_from_rules("999999"))  # 规则里没有 9 开头（除 900）
        self.assertIsNone(exchange_from_rules("60051"))
        self.assertIsNone(exchange_from_rules("BTCUSDT"))
        self.assertEqual(exchange_from_rules("600519"), "SH")

    def test_invalid_codes_raise_value_error(self) -> None:
        for raw in ("", "   ", None, "sh60051", "6005190", "abc", "60051A", "999999"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_a_share_code(raw)

    def test_is_a_share_code(self) -> None:
        self.assertTrue(is_a_share_code("600519"))
        self.assertTrue(is_a_share_code("sz000001"))
        self.assertTrue(is_a_share_code("600519.SH"))
        self.assertFalse(is_a_share_code("BTCUSDT"))
        self.assertFalse(is_a_share_code("999999"))

    def test_strip_exchange_does_not_validate(self) -> None:
        self.assertEqual(strip_exchange("sh600519"), "600519")
        self.assertEqual(strip_exchange("600519.SZ"), "600519")
        self.assertEqual(strip_exchange("600519"), "600519")
        self.assertEqual(strip_exchange(None), "")

    def test_custom_market_is_kept(self) -> None:
        code = normalize_a_share_code("600519", market="cn")
        self.assertEqual(code.market, "CN")

    def test_a_stock_code_dataclass_is_frozen(self) -> None:
        code = normalize_a_share_code("sh600519")
        self.assertIsInstance(code, AStockCode)
        with self.assertRaises(Exception):
            code.symbol = "000001"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
