"""交易日历（Stage 027′ 后续 Step 1）——**缓存文件 + 纯查询，不写数据库、不改 schema**。

为什么需要它
------------------------------------------------------------------
1. `klines.timestamp` 是"某只股票有 K 线"的事实，**不等于交易日历**：
   停牌 / 退市 / 未上市 都会让某只股票缺 bar，但那天仍是交易日。
   因此日历必须来自**指数**（如 `sh000001`，永不停牌），不能由个股 K 线反推。
2. `market.rules.limits.resolve_prev_close` 需要一个可信的"两个 bar 之间隔了几个交易日"
   来区分"正常前收"与"跨缺失交易日（必须用交易所参考前收盘价）"。
3. 涨跌停判定、连板计数、晋级率统计都依赖"相邻交易日"的准确定义。

本模块的边界（与项目"不猜数据 / 不入库"约定一致）
------------------------------------------------------------------
    - **只读缓存文件**：不连数据库、不新增表、不修改 schema（见 Stage 027′ 待决项 2）
    - **不猜测**：日内数据不完整的历史区间，日历覆盖范围要如实记录；
      查询落在覆盖范围之外时返回 `out_of_range`，**不返回 False 冒充"非交易日"**
    - **数据源可注入**：抓取通过网络注入的 `KlineDataSource`（默认新浪指数日 K），
      测试一律用假数据源，不打真实网络

数据来源（Preflight 实测）
------------------------------------------------------------------
    新浪指数日 K `sh000001`：单次 1 个请求可取 6000 根，实测覆盖 2002-01-11 ~ 2026-10-09；
    周末与节假日（如 2026 国庆 10-01~10-07 休市、10-08 恢复交易）已正确排除，
    与正式库 1d 的 365 个交易日逐日一致（0 差异）。可被腾讯 fqkline 交叉验证。
"""

from __future__ import annotations

import argparse
import json
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

# --------------------------------------------------------------------------- 常量
CALENDAR_SCHEMA_VERSION: int = 1
DEFAULT_INDEX_SYMBOL: str = "sh000001"  # 上证指数：A 股交易日历的锚
DEFAULT_TIMEFRAME: str = "1d"
DEFAULT_DATALEN: int = 6000  # 新浪单次上限（实测可用）
DATE_FORMAT: str = "%Y-%m-%d"

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
#: 默认缓存位置（data/cache 已在 .gitignore 内忽略内容、保留目录）
DEFAULT_CACHE_PATH: Path = PROJECT_ROOT / "data" / "cache" / "trading_calendar.json"

#: 查询状态（禁止把"超出覆盖范围"混同于"非交易日"）
CAL_TRADING: str = "trading"
CAL_NON_TRADING: str = "non_trading"
CAL_OUT_OF_RANGE: str = "out_of_range"

ProgressHook = Callable[[str], None]


def _noop(_: str) -> None:
    """默认进度回调：什么都不做。"""


def normalize_date(value: object) -> str:
    """把日期/时间戳规范化为 'YYYY-MM-DD'（校验合法性 + 统一补零，非法即抛 ValueError）。"""
    if isinstance(value, datetime):
        return value.strftime(DATE_FORMAT)
    if isinstance(value, _date):
        return value.strftime(DATE_FORMAT)
    text = str(value or "").strip()
    if len(text) >= 10:
        text = text[:10]
    parsed = datetime.strptime(text, DATE_FORMAT)  # 合法性校验（不合法 → ValueError）
    return parsed.strftime(DATE_FORMAT)  # 统一补零：避免 '2026-10-9' 与 '2026-10-09' 混排


