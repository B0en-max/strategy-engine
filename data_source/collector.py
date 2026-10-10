"""历史 K 线采集编排（Stage 032）：OKX → KlineRepository → klines 表。

职责划分（与既有 `data_source` 分层一致）
------------------------------------------------------------------
    数据源（OkxKlineSource）      只负责取数 / 解析 / 分页 / 限流 / 有限重试
    importer.prepare_kline_rows   只负责格式校验 + 本地裁切 + 排序（复用，不重写）
    quality.analyze_klines        只负责质量核对（复用，不重写）
    KlineRepository               只负责写库（幂等 upsert + 唯一约束 + OHLC 约束）
    本模块                        只做编排：游标 → 取数 → 校验 → 落库 → 报告

增量语义（硬约束）
------------------------------------------------------------------
    - 游标来自**实际持久化**的数据（klines 里该 (symbol, timeframe) 的最新时间戳），
      不是「上一次请求成功」；只有真正写进去的数据才会推进游标。
    - 首次回补必须显式给出起始时间（不做无边界全历史拉取，控制下载量）。
    - 增量从「已存最新一根」开始重取（允许修订这一根），更早的历史不受影响。
    - 重复执行幂等：同一区间再跑一次不会产生重复记录，也不会覆盖无关历史。
    - 每次采集是**一个原子事务**（bulk_upsert_klines）；中途失败 → 什么都不写，
      重跑即恢复（可重复执行）。
    - 未完结 K 线在数据源层就已过滤，绝不入库。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from database import KlineRepository, SymbolRepository, models
from database.validation import require_timestamp

from .base import DataSourceError, KlineBar, KlineDataSource
from .importer import prepare_kline_rows
from .okx import OKX_HISTORY_CANDLES_PATH, OkxFetchStats, OkxKlineSource, get_instrument
from .quality import KlineQualityReport, analyze_klines, deduplicate_rows, require_valid_rows

#: 数据来源与口径说明（写入报告，便于审计）
SOURCE_NAME = "OKX 公开行情接口（无需 API Key，不连接实盘账户）"
SOURCE_ENDPOINT = f"https://www.okx.com{OKX_HISTORY_CANDLES_PATH}"
TIMESTAMP_SEMANTICS_NOTE = "K 线**开始**时间，UTC（OKX 毫秒时间戳解析为 'YYYY-MM-DD HH:MM:SS'）"
VOLUME_UNIT_NOTE = "volume = 基础币成交量（OKX `vol`，如 BTC）"
AMOUNT_UNIT_NOTE = "amount = 计价币成交额（OKX `volCcyQuote`，如 USDT）；两者单位不同，绝不混用"


@dataclass(frozen=True)
class CollectionReport:
    """一次采集的完整报告（含请求区间、实际区间、质量与来源口径）。"""

    symbol: str
    inst_id: str
    market: str
    asset_type: str
    timeframe: str
    symbol_id: int
    symbol_created: bool
    requested_start: str | None
    requested_end: str | None
    actual_start: str | None
    actual_end: str | None
    fetched: int
    deduplicated: int
    written: int
    new_records: int
    updated_records: int
    total_in_db: int
    is_empty: bool
    dry_run: bool
    last_candle_confirmed: bool
    last_candle_confirm_raw: str | None
    quality: Mapping[str, Any]
    stats: Mapping[str, Any]
    source: str
    source_endpoint: str
    timestamp_semantics: str
    volume_unit: str
    amount_unit: str
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "inst_id": self.inst_id,
            "market": self.market,
            "asset_type": self.asset_type,
            "timeframe": self.timeframe,
            "symbol_id": self.symbol_id,
            "symbol_created": self.symbol_created,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "actual_start": self.actual_start,
            "actual_end": self.actual_end,
            "fetched": self.fetched,
            "deduplicated": self.deduplicated,
            "written": self.written,
            "new_records": self.new_records,
            "updated_records": self.updated_records,
            "total_in_db": self.total_in_db,
            "is_empty": self.is_empty,
            "dry_run": self.dry_run,
            "last_candle_confirmed": self.last_candle_confirmed,
            "last_candle_confirm_raw": self.last_candle_confirm_raw,
            "quality": dict(self.quality),
            "stats": dict(self.stats),
            "source": self.source,
            "source_endpoint": self.source_endpoint,
            "timestamp_semantics": self.timestamp_semantics,
            "volume_unit": self.volume_unit,
            "amount_unit": self.amount_unit,
            "warnings": list(self.warnings),
        }


def ensure_symbol(
    symbols: SymbolRepository, *, symbol: str, name: str, market: str, asset_type: str
) -> tuple[int, bool]:
    """确保标的已存在；不存在则创建。返回 (symbol_id, 是否新建)。

    标的身份 = (market, symbol)，唯一性由数据库 UNIQUE(market, symbol) 保证。
    """
    existing = symbols.get_symbol(market, symbol)
    if existing is not None:
        return int(existing["id"]), False
    return symbols.create_symbol(symbol, name, market, asset_type), True


def _empty_stats(start: str | None, end: str | None) -> OkxFetchStats:
    return OkxFetchStats(requested_start=start, requested_end=end)


class _CachedSource(KlineDataSource):
    """把已取到的 K 线包成数据源，以复用 importer.prepare_kline_rows 的校验/裁切/排序。"""

    def __init__(self, bars: Sequence[KlineBar]) -> None:
        self._bars = list(bars)

    def fetch_klines(
        self, *, symbol: str, timeframe: str,
        start_timestamp: str | None = None, end_timestamp: str | None = None,
    ) -> list[KlineBar]:
        return list(self._bars)


def collect_okx_klines(
    *,
    symbol: str,
    timeframe: str,
    db_path: str | Path | None = None,
    source: KlineDataSource | None = None,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    now_timestamp: str | None = None,
    dry_run: bool = False,
) -> CollectionReport:
    """采集一个 OKX 交易对的一个周期，写入 klines（默认正式库；测试传临时库）。

    参数：
        symbol          登记的 OKX 交易对（项目代码 / instId）
        timeframe       周期，如 "1h" / "1d"
        db_path         数据库路径（默认项目默认库）
        source          数据源（默认 OKX；测试可注入假数据源）
        start_timestamp 起始时间（含）；未给且库中无数据时报错（首次回补须显式指定）
        end_timestamp   结束时间（含）；默认到当前 UTC
        now_timestamp   覆盖「现在」（便于测试与确定性）
        dry_run         True 时只取数 + 校验，不写库
    """
    instrument = get_instrument(symbol)
    klines = KlineRepository(db_path)
    symbols = SymbolRepository(db_path)
    existing = symbols.get_symbol(instrument.market, instrument.project_symbol)
    # 只**读**游标：标的还不存在时不创建，避免取数失败时留下半成品登记
    latest = None
    if existing is not None:
        latest = klines.get_latest_kline(int(existing["id"]), timeframe)

    if start_timestamp is not None:
        effective_start = require_timestamp(start_timestamp)
    elif latest is not None:
        # 增量游标来自**实际持久化**的最新一根（重取该根以允许修订）
        effective_start = str(latest["timestamp"])
    else:
        raise DataSourceError(
            f"{instrument.inst_id} {timeframe} 在库中没有任何 K 线；"
            "首次回补必须显式给出 start_timestamp（不做无边界全历史拉取）"
        )

    effective_end = require_timestamp(end_timestamp or now_timestamp or models.utc_now_str())
    if effective_start > effective_end:
        raise DataSourceError(
            f"起始时间晚于结束时间：{effective_start} > {effective_end}"
        )

    data_source: KlineDataSource = source or OkxKlineSource()

    # 1) 先取数：**取数失败则什么都不写**（连标的登记都不做），不残留半成品
    bars = data_source.fetch_klines(
        symbol=instrument.inst_id,
        timeframe=timeframe,
        start_timestamp=effective_start,
        end_timestamp=effective_end,
    )

    # 2) 标的登记（幂等：(market, symbol) 由数据库 UNIQUE 约束保证）
    if existing is not None:
        symbol_id, created = int(existing["id"]), False
    else:
        symbol_id, created = ensure_symbol(
            symbols,
            symbol=instrument.project_symbol,
            name=instrument.name,
            market=instrument.market,
            asset_type=instrument.asset_type,
        )

    # 3) 复用既有采集编排：格式校验 + 本地裁切（闭区间）+ 排序
    fetched, rows = prepare_kline_rows(
        _CachedSource(bars),
        symbol=instrument.inst_id,
        timeframe=timeframe,
        symbol_id=symbol_id,
        start_timestamp=effective_start,
        end_timestamp=effective_end,
    )

    stats = _fetch_stats(data_source, effective_start, effective_end)
    deduped, removed = deduplicate_rows(rows)
    quality = analyze_klines(
        deduped, timeframe, start_timestamp=effective_start, end_timestamp=effective_end
    )
    require_valid_rows(deduped, quality)  # 非法行情 → 抛错，绝不静默入库

    before = klines.count_klines(symbol_id, timeframe)

    written = 0 if dry_run else klines.bulk_upsert_klines(deduped)
    total_in_db = klines.count_klines(symbol_id, timeframe)
    new_records = total_in_db - before
    updated_records = max(0, written - new_records)

    warnings = list(quality.warnings)
    if fetched == 0:
        warnings.append("本次请求没有任何 K 线返回（不是错误，但需要关注覆盖范围）")
    if not dry_run and new_records == 0 and deduped:
        warnings.append("本次没有新增记录（区间内数据已全部存在，幂等重复执行）")

    return CollectionReport(
        symbol=instrument.project_symbol,
        inst_id=instrument.inst_id,
        market=instrument.market,
        asset_type=instrument.asset_type,
        timeframe=timeframe,
        symbol_id=symbol_id,
        symbol_created=created,
        requested_start=effective_start,
        requested_end=effective_end,
        actual_start=quality.first_timestamp,
        actual_end=quality.last_timestamp,
        fetched=fetched,
        deduplicated=removed,
        written=written,
        new_records=new_records,
        updated_records=updated_records,
        total_in_db=total_in_db,
        is_empty=fetched == 0,
        dry_run=dry_run,
        last_candle_confirmed=bool(deduped),
        last_candle_confirm_raw=stats.newest_confirm,
        quality=quality.to_dict(),
        stats=stats.to_dict(),
        source=SOURCE_NAME,
        source_endpoint=SOURCE_ENDPOINT,
        timestamp_semantics=TIMESTAMP_SEMANTICS_NOTE,
        volume_unit=VOLUME_UNIT_NOTE,
        amount_unit=AMOUNT_UNIT_NOTE,
        warnings=tuple(warnings),
    )


def _fetch_stats(
    source: KlineDataSource, start: str | None, end: str | None
) -> OkxFetchStats:
    """取数据源最近一次的统计；非 OKX 数据源（测试替身）回退为空统计。"""
    if isinstance(source, OkxKlineSource) and source.last_result is not None:
        return source.last_result.stats
    return _empty_stats(start, end)


__all__ = [
    "AMOUNT_UNIT_NOTE",
    "CollectionReport",
    "SOURCE_ENDPOINT",
    "SOURCE_NAME",
    "TIMESTAMP_SEMANTICS_NOTE",
    "VOLUME_UNIT_NOTE",
    "collect_okx_klines",
    "ensure_symbol",
]
