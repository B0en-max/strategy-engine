"""数据库 schema 定义（Stage 02 基础设施 → Stage 05）。

当前正式业务表：
    symbols     —— 交易标的基础信息（Stage 03）
    klines      —— K 线（OHLCV）数据（Stage 04）
    strategies  —— 策略定义与策略版本（Stage 05）
    signals     —— 策略产生的交易信号（Stage 06）
    trades      —— 实际发生的成交记录（Stage 07）
    positions   —— 当前实际持仓（Stage 08）
    orders      —— 准备执行 / 提交给执行器的订单（Stage 09）

尚未创建的后续业务表：
    backtests / performance / reviews / optimization_candidates
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
# 版本 8（Stage 19）：新增 strategy_definitions（Strategy 身份表）；
#                    strategies 增加 strategy_id 列（版本 → 策略），并补充幂等回填
# 版本 9（Stage 20）：新增 symbol_groups / symbol_group_members（市场分类与标的集合）；
#                    纯新增表，不动 symbols / klines 及任何已有表
SCHEMA_VERSION: Final[int] = 9

# 时间统一格式（SQLite 稳定支持，不引入第三方时间库）
TIMESTAMP_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"

# 时区约定：全部时间戳按 UTC 存储，避免跨市场（A股/港股/美股/加密）本地时区歧义

# 业务表名
SYMBOLS_TABLE: Final[str] = "symbols"
KLINES_TABLE: Final[str] = "klines"
STRATEGIES_TABLE: Final[str] = "strategies"
#: Strategy 身份表（Stage 19）：一个策略本身（与“策略版本”分开）
STRATEGY_DEFINITIONS_TABLE: Final[str] = "strategy_definitions"
SIGNALS_TABLE: Final[str] = "signals"
TRADES_TABLE: Final[str] = "trades"
POSITIONS_TABLE: Final[str] = "positions"
ORDERS_TABLE: Final[str] = "orders"
#: 市场分类 / 标的集合（Stage 20）：行业、概念、自选、自定义股票池
SYMBOL_GROUPS_TABLE: Final[str] = "symbol_groups"
#: 集合成员关系（Stage 20）：group ↔ symbol 多对多
SYMBOL_GROUP_MEMBERS_TABLE: Final[str] = "symbol_group_members"

# 数据库初始化后应当存在的业务表（后续阶段新增表时在这里登记）
BUSINESS_TABLES: Final[tuple[str, ...]] = (
    SYMBOLS_TABLE,
    KLINES_TABLE,
    SYMBOL_GROUPS_TABLE,
    SYMBOL_GROUP_MEMBERS_TABLE,
    STRATEGY_DEFINITIONS_TABLE,
    STRATEGIES_TABLE,
    SIGNALS_TABLE,
    TRADES_TABLE,
    POSITIONS_TABLE,
    ORDERS_TABLE,
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
# symbol_groups / symbol_group_members：市场分类与标的集合（Stage 20）
#
# 设计要点：
#   1. symbol_groups 表达「行业板块 / 概念板块 / 自选 / 自定义股票池」；
#      group_type 由 CHECK 约束固定为四种，取值来源与 Python 层共用 GROUP_TYPES
#   2. market 可空：为空表示跨市场集合（例如「我的自选」可同时放 A 股与加密）；
#      **不**限定取值，沿用 symbols.market 的自由文本约定
#   3. symbol_group_members 是 group ↔ symbol 的多对多关系，
#      UNIQUE(group_id, symbol_id) 由数据库保证同一标的不会重复加入同一集合
#   4. 本阶段**不建**独立 universe 表：一个 group 就是一个标的集合（Universe），
#      未来需要动态 Universe（Top20 市值等）时再在其上加规则层
#   5. 「移出集合」用物理删除成员关系表示，与 symbols 的 is_active 逻辑停用互不影响
# ---------------------------------------------------------------------------
GROUP_TYPES: Final[tuple[str, ...]] = ("industry", "concept", "watchlist", "custom")

_GROUP_TYPE_SQL_LIST: Final[str] = ", ".join(f"'{t}'" for t in GROUP_TYPES)

CREATE_SYMBOL_GROUPS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SYMBOL_GROUPS_TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    group_type  TEXT    NOT NULL,
    market      TEXT,
    description TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_symbol_groups_name UNIQUE (name),
    CONSTRAINT ck_symbol_groups_group_type CHECK (group_type IN ({_GROUP_TYPE_SQL_LIST}))
)
"""

