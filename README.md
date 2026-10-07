# Strategy Engine

**策略研究与执行系统**（Strategy Research & Execution System）

> ⚠️ **重要声明**
> 当前项目**尚未实现任何真实交易功能**，仅用于后续研究、回测和模拟执行系统的开发。
> 项目禁止连接真实账户、禁止真实下单、禁止保存真实资金或账户凭证。
> 现阶段只允许使用历史数据、回测、模拟交易与 Paper Trading。

---

## 1. 项目定位

Strategy Engine 是一个**独立运行**的本地量化交易策略系统，不依赖任何外部 AI 工作台。
它负责把「数据 → 策略 → 信号 → 回测 → 风控 → 模拟执行 → 交易记录 → 复盘」这条链路
做成可测试、可追溯、可替换的模块化系统。

未来会通过 API / MCP 与外部 AI 工作台对接，但 **Strategy Engine 本体必须能脱离 AI 工作台独立运行**。

## 2. 当前目标

完成分阶段开发，先把**确定性的系统**（数据、数据库、策略、回测、风控、模拟执行、复盘）做扎实，
之后再引入 AI 分析模块。

## 3. 当前开发阶段

**Stage 11 — K 线数据采集层**

已建立 `data_source/`（数据源实现与 Repository 分离）：

- `data_source/base.py`：最小接口 `KlineDataSource`（**只有一个方法** `fetch_klines()`）+ 标准结构 `KlineBar`
- `data_source/http_source.py`：可配置的 **HTTP / JSON 数据源**（不绑定任何交易所）
- `data_source/importer.py`：采集编排 `import_klines(...)`，通过现有 `KlineRepository` 写入 `klines` 表
- 支持：`symbol` / `timeframe` / 起止时间 / 批量写入
- **重复 K 线不会重复插入**：使用 `bulk_upsert_klines()`（已存在则更新 OHLCV）
  + 数据库 `UNIQUE(symbol_id, timeframe, timestamp)` 兼底
- 本阶段**未修改任何表结构**（仍是 7 张表 / `user_version = 7`）；**不接**策略、执行器、实盘

### 数据源配置（密钥不入代码、不入 Git）

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `STRATEGY_ENGINE_DATA_SOURCE_URL` | 是 | API 地址 |
| `STRATEGY_ENGINE_DATA_SOURCE_API_KEY` | 否 | 认证密钥（**通过请求头**发送，不会出现在 URL 与错误信息里） |
| `STRATEGY_ENGINE_DATA_SOURCE_TIMEOUT` | 否 | 超时秒数，默认 10 |

可参考仓库内的 `.env.example`（不含真实密钥）。请求参数名、响应路径、字段映射、时间格式
均可通过 `config.DataSourceConfig` 配置，默认值适配「符号 + 周期 + 起止时间 → JSON 数组」的通用接口。

```python
from config import DataSourceConfig
from data_source import HttpKlineDataSource, import_klines
from database import KlineRepository

source = HttpKlineDataSource(DataSourceConfig.from_env())
import_klines(
    source, KlineRepository(), symbol_id=1, symbol="BTCUSDT", timeframe="1h",
    start_timestamp="2026-01-01 00:00:00", end_timestamp="2026-01-02 00:00:00",
)
```

> **时间语义（全局约定）**：所有时间戳为 **UTC**，格式 `YYYY-MM-DD HH:MM:SS`；
> `klines.timestamp` = K 线开始时间；`signals.timestamp` = 信号发生时间；
> `trades.timestamp` = 成交发生时间；`orders.created_at` = 订单创建时间。

（Stage 01 骨架；02 SQLite 基础；03 symbols；04 klines；05 strategies；06 signals；
07 trades；08 positions；09 orders；10 模拟执行器。）

## 4. 总体模块（规划）

