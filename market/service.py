"""市场 / 选股服务层（Stage 20 Step 1：只做数据层能力，不提供 HTTP 接口）。

分层：

    （Step 2 的 API）→ MarketService → Repository（SymbolGroupRepository / SymbolRepository / KlineRepository）→ SQLite

本步实现三类能力：

    1. 分组与成员：集合（行业 / 概念 / 自选 / 自定义）的增删改查 + 成员关系维护
    2. 展示代码：display_code()，**由后端统一生成**（A 股补交易所后缀：600519 → 600519.SH，
       000001 → 000001.SZ；加密等其它市场原样返回）。数据库里仍只存裸代码，前端不得自行推导。
    3. 最新行情快照：latest_quote() / list_symbols_with_quotes()，只用 klines 里已有的数据；
       **数据缺失一律返回 None，绝不伪造**（成交额 amount 缺失是常态）。

边界（本阶段刻意不做）：
    - 不实现选股算法 / 打分 / 排序规则（排序属于展示层）
    - 不实现动态 Universe（Top20 市值等）；一个 group 就是一个标的集合
    - 不写 SQL：全部经 Repository；不碰策略与执行器
"""

from __future__ import annotations

from typing import Any, Final

from database import (
    KlineRepository,
    SymbolGroupRepository,
    SymbolRepository,
    resolve_database_path,
)

#: 默认用于计算「最新行情」的周期（日线）
DEFAULT_QUOTE_TIMEFRAME: Final[str] = "1d"

#: A 股交易所后缀推导规则（按顺序匹配，具体段放在前面）
#: 说明：数据库里存的是裸代码（600519），后缀只用于**展示**
_CN_EXCHANGE_RULES: Final[tuple[tuple[str, str], ...]] = (
    ("900", "SH"),  # 沪市 B 股
    ("200", "SZ"),  # 深市 B 股
    ("920", "BJ"),  # 北交所新代码段
    ("6", "SH"),  # 沪市主板 / 科创板 688
    ("0", "SZ"),  # 深市主板
    ("3", "SZ"),  # 创业板
    ("4", "BJ"),  # 北交所
    ("8", "BJ"),  # 北交所
)


def display_code(market: str | None, symbol: str) -> str:
    """把库里的 (market, symbol) 转成展示代码（后端统一生成，前端不重复实现）。

    例：('CN', '600519') → '600519.SH'；('CN', '000001') → '000001.SZ'；
        ('CRYPTO', 'BTCUSDT') → 'BTCUSDT'；
        已带后缀（含 '.'）、空代码、未知市场 → 原样返回。
    """
    text = str(symbol or "").strip()
    if not text or "." in text:
        return text
    if str(market or "").strip().upper() != "CN":
        return text
    for prefix, exchange in _CN_EXCHANGE_RULES:
        if text.startswith(prefix):
            return f"{text}.{exchange}"
    return text


