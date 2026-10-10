"""除权（息）参考前收盘价：公式、适用条件、验证契约与**安全接入**（Stage 027′-D2/D3）。

本模块只做**纯计算与判定**（纯函数：数值进、结构出）：不联网、不连数据库、不改 schema、
**不接入涨跌停判定**（是否采用某个参考价由调用方决定，见下方"安全接入"）。

公式（沪深交易所：除权除息日参考价）
------------------------------------------------------------------
    参考前收 = (前收盘价 − 每股现金红利) / (1 + 每股送转比例)
    每股现金红利 = 派息(税前,元/10股) / 10；每股送转比例 = (送股+转增)(股/10股) / 10
    结果按最小报价单位（0.01 元）四舍五入。

字段单位（三方来源一致）
------------------------------------------------------------------
    派息 / 送股 / 转增 均为**每 10 股**：新浪分红配股页（HTML）、东方财富 F10
    BonusFinancing（fhyx 方案文本 "10转4派5元"）、东方财富 datacenter RPT_SHAREBONUS_DET
    （数值列 PRETAX_BONUS_RMB / BONUS_RATIO / IT_RATIO）。交叉样本：
    600519 2026-06-26 派 280.2423；001225 2026-09-23 转 4；001207 2026-04-08 转 4 派 5。

支持的事件类型（**不预设所有公司行为都能套用同一公式**）
------------------------------------------------------------------
    仅支持 **EVENT_REGULAR**：现金派息、送股、转增及其组合。
    以下类型**一律不套用本公式**，返回 unknown（`event_type` 默认即 UNKNOWN，调用方须显式声明）：
      - EVENT_RIGHTS_ISSUE：配股（缺完整的配股比例/配股价/适用规则，不猜交易所规则）；
      - EVENT_NON_STANDARD：重整转增、缩股及其他非标准事件；
      - EVENT_UNKNOWN：未声明类型。

验证依据的级别（**区分四类证据**）
------------------------------------------------------------------
    1. 公式计算测试（数值正确性）；
    2. 跨源字段一致性（source_agreement=agree）；
    3. 历史行情交叉验证（除权日行情落在公式预测涨跌停 band 内 → price_band）；
    4. 交易所规则依据（**尚未取得**：未获得交易所公布的参考价/规则文档）。
    结果的 `evidence` 字段如实列出通过了哪几类；**目前不存在第 4 类**，因此
    `status=verified` 仅表示"公式 + 字段 + 行情自洽"，**不构成交易所口径确认**，
    也不因"多源字段一致"或"开盘价接近"就宣称实际适用性已被验证。

安全接入（Stage 027′-D3）
------------------------------------------------------------------
    调用方**必须**通过 `usable_reference_prev_close()`（或先 `is_usable()`）取得参考价：
    只有 `status=verified` 且适用性明确时它才返回值，其余（unknown/conflict/not_applicable）
    一律返回 None，从而阻断把未验证价格用于正式涨跌停计算，并让未知状态沿调用链传递。
    "当日是否无涨跌幅限制"必须是**显式输入或独立验证的规则结果**（参数 `no_price_limit`），
    **禁止**由价格波动幅度反推。

已知未解决案例
------------------------------------------------------------------
    000908 2026-03-11：两来源均为"10转10"，但当日行情与公式 band 冲突（疑似停牌后复牌、
    当日无涨跌幅限制）→ 判 conflict，**不因两来源一致而通过**。
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

#: 事件类型（**默认 UNKNOWN**：不默认按常规除权处理）
EVENT_REGULAR: Final[str] = "regular"            # 现金派息 / 送股 / 转增 及其组合
EVENT_RIGHTS_ISSUE: Final[str] = "rights_issue"  # 配股（不支持）
EVENT_NON_STANDARD: Final[str] = "non_standard"  # 重整转增 / 缩股 / 其它非标准
EVENT_UNKNOWN: Final[str] = "unknown"            # 未声明

EVENT_TYPES: Final[tuple[str, ...]] = (EVENT_REGULAR, EVENT_RIGHTS_ISSUE, EVENT_NON_STANDARD,
                                       EVENT_UNKNOWN)

#: 验证依据类别（evidence 取值）
EVIDENCE_FORMULA: Final[str] = "formula"
EVIDENCE_SOURCE: Final[str] = "source_agreement"
EVIDENCE_PRICE_BAND: Final[str] = "price_band"
#: 交易所规则依据（**目前从未设置**：未取得交易所公布口径）
EVIDENCE_EXCHANGE_RULE: Final[str] = "exchange_rule"

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
    """一次除权除息事件的字段（单位见模块说明；未确认的字段为 None，不是 0）。

    `event_type` **默认 EVENT_UNKNOWN**：调用方必须显式声明 EVENT_REGULAR 才会按常规除权计算；
    配股/非标准事件请分别标记 EVENT_RIGHTS_ISSUE / EVENT_NON_STANDARD（均不套用本公式）。
    """

    code: str | None = None
    ex_date: str | None = None          # 除权除息日
    record_date: str | None = None      # 股权登记日
    cash_per_10: object = None          # 派息（税前，元/每10股）
    bonus_per_10: object = None         # 送股（股/每10股）
    transfer_per_10: object = None      # 转增（股/每10股）
    source: str = "unknown"
    event_type: str = EVENT_UNKNOWN     # regular / rights_issue / non_standard / unknown


@dataclass(frozen=True)
class ExReferenceResult:
    """验证结果契约：**计算结果 / 来源一致性 / 适用性 / 验证状态 / 证据级别**分别表达。

    status            verified（已验证：公式+字段+行情自洽）/ conflict（冲突）/ unknown（无法判定）
    reference_prev_close  计算出的参考前收（None = 未计算/不支持；**不是**默认值）
    applicability      ok / not_applicable / unknown
    source_agreement   agree / single / conflict / not_checked
    evidence           通过的证据类别（formula / source_agreement / price_band；**无** exchange_rule）

    注意：`status=verified` **不等于**交易所口径已确认；只有 `is_usable()` 为真才可用作正式判定输入。
    """

    status: str
    reference_prev_close: Decimal | None
    applicability: str
    source_agreement: str
    formula: str | None
    basis: str
    evidence: tuple[str, ...] = ()


def is_usable(result: ExReferenceResult) -> bool:
    """该结果是否可用于正式（涨跌停）判定：仅 verified 且适用性明确（ok / not_applicable）。"""
    return (
        result.status == STATUS_VERIFIED
        and result.applicability in (APPLICABILITY_OK, APPLICABILITY_NOT_APPLICABLE)
        and result.reference_prev_close is not None
    )


def usable_reference_prev_close(result: ExReferenceResult) -> Decimal | None:
    """**安全访问器**：只有可用时才返回参考前收，否则一律 None。

    用于阻断把 unknown / conflict / not_applicable（或未验证但已算出数值）当作有效价格使用。
    """
    return result.reference_prev_close if is_usable(result) else None


def ex_reference_prev_close(
    prev_close: object,
    *,
    cash_per_10: object = 0,
    bonus_per_10: object = 0,
    transfer_per_10: object = 0,
    tick: Decimal = DEFAULT_TICK,
) -> Decimal | None:
    """纯公式：(前收 − 派息/10) / (1 + (送股+转增)/10)，按报价单位四舍五入。

    这是**纯数值函数**（不做事件类型/适用性判断——那属于 verify_ex_rights_reference）。
    任何输入非法（前收缺失/非正、比例为负、非数值）→ 返回 None（**不返回默认值**）。
    参数单位均为**每 10 股**；不传表示 0。
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
    """校验"除权参考价公式 + 字段 + 行情"是否自洽，并给出可用性结论。

    只有 `event_type=EVENT_REGULAR` 才会计算；配股/非标准/未声明类型 → unknown（**不套用公式**）。
    缺前置条件 → unknown；来源字段冲突 → conflict；当日无涨跌幅限制 → not_applicable + unknown；
    行情落在公式预测 band 之外 → conflict（不通过）。不接入涨跌停判定、不写库、不猜缺值。
    """
    if source_agreement not in (SOURCE_AGREE, SOURCE_SINGLE, SOURCE_CONFLICT, SOURCE_NOT_CHECKED):
        raise ValueError(f"未知的 source_agreement：{source_agreement!r}")
    if fields.event_type not in EVENT_TYPES:
        raise ValueError(f"未知的 event_type：{fields.event_type!r}")

    if fields.event_type != EVENT_REGULAR:
        reason = {
            EVENT_RIGHTS_ISSUE: "配股：缺少完整的配股比例/配股价/适用规则，不套用常规除权公式",
            EVENT_NON_STANDARD: "非标准事件（重整转增/缩股等）：不按常规除权处理",
            EVENT_UNKNOWN: "未声明事件类型：不默认按常规除权处理",
        }.get(fields.event_type, "不支持的事件类型")
        return ExReferenceResult(STATUS_UNKNOWN, None, APPLICABILITY_UNKNOWN, source_agreement,
                                 None, reason, ())

    cash = fields.cash_per_10
    bonus = fields.bonus_per_10
    transfer = fields.transfer_per_10
    has_any = any(v not in (None, 0, "0") for v in (cash, bonus, transfer))

    if not has_any:
        base = _to_decimal(prev_close)
        if base is None or base <= 0:
            return ExReferenceResult(STATUS_UNKNOWN, None, APPLICABILITY_UNKNOWN, source_agreement,
                                     None, "无公司行为，但前收盘价缺失或非法：无法给出参考前收", ())
        return ExReferenceResult(STATUS_VERIFIED, round_to_tick(base), APPLICABILITY_NOT_APPLICABLE,
                                 source_agreement, None, "无公司行为：参考前收 = 前收盘价（恒等）", ())

    ref = ex_reference_prev_close(prev_close, cash_per_10=cash, bonus_per_10=bonus,
                                  transfer_per_10=transfer)
    if ref is None:
        return ExReferenceResult(STATUS_UNKNOWN, None, APPLICABILITY_UNKNOWN, source_agreement,
                                 FORMULA_EX_RIGHTS, "前收缺失/非法或字段非法（负值、非数值）：不计算", ())

    evidence: list[str] = [EVIDENCE_FORMULA]
    if source_agreement in (SOURCE_AGREE, SOURCE_SINGLE):
        evidence.append(EVIDENCE_SOURCE)

    if source_agreement == SOURCE_CONFLICT:
        return ExReferenceResult(STATUS_CONFLICT, ref, APPLICABILITY_UNKNOWN, SOURCE_CONFLICT,
                                 FORMULA_EX_RIGHTS, "来源之间字段不一致：结果为冲突，需人工核查",
                                 tuple(evidence))

    if no_price_limit:
        return ExReferenceResult(STATUS_UNKNOWN, ref, APPLICABILITY_NOT_APPLICABLE, source_agreement,
                                 FORMULA_EX_RIGHTS,
                                 "当日无涨跌幅限制（如停牌后复牌首日）：band 校验不适用，参考价仅供参考",
                                 tuple(evidence))

    high = _to_decimal(ex_bar_high)
    low = _to_decimal(ex_bar_low)
    if high is None or low is None or limit_ratio is None:
        return ExReferenceResult(STATUS_UNKNOWN, ref, APPLICABILITY_UNKNOWN, source_agreement,
                                 FORMULA_EX_RIGHTS, "缺少除权日行情或涨跌幅比例：不做 band 校验",
                                 tuple(evidence))

    if _band_contains(ref, limit_ratio, high, low):
        return ExReferenceResult(STATUS_VERIFIED, ref, APPLICABILITY_OK, source_agreement,
                                 FORMULA_EX_RIGHTS, f"行情 [{low}, {high}] 落在公式预测 band 内",
                                 (*evidence, EVIDENCE_PRICE_BAND))
    return ExReferenceResult(STATUS_CONFLICT, ref, APPLICABILITY_UNKNOWN, source_agreement,
                             FORMULA_EX_RIGHTS,
                             f"行情 [{low}, {high}] 落在公式预测 band 之外：疑似特殊事件（复牌/重整/缩股）"
                             "或数据问题，需人工核查，不判定通过",
                             tuple(evidence))


__all__ = [
    "APPLICABILITY_NOT_APPLICABLE",
    "APPLICABILITY_OK",
    "APPLICABILITY_UNKNOWN",
    "EVENT_NON_STANDARD",
    "EVENT_REGULAR",
    "EVENT_RIGHTS_ISSUE",
    "EVENT_TYPES",
    "EVENT_UNKNOWN",
    "EVIDENCE_EXCHANGE_RULE",
    "EVIDENCE_FORMULA",
    "EVIDENCE_PRICE_BAND",
    "EVIDENCE_SOURCE",
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
    "is_usable",
    "usable_reference_prev_close",
    "verify_ex_rights_reference",
]
