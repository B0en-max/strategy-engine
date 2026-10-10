"""Stage 027′-D2：除权参考前收公式与验证契约测试（market.rules.ex_rights）。

覆盖：公式与单位换算、恒等（无公司行为）、缺失/非法输入、来源冲突、无涨跌幅限制、
band 边界、以及此前已验证样本的回归（含未解决案例 000908）。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from market.rules.ex_rights import (  # noqa: E402
    APPLICABILITY_NOT_APPLICABLE,
    APPLICABILITY_OK,
    APPLICABILITY_UNKNOWN,
    SOURCE_AGREE,
    SOURCE_CONFLICT,
    STATUS_CONFLICT,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    ExRightsFields,
    ex_reference_prev_close,
    verify_ex_rights_reference,
)


class FormulaTest(unittest.TestCase):
    def test_pure_cash(self) -> None:
        # 600519 2026-06-26：前收 1212.1，派息 280.242 元/10股 → 1212.1 − 28.0242
        self.assertEqual(ex_reference_prev_close("1212.1", cash_per_10="280.242"), Decimal("1184.08"))

    def test_cash_unit_is_per_10_shares(self) -> None:
        # 若误当"每股"，结果会是 1212.1 − 280.242 = 931.86；断言不是它
        ref = ex_reference_prev_close("1212.1", cash_per_10="280.242")
        self.assertEqual(ref, Decimal("1184.08"))
        self.assertNotEqual(ref, Decimal("931.86"))

    def test_pure_transfer(self) -> None:
        # 001225 2026-09-23：前收 50.92，转增 4 股/10股 → 50.92 / 1.4
        self.assertEqual(ex_reference_prev_close("50.92", transfer_per_10="4"), Decimal("36.37"))

    def test_pure_bonus(self) -> None:
        # 送股同转增口径：前收 10，送 3 股/10股 → 10 / 1.3
        self.assertEqual(ex_reference_prev_close("10", bonus_per_10="3"), Decimal("7.69"))

    def test_combined(self) -> None:
        # 001207 2026-04-08：前收 29.6，派 5 元/10股、转 4 股/10股 → (29.6−0.5)/1.4
        self.assertEqual(ex_reference_prev_close("29.6", cash_per_10="5", transfer_per_10="4"),
                         Decimal("20.79"))

    def test_no_corporate_action_is_identity(self) -> None:
        self.assertEqual(ex_reference_prev_close("12.34"), Decimal("12.34"))
        self.assertEqual(ex_reference_prev_close("12.34", cash_per_10=0, bonus_per_10=0,
                                                 transfer_per_10=0), Decimal("12.34"))

    def test_invalid_inputs_return_none(self) -> None:
        for prev in (None, 0, -1, "abc", "", float("nan")):
            self.assertIsNone(ex_reference_prev_close(prev), f"prev={prev!r}")
        self.assertIsNone(ex_reference_prev_close("10", cash_per_10=-1))
        self.assertIsNone(ex_reference_prev_close("10", transfer_per_10="-11"))  # 1+ratio<=0
        self.assertIsNone(ex_reference_prev_close("10", transfer_per_10="abc"))

    def test_rounding_half_up(self) -> None:
        # 结果按最小报价单位四舍五入（ROUND_HALF_UP，非银行家舍入）
        self.assertEqual(ex_reference_prev_close("10.045"), Decimal("10.05"))
        self.assertEqual(ex_reference_prev_close("36.375"), Decimal("36.38"))


class VerifyContractTest(unittest.TestCase):
    def test_verified_band_contains(self) -> None:
        fields = ExRightsFields(code="001225", ex_date="2026-09-23", transfer_per_10="4")
        result = verify_ex_rights_reference(prev_close="50.92", fields=fields,
                                            ex_bar_high="37.8", ex_bar_low="36.37",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_VERIFIED)
        self.assertEqual(result.applicability, APPLICABILITY_OK)
        self.assertEqual(result.reference_prev_close, Decimal("36.37"))
        self.assertEqual(result.source_agreement, SOURCE_AGREE)

    def test_000908_remains_unresolved_conflict(self) -> None:
        # 两来源字段一致（10转10），但当日行情与公式 band 冲突 → 必须判 conflict，不通过
        fields = ExRightsFields(code="000908", ex_date="2026-03-11", transfer_per_10="10")
        result = verify_ex_rights_reference(prev_close="6.37", fields=fields,
                                            ex_bar_high="4.58", ex_bar_low="4.58",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_CONFLICT)
        self.assertEqual(result.applicability, APPLICABILITY_UNKNOWN)
        self.assertEqual(result.reference_prev_close, Decimal("3.19"))
        self.assertIn("band", result.basis)

    def test_source_conflict_forces_conflict(self) -> None:
        fields = ExRightsFields(code="X", ex_date="2026-01-01", transfer_per_10="4")
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            ex_bar_high="7.5", ex_bar_low="7.0",
                                            limit_ratio=Decimal("0.10"),
                                            source_agreement=SOURCE_CONFLICT)
        self.assertEqual(result.status, STATUS_CONFLICT)

    def test_no_price_limit_is_not_applicable(self) -> None:
        fields = ExRightsFields(code="000908", ex_date="2026-03-11", transfer_per_10="10")
        result = verify_ex_rights_reference(prev_close="6.37", fields=fields,
                                            ex_bar_high="4.58", ex_bar_low="4.58",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE,
                                            no_price_limit=True)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.applicability, APPLICABILITY_NOT_APPLICABLE)
        self.assertEqual(result.reference_prev_close, Decimal("3.19"))  # 计算给出，但未验证

    def test_missing_prev_close_is_unknown(self) -> None:
        fields = ExRightsFields(code="X", ex_date="2026-01-01", transfer_per_10="4")
        result = verify_ex_rights_reference(prev_close=None, fields=fields)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.reference_prev_close)

    def test_missing_bar_cannot_verify(self) -> None:
        fields = ExRightsFields(code="X", ex_date="2026-01-01", transfer_per_10="4")
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.reference_prev_close, Decimal("7.14"))  # 10/1.4

    def test_no_corporate_action_is_verified_identity(self) -> None:
        fields = ExRightsFields(code="X", ex_date="2026-01-01")
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            ex_bar_high="10.5", ex_bar_low="9.5",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_VERIFIED)
        self.assertEqual(result.applicability, APPLICABILITY_NOT_APPLICABLE)
        self.assertEqual(result.reference_prev_close, Decimal("10"))

    def test_bad_source_agreement_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            verify_ex_rights_reference(prev_close="10", fields=ExRightsFields(), source_agreement="bogus")


#: 此前 25 样本验证中抽出的代表样本（前收 / 派息-10股 / 送-10股 / 转-10股 / 除权日 H / L / 板比例 / 期望）
SAMPLES = [
    ("600519", "1212.1", "280.242", None, None, "1199.0", "1168.1", "0.10", STATUS_VERIFIED),
    ("001225", "50.92", None, None, "4", "37.80", "36.37", "0.10", STATUS_VERIFIED),
    ("001207", "29.6", "5", None, "4", "21.59", "20.81", "0.10", STATUS_VERIFIED),
    ("000990", "9.08", None, None, "4", "6.63", "6.37", "0.10", STATUS_VERIFIED),
    ("001230", "27.07", "3.8", None, "4", "19.49", "18.66", "0.10", STATUS_VERIFIED),
    ("000034", "41.57", "0.73", None, "4", "31.10", "29.24", "0.10", STATUS_VERIFIED),
    ("000908", "6.37", None, None, "10", "4.58", "4.58", "0.10", STATUS_CONFLICT),
]


class SampleRegressionTest(unittest.TestCase):
    def test_samples(self) -> None:
        for code, prev, cash, bonus, transfer, high, low, ratio, expected in SAMPLES:
            fields = ExRightsFields(code=code, cash_per_10=cash, bonus_per_10=bonus,
                                    transfer_per_10=transfer)
            result = verify_ex_rights_reference(prev_close=prev, fields=fields,
                                                ex_bar_high=high, ex_bar_low=low,
                                                limit_ratio=Decimal(ratio),
                                                source_agreement=SOURCE_AGREE)
            self.assertEqual(result.status, expected, f"{code}: {result.basis}")


if __name__ == "__main__":
    unittest.main()
