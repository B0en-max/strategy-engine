"""A 股涨跌停价格判定（Stage 027′ Data Foundation）——**纯函数，无 IO / 无数据库 / 无网络**。

设计原则（与项目「不猜数据」的一致约定）
------------------------------------------------------------------
1. 只做**可判定**的计算，判不了就返回 `unknown` + 原因，绝不猜：
   - 板块未知 → unknown
   - 前收盘价缺失/非法 → unknown
   - 除权除息日（或任何公司行为日）→ unknown，除非调用方明确声明
     "前收盘价 = 交易所公布的参考前收盘价"
   - 该交易日没有 K 线 → unknown（**不推断为"停牌"，也不推断为"未涨停"**）
2. **不把前复权价格当真实涨跌停价**：`prev_close_is_exchange_reference=False`
   表示调用方用的是行情序列里的前一根收盘（可能是前复权/不复权），此时若当日
   存在公司行为，结果不可信 → unknown。
3. 不自己判断板块：板块必须由调用方给出（可复用 `market.data.boards.classify_board`）。
4. 金额运算一律用 `Decimal` + 四舍五入（ROUND_HALF_UP）到最小报价单位，
   不用二进制浮点（避免 2.675 之类边界误差）。

制度依据（比例与生效日期属**制度信息**，正式使用前应以交易所公告复核）
------------------------------------------------------------------
    沪市主板 / 深市主板          ±10%      （ST、*ST ±5%）
    创业板（300/301/302）        ±20%      （含 ST；2020-08-24 起）
    科创板（688/689）            ±20%      （含 *ST；ST 不改变比例，仍 20%）
    北交所（430/830-879/920）    ±30%      （含 *ST；ST 不改变比例，仍 30%）
    新股无涨跌幅限制阶段          无限制    （创业板/科创板，以及 2023-04-10 起
                                            主板注册制新股，上市后前 5 个交易日；
                                            北交所上市首日）
    沪深主板核准制新股上市首日    +44%/−36% （**非对称**，基准=发行价；仅适用于
                                            上市日 < 2023-04-10 的主板新股）
最小报价单位：0.01 元（A 股）；本轮窗口（2025-04 起）内不存在涨跌幅比例变更。

未覆盖（明确留白，不假装支持）
------------------------------------------------------------------
    - 交易所公布的"参考前收盘价"来源（除权除息日的官方前收）——见审计报告缺口
    - 盘中临时停牌、价格笼子（±2% 有效申报价格范围）等交易机制
    - B 股（外币计价、最小报价单位不同）
    - 盘中临时停牌机制未建模：只做价格上下限判定，不模拟盘中临停
    - 创业板注册制前（上市日 < 2020-08-24）的旧首日规则未区分（不在本数据窗口内）
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final

# 板块代码：与 market.data.boards.BOARD_CODES 保持一致（由 tests/test_limit_rules.py 守护，
# 本模块不反向依赖数据层，避免规则层被数据层拖着走）。
SSE_MAIN: Final[str] = "SSE_MAIN"
SZSE_MAIN: Final[str] = "SZSE_MAIN"
SZSE_GEM: Final[str] = "SZSE_GEM"
SSE_STAR: Final[str] = "SSE_STAR"
BSE: Final[str] = "BSE"

# --------------------------------------------------------------------------- 状态
STATUS_OK: Final[str] = "ok"
STATUS_NO_LIMIT: Final[str] = "no_limit"
STATUS_UNKNOWN: Final[str] = "unknown"

# --------------------------------------------------------------------------- 板块比例
DEFAULT_TICK: Final[Decimal] = Decimal("0.01")

BOARD_LIMIT_RATIOS: Final[dict[str, Decimal]] = {
    SSE_MAIN: Decimal("0.10"),
    SZSE_MAIN: Decimal("0.10"),
    SZSE_GEM: Decimal("0.20"),
    SSE_STAR: Decimal("0.20"),
    BSE: Decimal("0.30"),
}

#: ST / *ST 的比例。除沪深主板降至 5% 外，其余板块 ST **不改变**比例：
#: 科创板/创业板 *ST 仍 20%、北交所 *ST 仍 30%。
#: 依据：真实 K 线实证（本机正式库 window 2025-04~2026-09）——
#:   科创板 ST 13 只 最大日内涨幅 20.45%、创业板 ST 40 只 20.26%、北交所 ST 3 只 29.97%、
#:   沪深主板 ST 常态日上限为 5%（5% 命中 4621 次 ≫ 10% 的 1210 次，后者来自样本内
#:   尚未实施 ST 的历史区段）。
ST_LIMIT_RATIOS: Final[dict[str, Decimal]] = {
    SSE_MAIN: Decimal("0.05"),
    SZSE_MAIN: Decimal("0.05"),
    SZSE_GEM: Decimal("0.20"),
    SSE_STAR: Decimal("0.20"),
    BSE: Decimal("0.30"),
}

#: 上市阶段：正常交易 / 无涨跌幅限制 / 沪深主板核准制新股首日（非对称）
LISTING_STAGE_TRADING: Final[str] = "trading"
LISTING_STAGE_NO_LIMIT: Final[str] = "no_limit_days"
#: 沪深主板**核准制**新股上市首日：非对称 +44% / −36%（基准=发行价）。
#: 仅适用于上市日 < 2023-04-10 的主板新股；2023-04-10 起（全面注册制首批主板
#: 新股上市）主板改为"上市后前 5 个交易日无涨跌幅限制"（见 resolve_listing_stage）。
LISTING_STAGE_MAIN_FIRST_DAY: Final[str] = "main_board_first_day"

#: 合法取值集合（供调用方/测试校验，避免拼错阶段名）
LIMIT_STAGES: Final[tuple[str, ...]] = (
    LISTING_STAGE_TRADING,
    LISTING_STAGE_NO_LIMIT,
    LISTING_STAGE_MAIN_FIRST_DAY,
)

#: 沪深主板核准制新股上市首日的非对称涨跌幅（相对发行价）
MAIN_FIRST_DAY_UPPER_RATIO: Final[Decimal] = Decimal("0.44")
MAIN_FIRST_DAY_LOWER_RATIO: Final[Decimal] = Decimal("0.36")
#: 全面注册制首批主板新股上市日：此后主板新股前 5 个交易日无涨跌幅限制
MAIN_BOARD_REGISTRATION_FIRST_LISTING_DATE: Final[str] = "2023-04-10"
#: 创业板 / 科创板 / 主板（注册制）新股"上市后前 N 个交易日"无涨跌幅限制
NO_LIMIT_SESSIONS: Final[int] = 5

BASIS_NO_LIMIT_NEW_LISTING: Final[str] = "新上市无涨跌幅限制阶段（制度）"


@dataclass(frozen=True)
class LimitPriceResult:
    """涨跌停价判定结果。`status != 'ok'` 时 upper/lower 一律为 None（不给"看起来能用"的数字）。"""

    status: str
    upper: Decimal | None
    lower: Decimal | None
    #: 对称比例（常规/ST）；非对称阶段（主板核准制首日）为 None
    ratio: Decimal | None
    basis: str
    #: 非对称阶段专用：上/下限比例（常规阶段为 None）
    upper_ratio: Decimal | None = None
    lower_ratio: Decimal | None = None


@dataclass(frozen=True)
class PrevCloseResult:
    """前收盘价可用性判定。status != 'ok' 时 prev_close 为 None。"""

    status: str
    prev_close: Decimal | None
    sessions_since_prev_bar: int | None
    reason: str


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if result != result or result.is_infinite():  # NaN / inf
        return None
    return result


def round_to_tick(value: Decimal, tick: Decimal = DEFAULT_TICK) -> Decimal:
    """按最小报价单位四舍五入（A 股规则为四舍五入，用 ROUND_HALF_UP，不用银行家舍入）。"""
    if tick <= 0:
        raise ValueError("tick 必须为正数")
    return (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick


def resolve_limit_ratio(board: str | None, *, is_st: bool = False) -> tuple[Decimal | None, str]:
    """返回 (涨跌幅比例, 依据说明)；无法确定时比例为 None。"""
    if not board:
        return None, "板块未知：不猜测，需由调用方给出（可用 market.data.boards.classify_board）"
    table = ST_LIMIT_RATIOS if is_st else BOARD_LIMIT_RATIOS
    ratio = table.get(board)
    if ratio is None:
        return None, f"未收录的板块代码：{board}（不做猜测）"
    label = "ST/*ST" if is_st else "常规"
    return ratio, f"{board} {label} 涨跌幅比例 {ratio}（制度）"


def resolve_listing_stage(
    board: str | None,
    *,
    sessions_since_listing: int | None,
    listing_date: str | None = None,
) -> tuple[str | None, str]:
    """按板块与"上市以来第几个交易日"判定上市阶段；**信息不足一律 (None, 原因)**——不猜。

    sessions_since_listing：上市首日记 0（应由交易日历算出）。
    listing_date：上市日期（沪深主板必须提供，用于区分 2023-04-10 之前/之后的规则）。

    制度（含生效日期，来源：交易所规则 / 证券时报 2023-04-10 首批注册制主板新股）
        创业板 / 科创板              上市后前 5 个交易日无涨跌幅限制，第 6 日起 ±20%
        沪深主板（上市日 >= 2023-04-10）前 5 个交易日无涨跌幅限制，第 6 日起 ±10%
        沪深主板（上市日 < 2023-04-10）首日 +44%/−36%（非对称，基准=发行价），次日起 ±10%
        北交所                      上市首日无涨跌幅限制，第 2 日起 ±30%
    """
    if board is None:
        return None, "板块未知：无法判定上市阶段（不猜测，需调用方给出）"
    if board not in BOARD_LIMIT_RATIOS:
        return None, f"未收录的板块代码：{board}（不做猜测）"
    if sessions_since_listing is None:
        return None, "缺少'上市以来交易日数'（需要上市日期 + 交易日历）：无法判定上市阶段（不猜测）"
    if isinstance(sessions_since_listing, bool) or sessions_since_listing < 0:
        return None, f"sessions_since_listing 必须是非负整数，实际为 {sessions_since_listing!r}"

    if board in (SZSE_GEM, SSE_STAR):
        if sessions_since_listing < NO_LIMIT_SESSIONS:
            return LISTING_STAGE_NO_LIMIT, (
                f"{board} 新股上市后前 {NO_LIMIT_SESSIONS} 个交易日无涨跌幅限制（制度）"
            )
        return LISTING_STAGE_TRADING, f"{board} 上市后第 {NO_LIMIT_SESSIONS + 1} 个交易日起恢复常规比例"

    if board == BSE:
        if sessions_since_listing == 0:
            return LISTING_STAGE_NO_LIMIT, "北交所新股上市首日无涨跌幅限制（制度）"
        return LISTING_STAGE_TRADING, "北交所上市第 2 个交易日起 ±30%（制度）"

    # 沪深主板：必须先知道上市日期才能选规则
    if listing_date is None:
        return None, "沪深主板需提供上市日期以区分 2023-04-10 之前/之后的规则（不猜测）"
    ld = str(listing_date)[:10]
    if ld < MAIN_BOARD_REGISTRATION_FIRST_LISTING_DATE:
        if sessions_since_listing == 0:
            return LISTING_STAGE_MAIN_FIRST_DAY, (
                "沪深主板核准制新股上市首日 +44%/−36%（非对称，基准=发行价）"
            )
        return LISTING_STAGE_TRADING, "沪深主板核准制新股上市次日起 ±10%"

    if sessions_since_listing < NO_LIMIT_SESSIONS:
        return LISTING_STAGE_NO_LIMIT, (
            f"沪深主板注册制新股（上市日 >= {MAIN_BOARD_REGISTRATION_FIRST_LISTING_DATE}）"
            f"前 {NO_LIMIT_SESSIONS} 个交易日无涨跌幅限制"
        )
    return LISTING_STAGE_TRADING, f"沪深主板注册制新股第 {NO_LIMIT_SESSIONS + 1} 个交易日起 ±10%"


def _compute_main_first_day(prev_close: object, *, board: str | None, tick: Decimal) -> LimitPriceResult:
    """沪深主板核准制新股上市首日：非对称 +44% / −36%，基准为发行价。

    只对 SSE_MAIN / SZSE_MAIN 有效；其它板块不适用该规则 → unknown（不硬算）。
    """
    if board not in (SSE_MAIN, SZSE_MAIN):
        return LimitPriceResult(
            STATUS_UNKNOWN, None, None, None,
            f"非对称首日限制仅适用于沪深主板（当前板块 {board!r}）：其它板块请用 no_limit/常规阶段",
        )
    base = _to_decimal(prev_close)
    if base is None or base <= 0:
        return LimitPriceResult(
            STATUS_UNKNOWN, None, None, None,
            f"上市首日基准价（发行价）缺失或非法：{prev_close!r}（不做猜测）",
        )
    one = Decimal("1")
    return LimitPriceResult(
        STATUS_OK,
        round_to_tick(base * (one + MAIN_FIRST_DAY_UPPER_RATIO), tick),
        round_to_tick(base * (one - MAIN_FIRST_DAY_LOWER_RATIO), tick),
        None,
        "沪深主板核准制新股上市首日 +44%/−36%（非对称，基准=发行价；仅上市日 < "
        f"{MAIN_BOARD_REGISTRATION_FIRST_LISTING_DATE} 适用）",
        MAIN_FIRST_DAY_UPPER_RATIO,
        MAIN_FIRST_DAY_LOWER_RATIO,
    )


def compute_limit_prices(
    prev_close: object,
    *,
    board: str | None,
    is_st: bool = False,
    listing_stage: str = LISTING_STAGE_TRADING,
    prev_close_is_exchange_reference: bool | None = None,
    tick: Decimal = DEFAULT_TICK,
) -> LimitPriceResult:
    """计算当日涨停价 / 跌停价。

    参数
        prev_close                      前收盘价（字符串/Decimal/数值均可）
        board                           板块代码（必须由调用方提供）
        is_st                           当日是否为 ST/*ST（**当日状态**，需要历史 ST 数据支撑）
        listing_stage                   'trading' | 'no_limit_days' | 'main_board_first_day'
        prev_close_is_exchange_reference
                                        **fail-closed（必填语义）**：prev_close 是否为交易所公布的
                                        参考前收盘价。**必须显式传 True 才会计算**；传 False 或留空
                                        （None=未确认）一律返回 unknown，**不默认可信**。

    阶段语义
        no_limit_days        -> 无涨跌幅限制（返回 STATUS_NO_LIMIT，不给数字）
        main_board_first_day -> 沪深主板核准制新股首日：**非对称** +44%/−36%，
                                此时 prev_close 被解释为**发行价**（首日无前收盘价）
        trading              -> 常规/ST 对称比例
    """
    if prev_close_is_exchange_reference is not None and not isinstance(prev_close_is_exchange_reference, bool):
        raise ValueError("prev_close_is_exchange_reference 必须是 bool 或 None（None = 未确认，fail-closed）")

    if listing_stage == LISTING_STAGE_NO_LIMIT:
        return LimitPriceResult(STATUS_NO_LIMIT, None, None, None, BASIS_NO_LIMIT_NEW_LISTING)
    if listing_stage == LISTING_STAGE_MAIN_FIRST_DAY:
        return _compute_main_first_day(prev_close, board=board, tick=tick)
    if listing_stage != LISTING_STAGE_TRADING:
        return LimitPriceResult(STATUS_UNKNOWN, None, None, None, f"未知的上市阶段：{listing_stage}")

    ratio, basis = resolve_limit_ratio(board, is_st=is_st)
    if ratio is None:
        return LimitPriceResult(STATUS_UNKNOWN, None, None, None, basis)

    if prev_close_is_exchange_reference is None:
        return LimitPriceResult(
            STATUS_UNKNOWN, None, None, ratio,
            "未显式确认参考前收可信（fail-closed）：必须显式声明 prev_close_is_exchange_reference=True，"
            "否则不给出涨跌停价",
        )
    if prev_close_is_exchange_reference is False:
        return LimitPriceResult(
            STATUS_UNKNOWN, None, None, ratio,
            "前收盘价不是交易所参考前收盘价（行情序列价格 / 除权除息日或有公司行为）；"
            "禁止用前复权价或未复权前一根收盘当真实涨跌停基准",
        )

    base = _to_decimal(prev_close)
    if base is None or base <= 0:
        return LimitPriceResult(STATUS_UNKNOWN, None, None, ratio,
                                f"前收盘价缺失或非法：{prev_close!r}（不做猜测）")

    one = Decimal("1")
    return LimitPriceResult(
        STATUS_OK,
        round_to_tick(base * (one + ratio), tick),
        round_to_tick(base * (one - ratio), tick),
        ratio,
        basis,
    )


def resolve_prev_close(
    prev_bar_close: object,
    *,
    bar_present: bool = True,
    sessions_since_prev_bar: int | None = None,
    corporate_action: bool = False,
) -> PrevCloseResult:
    """判断"上一根 K 线的收盘价"能否当作前收盘价使用。

    区分原则（不把缺失 K 线直接解释为未涨停或停牌）：
        - 当日没有 K 线               → unknown（原因写明，不推断停牌/未涨停）
        - 前一根 K 线缺失或价格非法    → unknown
        - 与前一交易日之间缺 bar      → unknown（缺口需要交易日历 + 交易所前收盘价确认）
        - 当日有公司行为（除权除息）  → unknown（必须使用交易所参考前收盘价）
    """
    if not bar_present:
        return PrevCloseResult(STATUS_UNKNOWN, None, sessions_since_prev_bar,
                               "该交易日没有 K 线：无法确定前收盘（不推断为停牌，也不推断为未涨停）")
    if corporate_action:
        return PrevCloseResult(STATUS_UNKNOWN, None, sessions_since_prev_bar,
                               "当日为除权除息日（或存在公司行为）：必须使用交易所参考前收盘价")
    if sessions_since_prev_bar is not None and sessions_since_prev_bar > 1:
        return PrevCloseResult(STATUS_UNKNOWN, None, sessions_since_prev_bar,
                               f"与上一根 K 线之间缺 {sessions_since_prev_bar - 1} 个交易日："
                               "需交易日历 + 交易所前收盘价确认，不能直接沿用")
    value = _to_decimal(prev_bar_close)
    if value is None or value <= 0:
        return PrevCloseResult(STATUS_UNKNOWN, None, sessions_since_prev_bar,
                               f"前一根 K 线收盘价缺失或非法：{prev_bar_close!r}")
    return PrevCloseResult(STATUS_OK, value, sessions_since_prev_bar, "前一根 K 线收盘价可用")


def classify_bar_against_limits(
    *,
    high: object,
    low: object,
    close: object,
    upper: Decimal | None,
    lower: Decimal | None,
    tick: Decimal = DEFAULT_TICK,
) -> str:
    """把一根 K 线与涨跌停价对照，返回**事实性**状态（不解释成信号）。

    返回：'at_upper' 收盘封住涨停 / 'touched_upper' 盘中触及未封住（炸板）/
          'at_lower' / 'touched_lower' / 'inside' / 'unknown'（缺值或无可判定边界）
    """
    h, l, c = _to_decimal(high), _to_decimal(low), _to_decimal(close)
    if h is None or l is None or c is None:
        return STATUS_UNKNOWN
    if upper is None and lower is None:
        return STATUS_UNKNOWN
    half = tick / 2
    if upper is not None:
        if abs(c - upper) < half:
            return "at_upper"
        if abs(h - upper) < half:
            return "touched_upper"
    if lower is not None:
        if abs(c - lower) < half:
            return "at_lower"
        if abs(l - lower) < half:
            return "touched_lower"
    return "inside"
