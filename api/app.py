"""执行器 API 层（Stage 16）。

定位：把已有的 `StrategyRunner` / `ExecutionEngine` 封装成稳定的 HTTP 接口，
供下一阶段的前端调用。**本层只做参数校验、资源存在性检查与 JSON 转换**：

    Frontend → API → StrategyRunner / ExecutionEngine → Repository → SQLite

明确不做（避免重复实现）：策略逻辑、订单逻辑、成交逻辑、持仓计算。
也不做：登录 / 用户 / 权限 / HTTPS / 公网部署 / 真实交易所对接。

错误约定：
    404  引用的资源不存在（strategy / symbol / signal）
    409  状态冲突（例如执行一个非 pending 的信号）或数据库约束冲突
    400  业务参数错误（ValueError，例如时间格式不对）
    422  请求体/查询参数不符合 schema（由 FastAPI 自动返回）
    500  其它未预期异常（**不吞异常**，交给框架记录堆栈）

数据库路径：从 `config.ENV_DB_PATH` 环境变量或调用方显式入参解析，
默认落到项目统一默认路径（`database.resolve_database_path`），
**不在本文件里写死任何绝对路径**。

浏览器跨域（Stage 17 前端）：仅允许本地开发的前端地址访问，来源可由
环境变量 `STRATEGY_ENGINE_CORS_ORIGINS`（逗号分隔）或 `create_app(cors_origins=...)` 覆盖。
本层仍然**不做登录 / 权限 / HTTPS**，也不连接任何真实交易所。

启动方式（本地开发）：
    .venv\\Scripts\\python.exe -m uvicorn api.app:app --reload --port 8000
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final, Sequence

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import ENV_CORS_ORIGINS, ENV_DB_PATH
from database import (
    KlineRepository,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    StrategyDefinitionRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    resolve_database_path,
)
from execution import ExecutionEngine
from market import MarketService
from strategies import StrategyRunner, StrategyService

from .schemas import ExecuteSignalRequest, ExecuteStrategyRequest, RunStrategyRequest

API_TITLE = "Strategy Engine API"
API_VERSION = "0.1.0"
API_DESCRIPTION = (
    "本地策略执行器的 HTTP 接口：运行策略、查看信号/订单/成交/持仓、模拟执行信号。"
    "当前仅支持本地模拟执行，**未连接任何真实交易所**。"
)

router = APIRouter()

#: 默认允许的浏览器来源：只覆盖本地开发用的 Vite 地址（127.0.0.1 与 localhost 两种写法）
DEFAULT_CORS_ORIGINS: Final[tuple[str, ...]] = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


# --------------------------------------------------------------------------- 工具
def _db_path(request: Request) -> Path:
    """当前请求使用的数据库路径（来自 app.state，统一由一个地方解析）。"""
    return request.app.state.db_path


def _rows(rows: list[Any]) -> list[dict[str, Any]]:
    """把 sqlite3.Row 列表转成纯 dict 列表（只返回 JSON，不泄漏连接对象）。"""
    return [dict(row) for row in rows]


def _plain(value: Any) -> dict[str, Any]:
    """把结果 dataclass（RunResult / ExecutionResult）转成 dict。"""
    return asdict(value)


def _not_found(resource: str, resource_id: int) -> HTTPException:
    return HTTPException(status_code=404, detail=f"{resource} 不存在: id={resource_id}")


def _require_strategy(db_path: Path, strategy_id: int) -> dict[str, Any]:
    strategy = StrategyRepository(db_path).get_strategy(strategy_id)
    if strategy is None:
        raise _not_found("策略", strategy_id)
    return strategy


def _require_symbol(db_path: Path, symbol_id: int) -> None:
    if SymbolRepository(db_path).get_symbol_by_id(symbol_id) is None:
        raise _not_found("标的", symbol_id)


# --------------------------------------------------------------------------- 基础
@router.get("/health", summary="健康检查")
def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "api_version": API_VERSION,
        "db_path": str(_db_path(request)),
        "simulation_only": True,
        "broker_connected": False,
    }


# --------------------------------------------------------------------------- 查询
@router.get("/strategies", summary="策略列表")
def list_strategies(request: Request, status: str | None = None) -> list[dict[str, Any]]:
    return StrategyRepository(_db_path(request)).list_strategies(status=status)


@router.get("/symbols", summary="标的列表")
def list_symbols(
    request: Request,
    market: str | None = None,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    repository = SymbolRepository(_db_path(request))
    rows = (
        repository.list_active_symbols(market=market)
        if active_only
        else repository.list_symbols(market=market)
    )
    return _rows(rows)


@router.get("/klines", summary="指定标的的 K 线")
def list_klines(
    request: Request,
    symbol_id: int = Query(..., gt=0, description="标的 id"),
    timeframe: str = Query(..., min_length=1, description="K 线周期，如 15m / 1d"),
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    limit: int | None = Query(None, gt=0, description="最多返回最近 N 根"),
) -> list[dict[str, Any]]:
    db_path = _db_path(request)
    _require_symbol(db_path, symbol_id)
    rows = KlineRepository(db_path).list_klines(
        symbol_id,
        timeframe,
        start_timestamp,
        end_timestamp,
        limit=limit,
    )
    return _rows(rows)


# ---------------------------------------------------------------------------
# 市场 / 选股（Stage 20 Step 2）
#
# 本组端点只做「参数校验 + 调用 MarketService + 转 JSON」，
# 不在 Router 里写 SQL；行情来自 klines 现有数据，缺失一律 null（不伪造）。
# ---------------------------------------------------------------------------
#: /market/symbols/{id}/klines 的默认与最大 limit
MARKET_KLINE_DEFAULT_LIMIT: Final[int] = 300
MARKET_KLINE_MAX_LIMIT: Final[int] = 2000


@router.get("/market/groups", summary="市场分组 / 板块列表（含板块行情聚合）")
def list_market_groups(
    request: Request,
    group_type: str | None = None,
    market: str | None = None,
    active_only: bool = True,
    timeframe: str = "1d",
) -> list[dict[str, Any]]:
    """板块列表：在成员数之外附带涨跌聚合（Stage 22 市场总览的板块 Tile 用）。

    聚合在 Repository / Service 层用**一条 SQL** 完成，前端不需要再逐个板块请求股票列表。
    """
    return MarketService(_db_path(request)).list_groups_with_stats(
        group_type=group_type, market=market, active_only=active_only, timeframe=timeframe
    )


@router.get("/market/groups/{group_id}/symbols", summary="板块 / 股票池内的标的（含最新行情）")
def list_market_group_symbols(
    group_id: int,
    request: Request,
    active_only: bool = True,
    timeframe: str = "1d",
) -> list[dict[str, Any]]:
    service = MarketService(_db_path(request))
    if service.get_group(group_id) is None:
        raise _not_found("市场分组", group_id)
    return service.list_market_symbols(
        group_id=group_id, active_only=active_only, timeframe=timeframe
    )


@router.get("/market/symbols", summary="市场标的列表（市场选股页统一入口）")
def list_market_symbols(
    request: Request,
    group_id: int | None = Query(None, gt=0, description="只取该分组内的标的"),
    market: str | None = None,
    active_only: bool = True,
    timeframe: str = "1d",
) -> list[dict[str, Any]]:
    service = MarketService(_db_path(request))
    if group_id is not None and service.get_group(group_id) is None:
        raise _not_found("市场分组", group_id)
    return service.list_market_symbols(
        group_id=group_id, market=market, active_only=active_only, timeframe=timeframe
    )


@router.get("/market/symbols/{symbol_id}", summary="单个标的的当前行情")
def get_market_symbol(
    symbol_id: int,
    request: Request,
    timeframe: str = "1d",
) -> dict[str, Any]:
    row = MarketService(_db_path(request)).get_market_symbol(symbol_id, timeframe=timeframe)
    if row is None:
        raise _not_found("标的", symbol_id)
    return row


@router.get("/market/symbols/{symbol_id}/klines", summary="标的的历史 K 线（复用既有查询语义）")
def list_market_symbol_klines(
    symbol_id: int,
    request: Request,
    timeframe: str = "1d",
    limit: int = Query(
        MARKET_KLINE_DEFAULT_LIMIT,
        ge=1,
        le=MARKET_KLINE_MAX_LIMIT,
        description="最多返回最近 N 根",
    ),
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    _db = _db_path(request)
    _require_symbol(_db, symbol_id)
    # 直接复用 KlineRepository.list_klines（排序、区间、limit 语义完全一致）
    rows = KlineRepository(_db).list_klines(symbol_id, timeframe, start, end, limit=limit)
    return [
        {
            "timestamp": row["timestamp"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "amount": row["amount"],
        }
        for row in rows
    ]


@router.get("/strategies/{strategy_id}/signals", summary="指定策略产生的信号")
def list_strategy_signals(
    strategy_id: int,
    request: Request,
    symbol_id: int | None = Query(None, gt=0),
    timeframe: str | None = None,
    status: str | None = None,
    limit: int | None = Query(None, gt=0),
) -> list[dict[str, Any]]:
    db_path = _db_path(request)
    _require_strategy(db_path, strategy_id)
    rows = SignalRepository(db_path).list_signals(
        strategy_id=strategy_id,
        symbol_id=symbol_id,
        timeframe=timeframe,
        status=status,
        limit=limit,
    )
    return _rows(rows)


@router.get("/positions", summary="当前持仓")
def list_positions(
    request: Request,
    symbol_id: int | None = Query(None, gt=0, description="只看某个标的"),
) -> list[dict[str, Any]]:
    repository = PositionRepository(_db_path(request))
    if symbol_id is None:
        return _rows(repository.list_positions())
    position = repository.get_position_by_symbol(symbol_id)
    return [] if position is None else [dict(position)]


@router.get("/orders", summary="订单列表")
def list_orders(
    request: Request,
    symbol_id: int | None = Query(None, gt=0),
    signal_id: int | None = Query(None, gt=0),
    status: str | None = None,
    limit: int | None = Query(None, gt=0),
) -> list[dict[str, Any]]:
    rows = OrderRepository(_db_path(request)).list_orders(
        symbol_id=symbol_id, signal_id=signal_id, status=status, limit=limit
    )
    return _rows(rows)


@router.get("/trades", summary="成交列表")
def list_trades(
    request: Request,
    symbol_id: int | None = Query(None, gt=0),
    signal_id: int | None = Query(None, gt=0),
    side: str | None = None,
    limit: int | None = Query(None, gt=0),
) -> list[dict[str, Any]]:
    rows = TradeRepository(_db_path(request)).list_trades(
        symbol_id=symbol_id, signal_id=signal_id, side=side, limit=limit
    )
    return _rows(rows)


# --------------------------------------------------------------------------- 运行
# --------------------------------------------------------------------------- Strategy / 版本
@router.get("/strategy-definitions", summary="Strategy 列表（策略身份）")
def list_strategy_definitions(
    request: Request, status: str | None = None
) -> list[dict[str, Any]]:
    """列出 Strategy 身份（一个策略一行，与「策略版本」分开）。"""
    return StrategyDefinitionRepository(_db_path(request)).list_definitions(status=status)


@router.get(
    "/strategy-definitions/{strategy_id}/versions",
    summary="某个 Strategy 的版本列表",
)
def list_strategy_versions(strategy_id: int, request: Request) -> list[dict[str, Any]]:
    """列出某个 Strategy 的全部版本（按版本号升序，含 config）。"""
    db_path = _db_path(request)
    if StrategyDefinitionRepository(db_path).get_definition(strategy_id) is None:
        raise _not_found("策略", strategy_id)
    return StrategyRepository(db_path).list_versions(strategy_id)


@router.get("/signals/{signal_id}/trace", summary="追溯 Signal 来自哪个策略 / 版本")
def trace_signal(signal_id: int, request: Request) -> dict[str, Any]:
    trace = StrategyService(_db_path(request)).trace_signal(signal_id)
    if trace is None:
        raise _not_found("信号", signal_id)
    return trace


@router.get("/trades/{trade_id}/trace", summary="追溯 Trade 来自哪个策略 / 版本")
def trace_trade(trade_id: int, request: Request) -> dict[str, Any]:
    trace = StrategyService(_db_path(request)).trace_trade(trade_id)
    if trace is None:
        raise _not_found("成交", trade_id)
    return trace


# --------------------------------------------------------------------------- 运行
@router.post("/executor/run", summary="按指定策略版本执行一段 K 线（Executor 调用入口）")
def run_executor(payload: ExecuteStrategyRequest, request: Request) -> dict[str, Any]:
    """按「策略 + 版本 + 标的 + 周期 + 时间范围」执行，返回执行结果汇总。

    默认在临时运行库里执行（正式库只读）：不污染正式库的历史信号 / 成交。
    """
    db_path = _db_path(request)
    service = StrategyService(db_path)
    if service.get_strategy(payload.strategy_id) is None:
        raise _not_found("策略", payload.strategy_id)
    version = service.get_version(payload.strategy_version_id)
    if version is None:
        raise _not_found("策略版本", payload.strategy_version_id)
    if version["strategy_id"] != payload.strategy_id:
        raise HTTPException(
            status_code=409,
            detail=(
                f"策略版本 {payload.strategy_version_id} 不属于策略 {payload.strategy_id}"
            ),
        )
    _require_symbol(db_path, payload.symbol_id)

    result = service.run_strategy(
        payload.strategy_id,
        payload.strategy_version_id,
        payload.symbol_id,
        payload.timeframe,
        start_timestamp=payload.start_timestamp,
        end_timestamp=payload.end_timestamp,
    )
    return _plain(result)


@router.post("/strategies/{strategy_id}/run", summary="运行策略（产生信号）")
def run_strategy(
    strategy_id: int,
    payload: RunStrategyRequest,
    request: Request,
) -> dict[str, Any]:
    db_path = _db_path(request)
    _require_strategy(db_path, strategy_id)
    _require_symbol(db_path, payload.symbol_id)

    result = StrategyRunner(db_path).run(
        strategy_id,
        payload.symbol_id,
        payload.timeframe,
        start_timestamp=payload.start_timestamp,
        end_timestamp=payload.end_timestamp,
    )
    return _plain(result)


@router.post("/execution/signals/{signal_id}", summary="模拟执行指定信号")
def execute_signal(
    signal_id: int,
    payload: ExecuteSignalRequest,
    request: Request,
) -> dict[str, Any]:
    db_path = _db_path(request)
    signals = SignalRepository(db_path)
    signal = signals.get_signal(signal_id)
    if signal is None:
        raise _not_found("信号", signal_id)
    if signal["status"] != "pending":
        # 明确区分"资源不存在"和"状态不允许"，不静默跳过
        raise HTTPException(
            status_code=409,
            detail=(
                f"信号 {signal_id} 当前状态为 {signal['status']}，只有 pending 状态可以模拟执行"
            ),
        )

    result = ExecutionEngine(db_path).execute_signal(
        signal_id, payload.quantity, price=payload.price
    )
    return _plain(result)


# --------------------------------------------------------------------------- 应用
async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """业务参数错误（例如时间格式不对、非法枚举值）→ 400，保留原始信息。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


