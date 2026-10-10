"""Strategy 身份（strategy_definitions）的数据访问层（Stage 19）。

定位（与 strategies 表的分工）：
    strategy_definitions  一个**策略本身**：id / name / description / status / created_at / updated_at
    strategies            该策略的**一个版本**：version + config（每个版本一行，不覆盖旧版）

    StrategyDefinitionRepository  只负责 strategy_definitions
    StrategyRepository            负责 strategies（版本），并用本 Repository 建立关联

语义：
    - create_definition 是**严格创建**：name 已存在时抛 sqlite3.IntegrityError，**不覆盖**
    - ensure_definition 是 get-or-create：创建版本时自动补齐 Strategy 身份（老数据也能追溯），
      已存在的同名 Strategy 不会被改写
    - 非法 status 在 Python 层直接 ValueError，不做静默转换
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import models
from .repository import Repository, enrich_error
from .validation import (
    optional_text,
    require_choice,
    require_positive_int,
    require_positive_int_limit,
    require_text,
)

STRATEGY_DEFINITIONS = models.STRATEGY_DEFINITIONS_TABLE

_INSERT_SQL = (
    f"INSERT INTO {STRATEGY_DEFINITIONS} (name, description, status, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?)"
)

# 稳定排序：created_at 有秒级精度，可能相同，用 id 作为次级排序键
_ORDER_BY = "ORDER BY created_at ASC, id ASC"
_ORDER_BY_DESC = "ORDER BY created_at DESC, id DESC"


def _require_name(value: object) -> str:
    return require_text(value, "name")


def _require_description(value: object) -> str | None:
    return optional_text(value, "description")


def _require_status(value: object) -> str:
    return require_choice(value, "status", models.STRATEGY_STATUSES)


class StrategyDefinitionRepository(Repository):
    """strategy_definitions 表（Strategy 身份）的数据访问对象。"""

    # ------------------------------------------------------------------ 创建
    def create_definition(
        self,
        name: str,
        *,
        description: str | None = None,
        status: str = models.DEFAULT_STRATEGY_STATUS,
    ) -> int:
        """严格创建一个 Strategy 身份，返回新记录 id。

        name 已存在时抛 sqlite3.IntegrityError（不覆盖已有策略）。
        """
        name = _require_name(name)
        description = _require_description(description)
        status = _require_status(status)
        now = models.utc_now_str()
        params = (name, description, status, now, now)
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, params) from exc
            return int(cursor.lastrowid)

    def ensure_definition(
        self,
        name: str,
        *,
        description: str | None = None,
        status: str | None = None,
    ) -> int:
        """get-or-create：同名 Strategy 已存在时直接返回其 id（不改写已有记录）。"""
        existing = self.get_definition_by_name(name)
        if existing is not None:
            return int(existing["id"])
        return self.create_definition(
            name,
            description=description,
            status=models.DEFAULT_STRATEGY_STATUS if status is None else status,
        )

    # ------------------------------------------------------------------ 查询
    def get_definition(self, strategy_id: int) -> dict[str, Any] | None:
        """按 id 查询 Strategy；不存在时返回 None。"""
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        row = self.query_one(f"SELECT * FROM {STRATEGY_DEFINITIONS} WHERE id = ?", (strategy_id,))
        return dict(row) if row is not None else None

    def get_definition_by_name(self, name: str) -> dict[str, Any] | None:
        """按 name 查询 Strategy；不存在时返回 None。"""
        name = _require_name(name)
        row = self.query_one(f"SELECT * FROM {STRATEGY_DEFINITIONS} WHERE name = ?", (name,))
        return dict(row) if row is not None else None

    def list_definitions(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """查询 Strategy 列表，按 created_at 升序、id 升序返回（结果稳定）。"""
        where = "1 = 1"
        params: list[object] = []
        if status is not None:
            where += " AND status = ?"
            params.append(_require_status(status))

        if limit is None:
            sql = f"SELECT * FROM {STRATEGY_DEFINITIONS} WHERE {where} {_ORDER_BY}"
        else:
            sql = (
                f"SELECT * FROM ("
                f"  SELECT * FROM {STRATEGY_DEFINITIONS} WHERE {where} {_ORDER_BY_DESC} LIMIT ?"
                f") {_ORDER_BY}"
            )
            params.append(require_positive_int_limit(limit))

        return [dict(row) for row in self.query_all(sql, tuple(params))]

    def count_definitions(self, *, status: str | None = None) -> int:
        """统计 Strategy 数量（可按 status 过滤）。"""
        where = "1 = 1"
        params: list[object] = []
        if status is not None:
            where += " AND status = ?"
            params.append(_require_status(status))
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM {STRATEGY_DEFINITIONS} WHERE {where}", tuple(params)
        )
        return int(row["n"]) if row is not None else 0

    # ------------------------------------------------------------------ 更新
    def update_definition_status(self, strategy_id: int, status: str) -> bool:
        """只更新 status（以及 updated_at），返回是否更新到了记录。"""
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        status = _require_status(status)
        sql = f"UPDATE {STRATEGY_DEFINITIONS} SET status = ?, updated_at = ? WHERE id = ?"
        return self.execute(sql, (status, models.utc_now_str(), strategy_id)) > 0


__all__ = [
    "STRATEGY_DEFINITIONS",
    "StrategyDefinitionRepository",
]
