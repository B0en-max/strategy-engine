"""A 股代码标准化（Stage 21 Step 1）。

职责：把数据源给的股票代码统一成**本项目数据库约定的形态**：

    symbols.symbol = '600519'      # 裸代码，不带交易所后缀
    symbols.market = 'CN'

交易所（SH / SZ / BJ）只用于两件事：

    1. 向数据源请求时的代码（新浪：sh600519 / sz000001 / bj920000）
    2. 展示代码（由 market.service.display_code 统一生成，前端不得自行推导）

判断顺序（**优先使用数据源提供的字段**，不靠猜）：

    1. 数据源代码前缀        'sh600519' / 'sz000001' / 'bj920000'
    2. 带交易所后缀的形式     '600519.SH'
    3. 都没有时，按代码段规则回退（复用 market.service.display_code），
       保证与「展示用后缀」永远一致，不出现两套互相矛盾的规则

边界：
    - 纯函数：不写库、不请求网络
    - 不处理非 A 股（港股 / 美股 / 加密由各自市场负责，本模块不猜测）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from market.service import display_code

#: A 股市场标识（与 symbols.market 约定一致）
A_SHARE_MARKET: Final[str] = "CN"
#: 标的类型（A 股股票）
A_SHARE_ASSET_TYPE: Final[str] = "STOCK"

#: 允许的交易所后缀
EXCHANGES: Final[tuple[str, ...]] = ("SH", "SZ", "BJ")

#: 数据源代码前缀 → 交易所
PREFIX_TO_EXCHANGE: Final[dict[str, str]] = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
#: 交易所后缀 → 交易所（规范大小写）
SUFFIX_TO_EXCHANGE: Final[dict[str, str]] = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}

_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^\d{6}$")
_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)
_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{6})\.(sh|sz|bj)$", re.IGNORECASE)


@dataclass(frozen=True)
class AStockCode:
    """标准化后的 A 股代码。"""

    #: 裸代码（入库用），例如 '600519'
    symbol: str
    #: 市场（恒为 'CN'）
    market: str
    #: 交易所（'SH' / 'SZ' / 'BJ'）
    exchange: str
    #: 交易所来源：'source'（数据源给的）或 'rule'（按代码段推导）
    exchange_source: str = "rule"

    @property
    def display_code(self) -> str:
        """展示代码（与后端 API 完全一致：复用 market.service.display_code）。"""
        return display_code(self.market, self.symbol)

    @property
    def source_symbol(self) -> str:
        """向数据源请求时使用的代码（新浪：sh600519）。"""
        return f"{self.exchange.lower()}{self.symbol}"


def exchange_from_rules(code: str) -> str | None:
    """按代码段规则推导交易所（复用展示代码规则）；无法判断时返回 None。"""
    text = str(code or "").strip()
    if not _CODE_RE.match(text):
        return None
    shown = display_code(A_SHARE_MARKET, text)
    if "." not in shown:
        return None
    suffix = shown.rsplit(".", 1)[1].upper()
    return SUFFIX_TO_EXCHANGE.get(suffix)


def is_a_share_code(value: object) -> bool:
    """判断是否是（或可还原为）A 股代码。"""
    try:
        normalize_a_share_code(value)
    except (TypeError, ValueError):
        return False
    return True


def strip_exchange(value: object) -> str:
    """去掉数据源代码里的交易所前缀 / 后缀，只保留裸代码（不校验合法性）。"""
    text = str(value or "").strip()
    prefix_match = _PREFIX_RE.match(text)
    if prefix_match is not None:
        return prefix_match.group(2)
    suffix_match = _SUFFIX_RE.match(text)
    if suffix_match is not None:
        return suffix_match.group(1)
    return text


def normalize_a_share_code(
    raw: object,
    *,
    market: str = A_SHARE_MARKET,
) -> AStockCode:
    """把数据源代码标准化为 AStockCode。

    支持形式：'sh600519'、'600519.SH'、'SZ000001'、'600519'。
    交易所优先取数据源给的前缀 / 后缀；没有时按代码段规则推导。

    非法输入（空值、非 6 位数字、无法判断交易所）抛出 ValueError，
    错误信息里带上原始值，便于导入时逐条报告。
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("代码为空")

    exchange: str | None = None
    exchange_source = "rule"

    prefix_match = _PREFIX_RE.match(text)
    if prefix_match is not None:
        exchange = PREFIX_TO_EXCHANGE[prefix_match.group(1).lower()]
        exchange_source = "source"
        text = prefix_match.group(2)
    else:
        suffix_match = _SUFFIX_RE.match(text)
        if suffix_match is not None:
            exchange = SUFFIX_TO_EXCHANGE[suffix_match.group(2).upper()]
            exchange_source = "source"
            text = suffix_match.group(1)

    if not _CODE_RE.match(text):
        raise ValueError(f"不是合法的 A 股代码（需要 6 位数字）: {raw!r}")

    resolved_market = str(market or A_SHARE_MARKET).strip().upper() or A_SHARE_MARKET
    if exchange is None:
        exchange = exchange_from_rules(text)
        if exchange is None:
            raise ValueError(f"无法判断交易所（数据源未提供，代码段规则也匹配不上）: {raw!r}")
    return AStockCode(symbol=text, market=resolved_market, exchange=exchange, exchange_source=exchange_source)