async def _integrity_error_handler(request: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
    """数据库约束冲突（例如重复创建）→ 409。"""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def resolve_api_database_path(explicit: str | Path | None = None) -> Path:
    """统一解析 API 使用的数据库路径：显式入参 > 环境变量 > 项目默认。"""
    if explicit is not None:
        return resolve_database_path(explicit)
    return resolve_database_path(os.environ.get(ENV_DB_PATH))


def resolve_cors_origins(explicit: str | Sequence[str] | None = None) -> list[str]:
    """统一解析允许跨域的来源：显式入参 > 环境变量 > 本地开发默认值。

    显式入参 / 环境变量都接受「逗号分隔字符串」或「字符串序列」，
    空白会被忽略（便于本地用逗号列表配置多个端口）。
    """
    if explicit is None:
        raw = os.environ.get(ENV_CORS_ORIGINS) or ""
        source: list[str] = raw.split(",") if raw else list(DEFAULT_CORS_ORIGINS)
    elif isinstance(explicit, str):
        source = explicit.split(",")
    else:
        source = [str(item) for item in explicit]

    origins: list[str] = []
    for item in source:
        # 每一项都允许再写逗号（环境变量/序列两种写法统一处理），并去重
        for part in str(item).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in origins:
                origins.append(cleaned)
    return origins


def create_app(
    db_path: str | Path | None = None,
    cors_origins: str | Sequence[str] | None = None,
) -> FastAPI:
    """创建 API 应用。

    参数：
        db_path     可选：显式指定数据库；不传则读环境变量 `STRATEGY_ENGINE_DB_PATH`，
                    再退到项目统一默认路径（不会写死绝对路径）。
        cors_origins 可选：允许跨域的浏览器来源；不传则读环境变量
                    `STRATEGY_ENGINE_CORS_ORIGINS`，再退到本地开发默认值。
    """
    app = FastAPI(title=API_TITLE, version=API_VERSION, description=API_DESCRIPTION)
    app.state.db_path = resolve_api_database_path(db_path)
    app.state.cors_origins = resolve_cors_origins(cors_origins)
    # 允许凭据（Cookie）保持关闭：本层没有登录态，避免无意义的宽松授权
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(app.state.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )
    app.include_router(router)
    app.add_exception_handler(ValueError, _value_error_handler)
    app.add_exception_handler(sqlite3.IntegrityError, _integrity_error_handler)
    return app


#: 供 `uvicorn api.app:app` 直接使用（本地开发）
app = create_app()