| 目录 | 职责 | 当前状态 |
|---|---|---|
| `config/` | 配置集中管理（路径、参数、运行模式） | 已实现：数据源配置（环境变量） |
| `database/` | SQLite 长期数据基础（连接、事务、各业务表访问层） | 基础设施 + `symbols` + `klines` + `strategies` + `signals` + `trades` + `positions` + `orders` |
| `data/raw/` | 原始行情数据落地 | 仅目录（当前采集直接写入 `klines` 表，未落地原始文件） |
| `data/processed/` | 清洗/加工后的中间数据 | 仅目录，未实现处理 |
| `data/cache/` | 可重建的缓存（不入库） | 仅目录 |
| `data_source/` | 行情数据采集（数据源接口 + HTTP/JSON 实现 + 导入编排） | 已实现（Stage 11）：仅历史 K 线，无实时推送 |
| `strategies/` | 策略实现（版本化：v1 / v2 / v3 …） | 目录为空；策略定义与版本记录已在数据库层建立，策略逻辑尚未实现 |
| `backtest/` | 回测引擎（严格按时间顺序，禁止未来数据泄漏） | 空包，仅占位 |
| `execution/` | 执行层（只机械执行已批准的信号，不自己判断市场） | **模拟执行器已实现**（`execution/engine.py`），无实盘 |
| `review/` | 交易记录与复盘 | 空包，仅占位 |
| `optimization/` | 参数寻优、防过拟合验证（Train/Validation/Test/Walk-forward） | 空包，仅占位 |
| `api/` | 对外 API，供 AI 工作台 / MCP 调用 | 空包，仅占位 |
| `tests/` | 测试 | 已可用 |
| `logs/` | 运行日志 | 仅目录 |
| `main.py` | 最小可运行入口 | 已完成 |

> 风控（Risk Manager）在架构上是独立模块，Strategy 不得绕过它；
> 它会在后续阶段以独立目录/模块的形式加入，**当前尚未实现**。
> 模拟执行器目前直接执行 Signal，**尚未接入风控检查**。

## 5. 数据库结构

数据库文件：`data/strategy_engine.db`（运行产物，被 `.gitignore` 忽略，**不入 Git**）。
当前 `PRAGMA user_version = 5`。所有时间戳均为 **UTC**，格式 `YYYY-MM-DD HH:MM:SS`。

### `symbols` — 交易标的（Stage 03）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `symbol` | TEXT | NOT NULL |
| `name` | TEXT | NOT NULL |
| `market` | TEXT | NOT NULL |
| `asset_type` | TEXT | NOT NULL |
| `is_active` | INTEGER | NOT NULL，DEFAULT 1，CHECK IN (0,1) |
| `created_at` / `updated_at` | TEXT | NOT NULL |

- `UNIQUE(market, symbol)`：不同市场允许存在同代码标的；停用用逻辑停用（`is_active = 0`），无物理删除

### `klines` — K 线 OHLCV（Stage 04）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `symbol_id` | INTEGER | NOT NULL，FOREIGN KEY → `symbols(id)` |
| `timeframe` | TEXT | NOT NULL（不限定取值） |
| `timestamp` | TEXT | NOT NULL，K 线**开始时间**（UTC） |
| `open` / `high` / `low` / `close` / `volume` | REAL | NOT NULL |
| `amount` | REAL | 可空（部分数据源无成交额） |
| `created_at` | TEXT | NOT NULL |

- `UNIQUE(symbol_id, timeframe, timestamp)`；`CHECK` 保证 `high >= open/close/low` 且 `low <= open/close/high`
- 故意不限制 `price > 0` / `volume > 0`；唯一约束已生成隐式索引（列顺序即查询路径），不重复建索引

### `strategies` — 策略定义与版本（Stage 05）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `name` | TEXT | NOT NULL |
| `description` | TEXT | 可空 |
| `status` | TEXT | NOT NULL，DEFAULT `'draft'`，CHECK IN (draft/testing/active/retired) |
| `version` | INTEGER | NOT NULL，DEFAULT 1，CHECK ≥ 1 |
| `config` | TEXT | NOT NULL（JSON 字符串，**不依赖** SQLite JSON1） |
| `created_at` / `updated_at` | TEXT | NOT NULL |

- `UNIQUE(name, version)`：**每个策略版本都是独立记录**，不靠修改原记录模拟版本

### `signals` — 策略产生的信号（Stage 06）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `strategy_id` | INTEGER | NOT NULL，FOREIGN KEY → `strategies(id)` |
| `symbol_id` | INTEGER | NOT NULL，FOREIGN KEY → `symbols(id)` |
| `timeframe` | TEXT | NOT NULL |
| `timestamp` | TEXT | NOT NULL，信号发生时间（UTC） |
| `direction` | TEXT | NOT NULL，CHECK IN (long/short) |
| `price` | REAL | NOT NULL |
| `reason` | TEXT | 可空 |
| `status` | TEXT | NOT NULL，DEFAULT `'pending'`，CHECK IN (pending/executed/cancelled) |
| `created_at` | TEXT | NOT NULL |

- 索引：`idx_signals_strategy_symbol_timeframe_timestamp (strategy_id, symbol_id, timeframe, timestamp)`

