"""持仓数据访问层（Stage 08）。

PositionRepository 建立在通用 Repository 之上，只负责 positions 表。

核心语义：
    - Position 描述**当前实际持仓**，不是成交记录、不是订单、不含盈亏。
    - **同一 symbol 只能有一条持仓**：由数据库 UNIQUE(symbol_id) 强制，
      重复创建会抛 sqlite3.IntegrityError；本模块不做 Python 层重复校验。
    - symbol_id 为真实外键，引用 symbols(id)，不复制一套 Python 外键检查。
    - quantity 允许为 0（已清仓）或负数（做空），故意不加正值限制。
    - 本阶段只做**存储层**：不会根据 trades 自动推算持仓，也不计算盈亏。
    - 读取方法返回 sqlite3.Row，与其他业务表保持一致。

字段校验复用 database/validation.py。
"""

from __future__ import annotations

import sqlite3

from . import models
from .repository import Repository, enrich_error
from .validation import require_number, require_positive_int

POSITIONS = models.POSITIONS_TABLE

_INSERT_SQL = (
    f"INSERT INTO {POSITIONS} (symbol_id, quantity, avg_price, updated_at) "
    "VALUES (?, ?, ?, ?)"
)

# symbol_id 有 UNIQUE 约束，按它排序结果天然稳定
_ORDER_BY = "ORDER BY symbol_id ASC"


class PositionRepository(Repository):
    """positions 表的数据访问对象。"""

    # ------------------------------------------------------------------ 创建
    def create_position(self, symbol_id: int, quantity: float, avg_price: float) -> int:
        """创建一条持仓记录，返回新记录 id。

        - 同一个 symbol_id 已存在时抛 sqlite3.IntegrityError（由数据库 UNIQUE 保证）
        - symbol_id 不存在于 symbols 时抛 sqlite3.IntegrityError（由数据库外键保证）
        - quantity / avg_price 只做数字类型校验，不限制正负
        """
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        quantity = require_number(quantity, "quantity")
        avg_price = require_number(avg_price, "avg_price")

        params = (symbol_id, quantity, avg_price, models.utc_now_str())
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, params) from exc
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 查询
    def get_position(self, position_id: int) -> sqlite3.Row | None:
        """按 id 查询持仓；不存在时返回 None。"""
        position_id = require_positive_int(position_id, "position_id")
        return self.query_one(f"SELECT * FROM {POSITIONS} WHERE id = ?", (position_id,))

    def get_position_by_symbol(self, symbol_id: int) -> sqlite3.Row | None:
        """按 symbol_id 查询持仓；不存在时返回 None。"""
        symbol_id = require_positive_int(symbol_id, "symbol_id")
        return self.query_one(
            f"SELECT * FROM {POSITIONS} WHERE symbol_id = ?", (symbol_id,)
        )

    def list_positions(self) -> list[sqlite3.Row]:
        """返回全部持仓，按 symbol_id 升序（每个 symbol 最多一条）。"""
        return self.query_all(f"SELECT * FROM {POSITIONS} {_ORDER_BY}")

    # ------------------------------------------------------------------ 更新
    def update_position(
        self,
        position_id: int,
        *,
        quantity: float | None = None,
        avg_price: float | None = None,
    ) -> bool:
        """更新持仓的 quantity / avg_price（以及 updated_at），返回是否更新到了记录。

        只更新显式传入的字段；未传任何字段时抛 ValueError（避免生成空 UPDATE）。
        """
        position_id = require_positive_int(position_id, "position_id")

        assignments: list[str] = []
        params: list[object] = []
        if quantity is not None:
            assignments.append("quantity = ?")
            params.append(require_number(quantity, "quantity"))
        if avg_price is not None:
            assignments.append("avg_price = ?")
            params.append(require_number(avg_price, "avg_price"))

        if not assignments:
            raise ValueError("update_position 至少需要提供 quantity / avg_price 之一")

        assignments.append("updated_at = ?")
        params.append(models.utc_now_str())
        params.append(position_id)

        sql = f"UPDATE {POSITIONS} SET {', '.join(assignments)} WHERE id = ?"
        return self.execute(sql, tuple(params)) > 0
