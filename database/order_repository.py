"""订单数据访问层（Stage 09）。

OrderRepository 建立在通用 Repository 之上，只负责 orders 表。

核心语义：
    - 定位：订单是**准备执行 / 提交给执行器的意图**，既不是成交（trades），
      也不是持仓（positions）。
    - 本阶段**只做数据层**：不会自动创建订单、不会自动成交、
      不会修改 Position、不会生成 Trade。
    - signal_id 可为空（手工下单可无 Signal），非空时由数据库外键引用 signals(id)；
      symbol_id 必填，由数据库外键引用 symbols(id)。
    - price 可为空：市价单（market）没有限价。数据层**不**强制
      "limit 单必须有 price" 这类交叉规则（属于执行层业务规则）。
    - side / order_type / status 的取值由数据库 CHECK 限定，
      Python 侧非法值直接抛 ValueError，不静默转换。
    - 时间：created_at 为订单记录的创建时间，updated_at 随状态变更刷新（均为 UTC）；
      时间范围筛选针对 **created_at**（闭区间）。
    - 读取方法返回 sqlite3.Row，与其他业务表保持一致。

字段校验复用 database/validation.py。
"""

from __future__ import annotations

import sqlite3

from . import models
from .repository import Repository, enrich_error
from .validation import (
    optional_number,
    optional_positive_int,
    require_choice,
    require_number,
    require_positive_int,
    require_positive_int_limit,
    require_timestamp,
)

ORDERS = models.ORDERS_TABLE

_INSERT_SQL = (
    f"INSERT INTO {ORDERS} "
    "(signal_id, symbol_id, side, quantity, price, order_type, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# 稳定排序：created_at 是秒级精度，可能相同，用 id 作次级键保证结果稳定
_ORDER_BY = "ORDER BY created_at ASC, id ASC"
_ORDER_BY_DESC = "ORDER BY created_at DESC, id DESC"


class OrderRepository(Repository):
    """orders 表的数据访问对象。"""

    # ------------------------------------------------------------------ 内部
    def _build_filters(
        self,
        *,
        symbol_id: int | None,
        signal_id: int | None,
        status: str | None,
        start_created_at: str | None,
        end_created_at: str | None,
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
        if status is not None:
            where += " AND status = ?"
            params.append(require_choice(status, "status", models.ORDER_STATUSES))
        if start_created_at is not None:
            where += " AND created_at >= ?"
            params.append(require_timestamp(start_created_at))
        if end_created_at is not None:
            where += " AND created_at <= ?"
            params.append(require_timestamp(end_created_at))
        return where, params

    # ------------------------------------------------------------------ 创建
    def create_order(
        self,
        symbol_id: int,
        side: str,
        quantity: float,
        *,
        order_type: str,
        price: float | None = None,
        signal_id: int | None = None,
        status: str = models.DEFAULT_ORDER_STATUS,
    ) -> int:
        """创建一条订单，返回新记录 id。

        - signal_id 可为 None（手工下单），非 None 时必须存在（数据库外键保证）
        - symbol_id 必须存在（数据库外键保证）
        - order_type 必填，只能是 market / limit
        - price 可为 None（市价单）；quantity / price 只做数字类型校验，不限制正负
        - side / status 非法时抛 ValueError
        """
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        side = require_choice(side, "side", models.ORDER_SIDES)
        quantity = require_number(quantity, "quantity")
        order_type = require_choice(order_type, "order_type", models.ORDER_TYPES)
        price = optional_number(price, "price")
        signal_id = optional_positive_int(signal_id, "signal_id")
        status = require_choice(status, "status", models.ORDER_STATUSES)

        now = models.utc_now_str()
        params = (
            signal_id,
            symbol_id,
            side,
            quantity,
            price,
            order_type,
            status,
            now,
            now,
        )
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, params) from exc
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 查询
    def get_order(self, order_id: int) -> sqlite3.Row | None:
        """按 id 查询订单；不存在时返回 None。"""
        order_id = require_positive_int(order_id, "order_id")
        return self.query_one(f"SELECT * FROM {ORDERS} WHERE id = ?", (order_id,))

    def list_orders(
        self,
        *,
        symbol_id: int | None = None,
        signal_id: int | None = None,
        status: str | None = None,
        start_created_at: str | None = None,
        end_created_at: str | None = None,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """查询订单，默认按 created_at 升序、id 升序返回（结果稳定）。

        时间范围语义与其它 Repository 一致：针对 created_at 的**闭区间**
        （start_created_at <= created_at <= end_created_at）。

        limit 语义与其它 Repository 一致：**最近的 N 条**，返回时仍按升序排列。
        """
        where, params = self._build_filters(
            symbol_id=symbol_id,
            signal_id=signal_id,
            status=status,
            start_created_at=start_created_at,
            end_created_at=end_created_at,
        )

        if limit is None:
            sql = f"SELECT * FROM {ORDERS} WHERE {where} {_ORDER_BY}"
        else:
            require_positive_int_limit(limit)
            # 先取"最近 N 条"，再在外层恢复升序，避免与"升序取前 N 条"混淆
            sql = (
                f"SELECT * FROM ("
                f"  SELECT * FROM {ORDERS} WHERE {where} {_ORDER_BY_DESC} LIMIT ?"
                f") {_ORDER_BY}"
            )
            params.append(limit)

        return self.query_all(sql, tuple(params))

    def count_orders(
        self,
        *,
        symbol_id: int | None = None,
        signal_id: int | None = None,
        status: str | None = None,
        start_created_at: str | None = None,
        end_created_at: str | None = None,
    ) -> int:
        """统计订单数量（筛选条件与 list_orders 一致）。"""
        where, params = self._build_filters(
            symbol_id=symbol_id,
            signal_id=signal_id,
            status=status,
            start_created_at=start_created_at,
            end_created_at=end_created_at,
        )
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {ORDERS} WHERE {where}", tuple(params))
        return int(row["n"]) if row is not None else 0

    # ------------------------------------------------------------------ 更新
    def update_order_status(self, order_id: int, status: str) -> bool:
        """只更新 status（以及 updated_at），返回是否更新到了记录。

        非法状态直接抛 ValueError，不做静默转换。
        signal_id / symbol_id / side / quantity / price / order_type / created_at 不受影响。
        """
        order_id = require_positive_int(order_id, "order_id")
        status = require_choice(status, "status", models.ORDER_STATUSES)
        sql = f"UPDATE {ORDERS} SET status = ?, updated_at = ? WHERE id = ?"
        return self.execute(sql, (status, models.utc_now_str(), order_id)) > 0
