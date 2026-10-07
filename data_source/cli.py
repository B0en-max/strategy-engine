"""命令行入口：执行一次历史 K 线导入（Stage 12 的人工验收工具）。

用途：把真实行情数据导入 `data/strategy_engine.db` 的 `klines` 表。
自动测试**不调用**本模块（不会依赖网络），它只用于人工验收 / 日常补数据。

用法（在项目根目录执行）：

    .venv\\Scripts\\python.exe -m data_source.cli \\
        --symbol 600519 --api-symbol sh600519 --name 贵州茅台 \\
        --timeframe 15m --datalen 320

参数说明：
    --symbol       本项目 symbols 表里的标的代码
    --api-symbol   数据源使用的代码（新浪：沪市 sh600519 / 深市 sz000001），默认同 --symbol
    --name         标的名称（仅新建时使用）
    --market       市场（默认 CN）；--asset-type 资产类型（默认 STOCK）
    --timeframe    K 线周期（默认 15m；支持 5m/15m/30m/60m/1h/1d）
    --datalen      向数据源请求最近多少根（1~1023，默认 320）
    --start/--end  可选的时间范围（UTC 'YYYY-MM-DD HH:MM:SS'），只在本地裁切
    --db           可选数据库路径（默认项目默认库）

数据源地址可用环境变量 STRATEGY_ENGINE_DATA_SOURCE_URL 覆盖（默认使用新浪公开地址）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from database import KlineRepository, SymbolRepository, database_info

from .base import KlineDataSource
from .http_source import HttpKlineDataSource
from .importer import import_klines
from .presets import sina_cn_kline_config


def ensure_symbol(
    symbols: SymbolRepository,
    *,
    symbol: str,
    name: str | None = None,
    market: str = "CN",
    asset_type: str = "STOCK",
) -> tuple[int, bool]:
    """确保标的已存在于 symbols 表；不存在则创建。返回 (symbol_id, 是否新建)。"""
    existing = symbols.get_symbol(market, symbol)
    if existing is not None:
        return int(existing["id"]), False
    symbol_id = symbols.create_symbol(symbol, name or symbol, market, asset_type)
    return symbol_id, True


def run_import(
    *,
    symbol: str,
    api_symbol: str | None = None,
    name: str | None = None,
    market: str = "CN",
    asset_type: str = "STOCK",
    timeframe: str = "15m",
    datalen: int = 320,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    db_path: str | Path | None = None,
    source: KlineDataSource | None = None,
) -> dict[str, object]:
    """执行一次导入并返回汇总信息（source 可注入，便于人工排查与测试）。"""
    symbols = SymbolRepository(db_path)
    klines = KlineRepository(db_path)
    symbol_id, created = ensure_symbol(
        symbols, symbol=symbol, name=name, market=market, asset_type=asset_type
    )

    data_source = source or HttpKlineDataSource(sina_cn_kline_config(datalen=datalen))
    result = import_klines(
        data_source,
        klines,
        symbol_id,
        symbol=api_symbol or symbol,
        timeframe=timeframe,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )

    rows = klines.list_klines(symbol_id, timeframe)
    summary: dict[str, object] = {
        "db_path": str(klines.db_path),
        "symbol_id": symbol_id,
        "symbol_created": created,
        "symbol": symbol,
        "api_symbol": api_symbol or symbol,
        "timeframe": timeframe,
        "fetched": result.fetched,
        "written": result.written,
        "total_in_db": len(rows),
        "first_timestamp": rows[0]["timestamp"] if rows else None,
        "last_timestamp": rows[-1]["timestamp"] if rows else None,
    }
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入真实历史 K 线到 klines 表")
    parser.add_argument("--symbol", required=True, help="本项目标的代码，例如 600519")
    parser.add_argument("--api-symbol", default=None, help="数据源代码，例如 sh600519")
    parser.add_argument("--name", default=None, help="标的名称（新建时使用）")
    parser.add_argument("--market", default="CN", help="市场（默认 CN）")
    parser.add_argument("--asset-type", default="STOCK", help="资产类型（默认 STOCK）")
    parser.add_argument("--timeframe", default="15m", help="K 线周期（默认 15m）")
    parser.add_argument("--datalen", type=int, default=320, help="请求最近多少根（1~1023）")
    parser.add_argument("--start", dest="start_timestamp", default=None, help="起始时间（可选）")
    parser.add_argument("--end", dest="end_timestamp", default=None, help="结束时间（可选）")
    parser.add_argument("--db", dest="db_path", default=None, help="数据库路径（可选）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回进程退出码。"""
    args = _build_parser().parse_args(argv)
    print(f"数据库: {database_info(args.db_path)['db_path']}")
    try:
        summary = run_import(
            symbol=args.symbol,
            api_symbol=args.api_symbol,
            name=args.name,
            market=args.market,
            asset_type=args.asset_type,
            timeframe=args.timeframe,
            datalen=args.datalen,
            start_timestamp=args.start_timestamp,
            end_timestamp=args.end_timestamp,
            db_path=args.db_path,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 需要把错误完整报给使用者
        print(f"导入失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        "导入完成: "
        f"symbol_id={summary['symbol_id']} "
        f"(新建={summary['symbol_created']}) "
        f"timeframe={summary['timeframe']} "
        f"fetched={summary['fetched']} written={summary['written']}"
    )
    print(
        f"库中该 (symbol, timeframe) 共 {summary['total_in_db']} 根，"
        f"时间范围 {summary['first_timestamp']} ~ {summary['last_timestamp']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