### `trades` — 实际成交记录（Stage 07）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `signal_id` | INTEGER | **可空**，FOREIGN KEY → `signals(id)`（手工交易可无 Signal） |
| `symbol_id` | INTEGER | NOT NULL，FOREIGN KEY → `symbols(id)` |
| `side` | TEXT | NOT NULL，CHECK IN (buy/sell) |
| `quantity` | REAL | NOT NULL |
| `price` | REAL | NOT NULL |
| `timestamp` | TEXT | NOT NULL，成交发生时间（UTC） |
| `fee` | REAL | NOT NULL，DEFAULT 0（可为负，用于返佣） |
| `created_at` | TEXT | NOT NULL |

- 索引：`idx_trades_symbol_timestamp (symbol_id, timestamp)`
- 数据关系：`strategies → signals → trades`；本表**只记录成交**，不含订单、持仓、盈亏

### `positions` — 当前实际持仓（Stage 08）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `symbol_id` | INTEGER | NOT NULL，FOREIGN KEY → `symbols(id)` |
| `quantity` | REAL | NOT NULL（可为 0 或负值：清仓 / 做空） |
| `avg_price` | REAL | NOT NULL |
| `updated_at` | TEXT | NOT NULL，DEFAULT 当前 UTC 时间（持仓最后更新时间） |

- `UNIQUE(symbol_id)`：**同一 symbol 只能有一条持仓**（数据库强制）
- 该唯一约束已生成隐式索引（`sqlite_autoindex_positions_1`），即按 symbol 查找的路径，不再重复建索引
- 本表**只做存储**：不会根据 `trades` 自动推算持仓，也不做盈亏计算

### `orders` — 订单（Stage 09）

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT |
| `signal_id` | INTEGER | **可空**，FOREIGN KEY → `signals(id)`（手工下单可无 Signal） |
| `symbol_id` | INTEGER | NOT NULL，FOREIGN KEY → `symbols(id)` |
| `side` | TEXT | NOT NULL，CHECK IN (buy/sell) |
| `quantity` | REAL | NOT NULL |
| `price` | REAL | **可空**（市价单无限价） |
| `order_type` | TEXT | NOT NULL，CHECK IN (market/limit) |
| `status` | TEXT | NOT NULL，DEFAULT `'pending'`，CHECK IN (pending/submitted/filled/cancelled/rejected) |
| `created_at` / `updated_at` | TEXT | NOT NULL，DEFAULT 当前 UTC 时间 |

- 索引：`idx_orders_symbol_created_at (symbol_id, created_at)`
- 数据层**不**强制“limit 单必须有 price”（属于执行层业务规则）
- 本表**只记录订单意图**：不会自动成交、不会改持仓、不会生成成交记录

## 6. 各阶段已完成的内容

### Stage 01 — 项目骨架

- [x] 建立标准项目目录结构
- [x] 为需要成为 Python 包的目录创建 `__init__.py`（均只有占位说明，无业务代码）
- [x] `main.py`：可运行的最小入口，输出启动信息
- [x] `requirements.txt`：当前阶段无第三方依赖（仅使用标准库），文件中写明了原因
- [x] `.gitignore`：正确忽略虚拟环境、缓存、数据库文件、`.env`、数据与日志内容
- [x] `README.md`：本文件
- [x] 初始化 Git 仓库并完成初始提交
- [x] `tests/`：最小测试结构（标准库 `unittest`，未引入 pytest）

### Stage 02 — SQLite 数据库基础设施

- [x] `database/database.py`：路径解析、连接创建/关闭、`row_factory`、`PRAGMA foreign_keys`、事务、幂等初始化
- [x] `database/models.py`：schema 版本（存放在 SQLite 原生 `PRAGMA user_version`）与初始化语句；**本阶段不创建任何业务表**
- [x] `database/repository.py`：最小数据访问层（`execute` / `executemany` / `query_one` / `query_all` / `transaction`）
- [x] `data/strategy_engine.db`：可正常初始化（该文件被 `.gitignore` 忽略，不入 Git）
- [x] `tests/test_database.py`：31 个数据库基础设施测试（文件、连接、row_factory、外键、CRUD、事务、Repository）

### Stage 03 — Symbols 标的基础数据模块

- [x] `database/models.py`：新增 `symbols` 表定义（schema 版本 → 1），初始化时幂等建表
- [x] `database/symbol_repository.py`：`SymbolRepository`（创建 / 单查 / 全查 / 启用查询 / 更新 / 逻辑停用）
- [x] `database/repository.py`：错误上下文工具改为公开 `enrich_error()`，供各 Repository 复用
- [x] `database/__init__.py`：对外导出 `SymbolRepository`
- [x] `tests/test_symbols.py`：31 个测试（建表、创建、查询、唯一性、必填字段、默认值、更新、逻辑停用、持久化、SQL 约束）

