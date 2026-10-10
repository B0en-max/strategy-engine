"""OKX 公开现货行情数据源（Stage 032）。

定位
------------------------------------------------------------------
把 OKX 官方**公开**历史 K 线接口接进既有 `data_source` 体系：
    - 复用 `KlineDataSource` / `KlineBar` 接口与 `DataSourceError`
    - 复用 `http_source.default_opener` / `parse_response_number` / `parse_response_timestamp`
    - 只负责「取数 + 解析 + 分页 + 限流 + 有限重试」；写库仍由 KlineRepository 负责

安全与边界
------------------------------------------------------------------
    - 只用**公开行情**接口（`/api/v5/market/...`），**不需要也不提交 API Key**，不连接实盘账户
    - 不写 SQL、不碰策略 / 执行器 / 组合层
    - 地址可用参数覆盖（默认 https://www.okx.com）

标的映射（**禁止按字符串猜测**）
------------------------------------------------------------------
OKX 的 instId（如 `BTC-USDT`）与项目标的之间用**显式登记表** `OKX_INSTRUMENTS` 关联：
只有登记在册的交易对才允许采集，未登记的直接报错，不做任何字符串推导。

字段与计量单位（依据 OKX 官方定义，逐列固定）
------------------------------------------------------------------
OKX 蜡烛数组（`/api/v5/market/history-candles` 与 `/market/candles` 同格式）：
    [0] ts           开盘时间，**毫秒** UTC
    [1] o / [2] h / [3] l / [4] c   开高低收
    [5] vol          成交量，**基础币**数量（现货，如 BTC）
    [6] volCcy       成交量，**计价币**数量（现货，如 USDT）
    [7] volCcyQuote  成交量，**计价币**（USDT）成交额
    [8] confirm      "1"=已完结 K 线；"0"=未完结
映射到本项目 klines：
    volume ← vol（基础币数量）        amount ← volCcyQuote（计价币成交额）
基础币成交量与计价币成交额**绝不混用**（另有单元测试与真实样本核对）。

分页规则（依据官方接口定义，不假定单次返回完整历史）
------------------------------------------------------------------
    - `after`  返回**早于**该时间戳（毫秒）的记录 → 用于从新到旧逐页回溯
    - `before` 返回**晚于**该时间戳（毫秒）的记录
    - `/market/history-candles` 单次 limit 上限 **100**，返回**从新到旧**
    - 未完结 K 线（confirm="0"）一律**过滤掉**，不入库
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from database import models
from database.validation import require_number, require_timeframe

from .base import DataSourceError, KlineBar, KlineDataSource, require_symbol_code
from .http_source import Opener, default_opener, parse_response_number, parse_response_timestamp

#: OKX 公开接口基地址（可参数覆盖）
OKX_BASE_URL: Final[str] = "https://www.okx.com"
OKX_HISTORY_CANDLES_PATH: Final[str] = "/api/v5/market/history-candles"
OKX_CANDLES_PATH: Final[str] = "/api/v5/market/candles"

#: 蜡烛数组字段顺序（官方定义；单位见模块 docstring）
CANDLE_FIELDS: Final[tuple[str, ...]] = (
    "ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm",
)
CANDLE_INDEX: Final[dict[str, int]] = {name: index for index, name in enumerate(CANDLE_FIELDS)}
CANDLE_MIN_FIELDS: Final[int] = len(CANDLE_FIELDS)

#: 本项目 timeframe → OKX bar 取值（OKX 的 H/D 为大写）
OKX_BAR_BY_TIMEFRAME: Final[dict[str, str]] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "2d": "2D", "3d": "3D", "1w": "1W", "1M": "1M",
}

#: 单次请求 limit 上限（history-candles = 100；candles = 300）
HISTORY_PAGE_LIMIT_MAX: Final[int] = 100
CANDLES_PAGE_LIMIT_MAX: Final[int] = 300
DEFAULT_PAGE_LIMIT: Final[int] = 100
DEFAULT_MAX_PAGES: Final[int] = 200
DEFAULT_MAX_RETRIES: Final[int] = 3
DEFAULT_MIN_REQUEST_INTERVAL: Final[float] = 0.25
DEFAULT_TIMEOUT: Final[float] = 15.0

#: OKX 业务错误码中可重试的（限流 / 系统繁忙）
RETRYABLE_CODES: Final[frozenset[str]] = frozenset({"50011", "50013", "50026"})

MILLISECONDS_PER_SECOND: Final[int] = 1000


@dataclass(frozen=True)
class OkxInstrument:
    """显式登记的一个 OKX 现货交易对与项目标的的映射关系。"""

    project_symbol: str   # 项目 symbols 表里的 symbol（此处与 instId 一致，保持 1:1 显式）
    inst_id: str          # OKX 交易对 id，例如 BTC-USDT
    base_ccy: str         # 基础币，例如 BTC
    quote_ccy: str        # 计价币，例如 USDT
    name: str             # 展示名
    market: str = "CRYPTO"
    asset_type: str = "SPOT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_symbol": self.project_symbol,
            "inst_id": self.inst_id,
            "base_ccy": self.base_ccy,
            "quote_ccy": self.quote_ccy,
            "name": self.name,
            "market": self.market,
            "asset_type": self.asset_type,
        }


#: **首批登记的交易对**（显式声明，不做字符串推导）
OKX_INSTRUMENTS: Final[dict[str, OkxInstrument]] = {
    inst.project_symbol: inst
    for inst in (
        OkxInstrument("BTC-USDT", "BTC-USDT", "BTC", "USDT", "Bitcoin / TetherUS"),
        OkxInstrument("ETH-USDT", "ETH-USDT", "ETH", "USDT", "Ethereum / TetherUS"),
        OkxInstrument("SOL-USDT", "SOL-USDT", "SOL", "USDT", "Solana / TetherUS"),
        OkxInstrument("XRP-USDT", "XRP-USDT", "XRP", "USDT", "XRP / TetherUS"),
        OkxInstrument("DOGE-USDT", "DOGE-USDT", "DOGE", "USDT", "Dogecoin / TetherUS"),
    )
}


def get_instrument(symbol: str) -> OkxInstrument:
    """按项目代码或 instId 取登记信息；未登记则报错（不做字符串猜测）。"""
    code = require_symbol_code(symbol)
    instrument = OKX_INSTRUMENTS.get(code)
    if instrument is None:
        for candidate in OKX_INSTRUMENTS.values():
            if candidate.inst_id == code:
                return candidate
    if instrument is None:
        raise DataSourceError(
            f"未登记的交易对 {symbol!r}；已登记: {sorted(OKX_INSTRUMENTS)}"
            "（OKX 交易对必须显式登记，禁止按字符串猜测映射）"
        )
    return instrument


def okx_bar(timeframe: str) -> str:
    """本项目 timeframe → OKX bar 取值。"""
    tf = require_timeframe(timeframe)
    bar = OKX_BAR_BY_TIMEFRAME.get(tf)
    if bar is None:
        raise DataSourceError(
            f"OKX 不支持该周期 {timeframe!r}，已支持: {sorted(OKX_BAR_BY_TIMEFRAME)}"
        )
    return bar


def timestamp_to_milliseconds(timestamp: str) -> int:
    """内部 UTC 时间字符串 → 毫秒时间戳（OKX 请求参数用）。"""
    from database.validation import require_timestamp

    parsed = datetime.strptime(require_timestamp(timestamp), models.TIMESTAMP_FORMAT)
    return int(parsed.replace(tzinfo=timezone.utc).timestamp() * MILLISECONDS_PER_SECOND)


def milliseconds_to_timestamp(value: object) -> str:
    """毫秒时间戳 → 内部 UTC 时间字符串。"""
    return parse_response_timestamp(value, "milliseconds")


@dataclass(frozen=True)
class OkxFetchStats:
    """一次拉取的统计（用于报告；不含密钥）。"""

    requests: int = 0
    pages: int = 0
    retries: int = 0
    empty_responses: int = 0
    unfinished_filtered: int = 0
    requested_start: str | None = None
    requested_end: str | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    #: 最新一根（请求区间内）的原始 confirm 取值，用于说明未完结 K 线的处理
    newest_confirm: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "pages": self.pages,
            "retries": self.retries,
            "empty_responses": self.empty_responses,
            "unfinished_filtered": self.unfinished_filtered,
            "requested_start": self.requested_start,
            "requested_end": self.requested_end,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "newest_confirm": self.newest_confirm,
        }


@dataclass(frozen=True)
class OkxFetchResult:
    """一次拉取的结果（K 线升序 + 统计）。"""

    bars: tuple[KlineBar, ...]
    stats: OkxFetchStats


def parse_okx_candle(
    instrument: OkxInstrument, timeframe: str, raw: object
) -> tuple[KlineBar, bool]:
    """解析一条 OKX 蜡烛数组 → (KlineBar, 是否已完结)。

    单位（现货）：volume ← vol（基础币数量）；amount ← volCcyQuote（计价币成交额）。
    """
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise DataSourceError(f"OKX 蜡烛应为数组，实际为 {type(raw).__name__}: {raw!r}")
    if len(raw) < CANDLE_MIN_FIELDS:
        raise DataSourceError(
            f"OKX 蜡烛字段不足（期望 ≥ {CANDLE_MIN_FIELDS}，实际 {len(raw)}）: {raw!r}"
        )

    def cell(name: str) -> object:
        return raw[CANDLE_INDEX[name]]

    try:
        timestamp = milliseconds_to_timestamp(cell("ts"))
        open_price = parse_response_number(cell("open"), "open")
        high = parse_response_number(cell("high"), "high")
        low = parse_response_number(cell("low"), "low")
        close = parse_response_number(cell("close"), "close")
        # vol = 基础币成交量；volCcyQuote = 计价币成交额（两者不可混用）
        base_volume = parse_response_number(cell("vol"), "vol")
        quote_amount = parse_response_number(cell("volCcyQuote"), "volCcyQuote")
    except DataSourceError:
        raise
    except ValueError as exc:
        raise DataSourceError(f"OKX 蜡烛数据无法转换: {exc}") from exc

    confirm = str(cell("confirm"))
    if confirm not in ("0", "1"):
        raise DataSourceError(f"OKX confirm 字段非法: {confirm!r}（应为 '0' 或 '1'）")

    bar = KlineBar(
        symbol=instrument.inst_id,
        timeframe=timeframe,
        timestamp=timestamp,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=base_volume,
        amount=quote_amount,
    )
    return bar, confirm == "1"


class OkxKlineSource(KlineDataSource):
    """OKX 公开历史 K 线数据源（自动分页 + 限流 + 有限重试 + 未完结过滤）。"""

    def __init__(
        self,
        *,
        base_url: str = OKX_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Opener | None = None,
        sleeper: Any = time.sleep,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = 1.0,
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_pages: int = DEFAULT_MAX_PAGES,
        path: str = OKX_HISTORY_CANDLES_PATH,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise DataSourceError("base_url 必须是非空字符串")
        if timeout <= 0:
            raise DataSourceError(f"timeout 必须为正数，实际为 {timeout!r}")
        if not 1 <= page_limit <= HISTORY_PAGE_LIMIT_MAX:
            raise DataSourceError(
                f"page_limit 必须在 1~{HISTORY_PAGE_LIMIT_MAX} 之间（history-candles 上限），"
                f"实际为 {page_limit!r}"
            )
        if max_retries < 0:
            raise DataSourceError(f"max_retries 不能为负数，实际为 {max_retries!r}")
        if max_pages < 1:
            raise DataSourceError(f"max_pages 必须 >= 1，实际为 {max_pages!r}")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener: Opener = opener or default_opener
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._min_request_interval = max(0.0, min_request_interval)
        self._page_limit = page_limit
        self._max_pages = max_pages
        self._path = path
        self._last_request_at: float | None = None
        #: 最近一次 fetch 的结果（含统计），供采集器读取
        self.last_result: OkxFetchResult | None = None

    # ------------------------------------------------------------------ 请求
    @property
    def max_retries(self) -> int:
        return self._max_retries

    def _throttle(self) -> None:
        """按最小请求间隔限流（不依赖真实时间即可被测试替换 sleeper）。"""
        if self._min_request_interval <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = self._min_request_interval - (now - self._last_request_at)
            if wait > 0:
                self._sleeper(wait)
        self._last_request_at = time.monotonic()

    def _get_json(self, params: Mapping[str, object]) -> Any:
        url = f"{self._base_url}{self._path}?{urlencode(params)}"
        try:
            raw = self._opener(url, {"Accept": "application/json"}, self._timeout)
        except DataSourceError:
            raise
        except HTTPError as exc:
            raise DataSourceError(f"OKX 返回 HTTP {exc.code} {exc.reason}: {url}") from exc
        except URLError as exc:
            raise DataSourceError(f"无法连接 OKX: {exc.reason} -> {url}") from exc
        except OSError as exc:
            raise DataSourceError(f"请求 OKX 失败: {exc} -> {url}") from exc

        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataSourceError(f"OKX 返回的不是合法 JSON: {exc}") from exc

    def _request_page(self, params: Mapping[str, object], stats: "_StatsBuilder") -> list[Any]:
        """请求一页；对网络异常与可重试错误码做**有限**重试（绝不无限重试）。"""
        attempt = 0
        while True:
            self._throttle()
            stats.requests += 1
            try:
                payload = self._get_json(params)
            except DataSourceError as exc:
                if attempt >= self._max_retries:
                    raise
                attempt += 1
                stats.retries += 1
                self._sleeper(self._retry_backoff * attempt)
                continue

            if not isinstance(payload, Mapping):
                raise DataSourceError(f"OKX 响应应为 JSON 对象，实际为 {type(payload).__name__}")
            code = str(payload.get("code", ""))
            if code == "0":
                data = payload.get("data")
                if not isinstance(data, list):
                    raise DataSourceError(f"OKX 响应 data 应为数组，实际为 {type(data).__name__}")
                return data
            if code in RETRYABLE_CODES and attempt < self._max_retries:
                attempt += 1
                stats.retries += 1
                self._sleeper(self._retry_backoff * attempt)
                continue
            raise DataSourceError(f"OKX 返回错误 code={code} msg={payload.get('msg')!r}")

    # ------------------------------------------------------------------ 取数
    def fetch_candles(
        self,
        instrument: OkxInstrument,
        timeframe: str,
        *,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
        now_timestamp: str | None = None,
    ) -> OkxFetchResult:
        """按 [start, end] 从新到旧分页拉取，返回**已完结**的 K 线（升序）。"""
        bar = okx_bar(timeframe)
        require_timeframe(timeframe)
        start_ms = timestamp_to_milliseconds(start_timestamp) if start_timestamp else None
        if end_timestamp is not None:
            end_ms = timestamp_to_milliseconds(end_timestamp)
        else:
            end_ms = timestamp_to_milliseconds(now_timestamp or models.utc_now_str())

        stats = _StatsBuilder(
            requested_start=start_timestamp, requested_end=end_timestamp
        )
        collected: dict[str, KlineBar] = {}
        cursor = end_ms + 1  # after = 严格早于该毫秒时间戳
        pages = 0

        while pages < self._max_pages:
            data = self._request_page(
                {
                    "instId": instrument.inst_id,
                    "bar": bar,
                    "after": cursor,
                    "limit": self._page_limit,
                },
                stats,
            )
            pages += 1
            stats.pages += 1
            if not data:
                stats.empty_responses += 1
                break

            oldest_ms: int | None = None
            for raw in data:
                parsed, confirmed = parse_okx_candle(instrument, timeframe, raw)
                ts_ms = int(raw[CANDLE_INDEX["ts"]])
                if stats.newest_confirm is None:
                    stats.newest_confirm = str(raw[CANDLE_INDEX["confirm"]])
                if oldest_ms is None or ts_ms < oldest_ms:
                    oldest_ms = ts_ms
                if not confirmed:
                    stats.unfinished_filtered += 1  # 未完结 K 线一律不入库
                    continue
                collected.setdefault(parsed.timestamp, parsed)

            if oldest_ms is None:
                # 整页都是未完结 K 线（正常只出现在最新一页）→ 不再回溯，避免死循环
                break
            if start_ms is not None and oldest_ms <= start_ms:
                break
            if len(data) < self._page_limit:
                break  # 返回不足一页 → 已无更早数据
            cursor = oldest_ms

        bars = tuple(collected[key] for key in sorted(collected))
        stats = stats.build(bars)
        result = OkxFetchResult(bars=bars, stats=stats)
        self.last_result = result
        return result

    def fetch_klines(
        self,
        *,
        symbol: str,
        timeframe: str,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> list[KlineBar]:
        """实现 KlineDataSource 接口：symbol 为登记过的项目代码或 instId。"""
        instrument = get_instrument(symbol)
        result = self.fetch_candles(
            instrument,
            timeframe,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        return list(result.bars)


class _StatsBuilder:
    """可变统计累加器（对外只暴露不可变的 OkxFetchStats）。"""

    def __init__(self, *, requested_start: str | None, requested_end: str | None) -> None:
        self.requests = 0
        self.pages = 0
        self.retries = 0
        self.empty_responses = 0
        self.unfinished_filtered = 0
        self.requested_start = requested_start
        self.requested_end = requested_end
        self.newest_confirm: str | None = None

    def build(self, bars: Sequence[KlineBar]) -> OkxFetchStats:
        return OkxFetchStats(
            requests=self.requests,
            pages=self.pages,
            retries=self.retries,
            empty_responses=self.empty_responses,
            unfinished_filtered=self.unfinished_filtered,
            requested_start=self.requested_start,
            requested_end=self.requested_end,
            first_timestamp=bars[0].timestamp if bars else None,
            last_timestamp=bars[-1].timestamp if bars else None,
            newest_confirm=self.newest_confirm,
        )


__all__ = [
    "CANDLE_FIELDS",
    "CANDLE_INDEX",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PAGE_LIMIT",
    "HISTORY_PAGE_LIMIT_MAX",
    "OKX_BAR_BY_TIMEFRAME",
    "OKX_BASE_URL",
    "OKX_CANDLES_PATH",
    "OKX_HISTORY_CANDLES_PATH",
    "OKX_INSTRUMENTS",
    "RETRYABLE_CODES",
    "OkxFetchResult",
    "OkxFetchStats",
    "OkxInstrument",
    "OkxKlineSource",
    "get_instrument",
    "milliseconds_to_timestamp",
    "okx_bar",
    "parse_okx_candle",
    "timestamp_to_milliseconds",
]