CREATE_SYMBOL_GROUP_MEMBERS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SYMBOL_GROUP_MEMBERS_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES {SYMBOL_GROUPS_TABLE}(id),
    symbol_id  INTEGER NOT NULL REFERENCES {SYMBOLS_TABLE}(id),
    created_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_symbol_group_members_group_symbol UNIQUE (group_id, symbol_id)
)
"""

# 反向查询索引：UNIQUE(group_id, symbol_id) 的隐式索引已覆盖
# 「某个集合里有哪些标的」；下面这个索引服务「某个标的属于哪些集合」。
CREATE_SYMBOL_GROUP_MEMBERS_INDEX_SQL: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_symbol_group_members_symbol
ON {SYMBOL_GROUP_MEMBERS_TABLE} (symbol_id)
"""

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


# ---------------------------------------------------------------------------
# strategy_definitions：**Strategy 身份**（Stage 19）
#
# 与 strategies（策略版本）分开：
#   strategy_definitions  一个策略本身：name 唯一，有自己的 status / 生命周期
#   strategies            该策略的一个版本：version + config，每个版本一行
#
# 关联方向：
#   strategies.strategy_id          → strategy_definitions(id)：版本属于哪个策略
#   signals.strategy_id             → strategies(id)：信号来自哪个**版本**（沿用现有字段，不重复加列）
#   trades.signal_id                → signals(id)：成交 → 信号 → 版本 → 策略
#
# 于是任意 Signal / Trade 都能回答「来自哪个策略、哪个版本」（见 strategies/service.py）。
# 说明：strategies 那栏的“策略版本”语义早已在第一个 Stage 确定（每版一行、不覆盖旧版），
#       本阶段只是把「策略身份」拆出来，不重建已工作的版本表。
# ---------------------------------------------------------------------------
CREATE_STRATEGY_DEFINITIONS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {STRATEGY_DEFINITIONS_TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    status      TEXT    NOT NULL DEFAULT '{DEFAULT_STRATEGY_STATUS}',
    created_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    updated_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_strategy_definitions_name UNIQUE (name),
    CONSTRAINT ck_strategy_definitions_status
        CHECK (status IN ({_STRATEGY_STATUS_SQL_LIST}))
)
"""

# strategies 增加 strategy_id 列。
# SQLite 不支持 ADD COLUMN IF NOT EXISTS，所以由 database.initialize_database()
# 先查 PRAGMA table_info 再决定是否执行（幂等）。
ADD_STRATEGIES_STRATEGY_ID_SQL: Final[str] = (
    f"ALTER TABLE {STRATEGIES_TABLE} ADD COLUMN strategy_id INTEGER "
    f"REFERENCES {STRATEGY_DEFINITIONS_TABLE}(id)"
)

# 幂等回填（老数据也能追溯）：按 name 为已有的策略版本补建 Strategy 身份，再建立关联。
BACKFILL_STRATEGY_DEFINITIONS_SQL: Final[str] = f"""
INSERT OR IGNORE INTO {STRATEGY_DEFINITIONS_TABLE}
    (name, description, status, created_at, updated_at)
SELECT name, NULL, '{DEFAULT_STRATEGY_STATUS}', MIN(created_at), MAX(updated_at)
  FROM {STRATEGIES_TABLE}
 GROUP BY name
"""

LINK_STRATEGIES_TO_DEFINITIONS_SQL: Final[str] = f"""
UPDATE {STRATEGIES_TABLE}
   SET strategy_id = (
        SELECT d.id FROM {STRATEGY_DEFINITIONS_TABLE} d WHERE d.name = {STRATEGIES_TABLE}.name
   )
 WHERE strategy_id IS NULL
