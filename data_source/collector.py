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
from typing import Any, Final, Mapping, Sequence

from database import KlineRepository, SymbolRepository, models
from database.validation import require_timestamp

from .base import DataSourceError, KlineBar, KlineDataSource, timeframe_to_seconds
from .importer import prepare_kline_rows
from .okx import (
    MODE_DERIVED,
    MODE_UNSUPPORTED,
    OKX_HISTORY_CANDLES_PATH,
    OKX_INSTRUMENTS,
    REQUIRED_TIMEFRAMES,
    OkxCandlePage,
    OkxFetchStats,
    OkxKlineSource,
    aggregate_bars,
    get_instrument,
    milliseconds_to_timestamp,
    timeframe_plan,
    timestamp_to_milliseconds,
)
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


# --------------------------------------------------------------------------- 全历史回补
CONFLICT_POLICY_REPORT: Final[str] = "report"        # 与已有数据冲突时只报告，不覆盖
CONFLICT_POLICY_OVERWRITE: Final[str] = "overwrite"  # 显式选择时才覆盖
CONFLICT_POLICIES: Final[tuple[str, ...]] = (CONFLICT_POLICY_REPORT, CONFLICT_POLICY_OVERWRITE)

#: 视为「回补完成」的分页停止原因
STOP_COMPLETE: Final[frozenset[str]] = frozenset({"no_more_data", "reached_start", "only_unfinished"})

_OHLCV_KEYS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume", "amount")
_RELATIVE_TOLERANCE: Final[float] = 1e-9
MILLISECONDS_PER_SECOND: Final[int] = 1000


def _number_matches(left: object, right: object) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    a, b = float(left), float(right)
    return abs(a - b) <= _RELATIVE_TOLERANCE * max(1.0, abs(a), abs(b))


def _split_against_existing(
    klines: KlineRepository, symbol_id: int, timeframe: str, rows: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[tuple[Mapping[str, Any], Any]], int]:
    """把待写行分成 (新行, 与已有数据冲突的行, 完全相同的行数)。

    **不覆盖既有有效数据**：只有显式选择 overwrite 才会写入冲突行。
    """
    if not rows:
        return [], [], 0
    timestamps = [str(row["timestamp"]) for row in rows]
    existing = {
        str(item["timestamp"]): item
        for item in klines.list_klines(symbol_id, timeframe, min(timestamps), max(timestamps))
    }
    new_rows: list[dict[str, Any]] = []
    conflicts: list[tuple[Mapping[str, Any], Any]] = []
    identical = 0
    for row in rows:
        current = existing.get(str(row["timestamp"]))
        if current is None:
            new_rows.append(dict(row))
        elif all(_number_matches(current[key], row[key]) for key in _OHLCV_KEYS):
            identical += 1
        else:
            conflicts.append((row, current))
    return new_rows, conflicts, identical


def _clip_rows(
    rows: Sequence[Mapping[str, Any]], start: str | None, end: str | None
) -> list[dict[str, Any]]:
    """按 [start, end] 闭区间裁切（与 importer.prepare_kline_rows 同一套规则）。"""
    return [
        dict(row)
        for row in rows
        if (start is None or str(row["timestamp"]) >= start)
        and (end is None or str(row["timestamp"]) <= end)
    ]


def _persist_rows(
    klines: KlineRepository,
    symbol_id: int,
    timeframe: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    start: str | None,
    end: str | None,
    conflict_policy: str,
    dry_run: bool,
) -> tuple[dict[str, Any], list[int]]:
    """一批：裁切 → 质量核对 → 去重 → 冲突检查 → 幂等写入。返回 (统计, 该批时间戳 ms)。"""
    clipped = _clip_rows(rows, start, end)
    quality = analyze_klines(clipped, timeframe, start_timestamp=start, end_timestamp=end)
    require_valid_rows(clipped, quality)  # 非法行情 → 抛错，绝不静默入库
    deduped, removed = deduplicate_rows(clipped)
    result: dict[str, Any] = {
        "valid": len(deduped), "duplicates": removed, "rejected": quality.invalid_bars,
        "written": 0, "identical": 0, "conflicts": [],
    }
    ms_list = [timestamp_to_milliseconds(str(row["timestamp"])) for row in deduped]
    if dry_run or not deduped:
        return result, ms_list
    new_rows, conflicts, identical = _split_against_existing(klines, symbol_id, timeframe, deduped)
    result["conflicts"] = conflicts
    result["identical"] = identical
    to_write: list[dict[str, Any]] = list(new_rows)
    if conflict_policy == CONFLICT_POLICY_OVERWRITE:
        to_write.extend(dict(row) for row, _ in conflicts)
    if to_write:
        klines.bulk_upsert_klines(to_write)
        result["written"] = len(to_write)
    return result, ms_list