# --------------------------------------------------------------------------- 日历
@dataclass(frozen=True)
class TradingCalendar:
    """一组**有序、去重**的交易日（'YYYY-MM-DD'）。

    只存事实：某日是否是交易日。不存"半天市"等特殊安排（本轮窗口无）。
    """

    days: tuple[str, ...]
    index_symbol: str = DEFAULT_INDEX_SYMBOL
    source: str = "sina"
    generated_at: str | None = None
    schema_version: int = CALENDAR_SCHEMA_VERSION
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized = tuple(sorted({normalize_date(d) for d in self.days}))
        object.__setattr__(self, "days", normalized)
        object.__setattr__(self, "_index_map", {d: i for i, d in enumerate(normalized)})
        object.__setattr__(self, "notes", tuple(self.notes))

    # ------------------------------------------------------------------ 基本信息
    def __len__(self) -> int:
        return len(self.days)

    def __contains__(self, value: object) -> bool:
        try:
            return normalize_date(value) in self._index_map  # type: ignore[attr-defined]
        except ValueError:
            return False

    @property
    def earliest(self) -> str | None:
        return self.days[0] if self.days else None

    @property
    def latest(self) -> str | None:
        return self.days[-1] if self.days else None

    def covers(self, value: object) -> bool:
        """该日期是否落在日历覆盖范围内（含端点）。"""
        d = normalize_date(value)
        if not self.days:
            return False
        return self.earliest <= d <= self.latest  # type: ignore[operator]

    # ------------------------------------------------------------------ 判定
    def classify(self, value: object) -> str:
        """返回 CAL_TRADING / CAL_NON_TRADING / CAL_OUT_OF_RANGE。

        **超出覆盖范围一律返回 out_of_range**，不返回 non_trading（避免把"不确定"当"休市"）。
        """
        d = normalize_date(value)
        if not self.days or d < self.days[0] or d > self.days[-1]:
            return CAL_OUT_OF_RANGE
        return CAL_TRADING if d in self._index_map else CAL_NON_TRADING  # type: ignore[attr-defined]

    def is_trading_day(self, value: object) -> bool:
        return self.classify(value) == CAL_TRADING

    def index_of(self, value: object) -> int | None:
        """该交易日在本日历中的序号（非交易日 / 超出范围 → None）。"""
        return self._index_map.get(normalize_date(value))  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ 相邻交易日
    def next_trading_day(self, value: object, *, inclusive: bool = False) -> str | None:
        """value 之后（含 inclusive）的第一个交易日；超出覆盖范围或没有则 None。"""
        if not self.days:
            return None
        d = normalize_date(value)
        pos = bisect_left(self.days, d) if inclusive else bisect_right(self.days, d)
        return self.days[pos] if pos < len(self.days) else None

    def prev_trading_day(self, value: object, *, inclusive: bool = False) -> str | None:
        """value 之前（含 inclusive）的最后一个交易日；没有则 None。"""
        if not self.days:
            return None
        d = normalize_date(value)
        pos = bisect_right(self.days, d) - 1
        if not inclusive and pos >= 0 and self.days[pos] == d:
            pos -= 1
        return self.days[pos] if pos >= 0 else None

    # ------------------------------------------------------------------ 区间与间隔
    def trading_days(self, start: object | None = None, end: object | None = None) -> tuple[str, ...]:
        """[start, end]（闭区间）内的交易日；None 表示不设该端。"""
        lo = self.days[0] if start is None else normalize_date(start)
        hi = self.days[-1] if end is None else normalize_date(end)
        left = bisect_left(self.days, lo)
        right = bisect_right(self.days, hi)
        return self.days[left:right]

    def sessions_between(self, start: object, end: object) -> int:
        """[start, end]（闭区间）内的交易日个数；start > end 时为 0。"""
        return len(self.trading_days(start, end))

    def sessions_since(self, prev_date: object, date: object) -> int | None:
        """从 prev_date 到 date 相隔的**交易日步数**（相邻交易日 = 1）。

        供 `market.rules.limits.resolve_prev_close` 判断"是否跨缺失交易日"：
        >1 表示中间缺 bar，需交易日历 + 交易所前收盘价确认。
        任一端不是交易日（或超出覆盖范围）时返回 None（不猜）。
        """
        a = self.index_of(prev_date)
        b = self.index_of(date)
        if a is None or b is None:
            return None
        return b - a

    # ------------------------------------------------------------------ 差异校验
    def missing_from(self, dates: Iterable[object]) -> tuple[str, ...]:
        """返回传入日期里"落在日历覆盖范围内、却不是本日历交易日"的日期。

        典型用法：把某只个股的 K 线日期传进来，若返回非空，说明存在
        "个股有 bar 但指数日历里不是交易日"的矛盾，需人工核查数据。
        """
        extra: list[str] = []
        for value in dates:
            d = normalize_date(value)
            if d in self._index_map or not self.covers(d):  # type: ignore[attr-defined]
                continue
            extra.append(d)
        return tuple(sorted(set(extra)))

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "index_symbol": self.index_symbol,
            "source": self.source,
            "generated_at": self.generated_at,
            "count": len(self.days),
            "earliest": self.earliest,
            "latest": self.latest,
            "days": list(self.days),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, payload: object) -> "TradingCalendar":
        if not isinstance(payload, dict):
            raise ValueError(f"日历缓存应为 JSON 对象，实际为 {type(payload).__name__}")
        days = payload.get("days")
        if not isinstance(days, list):
            raise ValueError("日历缓存缺少 'days' 列表")
        version = payload.get("schema_version", CALENDAR_SCHEMA_VERSION)
        if version != CALENDAR_SCHEMA_VERSION:
            raise ValueError(f"不支持的日历 schema_version={version!r}（当前 {CALENDAR_SCHEMA_VERSION}）")
        return cls(
            days=tuple(str(d) for d in days),
            index_symbol=str(payload.get("index_symbol") or DEFAULT_INDEX_SYMBOL),
            source=str(payload.get("source") or "unknown"),
            generated_at=payload.get("generated_at"),
            schema_version=int(version),
            notes=tuple(payload.get("notes") or ()),
        )

    def save(self, path: Path | str = DEFAULT_CACHE_PATH) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(target)  # 原子替换：避免写一半的缓存被读到
        return target

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CACHE_PATH) -> "TradingCalendar":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(payload)