"""


def backfill_statements() -> list[tuple[str, tuple[object, ...]]]:
    """返回 schema 适应后的幂等回填语句（在建表 + 加列之后执行）。

    新库不会插入任何行，也不会改变文件大小；老库会补齐 Strategy 身份并建立关联。
    """
    return [
        (BACKFILL_STRATEGY_DEFINITIONS_SQL, ()),
        (LINK_STRATEGIES_TO_DEFINITIONS_SQL, ()),
    ]


# ---------------------------------------------------------------------------
# signals：策略产生的交易信号
#
# 设计要点：
#   1. strategy_id → strategies(id)、symbol_id → symbols(id)，两者都是真实外键；
#      删除被引用的策略/标的会被 SQLite 外键机制拦下，
#      **不在 Python 层重复实现一套外键检查**。
#   2. direction 只能是 long / short；status 只能是
#      pending / executed / cancelled（数据库 CHECK 与 Python 校验共用同一来源）。
#   3. timestamp 表示**信号发生时间**（UTC，格式与其他表一致）。
#   4. price 是信号触发时记录的价格；reason 允许为空（自然语言说明）。
#   5. 本表没有唯一约束，因此不存在“唯一约束已生成隐式索引”的情况；
#      根据查询需求（按 strategy / symbol / timeframe / status 筛选并按时间排序）
#      显式建立一个复合索引，见 CREATE_SIGNALS_INDEX_SQL。
# ---------------------------------------------------------------------------
SIGNAL_DIRECTIONS: Final[tuple[str, ...]] = ("long", "short")
SIGNAL_STATUSES: Final[tuple[str, ...]] = ("pending", "executed", "cancelled")
DEFAULT_SIGNAL_STATUS: Final[str] = "pending"

_SIGNAL_DIRECTION_SQL_LIST: Final[str] = ", ".join(f"'{d}'" for d in SIGNAL_DIRECTIONS)
_SIGNAL_STATUS_SQL_LIST: Final[str] = ", ".join(f"'{s}'" for s in SIGNAL_STATUSES)

CREATE_SIGNALS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {SIGNALS_TABLE} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL REFERENCES {STRATEGIES_TABLE}(id),
    symbol_id   INTEGER NOT NULL REFERENCES {SYMBOLS_TABLE}(id),
    timeframe   TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    price       REAL    NOT NULL,
    reason      TEXT,
    status      TEXT    NOT NULL DEFAULT '{DEFAULT_SIGNAL_STATUS}',
    created_at  TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT ck_signals_direction CHECK (direction IN ({_SIGNAL_DIRECTION_SQL_LIST})),
    CONSTRAINT ck_signals_status CHECK (status IN ({_SIGNAL_STATUS_SQL_LIST}))
)
"""

# 索引说明：signals 表没有唯一约束（也就没有隐式索引），而最常见的查询是
# 「某策略 / 某标的 / 某周期 的信号，按时间排序」，所以显式建立这个复合索引。
# SQLite 的索引条目尾部隐含 rowid，因此它天然满足 ORDER BY timestamp, id，
# 不会再额外产生排序开销。本索引不与任何已有索引重复。
CREATE_SIGNALS_INDEX_SQL: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_signals_strategy_symbol_timeframe_timestamp
    ON {SIGNALS_TABLE} (strategy_id, symbol_id, timeframe, timestamp)
