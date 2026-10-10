"""Stage 027′-D2/D3：除权参考前收公式与验证契约测试（market.rules.ex_rights）。

覆盖：公式与单位换算、恒等、缺失/非法输入、来源冲突、无涨跌幅限制、band 边界、
事件类型（配股/非标准/未声明 → unknown）、证据级别、安全访问器边界、
此前已验证样本回归（含未解决案例 000908）。
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
    EVENT_NON_STANDARD,
    EVENT_REGULAR,
    EVENT_RIGHTS_ISSUE,
    EVIDENCE_EXCHANGE_RULE,
    EVIDENCE_FORMULA,
    EVIDENCE_PRICE_BAND,
    EVIDENCE_SOURCE,
    SOURCE_AGREE,
    SOURCE_CONFLICT,
    STATUS_CONFLICT,
    STATUS_UNKNOWN,
    STATUS_VERIFIED,
    ExRightsFields,
    ex_reference_prev_close,
    is_usable,
    usable_reference_prev_close,
    verify_ex_rights_reference,
)


class FormulaTest(unittest.TestCase):
    def test_pure_cash(self) -> None:
        # 600519 2026-06-26：前收 1212.1，派息 280.242 元/10股 → 1212.1 − 28.0242
        self.assertEqual(ex_reference_prev_close("1212.1", cash_per_10="280.242"), Decimal("1184.08"))

    def test_cash_unit_is_per_10_shares(self) -> None:
        ref = ex_reference_prev_close("1212.1", cash_per_10="280.242")
        self.assertEqual(ref, Decimal("1184.08"))
        self.assertNotEqual(ref, Decimal("931.86"))  # 若误当"每股"会得到这个

    def test_pure_transfer_and_bonus(self) -> None:
        self.assertEqual(ex_reference_prev_close("50.92", transfer_per_10="4"), Decimal("36.37"))
        self.assertEqual(ex_reference_prev_close("10", bonus_per_10="3"), Decimal("7.69"))

    def test_combined(self) -> None:
        self.assertEqual(ex_reference_prev_close("29.6", cash_per_10="5", transfer_per_10="4"),
                         Decimal("20.79"))

    def test_no_corporate_action_is_identity(self) -> None:
        self.assertEqual(ex_reference_prev_close("12.34"), Decimal("12.34"))

    def test_invalid_inputs_return_none(self) -> None:
        for prev in (None, 0, -1, "abc", "", float("nan")):
            self.assertIsNone(ex_reference_prev_close(prev), f"prev={prev!r}")
        self.assertIsNone(ex_reference_prev_close("10", cash_per_10=-1))
        self.assertIsNone(ex_reference_prev_close("10", transfer_per_10="-11"))
        self.assertIsNone(ex_reference_prev_close("10", transfer_per_10="abc"))

    def test_rounding_half_up(self) -> None:
        self.assertEqual(ex_reference_prev_close("10.045"), Decimal("10.05"))
        self.assertEqual(ex_reference_prev_close("36.375"), Decimal("36.38"))


class EventTypeGateTest(unittest.TestCase):
    """审计补齐：不支持/未声明的事件类型不得套用常规除权公式。"""

    def test_rights_issue_is_unknown_and_no_value(self) -> None:
        fields = ExRightsFields(code="X", cash_per_10="1", event_type=EVENT_RIGHTS_ISSUE)
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            ex_bar_high="10.5", ex_bar_low="9.5",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.reference_prev_close)  # 不套用公式
        self.assertEqual(result.applicability, APPLICABILITY_UNKNOWN)

    def test_non_standard_is_unknown(self) -> None:
        fields = ExRightsFields(code="X", transfer_per_10="10", event_type=EVENT_NON_STANDARD)
        result = verify_ex_rights_reference(prev_close="10", fields=fields)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.reference_prev_close)

    def test_undeclared_event_type_defaults_to_unknown(self) -> None:
        # 默认 event_type 是 UNKNOWN → 不得默认按常规除权处理
        fields = ExRightsFields(code="X", transfer_per_10="4")
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            ex_bar_high="7.5", ex_bar_low="7.0",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.reference_prev_close)

    def test_unknown_event_type_string_raises(self) -> None:
        with self.assertRaises(ValueError):
            verify_ex_rights_reference(prev_close="10",
                                       fields=ExRightsFields(event_type="dividend_magic"))


class VerifyContractTest(unittest.TestCase):
    def test_verified_band_contains(self) -> None:
        fields = ExRightsFields(code="001225", ex_date="2026-09-23", transfer_per_10="4",
                                event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close="50.92", fields=fields,
                                            ex_bar_high="37.8", ex_bar_low="36.37",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_VERIFIED)
        self.assertEqual(result.applicability, APPLICABILITY_OK)
        self.assertEqual(result.reference_prev_close, Decimal("36.37"))
        self.assertEqual(result.source_agreement, SOURCE_AGREE)
        self.assertIn(EVIDENCE_FORMULA, result.evidence)
        self.assertIn(EVIDENCE_SOURCE, result.evidence)
        self.assertIn(EVIDENCE_PRICE_BAND, result.evidence)
        self.assertNotIn(EVIDENCE_EXCHANGE_RULE, result.evidence)  # 无交易所规则依据

    def test_000908_remains_unresolved_conflict(self) -> None:
        fields = ExRightsFields(code="000908", ex_date="2026-03-11", transfer_per_10="10",
                                event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close="6.37", fields=fields,
                                            ex_bar_high="4.58", ex_bar_low="4.58",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_CONFLICT)
        self.assertEqual(result.applicability, APPLICABILITY_UNKNOWN)
        self.assertEqual(result.reference_prev_close, Decimal("3.19"))
        self.assertIn("band", result.basis)

    def test_source_conflict_forces_conflict(self) -> None:
        fields = ExRightsFields(code="X", transfer_per_10="4", event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            ex_bar_high="7.5", ex_bar_low="7.0",
                                            limit_ratio=Decimal("0.10"),
                                            source_agreement=SOURCE_CONFLICT)
        self.assertEqual(result.status, STATUS_CONFLICT)

    def test_no_price_limit_is_not_applicable(self) -> None:
        fields = ExRightsFields(code="000908", transfer_per_10="10", event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close="6.37", fields=fields,
                                            ex_bar_high="4.58", ex_bar_low="4.58",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE,
                                            no_price_limit=True)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.applicability, APPLICABILITY_NOT_APPLICABLE)
        self.assertEqual(result.reference_prev_close, Decimal("3.19"))  # 计算给出但未验证

    def test_missing_prev_close_is_unknown(self) -> None:
        fields = ExRightsFields(transfer_per_10="4", event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close=None, fields=fields)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertIsNone(result.reference_prev_close)

    def test_missing_bar_cannot_verify(self) -> None:
        fields = ExRightsFields(transfer_per_10="4", event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_UNKNOWN)
        self.assertEqual(result.reference_prev_close, Decimal("7.14"))  # 10/1.4

    def test_no_corporate_action_is_verified_identity(self) -> None:
        fields = ExRightsFields(event_type=EVENT_REGULAR)
        result = verify_ex_rights_reference(prev_close="10", fields=fields,
                                            ex_bar_high="10.5", ex_bar_low="9.5",
                                            limit_ratio=Decimal("0.10"), source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_VERIFIED)
        self.assertEqual(result.applicability, APPLICABILITY_NOT_APPLICABLE)
        self.assertEqual(result.reference_prev_close, Decimal("10"))

    def test_bad_source_agreement_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            verify_ex_rights_reference(prev_close="10", fields=ExRightsFields(), source_agreement="bogus")


class SafeAccessorTest(unittest.TestCase):
    """审计补齐：unknown / conflict / not_applicable 必须阻断参考价被当作有效价格使用。"""

    def _regular(self, **kw) -> ExRightsFields:
        return ExRightsFields(event_type=EVENT_REGULAR, **kw)

    def test_verified_is_usable(self) -> None:
        result = verify_ex_rights_reference(
            prev_close="50.92", fields=self._regular(transfer_per_10="4"),
            ex_bar_high="37.8", ex_bar_low="36.37", limit_ratio=Decimal("0.10"),
            source_agreement=SOURCE_AGREE)
        self.assertTrue(is_usable(result))
        self.assertEqual(usable_reference_prev_close(result), Decimal("36.37"))

    def test_not_applicable_value_is_blocked(self) -> None:
        result = verify_ex_rights_reference(
            prev_close="6.37", fields=self._regular(transfer_per_10="10"),
            ex_bar_high="4.58", ex_bar_low="4.58", limit_ratio=Decimal("0.10"),
            source_agreement=SOURCE_AGREE, no_price_limit=True)
        self.assertIsNotNone(result.reference_prev_close)   # 计算值仍在（供诊断）
        self.assertFalse(is_usable(result))
        self.assertIsNone(usable_reference_prev_close(result))  # 但被阻断

    def test_conflict_value_is_blocked(self) -> None:
        result = verify_ex_rights_reference(
            prev_close="6.37", fields=self._regular(transfer_per_10="10"),
            ex_bar_high="4.58", ex_bar_low="4.58", limit_ratio=Decimal("0.10"),
            source_agreement=SOURCE_AGREE)
        self.assertEqual(result.status, STATUS_CONFLICT)
        self.assertIsNone(usable_reference_prev_close(result))

    def test_unsupported_event_value_is_none_and_blocked(self) -> None:
        result = verify_ex_rights_reference(
            prev_close="10", fields=ExRightsFields(transfer_per_10="4",
                                                   event_type=EVENT_RIGHTS_ISSUE))
        self.assertIsNone(result.reference_prev_close)
        self.assertIsNone(usable_reference_prev_close(result))


#: 此前 25 样本验证中抽出的代表样本（代码 / 前收 / 派息-10 / 送-10 / 转-10 / H / L / 板比例 / 期望）
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
                                    transfer_per_10=transfer, event_type=EVENT_REGULAR)
            result = verify_ex_rights_reference(prev_close=prev, fields=fields,
                                                ex_bar_high=high, ex_bar_low=low,
                                                limit_ratio=Decimal(ratio),
                                                source_agreement=SOURCE_AGREE)
            self.assertEqual(result.status, expected, f"{code}: {result.basis}")


if __name__ == "__main__":
    unittest.main()
