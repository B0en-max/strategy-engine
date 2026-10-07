"""数据库 schema 定义（Stage 02 基础设施 → Stage 05）。

当前正式业务表：
    symbols     —— 交易标的基础信息（Stage 03）
    klines      —— K 线（OHLCV）数据（Stage 04）
    strategies  —— 策略定义与策略版本（Stage 05）

尚未创建的后续业务表：
    signals / orders / trades / positions / backtests / performance /
    reviews / optimization_candidates
    —— 属于后续阶段，本阶段一律不建。

schema 版本存放在 SQLite 原生 PRAGMA user_version 中，便于将来做迁移。

时间语义（全局约定）：
    - 所有时间戳都是 **UTC**，格式统一为 'YYYY-MM-DD HH:MM:SS'
    - klines.timestamp 表示该根 K 线的**开始时间**（不是结束时间）
    - created_at 表示该行记录**进入数据库**的时间（UTC）
    - 不使用 datetime.now() 作为历史数据的时间戳
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

# 当前 schema 版本。后续阶段新增/修改表结构时应递增，并补上迁移逻辑。
SCHEMA_VERSION: Final[int] = 3

# 时间统一格式（SQLite 稳定支持，不引入第三方时间库）
TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# 时区约定：全部时间戳按 UTC 存储，避免跨市场（A股/港股/美股/加密）本地时区歧义

# 业务表名
SYMBOLS_TABLE: Final[str] = "symbols"
KLINES_TABLE: Final[str] = "klines"
STRATEGIES_TABLE: Final[str] = "strategies"

# 数据库初始化后应当存在的业务表（后续阶段新增表时在这里登记）
BUSINESS_TABLES: Final[tuple[str, ...]] = (
    SYMBOLS_TABLE,
    KLINES_TABLE,
    STRATEGIES_TABLE,
)

# ---------------------------------------------------------------------------
# symbols：唯一性设计（UNIQUE(market, symbol) 而不是 UNIQUE(symbol)）
#   系统未来要同时支持 A股 / 港股 / 美股 / 加密货币等市场，
#   不同市场出现相同代码是常态（例如加密货币 SOL/LINK/DOT/GRT 与美股代码重名），
#   因此标的的身份是「市场 + 代码」的组合，而不是单独的代码字符串。
#   注意：UNIQUE 约束在数据库层强制，不在 Python 层判断。
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# klines：K 线（OHLCV）数据
#
# 设计要点：
#   1. symbol_id 通过真实 FOREIGN KEY 引用 symbols(id)（连接已启用 PRAGMA foreign_keys）
#   2. UNIQUE(symbol_id, timeframe, timestamp)：同一标的、同一周期、同一开始时间只有一条
#      —— 唯一性由数据库强制，不用「先 SELECT 再 INSERT」的 Python 判断
#   3. CHECK 保证 OHLC 的基本逻辑：high >= max(open, close, low)、low <= min(open, close, high)
#   4. 故意**不**强制 price > 0 或 volume > 0：不同市场存在合法但特殊的数据
#   5. volume / amount 使用 REAL：不同市场对 volume 的定义与精度不同，
#      amount 允许 NULL（部分数据源没有成交额）
#   6. timestamp 是 K 线的**开始时间**，UTC，格式 'YYYY-MM-DD HH:MM:SS'
# ---------------------------------------------------------------------------
CREATE_KLINES_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {KLINES_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id  INTEGER NOT NULL REFERENCES {SYMBOLS_TABLE}(id),
    timeframe  TEXT    NOT NULL,
    timestamp  TEXT    NOT NULL,
    open       REAL    NOT NULL,
    high       REAL    NOT NULL,
    low        REAL    NOT NULL,
    close      REAL    NOT NULL,
    volume     REAL    NOT NULL,
    amount     REAL,
    created_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_klines_symbol_timeframe_timestamp
        UNIQUE (symbol_id, timeframe, timestamp),
    CONSTRAINT ck_klines_ohlc CHECK (
        high >= open
        AND high >= close
        AND high >= low
        AND low <= open
        AND low <= close
        AND low <= high
    )
)
"""

# klines 的查询索引说明：
#   UNIQUE(symbol_id, timeframe, timestamp) 已经生成一个隐式唯一索引
#   （sqlite_autoindex_klines_1，列顺序与最常见的查询条件
#   "某标的 + 某周期 + 时间区间" 完全一致），因此**不再**额外创建重复索引。

# ---------------------------------------------------------------------------
# strategies：策略定义与策略版本
#
# 设计要点：
#   1. 一个策略的**每个版本都是独立记录**，不通过修改原记录来"模拟版本"。
#      例：id=1 mean_reversion v1 retired / id=2 v2 testing / id=3 v3 draft
#      —— 这样未来复盘某笔交易时，能准确知道当时用的是哪个策略版本。
#   2. UNIQUE(name, version)：同一策略名下的版本号不重复（数据库层强制）
#   3. CHECK(version >= 1)
#   4. CHECK(status IN (...))：状态取值由 STRATEGY_STATUSES 统一给出，
#      Python 层与之共用同一个来源，不在两处各写一份
#   5. config 用 TEXT 存 JSON 字符串（Python 层 json.dumps / json.loads），
#      **不依赖** SQLite JSON1 扩展
#   6. config 只描述"策略的结构化配置"，不是 AI prompt；
#      entry_signal / expected_return / win_rate / sharpe 等属于后续策略、
#      回测、复盘层，本阶段不建
# ---------------------------------------------------------------------------
STRATEGY_STATUSES: Final[tuple[str, ...]] = ("draft", "testing", "active", "retired")
DEFAULT_STRATEGY_STATUS: Final[str] = "draft"
DEFAULT_STRATEGY_VERSION: Final[int] = 1

_STRATEGY_STATUS_SQL_LIST: Final[str] = ", ".join(f"'{s}'" for s in STRATEGY_STATUSES)

CREATE_STRATEGIES_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {STRATEGIES_TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    status      TEXT    NOT NULL DEFAULT '{DEFAULT_STRATEGY_STATUS}',
    version     INTEGER NOT NULL DEFAULT {DEFAULT_STRATEGY_VERSION},
    config      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_strategies_name_version
        UNIQUE (name, version),
    CONSTRAINT ck_strategies_version
        CHECK (version >= 1),
    CONSTRAINT ck_strategies_status
        CHECK (status IN ({_STRATEGY_STATUS_SQL_LIST}))
)
"""


def utc_now_str() -> str:
    """返回当前 UTC 时间字符串（统一格式 TIMESTAMP_FORMAT）。"""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def initialization_statements() -> list[tuple[str, tuple[object, ...]]]:
    """返回数据库初始化需要执行的 (SQL, 参数) 列表。

    全部语句都必须是幂等的：
    重复执行 initialize_database() 不能报错。
    klines 建表语句必须排在 symbols 之后（外键引用 symbols(id)）。
    """
    return [
        (CREATE_SYMBOLS_TABLE_SQL, ()),
        (CREATE_KLINES_TABLE_SQL, ()),
        (CREATE_STRATEGIES_TABLE_SQL, ()),
        (f"PRAGMA user_version = {SCHEMA_VERSION:d}", ()),
    ]
