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

**Stage 14 — 基础回测引擎（Backtest Engine）**

已建立 `backtest/engine.py`，把已有组件串成最小可用的历史回测：

```
正式数据库（只读）                独立临时运行库（读写）
klines / strategies / symbols ─► signals → orders → trades → positions ─► BacktestResult
```

- `BacktestEngine(source_db_path=None, run_db_path=None).run(strategy_id, symbol_id, timeframe, *, start_timestamp, end_timestamp)`
- **复用现有组件，不重写业务逻辑**：`StrategyRunner`（策略→信号）、`ExecutionEngine`（信号→订单/成交/持仓）、各 Repository（引擎内不写 SQL）
- 回测规则（本阶段最简单实现）：`long → buy`、`short → sell`；成交价取**信号所在 K 线的收盘价**；
  每次固定数量 **1**；成交时间取**信号所在 K 线的时间**（历史模拟，不使用当前时间）；
  无手续费 / 滑点 / 资金管理 / 杠杆 / 保证金，无复杂绩效指标
- 结果字段：`strategy_id` / `symbol_id` / `timeframe` / `start_timestamp` / `end_timestamp` /
  `bars_processed` / `signals_generated` / `trades_executed` / `final_position_quantity` /
  `final_position_avg_price`（另附 `run_db_path` 便于事后查看）

### 隔离保证（本阶段硬性要求）

| 要求 | 实现 |
|---|---|
| 回测不修改正式库 | 正式库**全程只读**：只从中复制本次回测需要的策略 / 标的 / K 线 |
| 不把历史信号/订单/成交/持仓当回测结果 | 回测产生的全部数据只写在**独立临时运行库**里 |
| 不依赖正式 `signals` 表 | 运行库每次都是新的，回测从零开始；正式库 `signals` 始终不动 |
| 一次回测一个运行上下文 | 每个引擎实例一个运行库（默认系统临时目录，`close()` 后自动清理；
也可传 `run_db_path` 保留下来查看） |

### 本次人工验收（真实 600519 K 线，全程只操作临时库）

把正式库里的真实 K 线复制到临时“来源库”后回测：

| 周期 | bars_processed | signals_generated | trades_executed | 买 / 卖 | final_position_quantity | final_position_avg_price |
|---|---|---|---|---|---|---|
| 15m | 320 | 319 | 319 | 138 / 181 | -43.0 | 1247.50 |
| 1d | 60 | 59 | 59 | 28 / 31 | -3.0 | 1247.32 |

- 成交时间按 K 线时间升序（15m：`2026-09-02 09:45` ~ `2026-09-30 14:45`；1d：`2026-07-09` ~ `2026-09-30`）
- 运行库中 `orders = trades = signals`（319 / 59），持仓与结果一致；退出 `with` 后临时运行库已自动清理
- **正式库前后完全一致**：`symbols=1, klines=380, strategies=0, signals=0, orders=0, trades=0, positions=0`

> **时间语义（全局约定）**：所有时间戳为 **UTC**，格式 `YYYY-MM-DD HH:MM:SS`；
> `klines.timestamp` = K 线开始时间；`signals.timestamp` = 信号发生时间；
> `trades.timestamp` = 成交发生时间（回测中 = 信号所在 K 线时间）；`orders.created_at` = 订单创建时间。

（Stage 01 骨架；02 SQLite 基础；03 symbols；04 klines；05 strategies；06 signals；
07 trades；08 positions；09 orders；10 模拟执行器；11 K 线采集层；12 真实数据源；13 策略运行器。）

## 4. 总体模块（规划）

| 目录 | 职责 | 当前状态 |
|---|---|---|
| `config/` | 配置集中管理（路径、参数、运行模式） | 已实现：数据源配置（环境变量） |
| `database/` | SQLite 长期数据基础（连接、事务、各业务表访问层） | 基础设施 + `symbols` + `klines` + `strategies` + `signals` + `trades` + `positions` + `orders` |
| `data/raw/` | 原始行情数据落地 | 仅目录（当前采集直接写入 `klines` 表，未落地原始文件） |
| `data/processed/` | 清洗/加工后的中间数据 | 仅目录，未实现处理 |
| `data/cache/` | 可重建的缓存（不入库） | 仅目录 |
| `data_source/` | 行情数据采集（数据源接口 + HTTP/JSON 实现 + 预设 + 导入 CLI） | 已实现：已接入**新浪财经**并完成真实历史导入 |
| `strategies/` | 策略实现（版本化：v1 / v2 / v3 …） | 已有策略接口 + 示例策略 + **策略运行器**（产生信号） |
| `backtest/` | 回测引擎（严格按时间顺序，禁止未来数据泄漏） | 已有**基础回测引擎**（独立临时库运行；无手续费/滑点/杠杆） |
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
- 数据来源：Stage 12 已通过新浪财经接入真实历史 K 线（导入方式与结果见第 3 节）

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