### Stage 04 — K 线（OHLCV）数据基础模块

- [x] `database/models.py`：新增 `klines` 表（schema 版本 → 2）；外键、唯一约束、OHLC CHECK 均在数据库层
- [x] `database/kline_repository.py`：`KlineRepository`（create / upsert / 批量 create / 批量 upsert / 单查 / 最新 / 区间查询 / 计数）
- [x] `database/__init__.py`：对外导出 `KlineRepository`
- [x] `tests/test_klines.py`：63 个测试（Schema、外键、OHLC 约束、NULL、唯一性、upsert、批量原子性、区间与 limit、索引与查询计划、持久化）

### Stage 05 — 策略定义基础模块

- [x] `database/models.py`：新增 `strategies` 表（schema 版本 → 3）；`UNIQUE(name, version)`、`CHECK(version >= 1)`、`CHECK(status IN ...)` 均在数据库层，状态取值与 Python 共用同一个来源
- [x] `database/strategy_repository.py`：`StrategyRepository`（create / get / get_by_name_version / list / count / update_status / update_config）
- [x] `database/__init__.py`：对外导出 `StrategyRepository`
- [x] `tests/test_strategies.py`：49 个测试（Schema、唯一性、版本、状态、config JSON round trip 与非法类型、更新隔离、时间字段、持久化）

### Stage 06 — 交易信号（Signal）数据层

- [x] `database/validation.py`：抽出共享字段校验层（类型 / 非空 / 时间格式 / 数字 / 取值集合），供各 Repository 复用
- [x] `database/kline_repository.py`：改用共享校验层，删除模块内重复的校验实现
- [x] `database/models.py`：新增 `signals` 表（schema 版本 → 4）；两个真实外键 + `CHECK(direction)` + `CHECK(status)`；并建立复合索引
- [x] `database/signal_repository.py`：`SignalRepository`（create / get / list / update_status / count）
- [x] `database/__init__.py`：对外导出 `SignalRepository`
- [x] `tests/test_signals.py`：41 个测试（Schema、外键、direction、status、创建与查询、list 过滤、status 更新、count、持久化）

### Stage 07 — 交易记录（Trade）数据层

- [x] `database/models.py`：新增 `trades` 表（schema 版本 → 5）；两个真实外键 + `CHECK(side)` + `fee` 默认 0
- [x] `database/trade_repository.py`：`TradeRepository`（create / get / list / count）
- [x] `database/validation.py`：新增 `optional_positive_int`（可为空的外键字段校验）
- [x] `database/__init__.py`：对外导出 `TradeRepository`
- [x] `tests/test_trades.py`：23 个测试（建表、外键、buy/sell、创建与查询、基本筛选、计数、持久化）

### Stage 08 — 持仓（Position）数据层

- [x] `database/models.py`：新增 `positions` 表（schema 版本 → 6）；`UNIQUE(symbol_id)` + 真实外键
- [x] `database/position_repository.py`：`PositionRepository`（create / get / get_by_symbol / list / update）
- [x] `database/__init__.py`：对外导出 `PositionRepository`
- [x] `tests/test_positions.py`：19 个测试（建表与约束、创建/查询/更新、symbol 唯一性、外键、列表、持久化）

### Stage 09 — 订单（Order）数据层

- [x] `database/models.py`：新增 `orders` 表（schema 版本 → 7）；两个真实外键 + 三个枚举 CHECK；建立复合索引
- [x] `database/order_repository.py`：`OrderRepository`（create / get / list / update_status / count）
- [x] `database/__init__.py`：对外导出 `OrderRepository`
- [x] `tests/test_orders.py`：26 个测试（建表与约束、创建/查询、market/limit、状态更新、外键、筛选与计数、持久化）

### Stage 10 — 模拟执行器（Execution Engine）

- [x] `execution/engine.py`：`ExecutionEngine.execute_signal()`，固定链路 Signal → Order → Trade → Position
- [x] `execution/engine.py`：`apply_trade_to_position()` 纯函数（建仓/加仓/减仓/清仓/反手）
- [x] `database/repository.py`：新增 `unit_of_work(*repositories)`，把多个 Repository 绑定到同一事务（跨表原子写入）
- [x] `execution/__init__.py`：导出 `ExecutionEngine` / `ExecutionResult` / `apply_trade_to_position`
- [x] `tests/test_execution.py`：18 个测试（完整链路、加仓/减仓/清仓/反手、订单状态、成交记录、
  持仓更新、前置校验、失败整体回滚、纯函数规则、临时库隔离）

