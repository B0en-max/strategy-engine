"""K 线采集编排：把数据源拉到的 K 线写入 klines 表（Stage 11）。

职责划分：
    数据源实现（HTTP/JSON 解析）  ← 只负责 fetch
    KlineRepository              ← 只负责写库（含唯一约束、OHLC 校验）
    本模块                        ← 只做编排，不写 SQL、不碰 HTTP 细节

重复数据处理：
    使用 KlineRepository.bulk_upsert_klines()：
    同一 (symbol_id, timeframe, timestamp) 已存在时更新 OHLCV，不存在时插入；
    配合数据库 UNIQUE 约束，重复采集同一时间段**不会产生重复记录**。
"""

from __future__ import annotations

from dataclasses import dataclass

from database import KlineRepository
from database.validation import require_positive_int, require_timeframe, require_timestamp

from .base import DataSourceError, KlineDataSource, KlineBar, require_symbol_code


@dataclass(frozen=True)
class ImportResult:
    """一次采集的结果。"""

    symbol_id: int
    symbol: str
    timeframe: str
    start_timestamp: str | None
    end_timestamp: str | None
    #: 数据源返回的 K 线条数
    fetched: int
    #: 实际提交写入的条数（重复 K 线会被更新而不是新增，因此不等于新增行数）
    written: int


def import_klines(
    source: KlineDataSource,
    repository: KlineRepository,
    symbol_id: int,
    *,
    symbol: str,
    timeframe: str,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> ImportResult:
    """从数据源采集一段历史 K 线并写入数据库，返回采集结果。

    参数：
        source          任意实现了 KlineDataSource 的数据源
        repository      现有 KlineRepository（负责实际写库）
        symbol_id       klines.symbol_id（对应 symbols 表中的标的）
        symbol          外部数据源使用的标的代码（例如 "BTCUSDT"）
        timeframe       K 线周期（例如 "1h"）
        start_timestamp 起始时间（含），UTC 'YYYY-MM-DD HH:MM:SS'
        end_timestamp   结束时间（含），UTC 'YYYY-MM-DD HH:MM:SS'
    """
    symbol_id = require_positive_int(symbol_id, "symbol_id")
    symbol = require_symbol_code(symbol)
    timeframe = require_timeframe(timeframe)
    if start_timestamp is not None:
        start_timestamp = require_timestamp(start_timestamp)
    if end_timestamp is not None:
        end_timestamp = require_timestamp(end_timestamp)

    bars: list[KlineBar] = source.fetch_klines(
        symbol=symbol,
        timeframe=timeframe,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )
    if not isinstance(bars, list):
        raise DataSourceError(f"数据源应返回 KlineBar 列表，实际为 {type(bars).__name__}")

    # 先全部转换 + 校验格式（任何格式错误都在写库之前中止），并按时间排序
    rows = [bar.to_kline_row(symbol_id) for bar in bars]
    rows.sort(key=lambda row: str(row["timestamp"]))

    written = 0 if not rows else repository.bulk_upsert_klines(rows)
    return ImportResult(
        symbol_id=symbol_id,
        symbol=symbol,
        timeframe=timeframe,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        fetched=len(bars),
        written=written,
    )
