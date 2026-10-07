"""数据库 schema 定义（Stage 02 基础设施 + Stage 03 symbols 表）。

当前正式业务表：
    symbols —— 交易标的基础信息（Stage 03 建立）

尚未创建的后续业务表：
    klines / strategies / strategy_versions / signals / orders / trades /
    positions / backtests / performance / reviews / optimization_candidates
    —— 属于后续阶段，本阶段一律不建。

schema 版本存放在 SQLite 原生 PRAGMA user_version 中，便于将来做迁移。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

# 当前 schema 版本。后续阶段新增/修改表结构时应递增，并补上迁移逻辑。
SCHEMA_VERSION: Final[int] = 1

# 时间统一格式（SQLite 稳定支持，不引入第三方时间库）
TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# 时区约定：全部时间戳按 UTC 存储，避免跨市场（A股/港股/美股/加密）本地时区歧义

# 标的基础信息表（Stage 03）
SYMBOLS_TABLE: Final[str] = "symbols"

# 数据库初始化后应当存在的业务表（后续阶段新增表时在这里登记）
BUSINESS_TABLES: Final[tuple[str, ...]] = (SYMBOLS_TABLE,)

# 唯一性设计说明（UNIQUE(market, symbol) 而不是 UNIQUE(symbol)）：
#   系统未来要同时支持 A股 / 港股 / 美股 / 加密货币等市场，
#   不同市场出现相同代码是常态（例如加密货币 SOL/LINK/DOT/GRT 与美股代码重名），
#   因此标的的身份是「市场 + 代码」的组合，而不是单独的代码字符串。
#   注意：UNIQUE 约束在数据库层强制，不在 Python 层判断。
CREATE_SYMBOLS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SYMBOLS_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    market     TEXT    NOT NULL,
    asset_type TEXT    NOT NULL,
    is_active  INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_symbols_market_symbol UNIQUE (market, symbol)
)
"""


def utc_now_str() -> str:
    """返回当前 UTC 时间字符串（统一格式 TIMESTAMP_FORMAT）。"""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def initialization_statements() -> list[tuple[str, tuple[object, ...]]]:
    """返回数据库初始化需要执行的 (SQL, 参数) 列表。

    全部语句都必须是幂等的：
    重复执行 initialize_database() 不能报错。
    """
    return [
        (CREATE_SYMBOLS_TABLE_SQL, ()),
        (f"PRAGMA user_version = {SCHEMA_VERSION:d}", ()),
    ]
