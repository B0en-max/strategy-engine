"""市场分类 / 标的集合的数据访问层（Stage 20）。

定位：

    symbol_groups          一个集合：行业板块 / 概念板块 / 自选 / 自定义股票池
                           （name 唯一、group_type 四选一、market 可空表示跨市场）
    symbol_group_members   group ↔ symbol 的多对多成员关系

分工与边界：
    - 本文件只管这两张表的读写；行情、策略、执行都不在这里
    - 唯一性由数据库约束保证：group 的 UNIQUE(name)、成员的 UNIQUE(group_id, symbol_id)
      —— 不用「先 SELECT 再 INSERT」的 Python 判断
    - add_symbol_to_group 是**幂等**的：成员已存在时返回既有成员 id（不报错、不重复插入）；
      需要严格拒绝重复时可直接写 SQL 触发 UNIQUE 约束
    - 「移出集合」用物理删除成员关系表示；symbols 的 is_active 逻辑停用与集合成员互不影响
    - 不建独立 universe 表：一个 group 就是一个标的集合，动态 Universe 属于后续阶段

写法与其它 Repository 保持一致：参数先校验（非法值直接 ValueError），
数据库错误统一经 enrich_error 抛出。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Sequence

from . import models
from .repository import Repository, enrich_error
from .validation import (
    optional_text,
    require_choice,
    require_positive_int,
    require_text,
    require_timeframe,
)

SYMBOL_GROUPS = models.SYMBOL_GROUPS_TABLE
SYMBOL_GROUP_MEMBERS = models.SYMBOL_GROUP_MEMBERS_TABLE
SYMBOLS = models.SYMBOLS_TABLE

#: 「未提供该字段」与「显式置空（NULL）」的区分标记，仅用于 update_group
UNSET: Any = object()

#: 批量写入成员关系时每批的条数（单事务；过大反而增加锁与内存压力）
_BULK_BATCH_SIZE: int = 1000

_INSERT_GROUP_SQL = (
    f"INSERT INTO {SYMBOL_GROUPS} "
    "(name, group_type, market, description, is_active, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_MEMBER_SQL = (
    f"INSERT INTO {SYMBOL_GROUP_MEMBERS} (group_id, symbol_id, created_at) VALUES (?, ?, ?)"
)
_ORDER_BY = "ORDER BY group_type ASC, name ASC, id ASC"
#: 带别名（JOIN 查询）时使用的同序排序
_ORDER_BY_ALIASED = "ORDER BY g.group_type ASC, g.name ASC, g.id ASC"


def _require_group_name(value: object) -> str:
    return require_text(value, "name")


def _require_group_type(value: object) -> str:
    return require_choice(value, "group_type", models.GROUP_TYPES)


def _normalize_flag(value: object, field: str = "is_active") -> int:
    """把 True/False/1/0 归一成 1/0；其它值直接报错，不做静默转换。"""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError(f"{field} 必须是 True/False/1/0，实际为 {value!r}")


class SymbolGroupRepository(Repository):
    """symbol_groups / symbol_group_members 的数据访问对象。"""

    # ---------------------------------------------------------------- 集合
    def create_group(
        self,
        name: str,
        group_type: str,
        *,
        market: str | None = None,
        description: str | None = None,
        is_active: bool = True,
    ) -> int:
        """创建集合，返回新记录 id。

        - name 已存在时由 UNIQUE(name) 拒绝（抛 sqlite3.IntegrityError），不覆盖
        - group_type 必须是 models.GROUP_TYPES 之一，否则 ValueError
        - market 为空表示跨市场集合
        """
        name = _require_group_name(name)
        group_type = _require_group_type(group_type)
        market = optional_text(market, "market")
        description = optional_text(description, "description")
        active = _normalize_flag(is_active)

        now = models.utc_now_str()
        params = (name, group_type, market, description, active, now, now)
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_GROUP_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_GROUP_SQL, params) from exc
            return int(cursor.lastrowid)

    def get_group(self, group_id: int) -> dict[str, Any] | None:
        """按 id 查询集合；不存在时返回 None。"""
        group_id = require_positive_int(group_id, "group_id")
        row = self.query_one(f"SELECT * FROM {SYMBOL_GROUPS} WHERE id = ?", (group_id,))
        return dict(row) if row is not None else None

    def get_group_by_name(self, name: str) -> dict[str, Any] | None:
        """按名称查询集合（name 唯一）；不存在时返回 None。"""
        name = _require_group_name(name)
        row = self.query_one(f"SELECT * FROM {SYMBOL_GROUPS} WHERE name = ?", (name,))
        return dict(row) if row is not None else None

    def list_groups(
        self,
        *,
        group_type: str | None = None,
        market: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """列出集合（按 group_type、name、id 升序）；可按类型 / 市场过滤。"""
        where = "1 = 1"
        params: list[object] = []
        if group_type is not None:
            where += " AND group_type = ?"
            params.append(_require_group_type(group_type))
        if market is not None:
            where += " AND market = ?"
            params.append(require_text(market, "market"))
        if active_only:
            where += " AND is_active = 1"
        sql = f"SELECT * FROM {SYMBOL_GROUPS} WHERE {where} {_ORDER_BY}"
        return [dict(row) for row in self.query_all(sql, tuple(params))]

    def count_groups(self, *, group_type: str | None = None, active_only: bool = False) -> int:
        """统计集合数量（条件与 list_groups 一致）。"""
        where = "1 = 1"
        params: list[object] = []
        if group_type is not None:
            where += " AND group_type = ?"
            params.append(_require_group_type(group_type))
        if active_only:
            where += " AND is_active = 1"
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {SYMBOL_GROUPS} WHERE {where}", tuple(params))
        return int(row["n"]) if row is not None else 0

    def list_groups_with_counts(
        self,
        *,
        group_type: str | None = None,
        market: str | None = None,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        """列出集合并附带成员数量（一条 LEFT JOIN 查询，供市场 API 直接使用）。

        与 list_groups 的区别：额外返回 `symbol_count`（跨市场的集合也照实统计）。
        """
        where = "1 = 1"
        params: list[object] = []
        if group_type is not None:
            where += " AND g.group_type = ?"
            params.append(_require_group_type(group_type))
        if market is not None:
            where += " AND g.market = ?"
            params.append(require_text(market, "market"))
        if active_only:
            where += " AND g.is_active = 1"
        sql = (
            f"SELECT g.*, COUNT(m.id) AS symbol_count FROM {SYMBOL_GROUPS} g "
            f"LEFT JOIN {SYMBOL_GROUP_MEMBERS} m ON m.group_id = g.id "
            f"WHERE {where} GROUP BY g.id {_ORDER_BY_ALIASED}"
        )
        return [dict(row) for row in self.query_all(sql, tuple(params))]

    def list_groups_with_stats(
        self,
        *,
        group_type: str | None = None,
        market: str | None = None,
        active_only: bool = False,
        timeframe: str = "1d",
    ) -> list[dict[str, Any]]:
        """列出集合 + 成员数 + **板块行情聚合**（Stage 22）。

        与 list_groups_with_counts 的区别：额外给出板块内的涨跌统计，
        用于市场总览的板块 Tile（红涨绿跌、颜色深浅）。

        实现要点：
            - **一条 SQL 完成全部板块的聚合**（CTE + 窗口函数），
              不按板块逐个查询 → 不产生 N+1
            - 行情定义与 MarketService.latest_quote 一致：
              取该 timeframe 下每个标的的**最后两根**收盘价，涨跌幅 = (收 - 前收) / 前收 * 100
            - 没有有效行情的标的（无 K 线 / 只有一根 / 前收为 0）不计入任何涨跌统计，
              但仍计入 symbol_count；缺失就是缺失，不补 0

        返回字段（在 g.* 之外补充）：
            symbol_count / quoted_count / up_count / down_count / flat_count /
            avg_change_percent（等权平均，仅对有有效行情的标的取均值；无有效行情时为 None）
        """
        where = "1 = 1"
        params: list[object] = [require_timeframe(timeframe)]
        if group_type is not None:
            where += " AND g.group_type = ?"
            params.append(_require_group_type(group_type))
        if market is not None:
            where += " AND g.market = ?"
            params.append(require_text(market, "market"))
        if active_only:
            where += " AND g.is_active = 1"

        sql = (
            "WITH ranked AS ("
            "  SELECT symbol_id, close, ROW_NUMBER() OVER ("
            "    PARTITION BY symbol_id ORDER BY timestamp DESC) AS rn"
            f"  FROM {models.KLINES_TABLE} WHERE timeframe = ?"
            "), "
            "symbol_change AS ("
            "  SELECT cur.symbol_id AS symbol_id,"
            "         CASE WHEN cur.close IS NULL OR prev.close IS NULL OR prev.close = 0 THEN NULL"
            "              ELSE (cur.close - prev.close) * 100.0 / prev.close END AS change_percent"
            "  FROM ranked cur LEFT JOIN ranked prev"
            "    ON prev.symbol_id = cur.symbol_id AND prev.rn = 2"
            "  WHERE cur.rn = 1"
            ")"
            "SELECT g.*, COUNT(m.id) AS symbol_count,"
            "       SUM(CASE WHEN sc.change_percent IS NOT NULL THEN 1 ELSE 0 END) AS quoted_count,"
            "       SUM(CASE WHEN sc.change_percent > 0 THEN 1 ELSE 0 END) AS up_count,"
            "       SUM(CASE WHEN sc.change_percent < 0 THEN 1 ELSE 0 END) AS down_count,"
            "       SUM(CASE WHEN sc.change_percent = 0 THEN 1 ELSE 0 END) AS flat_count,"
            "       AVG(sc.change_percent) AS avg_change_percent"
            f" FROM {SYMBOL_GROUPS} g"
            f" LEFT JOIN {SYMBOL_GROUP_MEMBERS} m ON m.group_id = g.id"
            f" LEFT JOIN {SYMBOLS} s ON s.id = m.symbol_id"
            " LEFT JOIN symbol_change sc ON sc.symbol_id = m.symbol_id"
            f" WHERE {where} GROUP BY g.id {_ORDER_BY_ALIASED}"
        )
        return [dict(row) for row in self.query_all(sql, tuple(params))]

    def update_group(
        self,
        group_id: int,
        *,
        name: str | None = None,
        group_type: str | None = None,
        market: Any = UNSET,
        description: Any = UNSET,
        is_active: bool | None = None,
    ) -> bool:
        """局部更新集合：只改传入的字段，返回是否有记录被更新。

        market / description 支持显式置空：传 None 即写 NULL；
        不传（默认 UNSET）表示保持原值。name 不能置空（NOT NULL）。
        """
        group_id = require_positive_int(group_id, "group_id")
        assignments: list[str] = []
        params: list[object] = []

        if name is not None:
            assignments.append("name = ?")
            params.append(_require_group_name(name))
        if group_type is not None:
            assignments.append("group_type = ?")
            params.append(_require_group_type(group_type))
        if market is not UNSET:
            assignments.append("market = ?")
            params.append(optional_text(market, "market"))
        if description is not UNSET:
            assignments.append("description = ?")
            params.append(optional_text(description, "description"))
        if is_active is not None:
            assignments.append("is_active = ?")
            params.append(_normalize_flag(is_active))

        if not assignments:
            raise ValueError("update_group 至少需要一个要修改的字段")

        assignments.append("updated_at = ?")
        params.append(models.utc_now_str())
        params.append(group_id)
        sql = f"UPDATE {SYMBOL_GROUPS} SET {', '.join(assignments)} WHERE id = ?"
        with self.transaction() as conn:
            try:
                cursor = conn.execute(sql, tuple(params))
            except sqlite3.Error as exc:
                raise enrich_error(exc, sql, tuple(params)) from exc
            return cursor.rowcount > 0

    def deactivate_group(self, group_id: int) -> bool:
        """逻辑停用集合（is_active = 0），返回是否有记录被更新；不物理删除。"""
        return self.update_group(group_id, is_active=False)

    # ---------------------------------------------------------------- 成员关系
    def add_symbol_to_group(self, group_id: int, symbol_id: int) -> int:
        """把标的加入集合，返回成员关系 id（幂等）。

        - 集合或标的不存在 → ValueError
        - 已经是成员 → 直接返回既有成员 id（不报错、不重复插入）
        """
        group_id = require_positive_int(group_id, "group_id")
        symbol_id = require_positive_int(symbol_id, "symbol_id")

        if self.get_group(group_id) is None:
            raise ValueError(f"集合不存在: group_id={group_id}")
        if self.query_one(f"SELECT id FROM {SYMBOLS} WHERE id = ?", (symbol_id,)) is None:
            raise ValueError(f"标的不存在: symbol_id={symbol_id}")

        existing = self.query_one(
            f"SELECT id FROM {SYMBOL_GROUP_MEMBERS} WHERE group_id = ? AND symbol_id = ?",
            (group_id, symbol_id),
        )
        if existing is not None:
            return int(existing["id"])

        params = (group_id, symbol_id, models.utc_now_str())
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_MEMBER_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_MEMBER_SQL, params) from exc
            return int(cursor.lastrowid)

    def bulk_add_symbols_to_group(self, group_id: int, symbol_ids: Iterable[int]) -> int:
        """**批量**把标的加入集合，返回真正新增的条数（Stage 025）。

        为什么需要它：逐条 add_symbol_to_group 每条都要独立事务 + 多次查询，
        几万条关系会非常慢。本方法：
            - 一次 SELECT 确认集合存在
            - 一次 executemany 写入（单个事务、批量提交）
            - INSERT OR IGNORE + UNIQUE(group_id, symbol_id)，**幂等**：
              已存在的成员自动跳过，不报错、不重复
            - 单条失败则整批回滚（原子），不会出现“写一半”的状态

        参数 symbol_ids 可以是任意可迭代对象（会分块处理，不会一次构造巨大列表）。
        """
        group_id = require_positive_int(group_id, "group_id")
        if self.query_one(f"SELECT id FROM {SYMBOL_GROUPS} WHERE id = ?", (group_id,)) is None:
            raise ValueError(f"集合不存在: group_id={group_id}")

        inserted = 0
        now = models.utc_now_str()
        batch: list[tuple[int, int, str]] = []
        for symbol_id in symbol_ids:
            batch.append((group_id, require_positive_int(symbol_id, "symbol_id"), now))
            if len(batch) >= _BULK_BATCH_SIZE:
                inserted += self._insert_members_batch(batch)
                batch.clear()
        if batch:
            inserted += self._insert_members_batch(batch)
        return inserted

    def _insert_members_batch(self, batch: Sequence[tuple[int, int, str]]) -> int:
        """一批成员关系写入（单事务 + executemany + INSERT OR IGNORE）。"""
        sql = (
            f"INSERT OR IGNORE INTO {SYMBOL_GROUP_MEMBERS} (group_id, symbol_id, created_at) "
            "VALUES (?, ?, ?)"
        )
        with self.transaction() as conn:
            try:
                cursor = conn.executemany(sql, list(batch))
            except sqlite3.Error as exc:
                raise enrich_error(exc, sql, f"{len(batch)} 行批量成员") from exc
            return int(cursor.rowcount or 0)

    def remove_symbol_from_group(self, group_id: int, symbol_id: int) -> bool:
        """把标的移出集合，返回是否真的删除了一条成员关系（不存在时为 False）。"""
        group_id = require_positive_int(group_id, "group_id")
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        sql = f"DELETE FROM {SYMBOL_GROUP_MEMBERS} WHERE group_id = ? AND symbol_id = ?"
        with self.transaction() as conn:
            try:
                cursor = conn.execute(sql, (group_id, symbol_id))
            except sqlite3.Error as exc:
                raise enrich_error(exc, sql, (group_id, symbol_id)) from exc
            return cursor.rowcount > 0

    def list_group_symbols(self, group_id: int, *, active_only: bool = False) -> list[dict[str, Any]]:
        """列出集合内的标的（按 symbol 升序）；不存在集合 → 空列表。"""
        group_id = require_positive_int(group_id, "group_id")
        if self.get_group(group_id) is None:
            return []
        where = "m.group_id = ?"
        if active_only:
            where += " AND s.is_active = 1"
        sql = (
            f"SELECT s.* FROM {SYMBOL_GROUP_MEMBERS} m "
            f"JOIN {SYMBOLS} s ON s.id = m.symbol_id "
            f"WHERE {where} ORDER BY s.symbol ASC, s.id ASC"
        )
        return [dict(row) for row in self.query_all(sql, (group_id,))]

    def count_group_symbols(self, group_id: int) -> int:
        """集合内标的数量。"""
        group_id = require_positive_int(group_id, "group_id")
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM {SYMBOL_GROUP_MEMBERS} WHERE group_id = ?", (group_id,)
        )
        return int(row["n"]) if row is not None else 0

    def list_groups_of_symbol(self, symbol_id: int) -> list[dict[str, Any]]:
        """反向查询：某标的属于哪些集合（用 idx_symbol_group_members_symbol 索引）。"""
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        sql = (
            f"SELECT g.* FROM {SYMBOL_GROUP_MEMBERS} m "
            f"JOIN {SYMBOL_GROUPS} g ON g.id = m.group_id "
            "WHERE m.symbol_id = ? ORDER BY g.group_type ASC, g.name ASC, g.id ASC"
        )
        return [dict(row) for row in self.query_all(sql, (symbol_id,))]
