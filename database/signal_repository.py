"""交易信号数据访问层（Stage 06）。

SignalRepository 建立在通用 Repository 之上，只负责 signals 表。

核心语义：
    - create_signal 是**严格创建**，不做自动 upsert；重复数据不会被静默覆盖。
    - strategy_id / symbol_id 的**引用完整性由数据库外键负责**：
      指向不存在的策略或标的会抛 sqlite3.IntegrityError，
      本模块**不重复实现**一套 Python 外键检查。
    - direction 只能是 long / short；status 只能是 pending / executed / cancelled，
      非法值在 Python 层直接报错（不静默转换），数据库层还有 CHECK 兜底。
    - timestamp 表示**信号发生时间**（UTC，格式与其他表一致）。
    - 读取方法返回 sqlite3.Row，与其他业务表保持一致。

字段校验复用 database/validation.py，不在本模块重复实现。
"""

from __future__ import annotations

import sqlite3

from . import models
from .repository import Repository, enrich_error
from .validation import (
    optional_text,
    require_choice,
    require_number,
    require_positive_int,
    require_positive_int_limit,
    require_timeframe,
    require_timestamp,
)

SIGNALS = models.SIGNALS_TABLE

_INSERT_SQL = (
    f"INSERT INTO {SIGNALS} "
    "(strategy_id, symbol_id, timeframe, timestamp, direction, price, reason, status, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# 稳定排序：timestamp 是秒级精度，可能相同，用 id 作次级键保证结果稳定
_ORDER_BY = "ORDER BY timestamp ASC, id ASC"
_ORDER_BY_DESC = "ORDER BY timestamp DESC, id DESC"


class SignalRepository(Repository):
    """signals 表的数据访问对象。"""

    # ------------------------------------------------------------------ 内部
    def _build_filters(
        self,
        *,
        strategy_id: int | None,
        symbol_id: int | None,
        timeframe: str | None,
        status: str | None,
    ) -> tuple[str, list[object]]:
        """把可选筛选条件拼成 WHERE 子句（list 与 count 共用，避免重复）。"""
        where = "1 = 1"
        params: list[object] = []
        if strategy_id is not None:
            where += " AND strategy_id = ?"
            params.append(require_positive_int(strategy_id, "strategy_id"))
        if symbol_id is not None:
            where += " AND symbol_id = ?"
            params.append(require_positive_int(symbol_id, "symbol_id"))
        if timeframe is not None:
            where += " AND timeframe = ?"
            params.append(require_timeframe(timeframe))
        if status is not None:
            where += " AND status = ?"
            params.append(require_choice(status, "status", models.SIGNAL_STATUSES))
        return where, params

    # ------------------------------------------------------------------ 创建
    def create_signal(
        self,
        strategy_id: int,
        symbol_id: int,
        timeframe: str,
        timestamp: str,
        direction: str,
        price: float,
        *,
        reason: str | None = None,
        status: str = models.DEFAULT_SIGNAL_STATUS,
    ) -> int:
        """严格创建一条信号，返回新记录 id。

        - 不存在对应 strategy / symbol 时由数据库外键拒绝（sqlite3.IntegrityError）
        - direction / status 非法时抛 ValueError
        """
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        timeframe = require_timeframe(timeframe)
        timestamp = require_timestamp(timestamp)
        direction = require_choice(direction, "direction", models.SIGNAL_DIRECTIONS)
        price = require_number(price, "price")
        reason = optional_text(reason, "reason")
        status = require_choice(status, "status", models.SIGNAL_STATUSES)

        params = (
            strategy_id,
            symbol_id,
            timeframe,
            timestamp,
            direction,
            price,
            reason,
            status,
            models.utc_now_str(),
        )
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, params) from exc
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 查询
    def get_signal(self, signal_id: int) -> sqlite3.Row | None:
        """按 id 查询信号；不存在时返回 None。"""
        signal_id = require_positive_int(signal_id, "signal_id")
        return self.query_one(f"SELECT * FROM {SIGNALS} WHERE id = ?", (signal_id,))

    def list_signals(
        self,
        *,
        strategy_id: int | None = None,
        symbol_id: int | None = None,
        timeframe: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """查询信号列表，默认按 timestamp 升序、id 升序返回（结果稳定）。

        - strategy_id / symbol_id / timeframe / status 只作为筛选条件，不传则不过滤
        - limit 语义与 KlineRepository / StrategyRepository 保持一致：
          **最近的 N 条**（timestamp 最大者），返回时仍按升序排列
        """
        where, params = self._build_filters(
            strategy_id=strategy_id,
            symbol_id=symbol_id,
            timeframe=timeframe,
            status=status,
        )

        if limit is None:
            sql = f"SELECT * FROM {SIGNALS} WHERE {where} {_ORDER_BY}"
        else:
            require_positive_int_limit(limit)
            # 先取"最近 N 条"，再在外层恢复升序，避免与"升序取前 N 条"混淆
            sql = (
                f"SELECT * FROM ("
                f"  SELECT * FROM {SIGNALS} WHERE {where} {_ORDER_BY_DESC} LIMIT ?"
                f") {_ORDER_BY}"
            )
            params.append(limit)

        return self.query_all(sql, tuple(params))

    def count_signals(
        self,
        *,
        strategy_id: int | None = None,
        symbol_id: int | None = None,
        timeframe: str | None = None,
        status: str | None = None,
    ) -> int:
        """统计信号数量（筛选条件与 list_signals 一致）。"""
        where, params = self._build_filters(
            strategy_id=strategy_id,
            symbol_id=symbol_id,
            timeframe=timeframe,
            status=status,
        )
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {SIGNALS} WHERE {where}", tuple(params))
        return int(row["n"]) if row is not None else 0

    # ------------------------------------------------------------------ 更新
    def update_signal_status(self, signal_id: int, status: str) -> bool:
        """只更新 status，返回是否更新到了记录。

        非法状态直接抛 ValueError，不做静默转换。
        （signals 表没有 updated_at 字段，按设计不修改 created_at 等其他列。）
        """
        signal_id = require_positive_int(signal_id, "signal_id")
        status = require_choice(status, "status", models.SIGNAL_STATUSES)
        sql = f"UPDATE {SIGNALS} SET status = ? WHERE id = ?"
        return self.execute(sql, (status, signal_id)) > 0