@dataclass(frozen=True)
class BackfillReport:
    """一个（交易对, 周期）组合的全历史回补 / 增量采集报告。"""

    symbol: str
    inst_id: str
    timeframe: str
    fetch_timeframe: str
    mode: str                 # native / derived
    derived_from: str | None
    symbol_id: int | None
    symbol_created: bool
    requested_start: str | None
    requested_end: str
    #: 断点续传起点（该组合已入库的最早一根；None 表示本次不是续传）
    resumed_from: str | None
    earliest: str | None
    latest: str | None
    fetched: int
    valid: int
    duplicates: int
    rejected: int
    unfinished: int
    pages: int
    requests: int
    retries: int
    recoveries: int
    conflicts: int
    conflict_samples: tuple[Mapping[str, Any], ...]
    written: int
    new_records: int
    total_in_db: int
    gap_ranges: tuple[tuple[str, str, int], ...]
    missing_intervals: int
    coverage_days: float
    skipped_groups: int
    stop_reason: str | None
    complete: bool
    dry_run: bool
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "inst_id": self.inst_id,
            "timeframe": self.timeframe,
            "fetch_timeframe": self.fetch_timeframe,
            "mode": self.mode,
            "derived_from": self.derived_from,
            "symbol_id": self.symbol_id,
            "symbol_created": self.symbol_created,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "resumed_from": self.resumed_from,
            "earliest": self.earliest,
            "latest": self.latest,
            "fetched": self.fetched,
            "valid": self.valid,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "unfinished": self.unfinished,
            "pages": self.pages,
            "requests": self.requests,
            "retries": self.retries,
            "recoveries": self.recoveries,
            "conflicts": self.conflicts,
            "conflict_samples": [dict(sample) for sample in self.conflict_samples],
            "written": self.written,
            "new_records": self.new_records,
            "total_in_db": self.total_in_db,
            "gap_ranges": [[start, end, count] for start, end, count in self.gap_ranges],
            "missing_intervals": self.missing_intervals,
            "coverage_days": self.coverage_days,
            "skipped_groups": self.skipped_groups,
            "stop_reason": self.stop_reason,
            "complete": self.complete,
            "dry_run": self.dry_run,
            "warnings": list(self.warnings),
            "source": SOURCE_NAME,
            "timestamp_semantics": TIMESTAMP_SEMANTICS_NOTE,
            "volume_unit": VOLUME_UNIT_NOTE,
            "amount_unit": AMOUNT_UNIT_NOTE,
        }


def _derived_pages(
    data_source: KlineDataSource,
    instrument: Any,
    plan: Any,
    start: str | None,
    end: str,
    now: str,
    holder: dict[str, Any],
):
    """派生周期：先取全量基础周期 K 线，再**严格聚合**（整组不完整就丢弃）。"""
    fetched = data_source.fetch_candles(
        instrument, plan.derived_from, start_timestamp=start, end_timestamp=end, now_timestamp=now
    )
    aggregated, skipped = aggregate_bars(list(fetched.bars), plan.derived_from, plan.timeframe)
    holder["skipped_groups"] = skipped
    holder["base_bars"] = len(fetched.bars)
    stats = fetched.stats
    yield OkxCandlePage(
        1, tuple(aggregated), len(fetched.bars), stats.unfinished_filtered,
        stats.newest_confirm, stats.requests, stats.retries, not aggregated,
    )


