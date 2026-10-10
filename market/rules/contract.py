"""最简数据契约（Stage 027′ Data Foundation）——A 股历史数据质量与涨跌停判定的共同前提。

本模块是**契约**而不是工具：它把"必须一致的约定"写成可导入、可测试的常量与判定函数，
避免各层自行解释。契约变更 = 语义变更，必须单独评审。

1) 时间与交易日
   - 全部时间戳 UTC，格式 'YYYY-MM-DD HH:MM:SS'（沿用 Stage 02 约定）
   - `klines.timestamp` 是该根 K 线的**开始时间**；`1d` 根 = 该交易日的 `00:00:00` UTC 日标签。
     ⚠️ 它不是真实开盘时刻：A 股连续竞价 01:30–07:00 UTC（北京 09:30–15:00）。
     所以 `1d` 标签只能当"日期"用，跨周期/跨市场对齐必须显式转换，不得直接当时间点比较。
   - **交易日历是外部事实**（来自指数日 K，如 sh000001），**不得**由"某只股票有 K 线"反推。

2) 价格口径
   - `symbols` / `klines` 存**原始不复权** OHLCV（实测：与腾讯"不复权"序列 0 天差异）
   - 前复权是**派生视图**：除权后历史价格全部重算，随每日行情变化 → 不得当静态历史入库
   - `amount` 允许 NULL；当前数据源不提供历史成交额（实测）

3) 缺失状态四态（禁止混用，默认一律 UNKNOWN）
   - PRE_LISTING  上市前：需**上市日期**证据
   - SUSPENDED    停牌/无成交：需停牌公告等**外部证据**
   - SOURCE_GAP   数据源缺失：需"同日同池其它标的均有数据"等**外部证据**
   - UNKNOWN      无法判定：仅凭"没有 K 线"**不能**判定停牌，也**不能**判定未涨停
   约定：缺失默认为 UNKNOWN；只有拿到证据才升级为具体状态。

4) 股票池边界
   - 当前 `symbols` = "当前 A 股列表"快照，**不等于历史股票池**；退市股不在库中
     → 存在幸存者偏差；任何跨期统计必须声明使用的是"当前池"。
   - 历史池需要退市名单（来源未验证）。

5) 涨跌停判定所需输入（缺一不可）
   `board`（板块）+ `is_st`（**当日** ST 状态）+ `listing_stage`（是否新股无限制阶段）
   + `prev_close_is_exchange_reference`（是否交易所参考前收盘价）+ 交易日历。
   判定实现见 `market.rules.limits`。
"""

from __future__ import annotations

from typing import Final

# --------------------------------------------------------------------------- 缺失状态
MISSING_PRESENT: Final[str] = "present"
MISSING_NOT_TRADING_DAY: Final[str] = "not_a_trading_day"
MISSING_PRE_LISTING: Final[str] = "pre_listing"
MISSING_SUSPENDED: Final[str] = "suspended"
MISSING_SOURCE_GAP: Final[str] = "source_gap"
MISSING_UNKNOWN: Final[str] = "unknown"

# --------------------------------------------------------------------------- 口径常量
PRICE_BASIS_UNADJUSTED: Final[str] = "unadjusted"
PRICE_BASIS_FORWARD_ADJUSTED_VIEW: Final[str] = "forward_adjusted_view"
PRICE_BASIS_UNADJUSTED_NOTE: Final[str] = (
    "库内为原始不复权 OHLCV；前复权只作为派生视图，不得当静态历史入库"
)
UNIVERSE_CURRENT_SNAPSHOT: Final[str] = "current_a_share_snapshot"
UNIVERSE_HISTORICAL: Final[str] = "historical_universe"
UNIVERSE_WARNING: Final[str] = (
    "symbols 是当前 A 股列表快照，不是历史股票池（退市股缺失 → 幸存者偏差）"
)


def classify_missing(
    *,
    is_trading_day: bool,
    has_bar: bool,
    listing_date: str | None = None,
    bar_date: str | None = None,
    suspension_evidence: bool = False,
    source_gap_evidence: bool = False,
) -> str:
    """判定某个 (标的, 交易日) 无 K 线时的状态。

    **默认返回 MISSING_UNKNOWN**：仅凭"没有 K 线"无法区分停牌与数据源缺失，
    也无法证明"未涨停"。只有外部证据（上市日期 / 停牌公告 / 同池对照）才允许升级。
    """
    if not is_trading_day:
        return MISSING_NOT_TRADING_DAY
    if has_bar:
        return MISSING_PRESENT
    if listing_date and bar_date and bar_date < listing_date:
        return MISSING_PRE_LISTING
    if suspension_evidence:
        return MISSING_SUSPENDED
    if source_gap_evidence:
        return MISSING_SOURCE_GAP
    return MISSING_UNKNOWN