def load_cache(path: Path | str = DEFAULT_CACHE_PATH) -> TradingCalendar | None:
    """读缓存；文件不存在返回 None（调用方决定是否抓取）。"""
    p = Path(path)
    if not p.exists():
        return None
    return TradingCalendar.load(p)


# --------------------------------------------------------------------------- 构造
def calendar_from_dates(
    dates: Iterable[object],
    *,
    index_symbol: str = DEFAULT_INDEX_SYMBOL,
    source: str = "unknown",
    generated_at: str | None = None,
    notes: Sequence[str] = (),
) -> TradingCalendar:
    """由任意日期集合构造日历（去重排序；用于测试与离线校验）。"""
    return TradingCalendar(
        days=tuple(normalize_date(d) for d in dates),
        index_symbol=index_symbol,
        source=source,
        generated_at=generated_at,
        notes=tuple(notes),
    )


def calendar_from_bars(
    bars: Iterable[object],
    *,
    index_symbol: str = DEFAULT_INDEX_SYMBOL,
    source: str = "unknown",
    generated_at: str | None = None,
    notes: Sequence[str] = (),
) -> TradingCalendar:
    """由指数 K 线构造日历：取每根 bar 的 `timestamp`（其日期部分）作为交易日。

    bars 需带 `timestamp` 属性（KlineBar）或为可直接规范化的字符串/日期。
    """
    dates: list[str] = []
    for bar in bars:
        ts = getattr(bar, "timestamp", bar)
        dates.append(normalize_date(ts))
    return calendar_from_dates(
        dates, index_symbol=index_symbol, source=source, generated_at=generated_at, notes=notes
    )


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fetch_calendar(
    *,
    source: object | None = None,
    index_symbol: str = DEFAULT_INDEX_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    datalen: int = DEFAULT_DATALEN,
    progress: ProgressHook | None = None,
) -> TradingCalendar:
    """从数据源抓取指数日 K 并构造日历（source 可注入；默认新浪）。

    只发 **1 个请求**（单次 datalen 上限），不逐日/逐股抓取。
    """
    notify = progress or _noop
    if source is None:
        from data_source import HttpKlineDataSource
        from market.data.sina import sina_daily_kline_config

        source = HttpKlineDataSource(sina_daily_kline_config(datalen=datalen))

    notify(f"抓取 {index_symbol} {timeframe}（datalen={datalen}）…")
    bars = source.fetch_klines(symbol=index_symbol, timeframe=timeframe)  # type: ignore[attr-defined]
    if not bars:
        raise ValueError(f"数据源未返回任何 K 线（{index_symbol} {timeframe}），日历不可构造")
    notify(f"收到 {len(bars)} 根，构造日历…")
    return calendar_from_bars(
        bars,
        index_symbol=index_symbol,
        source=str(getattr(source, "name", None) or source.__class__.__name__),
        generated_at=_now_utc(),
    )


def refresh_cache(
    path: Path | str = DEFAULT_CACHE_PATH,
    *,
    source: object | None = None,
    index_symbol: str = DEFAULT_INDEX_SYMBOL,
    timeframe: str = DEFAULT_TIMEFRAME,
    datalen: int = DEFAULT_DATALEN,
    progress: ProgressHook | None = None,
) -> TradingCalendar:
    """抓取 → 存缓存文件 → 返回日历。**只落缓存文件，不写数据库**。"""
    notify = progress or _noop
    calendar = fetch_calendar(
        source=source, index_symbol=index_symbol, timeframe=timeframe, datalen=datalen, progress=notify
    )
    saved = calendar.save(path)
    notify(f"已写入缓存 {saved}（{len(calendar)} 个交易日，{calendar.earliest} ~ {calendar.latest}）")
    return calendar


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="交易日历：抓取指数日 K 到缓存文件（不写数据库）")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE_PATH), help="缓存文件路径")
    parser.add_argument("--show", action="store_true", help="只读取并打印缓存，不抓取")
    parser.add_argument("--index-symbol", default=DEFAULT_INDEX_SYMBOL, help="指数代码（默认 sh000001）")
    parser.add_argument("--datalen", type=int, default=DEFAULT_DATALEN, help="请求根数（默认 6000）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.show:
        calendar = load_cache(args.cache)
        if calendar is None:
            print(f"[calendar] 缓存不存在：{args.cache}", file=sys.stderr)
            return 1
    else:
        calendar = refresh_cache(
            args.cache, index_symbol=args.index_symbol, datalen=args.datalen, progress=lambda m: print(f"[calendar] {m}")
        )
    if args.json:
        print(json.dumps(calendar.to_dict(), ensure_ascii=False))
    else:
        print(
            f"[calendar] {len(calendar)} 个交易日 · {calendar.earliest} ~ {calendar.latest} "
            f"· 最新 {calendar.latest} · 来源 {calendar.source}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