"""


# ---------------------------------------------------------------------------
# trades：**实际发生的成交记录**
#
# 数据关系：strategies → signals → trades
#
# 设计要点：
#   1. signal_id 允许为空：手工交易可能没有对应的 Signal；
#      非空时通过外键引用 signals(id)。
#   2. symbol_id 必填，真实外键引用 symbols(id)。
#   3. side 只能是 buy / sell（数据库 CHECK；取值来源为 TRADE_SIDES）。
#   4. quantity / price / fee 用 REAL：不同市场的手数与币种精度不同；
#      fee 默认 0。
#   5. 故意**不**加 quantity > 0 / price > 0 / fee >= 0 这类限制：
#      续费返佣（fee 为负）与市场特殊性都存在，是否合理属于业务层判断，
#      沿用项目在 klines 上的一贯约定（不在数据库层强行限制正值）。
#   6. timestamp 表示成交发生时间（UTC，格式与其他表一致）。
#   7. 不加入本阶段用不到的字段（无 updated_at / 无盈亏字段）。
# ---------------------------------------------------------------------------
TRADE_SIDES: Final[tuple[str, ...]] = ("buy", "sell")

_TRADE_SIDE_SQL_LIST: Final[str] = ", ".join(f"'{s}'" for s in TRADE_SIDES)

CREATE_TRADES_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {TRADES_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  INTEGER REFERENCES {SIGNALS_TABLE}(id),
    symbol_id  INTEGER NOT NULL REFERENCES {SYMBOLS_TABLE}(id),
    side       TEXT    NOT NULL,
    quantity   REAL    NOT NULL,
    price      REAL    NOT NULL,
    timestamp  TEXT    NOT NULL,
    fee        REAL    NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT ck_trades_side CHECK (side IN ({_TRADE_SIDE_SQL_LIST}))
)
"""

# 索引说明：trades 表没有唯一约束（也就没有隐式索引）。成交记录最常见的查询是
# 「某标的的成交记录 + 按时间排序」，因此显式建立复合索引。
# SQLite 索引条目尾部隐含 rowid，因此它天然满足 ORDER BY timestamp, id。
# 注意：按 signal_id 筛选目前没有专门索引（SQLite 不会为外键列自动建索引），
# 后续如有需要再根据实际查询添加。
CREATE_TRADES_INDEX_SQL: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_trades_symbol_timestamp
    ON {TRADES_TABLE} (symbol_id, timestamp)
"""


# ---------------------------------------------------------------------------
# positions：当前实际持仓
#
# 设计要点：
#   1. **同一 symbol 只能有一条持仓**：UNIQUE(symbol_id) 由数据库强制，
#      不在 Python 层重复做存在性检查（重复创建会抛 sqlite3.IntegrityError）。
#   2. symbol_id 为真实外键，引用 symbols(id)。
#   3. quantity / avg_price 用 REAL：不同市场手数与币种精度不同；
#      quantity 允许为 0（已清仓）或负数（做空），故意不加正值限制。
#   4. updated_at 记录持仓最后更新时间（UTC）；本表按需求不设 created_at。
#   5. 只做存储层：**不**根据 trades 自动推算持仓，也不做盈亏计算。
#   6. 不加入本阶段用不到的字段。
# ---------------------------------------------------------------------------
CREATE_POSITIONS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {POSITIONS_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id  INTEGER NOT NULL REFERENCES {SYMBOLS_TABLE}(id),
    quantity   REAL    NOT NULL,
    avg_price  REAL    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT uq_positions_symbol_id UNIQUE (symbol_id)
)
"""

# 索引说明：positions 的 UNIQUE(symbol_id) 已经生成一个隐式唯一索引
# （sqlite_autoindex_positions_1），而本表最主要的查询就是按 symbol 精确查找，
# 所以**不再**额外创建索引。


# ---------------------------------------------------------------------------
# orders：准备执行 / 提交给执行器的订单
#
# 设计要点：
#   1. 定位：订单是**准备执行**的意图，不是成交（trade），也不是持仓（position）。
#      本阶段只建立数据层：不会自动创建订单、不会自动成交、
#      不会改 Position、不会生成 Trade。
#   2. signal_id 可为空（手工下单可无 Signal），非空时外键引用 signals(id)；
#      symbol_id 必填，外键引用 symbols(id)。
#   3. price 可为空：市价单（market）没有限价；限价单（limit）应当带 price。
#      这里**不**强制 "limit 必须有 price" 的交叉约束（属于执行层业务规则，
#      数据层只做基本完整性）。
#   4. 枚举型字段全部用数据库 CHECK 限定取值。
#   5. 时间：created_at 为订单记录创建时间，updated_at 随状态变更刷新（均为 UTC）。
#   6. quantity / price 用 REAL，故意不加正值限制（与项目既有约定一致）。
# ---------------------------------------------------------------------------
# 买卖方向沿用 trades 的取值（订单方向与成交方向必须一致，避免两处各写一份）
ORDER_SIDES: Final[tuple[str, ...]] = TRADE_SIDES
ORDER_TYPES: Final[tuple[str, ...]] = ("market", "limit")
ORDER_STATUSES: Final[tuple[str, ...]] = (
    "pending",
    "submitted",
    "filled",
    "cancelled",
    "rejected",
)
DEFAULT_ORDER_STATUS: Final[str] = "pending"

