"""K 线数据质量检查（Stage 032）。

定位
------------------------------------------------------------------
**纯计算**：对「已解析成 klines 行」的数据做质量核对，不访问网络、不写库。
供采集器在写库**之前**调用，保证：
    - 时间戳升序
    - 重复 K 线可被检测并幂等去重
    - OHLC 合理性（high ≥ max(open, close)、low ≤ min(open, close)、high ≥ low）
    - 非法价格（≤ 0）与非法成交量（< 0）被检出
    - 缺失区间能被**发现**（只报告，不插值补造）
    - 请求成功但为空时给出明确结论

明确不做：不插值、不补造行情、不把缺失 K 线当成「价格不变」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from database.validation import require_number, require_timestamp, require_timeframe

from .base import DataSourceError, timeframe_to_seconds
from .okx import milliseconds_to_timestamp, timestamp_to_milliseconds

EPSILON: float = 1e-9
MILLISECONDS_PER_SECOND: int = 1000


@dataclass(frozen=True)
class KlineQualityReport:
    """一次数据质量核对的结果。"""

    bars: int
    distinct_timestamps: int
    first_timestamp: str | None
    last_timestamp: str | None
    duplicates: int
    out_of_order: int
    ohlc_violations: int
    non_positive_prices: int
    negative_volumes: int
    #: 期望区间数（按周期长度从 first 到 last 推算）与缺失数
    expected_intervals: int
    missing_intervals: int
    #: 缺失区间明细：((起始缺失时间, 结束缺失时间, 缺失根数), ...)
    gap_ranges: tuple[tuple[str, str, int], ...]
    #: 实际覆盖天数（(last - first) / 一天）
    coverage_days: float
    invalid_bars: int
    warnings: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """无非法 / 重复 / 乱序即为干净（缺失只告警，不算非法）。"""
        return (
            self.duplicates == 0
            and self.out_of_order == 0
            and self.invalid_bars == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "bars": self.bars,
            "distinct_timestamps": self.distinct_timestamps,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "duplicates": self.duplicates,
            "out_of_order": self.out_of_order,
            "ohlc_violations": self.ohlc_violations,
            "non_positive_prices": self.non_positive_prices,
            "negative_volumes": self.negative_volumes,
            "expected_intervals": self.expected_intervals,
            "missing_intervals": self.missing_intervals,
            "gap_ranges": [[start, end, count] for start, end, count in self.gap_ranges],
            "coverage_days": self.coverage_days,
            "invalid_bars": self.invalid_bars,
            "warnings": list(self.warnings),
        }


def _bar_is_invalid(o: float, h: float, low: float, c: float, volume: float) -> tuple[bool, bool, bool]:
    """返回 (OHLC 违规, 非正价格, 负成交量)。"""
    ohlc = (
        h < max(o, c) - EPSILON
        or low > min(o, c) + EPSILON
        or h < low - EPSILON
    )
    non_positive = min(o, h, low, c) <= 0
    negative_volume = volume < 0
    return ohlc, non_positive, negative_volume


def analyze_klines(
    rows: Sequence[Mapping[str, Any]],
    timeframe: str,
    *,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
) -> KlineQualityReport:
    """对已解析的 klines 行做质量核对（不改数据、不补数据）。

    参数：
        rows             klines 行（含 timestamp / open / high / low / close / volume）
        timeframe        周期（用于推算缺失区间）
        start_timestamp  可选：只在「请求区间」内统计缺失（闭区间）
        end_timestamp    可选：同上
    """
    tf = require_timeframe(timeframe)
    if start_timestamp is not None:
        require_timestamp(start_timestamp)
    if end_timestamp is not None:
        require_timestamp(end_timestamp)

    timestamps: list[str] = []
    duplicates = 0
    seen: set[str] = set()
    out_of_order = 0
    previous: str | None = None
    ohlc_violations = 0
    non_positive = 0
    negative_volume = 0
    invalid_bars = 0

    for row in rows:
        ts = str(row["timestamp"])
        timestamps.append(ts)
        if ts in seen:
            duplicates += 1
        seen.add(ts)
        if previous is not None and ts < previous:
            out_of_order += 1
        previous = ts

        o = require_number(row["open"], "open")
        h = require_number(row["high"], "high")
        low = require_number(row["low"], "low")
        c = require_number(row["close"], "close")
        volume = require_number(row["volume"], "volume")
        bad_ohlc, bad_price, bad_volume = _bar_is_invalid(o, h, low, c, volume)
        if bad_ohlc:
            ohlc_violations += 1
        if bad_price:
            non_positive += 1
        if bad_volume:
            negative_volume += 1
        if bad_ohlc or bad_price or bad_volume:
            invalid_bars += 1

    first = min(seen) if seen else None
    last = max(seen) if seen else None

    expected_intervals = 0
    missing_intervals = 0
    gap_ranges: list[tuple[str, str, int]] = []
    coverage_days = 0.0
    step = timeframe_to_seconds(tf) * MILLISECONDS_PER_SECOND
    if first is not None and last is not None:
        first_ms = timestamp_to_milliseconds(first)
        last_ms = timestamp_to_milliseconds(last)
        coverage_days = round((last_ms - first_ms) / (MILLISECONDS_PER_SECOND * 86400), 6)

        # 缺失只在「实际收到的跨度」与「请求区间」的交集内统计，
        # 避免把「交易所本来没有更早数据」误报成缺失。
        window_start = timestamp_to_milliseconds(start_timestamp) if start_timestamp else first_ms
        window_end = timestamp_to_milliseconds(end_timestamp) if end_timestamp else last_ms
        intersect_start = max(first_ms, window_start)
        intersect_end = min(last_ms, window_end)
        aligned_start = first_ms + ((intersect_start - first_ms + step - 1) // step) * step
        aligned_end = first_ms + ((intersect_end - first_ms) // step) * step
        if aligned_end >= aligned_start:
            expected_intervals = (aligned_end - aligned_start) // step + 1
            grid = sorted(
                ms
                for ms in (timestamp_to_milliseconds(ts) for ts in seen)
                if aligned_start <= ms <= aligned_end
            )
            missing_intervals = max(0, expected_intervals - len(grid))
            previous: int | None = None
            for ms in grid:
                if previous is not None and ms - previous > step:
                    count = (ms - previous) // step - 1
                    gap_ranges.append(
                        (milliseconds_to_timestamp(previous + step),
                         milliseconds_to_timestamp(ms - step), count)
                    )
                previous = ms
            if not grid and expected_intervals:
                gap_ranges.append(
                    (milliseconds_to_timestamp(aligned_start),
                     milliseconds_to_timestamp(aligned_end), expected_intervals)
                )

    warnings: list[str] = []
    if not rows:
        warnings.append("数据源返回空数据（请求成功但没有任何 K 线）")
    if duplicates:
        warnings.append(f"存在 {duplicates} 条重复时间戳（写库前会幂等去重）")
    if out_of_order:
        warnings.append(f"存在 {out_of_order} 处时间乱序（写库前会排序）")
    if ohlc_violations:
        warnings.append(f"存在 {ohlc_violations} 处 OHLC 约束异常")
    if non_positive:
        warnings.append(f"存在 {non_positive} 处非正价格")
    if negative_volume:
        warnings.append(f"存在 {negative_volume} 处负成交量")
    if missing_intervals:
        warnings.append(
            f"区间内缺失 {missing_intervals} 根 K 线（只标记，不插值补造）"
        )

    return KlineQualityReport(
        bars=len(rows),
        distinct_timestamps=len(seen),
        first_timestamp=first,
        last_timestamp=last,
        duplicates=duplicates,
        out_of_order=out_of_order,
        ohlc_violations=ohlc_violations,
        non_positive_prices=non_positive,
        negative_volumes=negative_volume,
        expected_intervals=expected_intervals,
        missing_intervals=missing_intervals,
        gap_ranges=tuple(gap_ranges),
        coverage_days=coverage_days,
        invalid_bars=invalid_bars,
        warnings=tuple(warnings),
    )


def deduplicate_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """按 timestamp 幂等去重（保留最后一次出现），返回 (去重后按时间升序的行, 去掉的条数)。

    去重发生在写库前；配合 klines 的 UNIQUE(symbol_id, timeframe, timestamp)，
    重复采集同一区间不会产生重复记录。
    """
    by_timestamp: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        total += 1
        by_timestamp[str(row["timestamp"])] = dict(row)
    ordered = [by_timestamp[key] for key in sorted(by_timestamp)]
    return ordered, total - len(ordered)


def require_valid_rows(rows: Sequence[Mapping[str, Any]], report: KlineQualityReport) -> None:
    """存在非法行情时抛错（**不静默入库**）。"""
    if report.invalid_bars:
        bad = [
            str(row["timestamp"])
            for row in rows
            if _bar_is_invalid(
                float(row["open"]), float(row["high"]), float(row["low"]),
                float(row["close"]), float(row["volume"]),
            )
            != (False, False, False)
        ]
        raise DataSourceError(
            f"检测到 {report.invalid_bars} 条非法行情（OHLC 异常 / 非正价格 / 负成交量），"
            f"已拒绝写入；问题时间戳示例: {bad[:5]}"
        )


__all__ = [
    "KlineQualityReport",
    "analyze_klines",
    "deduplicate_rows",
    "require_valid_rows",
]