def _to_float(value: object) -> float | None:
    """None / 非数字 → None（缺失就是缺失，不补 0）。"""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class MarketService:
    """市场分类与行情快照的服务门面（只做编排，SQL 全在 Repository）。"""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = resolve_database_path(db_path)
        self.groups = SymbolGroupRepository(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)

    # ---------------------------------------------------------------- 分组
    def create_group(
        self,
        name: str,
        group_type: str,
        *,
        market: str | None = None,
        description: str | None = None,
        is_active: bool = True,
    ) -> int:
        """创建集合（行业 / 概念 / 自选 / 自定义），返回 id。"""
        return self.groups.create_group(
            name,
            group_type,
            market=market,
            description=description,
            is_active=is_active,
        )

    def get_group(self, group_id: int) -> dict[str, Any] | None:
        return self.groups.get_group(group_id)

    def list_groups(
        self,
        *,
        group_type: str | None = None,
        market: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self.groups.list_groups(group_type=group_type, market=market, active_only=active_only)

    def update_group(self, group_id: int, **fields: Any) -> bool:
        """局部更新集合（参数语义见 SymbolGroupRepository.update_group）。"""
        return self.groups.update_group(group_id, **fields)

    def deactivate_group(self, group_id: int) -> bool:
        """逻辑停用集合（不物理删除，集合内成员关系保留）。"""
        return self.groups.deactivate_group(group_id)

    # ------------------------------------------------------------ 成员关系
    def add_symbol_to_group(self, group_id: int, symbol_id: int) -> int:
        """把标的加入集合（幂等），返回成员关系 id。"""
        return self.groups.add_symbol_to_group(group_id, symbol_id)

    def remove_symbol_from_group(self, group_id: int, symbol_id: int) -> bool:
        """把标的移出集合，返回是否删除了成员关系。"""
        return self.groups.remove_symbol_from_group(group_id, symbol_id)

    def list_group_symbols(self, group_id: int, *, active_only: bool = False) -> list[dict[str, Any]]:
        return self.groups.list_group_symbols(group_id, active_only=active_only)

    def list_groups_of_symbol(self, symbol_id: int) -> list[dict[str, Any]]:
        return self.groups.list_groups_of_symbol(symbol_id)

    def count_group_symbols(self, group_id: int) -> int:
        return self.groups.count_group_symbols(group_id)

    # ------------------------------------------------------------ 展示形态
    @staticmethod
    def display_code(market: str | None, symbol: str) -> str:
        """展示代码（A 股补后缀），便于调用方统一走服务层。"""
        return display_code(market, symbol)

    def symbol_view(self, symbol_row: Any) -> dict[str, Any]:
        """symbols 行 → 展示用标的（补 display_code，不做额外业务判断）。"""
        data = dict(symbol_row)
        data["display_code"] = display_code(data.get("market"), data.get("symbol", ""))
        return data

    # ------------------------------------------------------------ 分组视图
    def list_groups_with_stats(
        self,
        *,
        group_type: str | None = None,
        market: str | None = None,
        active_only: bool = True,
        timeframe: str = DEFAULT_QUOTE_TIMEFRAME,
    ) -> list[dict[str, Any]]:
        """分组列表 + 成员数 + **板块行情聚合**（Stage 22 市场总览的板块 Tile）。

        在 Repository 一条 SQL 算出的聚合之上，服务层只做两件事：
            - 补齐 timeframe（行情口径，避免前端猜）
            - 计算 quote_coverage = 有有效行情的成员数 / 成员总数
              （成员为 0 时覆盖率无定义 → None，不编造 0 或 1）

        涨跌家数与平均涨跌幅都**只统计有有效行情的成员**（缺失不补 0）：
        up + down + flat === quoted_count，quoted_count <= symbol_count。
        """
        rows = self.groups.list_groups_with_stats(
            group_type=group_type,
            market=market,
            active_only=active_only,
            timeframe=timeframe,
        )
        for row in rows:
            symbol_count = int(row.get("symbol_count") or 0)
            quoted_count = int(row.get("quoted_count") or 0)
            row["symbol_count"] = symbol_count
            row["quoted_count"] = quoted_count
            row["up_count"] = int(row.get("up_count") or 0)
            row["down_count"] = int(row.get("down_count") or 0)
            row["flat_count"] = int(row.get("flat_count") or 0)
            row["avg_change_percent"] = _to_float(row.get("avg_change_percent"))
            row["quote_coverage"] = (quoted_count / symbol_count) if symbol_count else None
            row["timeframe"] = timeframe
        return rows

    def list_groups_with_counts(
        self,
        *,
        group_type: str | None = None,
        market: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        """分组列表 + 每个分组的标的数量（供 /market/groups 返回）。"""
        return self.groups.list_groups_with_counts(
            group_type=group_type, market=market, active_only=active_only
        )

    # ------------------------------------------------------------ 标的（含行情）
    def get_symbol(self, symbol_id: int) -> dict[str, Any] | None:
        """按 id 查标的（供 API 做 404 判断）；不存在返回 None。"""
        row = self.symbols.get_symbol_by_id(symbol_id)
        return dict(row) if row is not None else None

    def _symbol_rows(
        self,
        *,
        group_id: int | None = None,
        market: str | None = None,
        active_only: bool = True,
    ) -> list[Any]:
        """标的行集合：优先按分组取，否则按全市场取；market 过滤在内存完成。"""
        if group_id is not None:
            rows = self.groups.list_group_symbols(group_id, active_only=active_only)
        elif active_only:
            rows = self.symbols.list_active_symbols(market=market)
        else:
            rows = self.symbols.list_symbols(market=market)
        if market is None:
            return list(rows)
        return [row for row in rows if str(row["market"]) == market]

    def market_symbol_view(
        self, symbol_row: Any, *, timeframe: str = DEFAULT_QUOTE_TIMEFRAME
    ) -> dict[str, Any]:
        """标的 + display_code + 最新行情（**扁平结构**，供 /market/* 返回）。

        行情缺失时相关字段为 None（不伪造）；display_code 由本服务统一生成。
        """
        view = self.symbol_view(symbol_row)
        quote = self.latest_quote(int(view["id"]), timeframe)
        return {
            "id": int(view["id"]),
            "symbol": view["symbol"],
            "name": view["name"],
            "market": view["market"],
            "asset_type": view["asset_type"],
            "is_active": view["is_active"],
            "display_code": view["display_code"],
            "timeframe": timeframe,
            "latest_price": quote["close"],
            "previous_close": quote["previous_close"],
            "change": quote["change"],
            "change_percent": quote["change_percent"],
            "volume": quote["volume"],
            "amount": quote["amount"],
            "timestamp": quote["timestamp"],
        }

    def list_market_symbols(
        self,
        *,
        group_id: int | None = None,
        market: str | None = None,
        active_only: bool = True,
        timeframe: str = DEFAULT_QUOTE_TIMEFRAME,
    ) -> list[dict[str, Any]]:
        """市场标的列表（选股页面统一入口，结构见 market_symbol_view）。"""
        return [
            self.market_symbol_view(row, timeframe=timeframe)
            for row in self._symbol_rows(group_id=group_id, market=market, active_only=active_only)
        ]

    def get_market_symbol(
        self, symbol_id: int, *, timeframe: str = DEFAULT_QUOTE_TIMEFRAME
    ) -> dict[str, Any] | None:
        """单个标的 + 最新行情；不存在返回 None（由 API 转 404）。"""
        row = self.symbols.get_symbol_by_id(symbol_id)
        return None if row is None else self.market_symbol_view(row, timeframe=timeframe)

    # ------------------------------------------------------------ 最新行情
    def latest_quote(self, symbol_id: int, timeframe: str = DEFAULT_QUOTE_TIMEFRAME) -> dict[str, Any]:
        """最新行情快照（只用 klines 已有数据，缺失字段一律 None）。

        返回：symbol_id / timeframe / timestamp / close / previous_close /
              change / change_percent / volume / amount
        """
        rows = self.klines.list_recent_klines(symbol_id, timeframe, 2)
        quote: dict[str, Any] = {
            "symbol_id": int(symbol_id),
            "timeframe": timeframe,
            "timestamp": None,
            "close": None,
            "previous_close": None,
            "change": None,
            "change_percent": None,
            "volume": None,
            "amount": None,
        }
        if not rows:
            return quote

        latest = rows[-1]
        previous = rows[-2] if len(rows) > 1 else None
        close = _to_float(latest["close"])
        previous_close = _to_float(previous["close"]) if previous is not None else None
        change = None if close is None or previous_close is None else close - previous_close
        change_percent = (
            None if change is None or not previous_close else change / previous_close * 100.0
        )

        quote.update(
            timestamp=latest["timestamp"],
            close=close,
            previous_close=previous_close,
            change=change,
            change_percent=change_percent,
            volume=_to_float(latest["volume"]),
            amount=_to_float(latest["amount"]),
        )
        return quote

    def list_symbols_with_quotes(
        self,
        *,
        group_id: int | None = None,
        market: str | None = None,
        active_only: bool = True,
        timeframe: str = DEFAULT_QUOTE_TIMEFRAME,
    ) -> list[dict[str, Any]]:
        """标的列表 + 展示代码 + 最新行情（Step 2 的 API 直接复用本方法）。

        - 传 group_id 则只取该集合内的标的；否则取全部标的
        - market 过滤在内存里做（集合本身可以跨市场）
        - 行情缺失时 quote 内字段为 None，不伪造
        """
        results: list[dict[str, Any]] = []
        for row in self._symbol_rows(group_id=group_id, market=market, active_only=active_only):
            view = self.symbol_view(row)
            view["quote"] = self.latest_quote(int(row["id"]), timeframe)
            results.append(view)
        return results