### Stage 12 — 接入真实 K 线数据源（新浪财经）

- [x] `data_source/presets.py`：`sina_cn_kline_config()`（地址 / 参数名 / 周期映射 / 字段映射 / 时间语义 / datalen 校验）
- [x] `data_source/cli.py`：命令行导入工具（`python -m data_source.cli`），自动补建标的并打印汇总
- [x] `config/settings.py`：新增最小配置项 `timeframe_mapping`、`timestamp_semantics`；`start_param/end_param` 允许为 None
- [x] `data_source/http_source.py`：数字容错（字符串型数字）、时间字符串规范化、
  结束时间→开始时间回退、可不下发时间范围参数
- [x] `data_source/base.py`：`timeframe_to_seconds()` / `is_intraday_timeframe()`
- [x] `data_source/importer.py`：数据源不支持范围参数时，在本地按闭区间裁切
- [x] `tests/test_sina_source.py`：14 个测试（预设配置、请求构造、字符串型数字、
  15m 结束→开始回退、日线不回退、本地裁切、重复导入、CLI 汇总；**全部用 mock，不联网**）
- [x] `tests/test_data_source.py`：新增 10 个通用扩展测试
- [x] 真实验收：600519 的 15m 320 根 + 1d 60 根写入正式库（详见「行情数据导入」一节），重复导入行数不变

### Stage 13 — 策略运行器（Strategy Runner）

- [x] `strategies/base.py`：`Bar` / `SignalCandidate` / `Strategy` 接口（与数据库解耦）
- [x] `strategies/close_direction.py`：最简示例策略（相邻收盘价方向），参数从 config 读（`min_move`）
- [x] `strategies/runner.py`：`StrategyRunner`
  * 按 `strategy_id` 读参数、按 `symbol_id`+`timeframe` 读 K 线（升序）
  * 策略实现由 `config["type"]` 选择，注册表可注入（策略逻辑不写死在 Runner 里）
  * 信号写入 `signals`：`strategy_id/symbol_id/timeframe/timestamp/direction/price/reason`
  * 单事务落库（失败整批回滚）；已有信号会跳过（可重复运行）
  * **不创建 Order / Trade / Position**
- [x] `strategies/__init__.py`：对外导出
- [x] `tests/test_strategy_runner.py`：24 个测试（读配置、读 K 线、时间顺序、long/short/无信号、
  信号字段、不产生交易对象、重复运行、写入失败回滚、纯策略计算、临时库隔离）
- [x] 人工验收：真实 600519 K 线复制到临时库跑通（15m 319 条 / 1d 59 条信号，0/0/0 交易对象）

### Stage 14 — 基础回测引擎（Backtest Engine）

- [x] `backtest/engine.py`：`BacktestEngine.run()`（独立临时运行库 + 复制策略/标的/K 线，正式库全程只读）
  * 链路：K 线 → `StrategyRunner`（信号）→ `ExecutionEngine`（订单/成交/持仓）→ `BacktestResult`
  * 规则：long→buy、short→sell、成交价 = 信号所在 K 线收盘价、固定数量 1、成交时间 = K 线时间
  * 只执行本次新产生且仍为 pending 的信号；同一引擎再次 run 不会重复成交
  * 支持显式 `run_db_path`（保留便于查看）与默认临时库（`close()` 后自动清理）
- [x] `backtest/__init__.py`：对外导出 `BacktestEngine` / `BacktestResult`
- [x] `tests/test_backtest.py`：23 个测试（结果字段与计数、时间范围、时间顺序、
  long→buy / short→sell、持仓数量与均价、信号只成交一次、来源库只读、
  多次回测隔离、临时库清理、schema 版本、错误处理）
