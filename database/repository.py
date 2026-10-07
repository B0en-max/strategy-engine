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

跨 Repository 的原子写入（例如执行器）：
    with unit_of_work(order_repo, trade_repo, position_repo):
        ...   # 要么全部提交，要么全部回滚
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


def enrich_error(exc: sqlite3.Error, sql: str, params: SqlParams | None) -> sqlite3.Error:
    """构造一个补充了上下文的新异常（供各 Repository 复用）。

    关键点：保留**原始异常类型**（如 sqlite3.IntegrityError /
    sqlite3.OperationalError），否则调用方将无法区分约束冲突与语法错误。
    """
    return type(exc)(_describe_error(exc, sql, params))


class Repository:
    """最小数据访问层：连接获取、基础执行与查询、事务。"""

    def __init__(self, db_path: PathInput = None) -> None:
        # 只保存路径；连接在使用时创建，避免全局长期连接
        self._db_path: Path = database.resolve_database_path(db_path)
        # 仅在 unit_of_work() 期间非空：此时所有操作复用同一个连接与事务
        self._bound_connection: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 属性
    @property
    def db_path(self) -> Path:
        """当前 Repository 使用的数据库文件路径。"""
        return self._db_path

    # ------------------------------------------------------------ 连接 / 事务
    def initialize(self) -> Path:
        """初始化数据库（幂等），返回数据库文件路径。"""
        return database.initialize_database(self._db_path)

    def _bind(self, conn: sqlite3.Connection) -> None:
        """把本 Repository 绑定到外部事务的连接（仅供 unit_of_work 使用）。"""
        self._bound_connection = conn

    def _unbind(self) -> None:
        """解除绑定，恢复“每次操作自建连接”的默认行为。"""
        self._bound_connection = None

    @contextmanager
    def _acquire(self) -> Iterator[sqlite3.Connection]:
        """获取一个可用连接：已绑定时复用它，否则临时新建（用完即关）。"""
        if self._bound_connection is not None:
            yield self._bound_connection
            return
        with database.connection(self._db_path) as conn:
            yield conn

    @contextmanager
    def _atomic(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """在给定连接上开启事务；若已处于 unit_of_work 事务中则直接复用。"""
        if self._bound_connection is not None:
            yield conn
            return
        with database.transaction(conn):
            yield conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """打开一个连接，正常结束提交、异常回滚、最终关闭。

        在 unit_of_work() 中则复用同一个连接（由外层负责提交/回滚/关闭）。
        """
        with self._acquire() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """开启一个事务，产出一个连接；正常结束提交，异常回滚并抛出。

        用法：
            with repo.transaction() as conn:
                conn.execute("INSERT INTO ...")
                conn.execute("INSERT INTO ...")

        在 unit_of_work() 中不会重复开启事务，而是复用外层事务。
        """
        with self._acquire() as conn:
            with self._atomic(conn):
                yield conn

    def close(self) -> None:
        """本类不持有长连接，关闭操作为空操作（保留接口以便未来扩展）。"""

    # ----------------------------------------------------------------- 写入
    def execute(self, sql: str, params: SqlParams | None = None) -> int:
        """执行一条写入语句，返回受影响行数。

        单条语句在自己的连接上立即生效（autocommit）。
        """
        with self._acquire() as conn:
            try:
                cursor = conn.execute(sql, () if params is None else params)
                return cursor.rowcount
            except sqlite3.Error as exc:
                raise enrich_error(exc, sql, params) from exc

    def executemany(
        self,
        sql: str,
        seq_of_params: Iterable[SqlParams],
    ) -> int:
        """批量执行同一条写入语句，返回受影响行数。

        整个批次在一个事务内完成：任何一条失败都会整体回滚。
        """
        rows = list(seq_of_params)
        with self._acquire() as conn:
            with self._atomic(conn):
                try:
                    cursor = conn.executemany(sql, rows)
                    affected = cursor.rowcount
                except sqlite3.Error as exc:
                    # 事务上下文负责回滚，这里只补充上下文并抛出（保留异常类型）
                    raise enrich_error(exc, sql, rows) from exc
        return affected

    # ----------------------------------------------------------------- 查询
    def query_one(self, sql: str, params: SqlParams | None = None) -> sqlite3.Row | None:
        """查询单条记录；没有结果时返回 None。"""
        return self._fetch(sql, params, many=False)

    def query_all(self, sql: str, params: SqlParams | None = None) -> list[sqlite3.Row]:
        """查询多条记录；没有结果时返回空列表。"""
        return self._fetch(sql, params, many=True)

    def _fetch(self, sql: str, params: SqlParams | None, *, many: bool) -> Any:
        with self._acquire() as conn:
            try:
                cursor = conn.execute(sql, () if params is None else params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, sql, params) from exc
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


@contextmanager
def unit_of_work(*repositories: Repository) -> Iterator[sqlite3.Connection]:
    """把多个 Repository 的写操作绑定到**同一个连接与事务**（跨 Repository 原子性）。

    用途：当一个业务流程需要跨多张表写入（例如 执行器：Order → Trade → Position），
    必须保证“要么全部成功、要么全部回滚”，不能留下半完成状态。

    用法：
        with unit_of_work(order_repo, trade_repo, position_repo):
            order_repo.create_order(...)    # 不再各自提交
            trade_repo.create_trade(...)
            position_repo.update_position(...)   # 全部成功才一起提交

    行为：
        - 正常结束：一次性 COMMIT
        - 出现异常：ROLLBACK 并把异常继续抛出（不吞异常）
        - 结束后解除绑定并关闭连接，各 Repository 恢复默认行为
    """
    if not repositories:
        raise ValueError("unit_of_work 至少需要一个 Repository")

    db_paths = {repo.db_path for repo in repositories}
    if len(db_paths) > 1:
        raise ValueError(f"unit_of_work 中的 Repository 必须指向同一个数据库: {db_paths}")
    if any(repo._bound_connection is not None for repo in repositories):
        raise ValueError("Repository 已经在一个 unit_of_work 中，不支持嵌套")

    conn = database.connect(repositories[0].db_path)
    try:
        conn.execute("BEGIN")
        for repo in repositories:
            repo._bind(conn)
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        else:
            if conn.in_transaction:
                conn.execute("COMMIT")
    finally:
        for repo in repositories:
            repo._unbind()
        conn.close()
