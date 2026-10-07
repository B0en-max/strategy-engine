"""通用的 HTTP / JSON 行情数据源实现（Stage 11，Stage 12 扩展）。

特点：
    - **不绑定任何具体交易所**：地址、认证、查询参数名、响应路径、字段映射、
      周期映射、时间格式全部由 config.DataSourceConfig 提供。
    - 认证信息通过**请求头**发送（不拼进 URL），错误信息里也不会出现密钥。
    - 网络访问通过可注入的 opener 完成，便于测试时用 mock 替换（不打真实网络）。

Stage 12 增加的通用能力（为接入真实数据源所需的最小扩展）：
    - 数字容错：部分接口把价格 / 成交量 / 时间戳返回成字符串
    - 时间规范化：兼容 'YYYY-MM-DD'、'YYYY-MM-DD HH:MM'、ISO 的 'T' 分隔符
    - timeframe_mapping：把内部周期（如 '15m'）映射成数据源取值（如 '15'）
    - timestamp_semantics='end'：数据源时间标签是 K 线**结束**时间时，
      解析后按周期长度回退到开始时间（仅日内周期；日线及以上标签是日期，不回退）
    - start_param / end_param 为 None 时不下发时间范围参数（由采集器本地裁切）
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import DataSourceConfig
from database import models
from database.validation import require_number, require_timeframe, require_timestamp

from .base import (
    SECONDS_PER_DAY,
    DataSourceError,
    KlineBar,
    KlineDataSource,
    require_symbol_code,
    timeframe_to_seconds,
)

#: 请求执行器：(url, headers, timeout) -> 响应字节
Opener = Callable[[str, Mapping[str, str], float], bytes]

_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2})(?::(\d{2}))?)?$")


def default_opener(url: str, headers: Mapping[str, str], timeout: float) -> bytes:
    """默认的网络执行器（标准库 urllib，不引入第三方依赖）。"""
    request = Request(url, headers=dict(headers), method="GET")
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 (地址由配置提供)
        return response.read()


def parse_response_number(value: Any, field: str) -> float:
    """把响应中的数字转换成 float。

    兼容部分接口把数字返回成字符串（例如 "1261.440"）的情况。
    """
    if isinstance(value, str):
        text = value.strip()
        try:
            value = float(text)
        except ValueError as exc:
            raise DataSourceError(f"{field} 不是数字: {value!r}") from exc
    return require_number(value, field)


def format_request_timestamp(timestamp: str, timestamp_format: str) -> int | str:
    """把内部 UTC 时间字符串转换成请求参数需要的形式。"""
    if timestamp_format == "datetime":
        return timestamp
    parsed = datetime.strptime(timestamp, models.TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    epoch = parsed.timestamp()
    return int(epoch * 1000) if timestamp_format == "milliseconds" else int(epoch)


def normalize_datetime(value: Any) -> str:
    """把日期 / 时间字符串规范化为内部格式 'YYYY-MM-DD HH:MM:SS'。

    兼容 'YYYY-MM-DD'、'YYYY-MM-DD HH:MM'、'YYYY-MM-DD HH:MM:SS' 以及 ISO 的 'T' 分隔符。
    """
    if not isinstance(value, str):
        raise DataSourceError(f"时间字段应为字符串，实际为 {value!r}")
    match = _DATETIME_RE.match(value.strip())
    if match is None:
        raise DataSourceError(f"无法解析时间字符串: {value!r}")
    date_part, hhmm, seconds = match.groups()
    return f"{date_part} {hhmm or '00:00'}:{seconds or '00'}"


def parse_response_timestamp(value: Any, timestamp_format: str) -> str:
    """把响应里的时间转换成内部 UTC 时间字符串。"""
    if timestamp_format == "datetime":
        return normalize_datetime(value)
    number = parse_response_number(value, "timestamp")
    seconds = number / 1000 if timestamp_format == "milliseconds" else number
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(models.TIMESTAMP_FORMAT)
    except (OverflowError, OSError, ValueError) as exc:
        raise DataSourceError(f"无法解析时间戳 {value!r}: {exc}") from exc


def _extract_bars(payload: Any, response_path: str | None) -> list[Any]:
    """按点号路径从响应 JSON 中取出 K 线数组。"""
    current: Any = payload
    if response_path:
        for part in response_path.split("."):
            if not isinstance(current, Mapping) or part not in current:
                raise DataSourceError(f"响应中找不到路径 {response_path!r}（缺少 {part!r}）")
            current = current[part]
    if not isinstance(current, list):
        raise DataSourceError(
            f"K 线数组应为 JSON 列表，实际为 {type(current).__name__}（response_path={response_path!r}）"
        )
    return current


def shift_to_bar_start(timestamp: str, timeframe: str) -> str:
    """把 K 线**结束**时间回退成**开始**时间（仅日内周期需要）。"""
    seconds = timeframe_to_seconds(timeframe)
    if seconds >= SECONDS_PER_DAY:
        # 日线以上的时间标签是日期，本身就代表该周期的起点，不回退
        return timestamp
    try:
        start = datetime.strptime(timestamp, models.TIMESTAMP_FORMAT) - timedelta(seconds=seconds)
    except ValueError as exc:
        raise DataSourceError(f"无法按结束时间语义回退 K 线时间 {timestamp!r}: {exc}") from exc
    return start.strftime(models.TIMESTAMP_FORMAT)


class HttpKlineDataSource(KlineDataSource):
    """可配置的 HTTP/JSON K 线数据源。"""

    def __init__(self, config: DataSourceConfig, opener: Opener | None = None) -> None:
        self._config = config
        self._opener: Opener = opener or default_opener

    @property
    def config(self) -> DataSourceConfig:
        """当前使用的配置（只读）。"""
        return self._config

    # ------------------------------------------------------------------ 请求
    def _build_url(self, params: Mapping[str, object]) -> str:
        query = urlencode(params)
        separator = "&" if "?" in self._config.base_url else "?"
        return f"{self._config.base_url}{separator}{query}"

    def _get_json(self, params: Mapping[str, object]) -> Any:
        url = self._build_url(params)
        try:
            raw = self._opener(url, self._config.headers(), self._config.timeout)
        except DataSourceError:
            raise
        except HTTPError as exc:
            raise DataSourceError(f"数据源返回 HTTP {exc.code} {exc.reason}: {url}") from exc
        except URLError as exc:
            raise DataSourceError(f"无法连接数据源: {exc.reason} -> {url}") from exc
        except OSError as exc:
            raise DataSourceError(f"请求数据源失败: {exc} -> {url}") from exc

        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataSourceError(f"数据源返回的不是合法 JSON: {exc}") from exc

    # ------------------------------------------------------------------ 解析
    def _parse_bar(self, symbol: str, timeframe: str, raw: Any) -> KlineBar:
        if not isinstance(raw, Mapping):
            raise DataSourceError(f"K 线数据应为 JSON 对象，实际为 {type(raw).__name__}")
        mapping = self._config.field_mapping

        def field(name: str, *, required: bool = True) -> Any:
            key = mapping.get(name)
            if key is None or key not in raw:
                if required:
                    raise DataSourceError(f"响应缺少字段映射: {name} → {key!r}")
                return None
            value = raw[key]
            return None if (not required and value is None) else value

        try:
            timestamp = parse_response_timestamp(
                field("timestamp"), self._config.response_timestamp_format
            )
            if self._config.timestamp_semantics == "end":
                timestamp = shift_to_bar_start(timestamp, timeframe)
            amount_value = field("amount", required=False)
            return KlineBar(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=timestamp,
                open=parse_response_number(field("open"), "open"),
                high=parse_response_number(field("high"), "high"),
                low=parse_response_number(field("low"), "low"),
                close=parse_response_number(field("close"), "close"),
                volume=parse_response_number(field("volume"), "volume"),
                amount=None if amount_value is None else parse_response_number(amount_value, "amount"),
            )
        except DataSourceError:
            raise
        except ValueError as exc:
            raise DataSourceError(f"K 线数据无法转换: {exc}") from exc

    # ------------------------------------------------------------------ 接口
    def fetch_klines(
        self,
        *,
        symbol: str,
        timeframe: str,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> list[KlineBar]:
        """按配置请求数据源并转换成标准 K 线列表。"""
        symbol = require_symbol_code(symbol)
        timeframe = require_timeframe(timeframe)

        params: dict[str, object] = dict(self._config.extra_params)
        params[self._config.symbol_param] = symbol
        params[self._config.timeframe_param] = self._config.map_timeframe(timeframe)
        if start_timestamp is not None:
            start = require_timestamp(start_timestamp)
            if self._config.start_param is not None:
                params[self._config.start_param] = format_request_timestamp(
                    start, self._config.request_timestamp_format
                )
        if end_timestamp is not None:
            end = require_timestamp(end_timestamp)
            if self._config.end_param is not None:
                params[self._config.end_param] = format_request_timestamp(
                    end, self._config.request_timestamp_format
                )

        payload = self._get_json(params)
        return [
            self._parse_bar(symbol, timeframe, raw)
            for raw in _extract_bars(payload, self._config.response_path)
        ]
