"""交易记录（实际成交）数据访问层（Stage 07）。

TradeRepository 建立在通用 Repository 之上，只负责 trades 表。

数据关系：strategies → signals → trades

核心语义：
    - Trade 表示**已经发生的实际成交**，不是委托 / 订单，也不是持仓。
    - signal_id 允许为空：手工交易可能没有对应 Signal；非空时由数据库外键
      引用 signals(id)。
    - symbol_id 必填，由数据库外键引用 symbols(id)。
    - side 只能是 buy / sell。
    - 引用完整性与必要约束**全部由数据库负责**，本模块不复制一套 Python 检查。
    - 读取方法返回 sqlite3.Row，与其他业务表保持一致。
    - 本模块**不做**盈亏计算、持仓、订单、回测（属于后续阶段）。

字段校验复用 database/validation.py。
"""

from __future__ import annotations

import sqlite3

from . import models
from .repository import Repository, enrich_error
from .validation import (
    optional_positive_int,
    require_choice,
    require_number,
    require_positive_int,
    require_positive_int_limit,
    require_timestamp,
)

TRADES = models.TRADES_TABLE

_INSERT_SQL = (
    f"INSERT INTO {TRADES} "
    "(signal_id, symbol_id, side, quantity, price, timestamp, fee, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# 稳定排序：timestamp 是秒级精度，可能相同，用 id 作次级键保证结果稳定
_ORDER_BY = "ORDER BY timestamp ASC, id ASC"
_ORDER_BY_DESC = "ORDER BY timestamp DESC, id DESC"


class TradeRepository(Repository):
    """trades 表的数据访问对象。"""

    # ------------------------------------------------------------------ 内部
    def _build_filters(
        self,
        *,
        symbol_id: int | None,
        signal_id: int | None,
        side: str | None,
        start_timestamp: str | None,
        end_timestamp: str | None,
    ) -> tuple[str, list[object]]:
        """把可选筛选条件拼成 WHERE 子句（list 与 count 共用，避免重复）。"""
        where = "1 = 1"
        params: list[object] = []
        if symbol_id is not None:
            where += " AND symbol_id = ?"
            params.append(require_positive_int(symbol_id, "symbol_id"))
        if signal_id is not None:
            where += " AND signal_id = ?"
            params.append(require_positive_int(signal_id, "signal_id"))
        if side is not None:
            where += " AND side = ?"
            params.append(require_choice(side, "side", models.TRADE_SIDES))
        if start_timestamp is not None:
            where += " AND timestamp >= ?"
            params.append(require_timestamp(start_timestamp))
        if end_timestamp is not None:
            where += " AND timestamp <= ?"
            params.append(require_timestamp(end_timestamp))
        return where, params

    # ------------------------------------------------------------------ 创建
    def create_trade(
        self,
        symbol_id: int,
        side: str,
        quantity: float,
        price: float,
        timestamp: str,
        *,
        signal_id: int | None = None,
        fee: float = 0.0,
    ) -> int:
        """创建一条成交记录，返回新记录 id。

        - signal_id 可为 None（手工交易），非 None 时必须存在（由数据库外键保证）
        - symbol_id 必须存在（由数据库外键保证）
        - side 非法时抛 ValueError（数据库层还有 CHECK 兜底）
        - quantity / price / fee 只做数字类型校验，不限制正负
          （返佣可能让 fee 为负；合理性判断属于业务层）
        """
        signal_id = optional_positive_int(signal_id, "signal_id")
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        side = require_choice(side, "side", models.TRADE_SIDES)
        quantity = require_number(quantity, "quantity")
        price = require_number(price, "price")
        timestamp = require_timestamp(timestamp)
        fee = require_number(fee, "fee")

        params = (
            signal_id,
            symbol_id,
            side,
            quantity,
            price,
            timestamp,
            fee,
            models.utc_now_str(),
        )
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, params) from exc
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 查询
    def get_trade(self, trade_id: int) -> sqlite3.Row | None:
        """按 id 查询成交记录；不存在时返回 None。"""
        trade_id = require_positive_int(trade_id, "trade_id")
        return self.query_one(f"SELECT * FROM {TRADES} WHERE id = ?", (trade_id,))

    def list_trades(
        self,
        *,
        symbol_id: int | None = None,
        signal_id: int | None = None,
        side: str | None = None,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """查询成交记录，默认按 timestamp 升序、id 升序返回（结果稳定）。

        时间范围语义与 KlineRepository.list_klines 一致：
        start_timestamp <= timestamp <= end_timestamp（**闭区间**）。

        limit 语义与其它 Repository 一致：**最近的 N 条**，返回时仍按升序排列。
        """
        where, params = self._build_filters(
            symbol_id=symbol_id,
            signal_id=signal_id,
            side=side,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        if limit is None:
            sql = f"SELECT * FROM {TRADES} WHERE {where} {_ORDER_BY}"
        else:
            require_positive_int_limit(limit)
            # 先取"最近 N 条"，再在外层恢复升序，避免与"升序取前 N 条"混淆
            sql = (
                f"SELECT * FROM ("
                f"  SELECT * FROM {TRADES} WHERE {where} {_ORDER_BY_DESC} LIMIT ?"
                f") {_ORDER_BY}"
            )
            params.append(limit)

        return self.query_all(sql, tuple(params))

    def count_trades(
        self,
        *,
        symbol_id: int | None = None,
        signal_id: int | None = None,
        side: str | None = None,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> int:
        """统计成交记录数量（筛选条件与 list_trades 一致）。"""
        where, params = self._build_filters(
            symbol_id=symbol_id,
            signal_id=signal_id,
            side=side,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {TRADES} WHERE {where}", tuple(params))
        return int(row["n"]) if row is not None else 0