def backfill_okx_klines(
    *,
    symbol: str,
    timeframe: str,
    db_path: str | Path | None = None,
    source: KlineDataSource | None = None,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    now_timestamp: str | None = None,
    dry_run: bool = False,
    conflict_policy: str = CONFLICT_POLICY_REPORT,
    allow_derivation: bool = True,
) -> BackfillReport:
    """全历史 / 指定区间回补：**逐页落库**（批次提交 + 断点续传）。

    start_timestamp 为 None 时一直回溯到交易所的最早记录；
    同一唯一键出现 OHLCV 冲突时默认**只报告不覆盖**（conflict_policy="report"）。
    """
    if conflict_policy not in CONFLICT_POLICIES:
        raise DataSourceError(f"未知冲突策略 {conflict_policy!r}，可选 {list(CONFLICT_POLICIES)}")
    plan = timeframe_plan(timeframe, allow_derivation=allow_derivation)
    if plan.mode == MODE_UNSUPPORTED:
        raise DataSourceError(
            f"周期 {timeframe!r} 既没有 OKX 原生 bar，也不可派生（原生支持: {list(REQUIRED_TIMEFRAMES)}）"
        )
    instrument = get_instrument(symbol)
    klines = KlineRepository(db_path)
    symbols = SymbolRepository(db_path)
    now = now_timestamp or models.utc_now_str()
    data_source: KlineDataSource = source or OkxKlineSource()
    requested_start = require_timestamp(start_timestamp) if start_timestamp is not None else None
    requested_end = require_timestamp(end_timestamp or now)

    symbol_row = symbols.get_symbol(instrument.market, instrument.project_symbol)
    symbol_id = int(symbol_row["id"]) if symbol_row is not None else None
    created = False
    before = klines.count_klines(symbol_id, timeframe) if symbol_id is not None else 0

    # 断点续传：全历史回补时，若已有数据，只向**更早**的方向继续（新端已入库，不重复下载）
    fetch_end = requested_end
    resumed_from: str | None = None
    if requested_start is None and symbol_id is not None:
        earliest_row = klines.get_earliest_kline(symbol_id, timeframe)
        if earliest_row is not None:
            resumed_from = str(earliest_row["timestamp"])
            fetch_end = resumed_from

    counters = {"fetched": 0, "valid": 0, "duplicates": 0, "rejected": 0,
                "unfinished": 0, "pages": 0, "requests": 0, "retries": 0, "recoveries": 0}
    written = 0
    conflict_samples: list[dict[str, Any]] = []
    gap_ranges: list[tuple[int, int, int]] = []
    prev_oldest: int | None = None
    retries_before = 0
    step_ms = timeframe_to_seconds(timeframe) * MILLISECONDS_PER_SECOND
    holder: dict[str, Any] = {"skipped_groups": 0, "base_bars": 0}

    if plan.mode == MODE_DERIVED:
        pages = _derived_pages(data_source, instrument, plan, requested_start, fetch_end, now, holder)
    else:
        pages = data_source.iter_candle_pages(
            instrument, timeframe,
            start_timestamp=requested_start, end_timestamp=fetch_end, now_timestamp=now,
        )

    for page in pages:
        counters["pages"] += 1
        counters["requests"] = page.requests
        counters["unfinished"] += page.unfinished_filtered
        if page.retries > retries_before:
            counters["recoveries"] += 1
            retries_before = page.retries
        counters["retries"] = page.retries
        counters["fetched"] += page.raw_count
        if page.is_empty or not page.bars:
            continue
        if symbol_id is None:
            symbol_id, created = ensure_symbol(
                symbols, symbol=instrument.project_symbol, name=instrument.name,
                market=instrument.market, asset_type=instrument.asset_type,
            )
        rows = [bar.to_kline_row(symbol_id) for bar in page.bars]
        stats, ms_list = _persist_rows(
            klines, symbol_id, timeframe, rows, start=requested_start, end=requested_end,
            conflict_policy=conflict_policy, dry_run=dry_run,
        )
        counters["valid"] += stats["valid"]
        counters["duplicates"] += stats["duplicates"]
        counters["rejected"] += stats["rejected"]
        written += stats["written"]
        for row, current in stats["conflicts"][: max(0, 5 - len(conflict_samples))]:
            conflict_samples.append({
                "timestamp": str(row["timestamp"]),
                "existing": {key: (None if current[key] is None else float(current[key])) for key in _OHLCV_KEYS},
                "incoming": {key: row[key] for key in _OHLCV_KEYS},
            })
        # 缺口检测：页内相邻跳变 + 页与页之间的跳变（页面按时间递减且不重叠）
        for left, right in zip(ms_list, ms_list[1:]):
            if right - left > step_ms:
                gap_ranges.append((left + step_ms, right - step_ms, (right - left) // step_ms - 1))
        if prev_oldest is not None and ms_list[0] < prev_oldest - step_ms:
            count = (prev_oldest - step_ms - ms_list[0]) // step_ms
            gap_ranges.append((ms_list[0] + step_ms, prev_oldest - step_ms, count))
        prev_oldest = ms_list[0]

    if plan.mode == MODE_DERIVED:
        stop_reason = f"derived_from:{plan.derived_from}"
        complete = holder["skipped_groups"] == 0 and counters["fetched"] > 0
    else:
        stop_reason = getattr(data_source, "last_stop_reason", None)
        complete = stop_reason in STOP_COMPLETE

    merged_gaps = _merge_gaps(gap_ranges)
    missing_intervals = sum(count for _start, _end, count in merged_gaps)
    all_ms: list[int] = []
    if symbol_id is not None:
        rows_in_db = klines.list_klines(symbol_id, timeframe)
        all_ms = [timestamp_to_milliseconds(str(row["timestamp"])) for row in rows_in_db]
    earliest = milliseconds_to_timestamp(min(all_ms)) if all_ms else None
    latest = milliseconds_to_timestamp(max(all_ms)) if all_ms else None
    coverage_days = 0.0
    if all_ms:
        coverage_days = round((max(all_ms) - min(all_ms)) / (MILLISECONDS_PER_SECOND * 86400), 6)
    total_in_db = len(all_ms)

    warnings: list[str] = []
    if counters["fetched"] == 0 and total_in_db == 0:
        warnings.append("交易所没有返回任何 K 线（区间内可能本来就没有数据）")
    if missing_intervals:
        warnings.append(f"区间内缺失 {missing_intervals} 根 K 线（只标记，不插值补造）")
    if conflict_samples:
        warnings.append("已有数据与本次返回值不一致（默认不覆盖，仅报告差异）")
    if plan.mode == MODE_DERIVED and holder["skipped_groups"]:
        warnings.append(
            f"派生 {timeframe} 时有 {holder['skipped_groups']} 组基础数据不完整，已整组丢弃"
        )
    if not complete and counters["pages"]:
        warnings.append(f"回补未完成（停止原因: {stop_reason!r}）")

    return BackfillReport(
        symbol=instrument.project_symbol,
        inst_id=instrument.inst_id,
        timeframe=timeframe,
        fetch_timeframe=plan.derived_from or timeframe,
        mode=plan.mode,
        derived_from=plan.derived_from,
        symbol_id=symbol_id,
        symbol_created=created,
        requested_start=requested_start,
        requested_end=fetch_end,
        resumed_from=resumed_from,
        earliest=earliest,
        latest=latest,
        fetched=counters["fetched"],
        valid=counters["valid"],
        duplicates=counters["duplicates"],
        rejected=counters["rejected"],
        unfinished=counters["unfinished"],
        pages=counters["pages"],
        requests=counters["requests"],
        retries=counters["retries"],
        recoveries=counters["recoveries"],
        conflicts=len(conflict_samples),
        conflict_samples=tuple(conflict_samples),
        written=written,
        new_records=total_in_db - before,
        total_in_db=total_in_db,
        gap_ranges=tuple((milliseconds_to_timestamp(a), milliseconds_to_timestamp(b), c) for a, b, c in merged_gaps),
        missing_intervals=missing_intervals,
        coverage_days=coverage_days,
        skipped_groups=holder["skipped_groups"],
        stop_reason=stop_reason,
        complete=complete,
        dry_run=dry_run,
        warnings=tuple(warnings),
    )


def _merge_gaps(gaps: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """合并相邻 / 重叠的缺口区间（按起点排序后依次合并）。"""
    if not gaps:
        return []
    ordered = sorted(gaps, key=lambda item: item[0])
    merged: list[tuple[int, int, int]] = [ordered[0]]
    for start, end, count in ordered[1:]:
        last_start, last_end, last_count = merged[-1]
        if start <= last_end:
            new_end = max(last_end, end)
            merged[-1] = (last_start, new_end, last_count + count)
        else:
            merged.append((start, end, count))
    return merged


@dataclass(frozen=True)
class ComboResult:
    """矩阵中一个 (交易对, 周期) 组合的结果。"""

    symbol: str
    timeframe: str
    ok: bool
    report: BackfillReport | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ok": self.ok,
            "error": self.error,
            "report": None if self.report is None else self.report.to_dict(),
        }


@dataclass(frozen=True)
class MatrixReport:
    """5 交易对 × 9 周期的组合回补总报告。"""

    combos: tuple[ComboResult, ...]
    total: int
    ok: int
    failed: int
    complete: int
    incomplete: int
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "combos": [combo.to_dict() for combo in self.combos],
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "complete": self.complete,
            "incomplete": self.incomplete,
            "dry_run": self.dry_run,
        }


def backfill_matrix(
    *,
    symbols: Sequence[str] | None = None,
    timeframes: Sequence[str] | None = None,
    db_path: str | Path | None = None,
    source_factory: Any = None,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    now_timestamp: str | None = None,
    dry_run: bool = False,
    conflict_policy: str = CONFLICT_POLICY_REPORT,
    allow_derivation: bool = True,
    continue_on_error: bool = True,
) -> MatrixReport:
    """对「交易对 × 周期」矩阵逐个回补（默认 5 × 9 = 45 个组合）。

    source_factory 每次调用返回一个**新的**数据源（各自限流与统计独立）；
    默认 `OkxKlineSource()`。单个组合失败不影响其他组合（continue_on_error）。
    """
    symbol_list = list(symbols) if symbols is not None else list(OKX_INSTRUMENTS)
    timeframe_list = list(timeframes) if timeframes is not None else list(REQUIRED_TIMEFRAMES)
    factory = source_factory or (lambda: OkxKlineSource())

    combos: list[ComboResult] = []
    for symbol_code in symbol_list:
        for timeframe in timeframe_list:
            try:
                report = backfill_okx_klines(
                    symbol=symbol_code, timeframe=timeframe, db_path=db_path,
                    source=factory(), start_timestamp=start_timestamp, end_timestamp=end_timestamp,
                    now_timestamp=now_timestamp, dry_run=dry_run,
                    conflict_policy=conflict_policy, allow_derivation=allow_derivation,
                )
                combos.append(ComboResult(symbol_code, timeframe, True, report, None))
            except Exception as exc:  # noqa: BLE001 - 单个组合失败不影响其它组合
                if not continue_on_error:
                    raise
                combos.append(ComboResult(symbol_code, timeframe, False, None,
                                          f"{type(exc).__name__}: {exc}"))

    ok = sum(1 for combo in combos if combo.ok)
    complete = sum(1 for combo in combos if combo.ok and combo.report is not None and combo.report.complete)
    return MatrixReport(
        combos=tuple(combos),
        total=len(combos),
        ok=ok,
        failed=len(combos) - ok,
        complete=complete,
        incomplete=ok - complete,
        dry_run=dry_run,
    )


__all__ = [
    "AMOUNT_UNIT_NOTE",
    "BackfillReport",
    "CONFLICT_POLICIES",
    "CONFLICT_POLICY_OVERWRITE",
    "CONFLICT_POLICY_REPORT",
    "CollectionReport",
    "ComboResult",
    "MatrixReport",
    "SOURCE_ENDPOINT",
    "SOURCE_NAME",
    "STOP_COMPLETE",
    "TIMESTAMP_SEMANTICS_NOTE",
    "VOLUME_UNIT_NOTE",
    "backfill_matrix",
    "backfill_okx_klines",
    "collect_okx_klines",
    "ensure_symbol",
]
