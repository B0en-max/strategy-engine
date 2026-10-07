"""K 线数据源的最小接口（Stage 11）。

只保留「获取历史 K 线」所必需的东西：
    - KlineBar          统一的一根 K 线（数据源之间互通的标准结构）
    - KlineDataSource   只有一个 fetch_klines() 方法的接口
    - DataSourceError   数据源相关错误（网络 / HTTP / 数据格式）

刻意不做：连接池、缓存、重试策略、订阅推送、多数据源调度 —— 这些属于后续需要时再加。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from database.validation import (
    optional_number,
    require_number,
    require_positive_int,
    require_text,
    require_timeframe,
    require_timestamp,
)


class DataSourceError(RuntimeError):
    """数据源错误：网络不可用、HTTP 状态异常、返回内容无法解析等。"""


@dataclass(frozen=True)
class KlineBar:
    """一根标准 K 线（与 klines 表字段一一对应，未包含 symbol_id）。"""

    symbol: str
    timeframe: str
    timestamp: str  # K 线开始时间，UTC，'YYYY-MM-DD HH:MM:SS'
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None = None

    def to_kline_row(self, symbol_id: int) -> dict[str, object]:
        """转换成 KlineRepository.bulk_upsert_klines() 需要的行。

        这里做一次格式校验，让"外部数据不合法"在写库之前就报出清晰错误；
        真正的业务约束（OHLC 逻辑、唯一性、外键）仍由数据库负责。
        """
        return {
            "symbol_id": require_positive_int(symbol_id, "symbol_id"),
            "timeframe": require_timeframe(self.timeframe),
            "timestamp": require_timestamp(self.timestamp),
            "open": require_number(self.open, "open"),
            "high": require_number(self.high, "high"),
            "low": require_number(self.low, "low"),
            "close": require_number(self.close, "close"),
            "volume": require_number(self.volume, "volume"),
            "amount": optional_number(self.amount, "amount"),
        }


class KlineDataSource(ABC):
    """最小 K 线数据源接口：以后换数据提供方只需实现这个方法。"""

    @abstractmethod
    def fetch_klines(
        self,
        *,
        symbol: str,
        timeframe: str,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> list[KlineBar]:
        """获取历史 K 线。

        参数：
            symbol           标的代码（外部数据源使用的原始代码）
            timeframe        K 线周期
            start_timestamp  起始时间（含），UTC 'YYYY-MM-DD HH:MM:SS'，None 表示不限
            end_timestamp    结束时间（含），UTC 'YYYY-MM-DD HH:MM:SS'，None 表示不限

        返回：
            KlineBar 列表（按数据源返回顺序）。取不到数据时返回空列表。
        """
        raise NotImplementedError


def require_symbol_code(value: object) -> str:
    """校验外部数据源使用的标的代码。"""
    return require_text(value, "symbol")


_TIMEFRAME_RE = re.compile(r"^(\d+)([mhdw])$")
_TIMEFRAME_UNIT_SECONDS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400, "w": 604800}

#: 一天的秒数：用于判断是否为日内周期
SECONDS_PER_DAY: int = 86400


def timeframe_to_seconds(timeframe: str) -> int:
    """把周期字符串换算成秒数（例如 "15m" → 900、"1h" → 3600、"1d" → 86400）。

    只支持 m / h / d / w 这几种单位；无法识别时抛出 ValueError
    （因为需要周期长度才能判断行情数据的时间语义）。
    """
    text = require_timeframe(timeframe)
    match = _TIMEFRAME_RE.match(text)
    if match is None:
        raise ValueError(
            f"无法从 timeframe {timeframe!r} 推断周期长度（支持如 15m / 1h / 4h / 1d / 1w）"
        )
    amount, unit = match.groups()
    return int(amount) * _TIMEFRAME_UNIT_SECONDS[unit]


def is_intraday_timeframe(timeframe: str) -> bool:
    """是否为日内周期（短于一天）。"""
    return timeframe_to_seconds(timeframe) < SECONDS_PER_DAY