- [x] 人工验收：真实 600519 K 线（15m 320 根 / 1d 60 根）在临时库里完成完整回测，正式库未被修改

#### 当前已支持

- `symbols` 标的
- `klines` K 线（OHLCV）—— 已含**真实历史数据**（新浪财经）
- `strategies` 策略定义（含 version / config / status）
- **策略运行器**：策略配置 + K 线 → `signals`（示例策略，逻辑可替换）
- `signals` 交易信号
- `trades` 实际成交记录
- `positions` 当前持仓
- `orders` 订单（market / limit、五种状态）
- **模拟执行器**：Signal → Order → Trade → Position 全链路，单事务原子、失败整体回滚
- **K 线采集层**：可配置 HTTP/JSON 数据源（已接入新浪财经）→ 写入 `klines`，重复采集不重复插入
- **基础回测引擎**：K 线 → 信号 → 模拟成交 → 订单/成交/持仓 → 回测结果（独立临时库，正式库只读）

#### 当前仍未实现

实时行情推送与定时采集、自动交易（无人值守）、风控（Risk Manager）、
回测增强（手续费 / 滑点 / 资金管理 / 杠杆 / 复杂绩效指标 / 防过拟合流程）、
盈亏计算（PnL）、复盘、复杂指标与最终交易策略、AI、API、MCP、前端、实盘交易。

## 7. 后续阶段会逐步加入的内容（均尚未实现）

- 阶段 15 起：实时行情 / 定时采集调度（当前只支持手动按需拉取历史 K 线）
- 自动化串联：定时跑策略 → 风控 → 模拟执行（当前各环节需手动调用）
- 风控（Risk Manager）：独立于策略放行信号（当前模拟执行器不做风控判断）
- 回测增强：手续费 / 滑点 / 资金管理 / 杠杆、复杂绩效指标与防过拟合验证（Train / Validation / Test / Walk-forward / Stress Test）
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

### 行情数据导入（Stage 12 接入的数据源）

数据源：**新浪财经** `https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData`
（公开、免费、**无需认证**；字段 `day/open/high/low/close/volume`，无成交额）。

- 日内周期的时间标签是 K 线**结束**时间 → 入库前自动回退为**开始**时间（如 `09:45` → `09:30`）
- 该接口不支持起止时间参数（只能取最近 N 根）→ 时间范围在本地按闭区间裁切
- 重复导入同一时间段**不会产生重复数据**（upsert + 唯一约束）

| 环境变量 | 必填 | 说明 |
|---|---|---|
| `STRATEGY_ENGINE_DATA_SOURCE_URL` | 否 | 不设置时用内置新浪公开地址 |
| `STRATEGY_ENGINE_DATA_SOURCE_API_KEY` | 否 | 新浪不需要；如需认证走**请求头**，不进 URL 与错误信息 |
| `STRATEGY_ENGINE_DATA_SOURCE_TIMEOUT` | 否 | 超时秒数，默认 10 |

环境变量示例见仓库内 `.env.example`（**不含任何真实密钥**）。

```powershell
# 15 分钟：最近 320 根（约 20 个交易日）
.venv\Scripts\python.exe -m data_source.cli --symbol 600519 --api-symbol sh600519 `
    --name 贵州茅台 --timeframe 15m --datalen 320

# 日线：最近 60 根
.venv\Scripts\python.exe -m data_source.cli --symbol 600519 --api-symbol sh600519 --timeframe 1d --datalen 60
```

当前正式库中已有：600519 贵州茅台（CN/STOCK）15m 320 根（2026-09-02 09:30 ~ 2026-09-30 14:45）、
1d 60 根（2026-07-08 ~ 2026-09-30）。

### 运行策略产生信号（Stage 13）

```python
from strategies import StrategyRunner

runner = StrategyRunner()                 # 默认数据库 data/strategy_engine.db
result = runner.run(strategy_id=1, symbol_id=1, timeframe="15m")
print(result)   # RunResult(bars=320, signals=319, long_signals=138, short_signals=181, ...)
```

- 策略实现由 `strategies.config["type"]` 选择（内置：`close_direction`）
- 只写 `signals` 表，**不会**创建 Order / Trade / Position
- 可重复运行：已有信号的 `(strategy, symbol, timeframe, timestamp)` 会被跳过

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
