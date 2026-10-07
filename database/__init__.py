"""数据库模块：Strategy Engine 统一的数据访问层入口（Stage 02 ~ Stage 09）。

对外暴露的接口：

    initialize_database()   初始化数据库（幂等，创建 symbols / klines 表）
    connect() / close()     连接创建与关闭
    connection()            连接上下文：提交 / 回滚 / 自动关闭
    transaction()           在已有连接上开启显式事务
    Repository              最小数据访问层
    SymbolRepository        标的基础数据访问层（symbols 表）
    KlineRepository         K 线数据访问层（klines 表）
    StrategyRepository     策略定义与版本的数据访问层（strategies 表）
    SignalRepository       交易信号数据访问层（signals 表）
    TradeRepository        实际成交记录数据访问层（trades 表）
    PositionRepository     当前持仓数据访问层（positions 表）
    OrderRepository        订单数据访问层（orders 表）
    validation              字段级校验工具（各 Repository 共用）
    get_database_path()     默认数据库路径（data/strategy_engine.db）
    database_info()         只读状态信息，便于排查问题

当前正式业务表：symbols、klines、strategies、signals、trades、positions
（Stage 03~08）、orders（Stage 09）。回测等业务表尚未建立。
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
from .order_repository import OrderRepository
from .position_repository import PositionRepository
from .repository import Repository
from .signal_repository import SignalRepository
from .strategy_repository import StrategyRepository
from .symbol_repository import SymbolRepository
from .trade_repository import TradeRepository

__all__ = [
    "DEFAULT_DATA_DIR",
    "DEFAULT_DB_FILENAME",
    "DEFAULT_DB_PATH",
    "PROJECT_ROOT",
    "KlineRepository",
    "OrderRepository",
    "PositionRepository",
    "Repository",
    "SignalRepository",
    "StrategyRepository",
    "SymbolRepository",
    "TradeRepository",
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
