"""数据库模块：Strategy Engine 统一的数据访问层入口（Stage 02 ~ Stage 05）。

对外暴露的接口：

    initialize_database()   初始化数据库（幂等，创建 symbols / klines 表）
    connect() / close()     连接创建与关闭
    connection()            连接上下文：提交 / 回滚 / 自动关闭
    transaction()           在已有连接上开启显式事务
    Repository              最小数据访问层
    SymbolRepository        标的基础数据访问层（symbols 表）
    KlineRepository         K 线数据访问层（klines 表）
    StrategyRepository     策略定义与版本的数据访问层（strategies 表）
    get_database_path()     默认数据库路径（data/strategy_engine.db）
    database_info()         只读状态信息，便于排查问题

当前正式业务表：symbols（Stage 03）、klines（Stage 04）、strategies（Stage 05）。
信号、订单、交易、持仓、回测等业务表尚未建立。
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
from .kline_repository import KlineRepository
from .repository import Repository
from .strategy_repository import StrategyRepository
from .symbol_repository import SymbolRepository

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_DB_FILENAME",
    "DEFAULT_DB_PATH",
    "PROJECT_ROOT",
    "KlineRepository",
    "Repository",
    "StrategyRepository",
    "SymbolRepository",
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
