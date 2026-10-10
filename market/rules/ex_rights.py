"""除权（息）参考前收盘价：公式、适用条件与验证契约（Stage 027′-D2）。

本模块只做**纯计算与判定**（纯函数：数值进、结构出）：不联网、不连数据库、不改 schema、
**不接入涨跌停判定**（是否采用某个参考价由调用方决定）。

公式（沪深交易所：除权除息日参考价）
------------------------------------------------------------------
    参考前收 = (前收盘价 − 每股现金红利) / (1 + 每股送转比例)
    其中：每股现金红利 = 派息(税前,元/10股) / 10
          每股送转比例 = (送股(股/10股) + 转增(股/10股)) / 10
    结果按最小报价单位（0.01 元）四舍五入——与交易所公布口径一致。

字段单位（三方来源一致，见下）
------------------------------------------------------------------
    派息 / 送股 / 转增 均为**每 10 股**：
      新浪分红配股页（vISSUE_ShareBonus，HTML）
      东方财富 F10 BonusFinancing（fhyx，方案文本 "10转4派5元"）
      东方财富 datacenter RPT_SHAREBONUS_DET（数值列 PRETAX_BONUS_RMB / BONUS_RATIO / IT_RATIO）
    交叉验证样本（2025-04~2026-09 窗口内，三者除权日/登记日/送转/派现一致）：
      600519 2026-06-26 派 280.2423；001225 2026-09-23 转 4；001207 2026-04-08 转 4 派 5。

适用条件（**不预设所有公司行为都能套用同一公式**）
------------------------------------------------------------------
    公式只适用于"常规除权除息"，且需要：前收盘价可用、除权日有行情、当日受常规涨跌幅限制。
    以下情形**不得**套用，一律按 unknown / conflict 处理（不猜）：
      - 缺前收 / 字段缺失 / 除权日不明；
      - 当日无涨跌幅限制（停牌后复牌首日等）→ 参考价与涨跌停 band 校验不适用；
      - 特殊/非标准事件（重整转增、缩股、定向转增用于偿债等）；
      - 来源之间字段不一致 → conflict；
      - 行情明显落在公式预测 band 之外 → conflict（可能是特殊事件或数据问题，需人工核查）。
    已知未解决案例：000908 2026-03-11（两来源均为"10转10"，但当日行情与公式 band 冲突，
    疑似停牌后复牌、当日无涨跌幅限制）→ 本模块判 conflict，**不因两来源一致就通过**。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from .limits import DEFAULT_TICK, round_to_tick

# --------------------------------------------------------------------------- 状态
STATUS_VERIFIED: Final[str] = "verified"
STATUS_CONFLICT: Final[str] = "conflict"
STATUS_UNKNOWN: Final[str] = "unknown"

#: 适用性（与验证状态分开表达）
APPLICABILITY_OK: Final[str] = "ok"
APPLICABILITY_NOT_APPLICABLE: Final[str] = "not_applicable"
APPLICABILITY_UNKNOWN: Final[str] = "unknown"

#: 来源一致性（与计算结果分开表达）
SOURCE_AGREE: Final[str] = "agree"
SOURCE_SINGLE: Final[str] = "single"
SOURCE_CONFLICT: Final[str] = "conflict"
SOURCE_NOT_CHECKED: Final[str] = "not_checked"

#: 字段单位：每 10 股
PER_10: Final[Decimal] = Decimal("10")
FORMULA_EX_RIGHTS: Final[str] = (
    "参考前收 = (前收盘价 − 派息/10) / (1 + (送股+转增)/10)，派息/送股/转增均为每10股"
)


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if result != result or result.is_infinite():  # NaN / inf
        return None
    return result


@dataclass(frozen=True)
class ExRightsFields:
    """一次除权除息事件的字段（单位见模块说明；未确认的字段为 None，不是 0）。"""

    code: str | None = None
    ex_date: str | None = None          # 除权除息日
    record_date: str | None = None      # 股权登记日
    cash_per_10: object = None          # 派息（税前，元/每10股）
    bonus_per_10: object = None         # 送股（股/每10股）
    transfer_per_10: object = None      # 转增（股/每10股）
    source: str = "unknown"


@dataclass(frozen=True)
class ExReferenceResult:
    """验证结果契约：**计算结果 / 来源一致性 / 适用性 / 验证状态**分别表达。

    status            verified（已验证）/ conflict（冲突）/ unknown（无法判定）
    reference_prev_close  计算出的参考前收（None = 未计算/不可用，**绝不是默认值**）
    applicability      ok / not_applicable / unknown
    source_agreement   agree / single / conflict / not_checked
    """

    status: str
    reference_prev_close: Decimal | None
    applicability: str
    source_agreement: str
    formula: str | None
    basis: str


def ex_reference_prev_close(
    prev_close: object,
    *,
    cash_per_10: object = 0,
    bonus_per_10: object = 0,
    transfer_per_10: object = 0,
    tick: Decimal = DEFAULT_TICK,
) -> Decimal | None:
    """纯公式：(前收 − 派息/10) / (1 + (送股+转增)/10)，按报价单位四舍五入。

    任何输入非法（前收缺失/非正、比例为负、非数值）→ 返回 None（**不返回默认值**）。
    参数单位均为**每 10 股**；不传表示 0（无该类公司行为）。
    """
    base = _to_decimal(prev_close)
    if base is None or base <= 0:
        return None
    cash = _to_decimal(cash_per_10) if cash_per_10 not in (None, 0) else Decimal("0")
    bonus = _to_decimal(bonus_per_10) if bonus_per_10 not in (None, 0) else Decimal("0")
    transfer = _to_decimal(transfer_per_10) if transfer_per_10 not in (None, 0) else Decimal("0")
    if cash is None or bonus is None or transfer is None:
        return None
    if cash < 0 or bonus < 0 or transfer < 0:
        return None
    ratio = (bonus + transfer) / PER_10
    if ratio <= Decimal("-1"):
        return None
    value = (base - cash / PER_10) / (Decimal("1") + ratio)
    if value <= 0:
        return None
    return round_to_tick(value, tick)


def _band_contains(ref: Decimal, ratio: Decimal, high: Decimal, low: Decimal) -> bool:
    tol = max(Decimal("0.02"), ref * Decimal("0.005"))
    upper = round_to_tick(ref * (Decimal("1") + ratio))
    lower = round_to_tick(ref * (Decimal("1") - ratio))
    return low >= lower - tol and high <= upper + tol


def verify_ex_rights_reference(
    *,
    prev_close: object,
    fields: ExRightsFields,
    ex_bar_high: object | None = None,
    ex_bar_low: object | None = None,
    limit_ratio: Decimal | None = None,
    source_agreement: str = SOURCE_NOT_CHECKED,
    no_price_limit: bool = False,
) -> ExReferenceResult:
    """校验"除权参考价公式 + 字段 + 行情"是否自洽。

    任一前置条件缺失 → unknown；来源冲突 → conflict；行情与预测 band 不一致 → conflict。
    不接入涨跌停判定、不写库、不猜缺值。
    """
    if source_agreement not in (SOURCE_AGREE, SOURCE_SINGLE, SOURCE_CONFLICT, SOURCE_NOT_CHECKED):
        raise ValueError(f"未知的 source_agreement：{source_agreement!r}")

    cash = fields.cash_per_10
    bonus = fields.bonus_per_10
    transfer = fields.transfer_per_10
    has_any = any(v not in (None, 0, "0") for v in (cash, bonus, transfer))

    if not has_any:
        base = _to_decimal(prev_close)
        if base is None or base <= 0:
            return ExReferenceResult(STATUS_UNKNOWN, None, APPLICABILITY_UNKNOWN, source_agreement,
                                     None, "无公司行为，但前收盘价缺失或非法：无法给出参考前收")
        return ExReferenceResult(STATUS_VERIFIED, round_to_tick(base), APPLICABILITY_NOT_APPLICABLE,
                                 source_agreement, None, "无公司行为：参考前收 = 前收盘价（恒等）")

    ref = ex_reference_prev_close(prev_close, cash_per_10=cash, bonus_per_10=bonus,
                                  transfer_per_10=transfer)
    if ref is None:
        return ExReferenceResult(STATUS_UNKNOWN, None, APPLICABILITY_UNKNOWN, source_agreement,
                                 FORMULA_EX_RIGHTS, "前收缺失/非法或字段非法（负值、非数值）：不计算")

    if source_agreement == SOURCE_CONFLICT:
        return ExReferenceResult(STATUS_CONFLICT, ref, APPLICABILITY_UNKNOWN, SOURCE_CONFLICT,
                                 FORMULA_EX_RIGHTS, "来源之间字段不一致：结果为冲突，需人工核查")

    if no_price_limit:
        return ExReferenceResult(STATUS_UNKNOWN, ref, APPLICABILITY_NOT_APPLICABLE, source_agreement,
                                 FORMULA_EX_RIGHTS,
                                 "当日无涨跌幅限制（如停牌后复牌首日）：band 校验不适用，参考价仅供参考")

    high = _to_decimal(ex_bar_high)
    low = _to_decimal(ex_bar_low)
    if high is None or low is None or limit_ratio is None:
        return ExReferenceResult(STATUS_UNKNOWN, ref, APPLICABILITY_UNKNOWN, source_agreement,
                                 FORMULA_EX_RIGHTS, "缺少除权日行情或涨跌幅比例：不做 band 校验")

    if _band_contains(ref, limit_ratio, high, low):
        return ExReferenceResult(STATUS_VERIFIED, ref, APPLICABILITY_OK, source_agreement,
                                 FORMULA_EX_RIGHTS, f"行情 [{low}, {high}] 落在公式预测 band 内")
    return ExReferenceResult(STATUS_CONFLICT, ref, APPLICABILITY_UNKNOWN, source_agreement,
                             FORMULA_EX_RIGHTS,
                             f"行情 [{low}, {high}] 落在公式预测 band 之外：疑似特殊事件（复牌/重整/缩股）"
                             "或数据问题，需人工核查，不判定通过")


__all__ = [
    "APPLICABILITY_NOT_APPLICABLE",
    "APPLICABILITY_OK",
    "APPLICABILITY_UNKNOWN",
    "ExReferenceResult",
    "ExRightsFields",
    "FORMULA_EX_RIGHTS",
    "PER_10",
    "SOURCE_AGREE",
    "SOURCE_CONFLICT",
    "SOURCE_NOT_CHECKED",
    "SOURCE_SINGLE",
    "STATUS_CONFLICT",
    "STATUS_UNKNOWN",
    "STATUS_VERIFIED",
    "ex_reference_prev_close",
    "verify_ex_rights_reference",
]
