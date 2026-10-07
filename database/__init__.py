"""数据库模块：Strategy Engine 统一的数据访问层入口（Stage 02）。

对外暴露的最小接口：

    initialize_database()   初始化数据库（幂等，不创建任何业务表）
    connect() / close()     连接创建与关闭
    connection()            连接上下文：提交 / 回滚 / 自动关闭
    transaction()           在已有连接上开启显式事务
    Repository              最小数据访问层
    get_database_path()     默认数据库路径（data/strategy_engine.db）
    database_info()         只读状态信息，便于排查问题

本阶段不含任何业务表与业务实体。
"""

from __future__ import annotations

from .database import (
    DEFAULT_DATA_DIR,
    DEFAULT_DB_FILENAME,
    DEFAULT_DB_PATH,
    PROJECT_ROOT,
    close,
    connect,
    connection,
    database_info,
    ensure_database_dir,
    get_database_path,
    initialize_database,
    resolve_database_path,
    transaction,
)
from .repository import Repository

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_DB_FILENAME",
    "DEFAULT_DB_PATH",
    "PROJECT_ROOT",
    "Repository",
    "close",
    "connect",
    "connection",
    "database_info",
    "ensure_database_dir",
    "get_database_path",
    "initialize_database",
    "resolve_database_path",
    "transaction",
]
