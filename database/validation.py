"""字段级校验工具（Stage 06 抽出为共享模块）。

这些校验只判断**格式错误**（类型、空值、时间格式、数字类型），
不判断业务上限/下限，也不承担任何数据库约束的职责：
数据库层的职责仍是 NOT NULL / UNIQUE / CHECK / FOREIGN KEY。

约定：校验失败统一抛 ValueError（参数错误），
数据库约束冲突则保留 sqlite3 异常类型（不降级成基类）。

被复用的地方：
    database/kline_repository.py   —— timeframe / timestamp / 价格 / 成交量
    database/signal_repository.py —— timeframe / timestamp / direction / status / price
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable

from . import models

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# timeframe 长度上限：只做最小保护，不限制具体取值
_MAX_TIMEFRAME_LENGTH = 16


def require_text(value: object, field: str) -> str:
    """必填文本：必须是非空字符串（去掉首尾空白）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串，实际为 {value!r}")
    return value.strip()


def optional_text(value: object, field: str) -> str | None:
    """可选文本：None 或字符串。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串或 None，实际为 {value!r}")
    return value


def require_positive_int(value: object, field: str) -> int:
    """必填正整数（用于 id 类字段；布尔值不算整数）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数，实际为 {value!r}")
    if value <= 0:
        raise ValueError(f"{field} 必须是正整数，实际为 {value!r}")
    return value


def require_int_at_least(value: object, field: str, minimum: int) -> int:
    """必填整数且不小于 minimum。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数，实际为 {value!r}")
    if value < minimum:
        raise ValueError(f"{field} 必须 >= {minimum}，实际为 {value!r}")
    return value


def require_choice(value: object, field: str, choices: Iterable[str]) -> str:
    """取值必须来自给定集合；非法值直接报错，不静默转换。"""
    allowed = tuple(choices)
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{field} 必须是 {list(allowed)} 之一，实际为 {value!r}")
    return value


def require_timeframe(value: object) -> str:
    """K 线周期：只做最小校验（非空、无空白字符、长度合理）。

    故意**不**把周期限制成固定取值（1m/1h/1d/1w ...），因为未来会扩展
    2m/3m/10m/2h/6h/12h/1M 等。
    """
    text = require_text(value, "timeframe")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"timeframe 不应包含空白字符，实际为 {value!r}")
    if len(text) > _MAX_TIMEFRAME_LENGTH:
        raise ValueError(f"timeframe 长度异常，实际为 {value!r}")
    return text


def require_timestamp(value: object) -> str:
    """时间戳：必须是 'YYYY-MM-DD HH:MM:SS'（UTC）且为真实时间。

    注意：具体字段语义由各表定义（klines 表示 K 线开始时间，
    signals 表示信号发生时间），格式与 UTC 约定统一由这里保证。
    """
    text = require_text(value, "timestamp")
    if not _TIMESTAMP_RE.match(text):
        raise ValueError(
            f"timestamp 格式必须是 'YYYY-MM-DD HH:MM:SS'（UTC），实际为 {value!r}"
        )
    try:
        datetime.strptime(text, models.TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"timestamp 不是有效时间 {value!r}: {exc}") from exc
    return text


def require_number(value: object, field: str) -> float:
    """必填数字（int / float，布尔不算）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字，实际为 {value!r}")
    number = float(value)
    if number != number:  # NaN
        # SQLite 会把 NaN 静默存成 NULL，会让报错信息变得难以理解，这里提前拦住
        raise ValueError(f"{field} 不能是 NaN")
    return number


def optional_number(value: object, field: str) -> float | None:
    """可选数字：None 或数字。"""
    if value is None:
        return None
    return require_number(value, field)


def require_positive_int_limit(value: object, field: str = "limit") -> int:
    """分页参数：必须是正整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} 必须是正整数，实际为 {value!r}")
    return value