_ORDER_SIDE_SQL_LIST: Final[str] = ", ".join(f"'{s}'" for s in ORDER_SIDES)
_ORDER_TYPE_SQL_LIST: Final[str] = ", ".join(f"'{t}'" for t in ORDER_TYPES)
_ORDER_STATUS_SQL_LIST: Final[str] = ", ".join(f"'{s}'" for s in ORDER_STATUSES)

CREATE_ORDERS_TABLE_SQL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS {ORDERS_TABLE} (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id  INTEGER REFERENCES {SIGNALS_TABLE}(id),
    symbol_id  INTEGER NOT NULL REFERENCES {SYMBOLS_TABLE}(id),
    side       TEXT    NOT NULL,
    quantity   REAL    NOT NULL,
    price      REAL,
    order_type TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT '{DEFAULT_ORDER_STATUS}',
    created_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    updated_at TEXT    NOT NULL DEFAULT (strftime('{TIMESTAMP_FORMAT}', 'now')),
    CONSTRAINT ck_orders_side CHECK (side IN ({_ORDER_SIDE_SQL_LIST})),
    CONSTRAINT ck_orders_order_type CHECK (order_type IN ({_ORDER_TYPE_SQL_LIST})),
    CONSTRAINT ck_orders_status CHECK (status IN ({_ORDER_STATUS_SQL_LIST}))
)
"""

# 索引说明：orders 表没有唯一约束（也就没有隐式索引）。最常见的查询是
# 「某个标的的订单 + 按时间排序 / 时间区间」，因此显式建立这个复合索引。
# 按 status / signal_id 筛选目前没有专用索引，后续如有需要再根据实际查询添加。
CREATE_ORDERS_INDEX_SQL: Final[str] = f"""
CREATE INDEX IF NOT EXISTS idx_orders_symbol_created_at
    ON {ORDERS_TABLE} (symbol_id, created_at)
"""


def utc_now_str() -> str:
    """返回当前 UTC 时间字符串（统一格式 TIMESTAMP_FORMAT）。"""
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def initialization_statements() -> list[tuple[str, tuple[object, ...]]]:
    """返回数据库初始化需要执行的 (SQL, 参数) 列表。

    全部语句都必须是幂等的：
    重复执行 initialize_database() 不能报错。
    klines 建表语句必须排在 symbols 之后（外键引用 symbols(id)）。
    strategy_definitions 建表语句必须排在 strategies 之后被引用的位置（strategies.strategy_id 引用它）。
    """
    return [
        (CREATE_SYMBOLS_TABLE_SQL, ()),
        (CREATE_KLINES_TABLE_SQL, ()),
        (CREATE_SYMBOL_GROUPS_TABLE_SQL, ()),
        (CREATE_SYMBOL_GROUP_MEMBERS_TABLE_SQL, ()),
        (CREATE_SYMBOL_GROUP_MEMBERS_INDEX_SQL, ()),
        (CREATE_STRATEGY_DEFINITIONS_TABLE_SQL, ()),
        (CREATE_STRATEGIES_TABLE_SQL, ()),
        (CREATE_SIGNALS_TABLE_SQL, ()),
        (CREATE_SIGNALS_INDEX_SQL, ()),
        (CREATE_TRADES_TABLE_SQL, ()),
        (CREATE_TRADES_INDEX_SQL, ()),
        (CREATE_POSITIONS_TABLE_SQL, ()),
        (CREATE_ORDERS_TABLE_SQL, ()),
        (CREATE_ORDERS_INDEX_SQL, ()),
        (f"PRAGMA user_version = {SCHEMA_VERSION:d}", ()),
    ]
