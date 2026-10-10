"""Stage 027′：数据契约测试（market.rules.contract）。

关键不变式：**缺失默认 unknown**；只有拿到外部证据才允许升级到具体状态。
（更细的数据质量统计属于后续阶段，本文件只锁契约行为。）
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market.rules import contract  # noqa: E402


class ClassifyMissingTest(unittest.TestCase):
    def test_non_trading_day(self) -> None:
        self.assertEqual(
            contract.classify_missing(is_trading_day=False, has_bar=False), contract.MISSING_NOT_TRADING_DAY
        )

    def test_present(self) -> None:
        self.assertEqual(contract.classify_missing(is_trading_day=True, has_bar=True), contract.MISSING_PRESENT)

    def test_pre_listing_requires_listing_date(self) -> None:
        self.assertEqual(
            contract.classify_missing(is_trading_day=True, has_bar=False,
                                      listing_date="2026-09-20", bar_date="2026-09-10"),
            contract.MISSING_PRE_LISTING,
        )
        # 没有上市日期时不得推断为上市前
        self.assertEqual(
            contract.classify_missing(is_trading_day=True, has_bar=False, bar_date="2026-09-10"),
            contract.MISSING_UNKNOWN,
        )

    def test_missing_defaults_to_unknown(self) -> None:
        # 仅"没有 K 线"既不能证明停牌，也不能证明数据源缺失
        self.assertEqual(
            contract.classify_missing(is_trading_day=True, has_bar=False), contract.MISSING_UNKNOWN
        )

    def test_unknown_only_upgraded_with_external_evidence(self) -> None:
        self.assertEqual(
            contract.classify_missing(is_trading_day=True, has_bar=False, suspension_evidence=True),
            contract.MISSING_SUSPENDED,
        )
        self.assertEqual(
            contract.classify_missing(is_trading_day=True, has_bar=False, source_gap_evidence=True),
            contract.MISSING_SOURCE_GAP,
        )

    def test_pre_listing_takes_precedence(self) -> None:
        status = contract.classify_missing(
            is_trading_day=True, has_bar=False, listing_date="2026-09-20", bar_date="2026-09-01",
            suspension_evidence=True,
        )
        self.assertEqual(status, contract.MISSING_PRE_LISTING)

    def test_universe_and_price_basis_constants_are_explicit(self) -> None:
        self.assertNotEqual(contract.UNIVERSE_CURRENT_SNAPSHOT, contract.UNIVERSE_HISTORICAL)
        self.assertEqual(contract.PRICE_BASIS_UNADJUSTED, "unadjusted")
        self.assertIn("幸存者偏差", contract.UNIVERSE_WARNING)
        self.assertIn("不复权", contract.PRICE_BASIS_UNADJUSTED_NOTE)


if __name__ == "__main__":
    unittest.main()
