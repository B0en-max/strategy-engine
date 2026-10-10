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
    #: 数据源返回的 K 线条数（裁切前）
    fetched: int
    #: 实际提交写入的条数（已按请求时间范围裁切；重复 K 线会被更新而不是新增）
    written: int


def prepare_kline_rows(
    source: KlineDataSource,
    *,
    symbol: str,
    timeframe: str,
    symbol_id: int,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> tuple[int, list[dict[str, object]]]:
    """向数据源取数 → 校验 → 转换 → 本地裁切 → 排序，**不写库**。

    返回 (数据源返回的条数, 待写入的行)。

    这是「预演（dry-run）」与「正式导入」共用的同一套规则：
    先全部转换并校验格式（任何格式错误都在写库之前中止），
    再按 [start, end]（**闭区间**）裁切，保证入库数据落在请求范围内。
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
    fetched = len(bars)

    # 先全部转换 + 校验格式（任何格式错误都在写库之前中止，时间戳也已规范化）
    rows = [bar.to_kline_row(symbol_id) for bar in bars]

    # 本地裁切到请求的时间范围（闭区间）
    if start_timestamp is not None or end_timestamp is not None:
        rows = [
            row
            for row in rows
            if (start_timestamp is None or str(row["timestamp"]) >= start_timestamp)
            and (end_timestamp is None or str(row["timestamp"]) <= end_timestamp)
        ]

    rows.sort(key=lambda row: str(row["timestamp"]))
    return fetched, rows


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
        symbol          外部数据源使用的标的代码（例如 "sh600519"）
        timeframe       K 线周期（例如 "15m"）
        start_timestamp 起始时间（含），UTC 'YYYY-MM-DD HH:MM:SS'
        end_timestamp   结束时间（含），UTC 'YYYY-MM-DD HH:MM:SS'

    时间范围处理：
        时间范围会先传给数据源；若数据源不支持范围参数（例如只返回最近 N 根），
        返回结果会再在本地按 [start, end]（**闭区间**）裁切，保证入库数据落在请求范围内。
    """
    fetched, rows = prepare_kline_rows(
        source,
        symbol=symbol,
        timeframe=timeframe,
        symbol_id=symbol_id,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
    )

    written = 0 if not rows else repository.bulk_upsert_klines(rows)
    return ImportResult(
        symbol_id=symbol_id,
        symbol=symbol,
        timeframe=timeframe,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        fetched=fetched,
        written=written,
    )
