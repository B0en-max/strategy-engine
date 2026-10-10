"""SQLite 数据库基础设施（Stage 02）。

职责：
    1. 数据库路径解析与目录创建
    2. 统一的连接创建 / 关闭
    3. 连接级配置（row_factory、PRAGMA foreign_keys）
    4. 事务上下文（提交 / 回滚）
    5. 幂等的数据库初始化入口

明确不做的事：
    - 不引入 SQLAlchemy / ORM / 第三方数据库库
    - 不建立任何业务表（symbols / klines / strategies / signals / orders ... 属于后续阶段）
    - 不持有全局长期打开的连接

关于隔离级别：
    连接使用 sqlite3 的 autocommit 模式（isolation_level=None）。
    单条语句立即生效；需要原子性的多条语句必须显式放进
    connection() 或 transaction() 上下文里。
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from . import models

# 允许传入字符串、PathLike 或 Path 来指定数据库位置
DatabasePath = Union[str, "os.PathLike[str]", Path]

# 项目根目录：database/ 的上一级
PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]

# 默认数据目录与数据库文件（路径只在这里定义一次，其他地方不得重复硬编码）
DEFAULT_DATA_DIR: Path = PROJECT_ROOT / "data"
DEFAULT_DB_FILENAME: str = "strategy_engine.db"
DEFAULT_DB_PATH: Path = DEFAULT_DATA_DIR / DEFAULT_DB_FILENAME

# 连接默认超时（秒）：等待 SQLite 锁的时间
DEFAULT_TIMEOUT: float = 5.0


def get_database_path() -> Path:
    """返回默认数据库文件路径（只计算路径，不创建文件）。"""
    return DEFAULT_DB_PATH


def resolve_database_path(db_path: DatabasePath | None = None) -> Path:
    """把传入的路径规范化为绝对路径。

    - 传 None 时返回默认数据库路径
    - 相对路径按项目根目录解析，避免依赖当前工作目录
    """
    if db_path is None:
        return DEFAULT_DB_PATH
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def ensure_database_dir(db_path: DatabasePath | None = None) -> Path:
    """确保数据库文件所在目录存在，并返回规范化后的数据库路径。"""
    path = resolve_database_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect(
    db_path: DatabasePath | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> sqlite3.Connection:
    """创建一个配置好的 SQLite 连接。

    调用方负责关闭连接；推荐使用 connection() 上下文管理器，
    以免忘记 close()。

    连接配置：
        - row_factory = sqlite3.Row（可按列名访问）
        - PRAGMA foreign_keys = ON（SQLite 默认是关闭的，必须显式开启）
        - isolation_level = None（autocommit，事务由 transaction() 显式控制）
    """
    path = ensure_database_dir(db_path)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
    except sqlite3.Error:
        # 配置失败时不要泄漏连接
        conn.close()
        raise
    return conn


def close(conn: sqlite3.Connection) -> None:
    """关闭连接。

    重复关闭同一个连接不会报错（close 视为幂等操作）。
    """
    try:
        conn.close()
    except sqlite3.ProgrammingError:
        # 连接已经关闭：幂等语义，无需上报
        pass


@contextmanager
def connection(db_path: DatabasePath | None = None) -> Iterator[sqlite3.Connection]:
    """连接上下文管理器：按需创建连接，结束时提交、异常时回滚，最终一定关闭。

    用法：
        with connection() as conn:
            conn.execute("INSERT INTO ...")
    """
    conn = connect(db_path)
    try:
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """在**已存在的连接**上开启一个显式事务。

    正常结束提交，出现任何异常回滚并把异常继续抛出（不吞异常）。

    用法：
        with connection() as conn:
            with transaction(conn):
                conn.execute("...")
                conn.execute("...")
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        if conn.in_transaction:
            conn.execute("COMMIT")


def _migrate_strategies_strategy_id(conn: sqlite3.Connection) -> None:
    """幂等地给 strategies 表补上 strategy_id 列（版本 → Strategy 身份）。

    SQLite 支持 ADD COLUMN，但不支持 IF NOT EXISTS，所以先查 PRAGMA table_info；
    列已存在时什么都不做（保证 initialize_database 可重复执行且不改动数据库）。
    """
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({models.STRATEGIES_TABLE})").fetchall()
    }
    if "strategy_id" not in columns:
        conn.execute(models.ADD_STRATEGIES_STRATEGY_ID_SQL)


def initialize_database(db_path: DatabasePath | None = None) -> Path:
    """初始化数据库，返回数据库文件路径。

    本阶段只建立“基础设施状态”：
        - 确保 data/ 目录存在
        - 创建数据库文件（若不存在）
        - 应用 schema 版本标记

    **不会创建任何业务表**（symbols / klines / strategies / ... 全部不存在）。

    幂等：重复执行不会报错，也不会破坏已有数据。
    """
    path = ensure_database_dir(db_path)
    with connection(path) as conn:
        for sql, params in models.initialization_statements():
            conn.execute(sql, params)
        # Stage 19：建表之后补列，再执行幂等回填（新库两步都是空操作）
        _migrate_strategies_strategy_id(conn)
        for sql, params in models.backfill_statements():
            conn.execute(sql, params)
    return path


def database_info(db_path: DatabasePath | None = None) -> dict[str, object]:
    """返回数据库基础设施的只读状态信息（便于排查问题）。

    只读取信息，不修改数据库。
    """
    with connection(db_path) as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
        user_version = int(row[0]) if row is not None else 0
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        sqlite_version = conn.execute("SELECT sqlite_version()").fetchone()[0]
    return {
        "db_path": str(resolve_database_path(db_path)),
        "user_version": user_version,
        "tables": tables,
        "foreign_keys_enabled": foreign_keys_enabled,
        "sqlite_version": sqlite_version,
    }