### Stage 11 — K 线数据采集层

- [x] `data_source/base.py`：`KlineBar` 标准结构 + `KlineDataSource` 最小接口（单个 `fetch_klines`）+ `DataSourceError`
- [x] `data_source/http_source.py`：`HttpKlineDataSource`（可配置 HTTP/JSON，认证走请求头，可注入 opener 便于测试）
- [x] `data_source/importer.py`：`import_klines()` / `ImportResult`（通过 `KlineRepository` 写作，重复数据 upsert）
- [x] `config/settings.py`：`DataSourceConfig`（环境变量读取、字段映射、时间格式、参数名）
- [x] `.env.example`：环境变量示例（不含真实密钥）
- [x] `tests/test_data_source.py`：30 个测试（配置、请求构造、响应解析与字段映射、
  时间格式转换、错误处理与密钥不泄漏、写入、重复采集、时间范围、mock HTTP 端到端）

#### 当前已支持

- `symbols` 标的
- `klines` K 线（OHLCV）
- `strategies` 策略定义（含 version / config / status）
- `signals` 交易信号
- `trades` 实际成交记录
- `positions` 当前持仓
- `orders` 订单（market / limit、五种状态）
- **模拟执行器**：Signal → Order → Trade → Position 全链路，单事务原子、失败整体回滚
- **K 线采集层**：可配置 HTTP/JSON 数据源 → 通过 `KlineRepository` 写入 `klines`，重复采集不重复插入

#### 当前仍未实现

实时行情推送、实盘交易（OKX / 券商 API）、回测、盈亏计算（PnL）、复盘、
策略逻辑与信号生成、策略修改、风控（Risk Manager）、AI、API、MCP、前端。

## 7. 后续阶段会逐步加入的内容（均尚未实现）

- 阶段 12 起：实时行情 / 定时采集调度（当前只支持按需拉取历史 K 线）
- 策略逻辑实现与信号生成（Stage 05 / 06 只建立了策略定义与信号数据层，尚无生成逻辑）
- 风控（Risk Manager）：独立于策略放行信号（当前模拟执行器不做风控判断）
- 回测引擎与防过拟合验证（Train / Validation / Test / Walk-forward / Stress Test）
- 盈亏计算（PnL）与交易复盘
- 参数优化
- AI 分析模块（DeepSeek）：只产出 Candidate 策略，不得修改 ACTIVE 策略
- FastAPI / MCP 对外接口
- （更远期）真实交易 —— 目前明确禁止

## 8. 开发与运行方式

环境：Windows + Python 3.13 + 项目内虚拟环境 `.venv`

```powershell
# 进入项目根目录
cd C:\Users\T14\Projects\strategy-engine

# 运行入口
.venv\Scripts\python.exe main.py

# 初始化数据库（幂等，可重复执行；不创建任何业务表）
.venv\Scripts\python.exe -c "from database import initialize_database; print(initialize_database())"

# 运行全部测试
.venv\Scripts\python.exe -m unittest discover -s tests -t . -v

# 安装依赖（当前阶段不会安装任何东西）
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 9. 架构原则（长期遵守）

1. **模块化**：数据、策略、回测、风控、执行、复盘、优化、API 尽可能解耦，可单独替换。
2. **策略版本化**：策略不覆盖旧版本，每一版都可追溯（参数、创建时间、测试数据、回测与复盘结果、是否批准）。
3. **执行与思考分离**：Executor 只机械执行 `Strategy → Signal → Risk Manager → Executor`，不自行判断市场，不调用 AI。
4. **风控独立**：Strategy 只产生信号，Risk Manager 独立决定是否放行，且不可被绕过。
5. **AI 与正式策略分离**：AI 只能分析、提假设、产出 Candidate，不能改 ACTIVE 策略、生产参数和风控，也不能执行交易。
6. **禁止未来数据泄漏**：所有回测与验证严格按时间顺序。
7. **防止过拟合**：必须经过 Train / Validation / Test / Walk-forward / Stress Test / Paper Trading。
8. **可追溯**：任意一笔交易都能追溯到具体的策略版本。

## 10. 安全边界（禁止事项）

- 不自动真实下单、不自动修改真实账户、不自动转账、不使用真实资金
- 不绕过 Risk Manager，不让 AI 直接修改 ACTIVE 策略
- 不把 API Key 硬编码进代码（一律使用环境变量）
- 不删除历史交易记录来掩盖错误，不删除失败的回测结果来美化表现
