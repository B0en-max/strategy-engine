"""标的基础数据访问层（Stage 03）。

SymbolRepository 建立在通用 Repository 基础设施之上，只负责 symbols 表，
不把业务 SQL 塞进通用 Repository，也不新建 services / schemas / DTO 等分层。

设计说明：
    - 标的身份是「market + symbol」的组合（见 models.py 中的唯一性说明）。
    - **不提供物理 DELETE**：后续 K线、交易、回测等数据都会引用 symbol，
      物理删除会造成孤儿数据与不可追溯。停用请使用 deactivate_symbol()
      （即 is_active = 0，逻辑停用）。
    - 唯一性由数据库的 UNIQUE(market, symbol) 强制；重复创建会抛出
      sqlite3.IntegrityError，这里**不做** Python 层去重判断，
      避免出现"Python 认为可以、数据库认为不可以"的双重真相。
    - 时间戳统一使用 UTC 的 'YYYY-MM-DD HH:MM:SS' 格式（models.utc_now_str）。
"""

from __future__ import annotations

import sqlite3

from . import models
from .repository import Repository, enrich_error

# 必须为非空文本的字段
_REQUIRED_TEXT_FIELDS = ("symbol", "name", "market", "asset_type")


def _require_text(value: object, field: str) -> str:
    """校验必填文本字段：必须是非空字符串（去掉首尾空白）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串，实际为 {value!r}")
    return value.strip()


def _normalize_is_active(value: object) -> int:
    """把 is_active 规范化为 0 / 1。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in (0, 1):
        return value
    raise ValueError(f"is_active 只能是 0 或 1，实际为 {value!r}")


class SymbolRepository(Repository):
    """symbols 表的数据访问对象。"""

    # ------------------------------------------------------------------ 创建
    def create_symbol(
        self,
        symbol: str,
        name: str,
        market: str,
        asset_type: str,
        *,
        is_active: int = 1,
    ) -> int:
        """创建一个标的，返回新记录的 id。

        重复的 (market, symbol) 会由数据库 UNIQUE 约束拒绝，
        抛出 sqlite3.IntegrityError。
        """
        symbol = _require_text(symbol, "symbol")
        name = _require_text(name, "name")
        market = _require_text(market, "market")
        asset_type = _require_text(asset_type, "asset_type")
        active = _normalize_is_active(is_active)

        now = models.utc_now_str()
        sql = (
            f"INSERT INTO {models.SYMBOLS_TABLE} "
            "(symbol, name, market, asset_type, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)"
        )
        params = (symbol, name, market, asset_type, active, now, now)

        with self.transaction() as conn:
            try:
                cursor = conn.execute(sql, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, sql, params) from exc
            return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 查询
    def get_symbol(self, market: str, symbol: str) -> sqlite3.Row | None:
        """按 market + symbol 查询单个标的；不存在时返回 None。"""
        market = _require_text(market, "market")
        symbol = _require_text(symbol, "symbol")
        return self.query_one(
            f"SELECT * FROM {models.SYMBOLS_TABLE} WHERE market = ? AND symbol = ?",
            (market, symbol),
        )

    def get_symbol_by_id(self, symbol_id: int) -> sqlite3.Row | None:
        """按主键查询单个标的；不存在时返回 None。"""
        return self.query_one(
            f"SELECT * FROM {models.SYMBOLS_TABLE} WHERE id = ?",
            (int(symbol_id),),
        )

    def list_symbols(self, *, market: str | None = None) -> list[sqlite3.Row]:
        """查询全部标的（**包含已停用的**），按 market、symbol 排序。

        market 传入时只返回该市场的标的。
        """
        if market is None:
            return self.query_all(
                f"SELECT * FROM {models.SYMBOLS_TABLE} ORDER BY market, symbol"
            )
        return self.query_all(
            f"SELECT * FROM {models.SYMBOLS_TABLE} WHERE market = ? ORDER BY symbol",
            (_require_text(market, "market"),),
        )

    def list_active_symbols(self, *, market: str | None = None) -> list[sqlite3.Row]:
        """只返回启用中的标的（is_active = 1）。"""
        if market is None:
            return self.query_all(
                f"SELECT * FROM {models.SYMBOLS_TABLE} "
                "WHERE is_active = 1 ORDER BY market, symbol"
            )
        return self.query_all(
            f"SELECT * FROM {models.SYMBOLS_TABLE} "
            "WHERE is_active = 1 AND market = ? ORDER BY symbol",
            (_require_text(market, "market"),),
        )

    # ------------------------------------------------------------------ 更新
    def update_symbol(
        self,
        market: str,
        symbol: str,
        *,
        name: str | None = None,
        asset_type: str | None = None,
        is_active: int | None = None,
    ) -> bool:
        """更新标的的 name / asset_type / is_active，返回是否更新到了记录。

        只更新显式传入的字段，并同步刷新 updated_at。
        未传任何字段时抛出 ValueError（避免生成空 UPDATE）。
        """
        market = _require_text(market, "market")
        symbol = _require_text(symbol, "symbol")

        assignments: list[str] = []
        params: list[object] = []
        if name is not None:
            assignments.append("name = ?")
            params.append(_require_text(name, "name"))
        if asset_type is not None:
            assignments.append("asset_type = ?")
            params.append(_require_text(asset_type, "asset_type"))
        if is_active is not None:
            assignments.append("is_active = ?")
            params.append(_normalize_is_active(is_active))

        if not assignments:
            raise ValueError("update_symbol 至少需要提供 name / asset_type / is_active 之一")

        assignments.append("updated_at = ?")
        params.append(models.utc_now_str())
        params.extend([market, symbol])

        sql = (
            f"UPDATE {models.SYMBOLS_TABLE} SET {', '.join(assignments)} "
            "WHERE market = ? AND symbol = ?"
        )
        affected = self.execute(sql, tuple(params))
        return affected > 0

    def deactivate_symbol(self, market: str, symbol: str) -> bool:
        """逻辑停用标的（is_active = 0），不删除任何数据。"""
        return self.update_symbol(market, symbol, is_active=0)
