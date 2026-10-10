"""A 股数据导入层的标准结构（Stage 21 Step 1）。

链路约定（**第三方字段不得直接污染数据库层**）：

    数据源（新浪 HTML/JSON）
        ↓  Adapter（market/data/sina.py）——字段映射 + 容错
    标准化数据（本模块：StockSymbol / SymbolPage）
        ↓  ImportService（market/data/importer.py）——编排、统计、dry-run
    Repository（SymbolRepository / KlineRepository）
        ↓
    SQLite

边界：
    - 本模块只描述「标准形态」，不请求网络、不写 SQL
    - K 线沿用 data_source.base.KlineBar（Stage 11 已定义的标准结构，不重复造）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from market.data.codes import A_SHARE_ASSET_TYPE, A_SHARE_MARKET

#: 数据源标识（写进报告，便于排查数据来自哪里）
SINA_SOURCE_ID: Final[str] = "sina"


@dataclass(frozen=True)
class StockSymbol:
    """标准化后的股票基础数据（与 symbols 表字段一一对应）。"""

    #: 裸代码：'600519'
    symbol: str
    name: str
    market: str = A_SHARE_MARKET
    asset_type: str = A_SHARE_ASSET_TYPE
    #: 交易所：'SH' / 'SZ' / 'BJ'（仅用于取数与展示，不入库）
    exchange: str | None = None
    #: 数据来源标识
    source: str = SINA_SOURCE_ID
    is_active: int = 1

    def __post_init__(self) -> None:
        if not str(self.symbol or "").strip():
            raise ValueError("symbol 不能为空")
        if not str(self.name or "").strip():
            raise ValueError(f"name 不能为空（symbol={self.symbol!r}）")
        if self.is_active not in (0, 1):
            raise ValueError(f"is_active 只能是 0 / 1，实际为 {self.is_active!r}")


@dataclass(frozen=True)
class SymbolPage:
    """一页股票列表（分页是数据源的形态，这里如实表达）。"""

    page: int
    page_size: int
    items: tuple[StockSymbol, ...]
    #: 数据源给出的总条数（拿不到时为 None，不做猜测）
    total: int | None = None
    #: 被跳过的行（代码非法 / 非对象行等），如实上报而不是静默丢弃
    skipped: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.items


class SymbolListSource(ABC):
    """股票列表数据源接口：换数据源只需再实现这一个方法。"""

    #: 数据源标识（报告里展示）
    name: str = "source"

    @abstractmethod
    def fetch_page(self, *, page: int, page_size: int) -> SymbolPage:
        """取第 page 页（从 1 开始）股票列表。"""

    def count(self) -> int | None:
        """数据源给出的股票总数；不支持时返回 None（不猜测）。"""
        return None

    def lookup_name(self, code: str) -> str | None:
        """按代码查股票名称；不支持时返回 None。"""
        return None
