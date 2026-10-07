"""最小数据访问层（Stage 02）。

目的：让后续业务模块不必在各处重复写 sqlite3.connect(...)。

设计要点：
    - Repository 只保存数据库路径，**不持有长期打开的连接**
    - 每条语句用完即关连接；单条语句默认立即生效（autocommit）
    - 需要原子性的多条语句，使用 transaction() 上下文
    - 不包含任何业务实体（SymbolRepository / KlineRepository / TradeRepository
      ... 一律属于后续阶段）

用法示例：
    repo = Repository()
    repo.initialize()
    repo.create_test_table()          # 仅用于验证，不是业务表
    repo.execute("INSERT INTO ...", (1,))
    rows = repo.query_all("SELECT * FROM ...")
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence, Union

from . import database

# 支持的 SQL 参数形式：位置参数序列（含 tuple / list）或命名参数字典
SqlParams = Union[Sequence[Any], Mapping[str, Any]]
PathInput = Union[str, "os.PathLike[str]", Path, None]


def _describe_error(exc: sqlite3.Error, sql: str, params: SqlParams | None) -> str:
    """把 SQL 与参数附加到错误信息里，方便定位问题（不吞异常，只补充上下文）。"""
    return f"{exc} | SQL={sql!r} | params={params!r}"


def _enrich_error(exc: sqlite3.Error, sql: str, params: SqlParams | None) -> sqlite3.Error:
    """构造一个补充了上下文的新异常。

    关键点：保留**原始异常类型**（如 sqlite3.IntegrityError /
    sqlite3.OperationalError），否则调用方将无法区分约束冲突与语法错误。
    """
    return type(exc)(_describe_error(exc, sql, params))


class Repository:
    """最小数据访问层：连接获取、基础执行与查询、事务。"""

    def __init__(self, db_path: PathInput = None) -> None:
        # 只保存路径；连接在使用时创建，避免全局长期连接
        self._db_path: Path = database.resolve_database_path(db_path)

    # ------------------------------------------------------------------ 属性
    @property
    def db_path(self) -> Path:
        """当前 Repository 使用的数据库文件路径。"""
        return self._db_path

    # ------------------------------------------------------------ 连接 / 事务
    def initialize(self) -> Path:
        """初始化数据库（幂等），返回数据库文件路径。"""
        return database.initialize_database(self._db_path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """打开一个连接，正常结束提交、异常回滚、最终关闭。"""
        with database.connection(self._db_path) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开启一个事务，产出一个连接；正常结束提交，异常回滚并抛出。

        用法：
            with repo.transaction() as conn:
                conn.execute("INSERT INTO ...")
                conn.execute("INSERT INTO ...")
        """
        with database.connection(self._db_path) as conn:
            with database.transaction(conn):
                yield conn

    def close(self) -> None:
        """本类不持有长连接，关闭操作为空操作（保留接口以便未来扩展）。"""

    # ----------------------------------------------------------------- 写入
    def execute(self, sql: str, params: SqlParams | None = None) -> int:
        """执行一条写入语句，返回受影响行数。

        单条语句在自己的连接上立即生效（autocommit）。
        """
        with database.connection(self._db_path) as conn:
            try:
                cursor = conn.execute(sql, () if params is None else params)
                return cursor.rowcount
            except sqlite3.Error as exc:
                raise _enrich_error(exc, sql, params) from exc

    def executemany(
        self,
        sql: str,
        seq_of_params: Iterable[SqlParams],
    ) -> int:
        """批量执行同一条写入语句，返回受影响行数。

        整个批次在一个事务内完成：任何一条失败都会整体回滚。
        """
        rows = list(seq_of_params)
        with database.connection(self._db_path) as conn:
            with database.transaction(conn):
                try:
                    cursor = conn.executemany(sql, rows)
                    affected = cursor.rowcount
                except sqlite3.Error as exc:
                    # 事务上下文负责回滚，这里只补充上下文并抛出（保留异常类型）
                    raise _enrich_error(exc, sql, rows) from exc
        return affected

    # ----------------------------------------------------------------- 查询
    def query_one(self, sql: str, params: SqlParams | None = None) -> sqlite3.Row | None:
        """查询单条记录；没有结果时返回 None。"""
        return self._fetch(sql, params, many=False)

    def query_all(self, sql: str, params: SqlParams | None = None) -> list[sqlite3.Row]:
        """查询多条记录；没有结果时返回空列表。"""
        return self._fetch(sql, params, many=True)

    def _fetch(self, sql: str, params: SqlParams | None, *, many: bool) -> Any:
        with database.connection(self._db_path) as conn:
            try:
                cursor = conn.execute(sql, () if params is None else params)
            except sqlite3.Error as exc:
                raise _enrich_error(exc, sql, params) from exc
            return cursor.fetchall() if many else cursor.fetchone()

    # ----------------------------------------------------------- 基础设施查询
    def table_exists(self, table_name: str) -> bool:
        """判断表是否存在（仅查询 sqlite_master，不修改数据库）。"""
        row = self.query_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return row is not None

    def list_tables(self) -> list[str]:
        """列出数据库中的表名（不含 SQLite 内部表）。"""
        rows = self.query_all(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [row["name"] for row in rows]
