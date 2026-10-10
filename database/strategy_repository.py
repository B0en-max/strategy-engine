"""策略定义与策略版本的数据访问层（Stage 05）。

StrategyRepository 建立在通用 Repository 之上，只负责 strategies 表。

核心语义：
    - **每个策略版本都是一条独立记录**：修改策略不覆盖旧记录，而是新建
      (name, version) 的另一条记录。这样未来复盘某笔交易时能准确知道
      "当时使用的是哪个策略版本"。
    - create_strategy 是**严格创建**：若 (name, version) 已存在，直接抛出
      数据库的 sqlite3.IntegrityError，**不自动覆盖**已有版本。
    - config 用 JSON 字符串存储（TEXT 列），Python 层负责
      dict → json.dumps → TEXT 与 TEXT → json.loads → dict，
      **不依赖** SQLite JSON1 扩展。
    - 读取方法返回的是 **dict**（而不是 sqlite3.Row），因为 config 需要
      从 JSON 字符串还原成 dict 才能与调用方的原始配置做等值比较。
    - 时间：created_at 为首次创建时间（UTC），任何更新只刷新 updated_at。

Python 层与数据库层的职责划分：
    Python 层：参数类型（name 非空字符串、config 必须是 dict、version 正整数、
        status 必须是合法状态）+ JSON 序列化与反序列化。
    数据库层：UNIQUE(name, version)、CHECK(version >= 1)、
        CHECK(status IN (...))、NOT NULL、DEFAULT。
    非法 status 在 Python 层直接报错，**不做静默转换**。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from . import models
from .repository import Repository, enrich_error, unit_of_work
from .strategy_definition_repository import StrategyDefinitionRepository
from .validation import require_positive_int

STRATEGIES = models.STRATEGIES_TABLE

_INSERT_SQL = (
    f"INSERT INTO {STRATEGIES} "
    "(name, description, status, version, config, strategy_id, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)

# 稳定排序：created_at 有秒级精度，可能相同，用 id 作为次级排序键保证结果稳定
_ORDER_BY = "ORDER BY created_at ASC, id ASC"
_ORDER_BY_DESC = "ORDER BY created_at DESC, id DESC"

#: 策略状态：与数据库 CHECK 约束共用同一个来源（models.STRATEGY_STATUSES）
STRATEGY_STATUSES = models.STRATEGY_STATUSES


# --------------------------------------------------------------------------
# 参数校验 / JSON 序列化
# --------------------------------------------------------------------------
def _require_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"name 必须是非空字符串，实际为 {value!r}")
    return value.strip()


def _require_description(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"description 必须是字符串或 None，实际为 {value!r}")
    return value


def _require_status(value: object) -> str:
    """校验状态取值；非法值直接报错，不静默转换。"""
    if not isinstance(value, str) or value not in STRATEGY_STATUSES:
        raise ValueError(
            f"status 必须是 {list(STRATEGY_STATUSES)} 之一，实际为 {value!r}"
        )
    return value


def _require_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"version 必须是整数，实际为 {value!r}")
    if value < 1:
        raise ValueError(f"version 必须 >= 1，实际为 {value!r}")
    return value


def _require_config_mapping(config: object) -> Mapping[str, Any]:
    """config 必须是 dict（映射），拒绝 None / list / str / int 等类型。"""
    if not isinstance(config, Mapping):
        raise ValueError(
            f"config 必须是 dict（映射），实际为 {type(config).__name__}: {config!r}"
        )
    return config


def dump_config(config: object) -> str:
    """把 config（dict）序列化为 JSON 字符串。"""
    mapping = _require_config_mapping(config)
    try:
        return json.dumps(dict(mapping), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"config 无法序列化为 JSON: {exc}") from exc


def load_config(text: str) -> dict[str, Any]:
    """把数据库里的 JSON 字符串还原为 dict。"""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"config 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"config 必须是 JSON 对象，实际为 {type(parsed).__name__}")
    return parsed


def _row_to_strategy(row: sqlite3.Row) -> dict[str, Any]:
    """把数据库行转换成策略 dict，并把 config 从 JSON 字符串还原为 dict。"""
    strategy = dict(row)
    try:
        strategy["config"] = load_config(row["config"])
    except ValueError as exc:
        raise ValueError(f"strategies.id={row['id']} 读取失败: {exc}") from exc
    return strategy


class StrategyRepository(Repository):
    """strategies 表的数据访问对象。"""

    # ------------------------------------------------------------------ 创建
    def create_strategy(
        self,
        name: str,
        config: Mapping[str, Any],
        *,
        description: str | None = None,
        status: str = models.DEFAULT_STRATEGY_STATUS,
        version: int = models.DEFAULT_STRATEGY_VERSION,
        strategy_id: int | None = None,
    ) -> int:
        """严格创建一个策略版本，返回新记录 id。

        - (name, version) 已存在时抛出 sqlite3.IntegrityError，**不覆盖旧版本**
        - config 必须是 dict；写入前会被 json.dumps 成 JSON 字符串
        - strategy_id（Stage 19）：该版本属于哪个 Strategy（strategy_definitions.id）；
          不传时按 name 自动 ensure 一个 Strategy 身份（老调用方式保持可用，
          新代码推荐用 create_version() 显式指定 Strategy）
        """
        name = _require_name(name)
        description = _require_description(description)
        status = _require_status(status)
        version = _require_version(version)
        config_json = dump_config(config)
        if strategy_id is not None:
            strategy_id = require_positive_int(strategy_id, "strategy_id")

        definitions = StrategyDefinitionRepository(self.db_path)
        now = models.utc_now_str()

        # Strategy 身份 + 版本在同一个事务里写入：要么都成立，要么都不写
        with unit_of_work(self, definitions):
            resolved_strategy_id = (
                definitions.ensure_definition(name, description=description)
                if strategy_id is None
                else strategy_id
            )
            params = (
                name,
                description,
                status,
                version,
                config_json,
                resolved_strategy_id,
                now,
                now,
            )
            with self.transaction() as conn:
                try:
                    cursor = conn.execute(_INSERT_SQL, params)
                except sqlite3.Error as exc:
                    raise enrich_error(exc, _INSERT_SQL, params) from exc
                return int(cursor.lastrowid)

    # ------------------------------------------------------------------ 版本
    def next_version(self, strategy_id: int) -> int:
        """该 Strategy 的下一个版本号（还没有版本时从 1 开始）。"""
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        row = self.query_one(
            f"SELECT COALESCE(MAX(version), 0) + 1 AS next FROM {STRATEGIES} "
            "WHERE strategy_id = ?",
            (strategy_id,),
        )
        return int(row["next"]) if row is not None else 1

    def create_version(
        self,
        strategy_id: int,
        config: Mapping[str, Any],
        *,
        version: int | None = None,
        status: str = models.DEFAULT_STRATEGY_STATUS,
        description: str | None = None,
    ) -> int:
        """给指定 Strategy 新建一个版本，返回版本记录 id（signals.strategy_id 指向它）。

        - strategy_id 指向 strategy_definitions（Strategy 身份）；不存在时抛 ValueError
        - version 不传时自动取「当前最大版本 + 1」
        - name / description 继承 Strategy 身份（版本与策略保持同名，避免两处各写一份）
        - (name, version) 已存在时抛 sqlite3.IntegrityError，**不覆盖旧版本**
        """
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        definition = StrategyDefinitionRepository(self.db_path).get_definition(strategy_id)
        if definition is None:
            raise ValueError(f"策略不存在: {strategy_id}")

        resolved_version = (
            self.next_version(strategy_id) if version is None else _require_version(version)
        )
        resolved_description = (
            definition["description"] if description is None else description
        )
        return self.create_strategy(
            definition["name"],
            config,
            description=resolved_description,
            status=status,
            version=resolved_version,
            strategy_id=strategy_id,
        )

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        """按版本记录 id 查询版本（等价于 get_strategy，名字更贴近语义）。"""
        return self.get_strategy(version_id)

    def get_version_by_number(self, strategy_id: int, version: int) -> dict[str, Any] | None:
        """按 (Strategy 身份, 版本号) 查询版本；不存在时返回 None。"""
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        version = _require_version(version)
        row = self.query_one(
            f"SELECT * FROM {STRATEGIES} WHERE strategy_id = ? AND version = ?",
            (strategy_id, version),
        )
        return _row_to_strategy(row) if row is not None else None

    def list_versions(
        self,
        strategy_id: int,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出某个 Strategy 的全部版本（按 version 升序，结果稳定）。"""
        strategy_id = require_positive_int(strategy_id, "strategy_id")
        where = "strategy_id = ?"
        params: list[object] = [strategy_id]
        if status is not None:
            where += " AND status = ?"
            params.append(_require_status(status))
        sql = f"SELECT * FROM {STRATEGIES} WHERE {where} ORDER BY version ASC, id ASC"
        return [_row_to_strategy(row) for row in self.query_all(sql, tuple(params))]

    # ------------------------------------------------------------------ 查询
    def get_strategy(self, strategy_id: int) -> dict[str, Any] | None:
        """按 id 查询策略；不存在时返回 None。"""
        if isinstance(strategy_id, bool) or not isinstance(strategy_id, int):
            raise ValueError(f"strategy_id 必须是整数，实际为 {strategy_id!r}")
        row = self.query_one(f"SELECT * FROM {STRATEGIES} WHERE id = ?", (strategy_id,))
        return _row_to_strategy(row) if row is not None else None

    def get_strategy_by_name_version(self, name: str, version: int) -> dict[str, Any] | None:
        """按 (name, version) 查询策略；不存在时返回 None。"""
        name = _require_name(name)
        version = _require_version(version)
        row = self.query_one(
            f"SELECT * FROM {STRATEGIES} WHERE name = ? AND version = ?",
            (name, version),
        )
        return _row_to_strategy(row) if row is not None else None

    def list_strategies(
        self,
        *,
        status: str | None = None,
        name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """查询策略列表，按 created_at 升序、id 升序返回（结果稳定）。

        - status / name 只作为过滤条件，不传则不过滤
        - limit 语义与 KlineRepository.list_klines 保持一致：
          **最近的 N 条**（created_at 最大者），返回时仍按升序排列
        """
        where = "1 = 1"
        params: list[object] = []
        if status is not None:
            where += " AND status = ?"
            params.append(_require_status(status))
        if name is not None:
            where += " AND name = ?"
            params.append(_require_name(name))

        if limit is None:
            sql = f"SELECT * FROM {STRATEGIES} WHERE {where} {_ORDER_BY}"
        else:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError(f"limit 必须是正整数，实际为 {limit!r}")
            # 先取"最近 N 条"，再在外层恢复升序，避免与"升序取前 N 条"混淆
            sql = (
                f"SELECT * FROM ("
                f"  SELECT * FROM {STRATEGIES} WHERE {where} {_ORDER_BY_DESC} LIMIT ?"
                f") {_ORDER_BY}"
            )
            params.append(limit)

        return [_row_to_strategy(row) for row in self.query_all(sql, tuple(params))]

    def count_strategies(
        self,
        *,
        status: str | None = None,
        name: str | None = None,
    ) -> int:
        """统计策略数量（可按 status / name 过滤）。"""
        where = "1 = 1"
        params: list[object] = []
        if status is not None:
            where += " AND status = ?"
            params.append(_require_status(status))
        if name is not None:
            where += " AND name = ?"
            params.append(_require_name(name))
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {STRATEGIES} WHERE {where}", tuple(params))
        return int(row["n"]) if row is not None else 0

    # ------------------------------------------------------------------ 更新
    def update_strategy_status(self, strategy_id: int, status: str) -> bool:
        """只更新 status（以及 updated_at），返回是否更新到了记录。

        非法状态直接抛 ValueError，不做静默转换。
        name / version / config / created_at 不受影响。
        """
        if isinstance(strategy_id, bool) or not isinstance(strategy_id, int):
            raise ValueError(f"strategy_id 必须是整数，实际为 {strategy_id!r}")
        status = _require_status(status)
        sql = f"UPDATE {STRATEGIES} SET status = ?, updated_at = ? WHERE id = ?"
        return self.execute(sql, (status, models.utc_now_str(), strategy_id)) > 0

    def update_strategy_config(self, strategy_id: int, config: Mapping[str, Any]) -> bool:
        """只更新 config（以及 updated_at），返回是否更新到了记录。

        不允许修改 name / version / created_at / status。
        """
        if isinstance(strategy_id, bool) or not isinstance(strategy_id, int):
            raise ValueError(f"strategy_id 必须是整数，实际为 {strategy_id!r}")
        config_json = dump_config(config)
        sql = f"UPDATE {STRATEGIES} SET config = ?, updated_at = ? WHERE id = ?"
        return self.execute(sql, (config_json, models.utc_now_str(), strategy_id)) > 0
