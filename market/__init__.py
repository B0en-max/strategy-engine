"""市场 / 选股模块（Stage 20）。

    market/service.py   MarketService：市场分类（分组）、展示代码、最新行情快照

分层位置：

    （Step 2 的 API）→ MarketService → Repository（SymbolGroupRepository / SymbolRepository / KlineRepository）→ SQLite

边界：
    - 策略 / 执行器不在这里，也不反向依赖本模块
    - 本模块只读行情、读写「集合与成员」；不实现选股算法、不实现动态 Universe
"""

from .service import (
    DEFAULT_QUOTE_TIMEFRAME,
    MarketService,
    display_code,
)

__all__ = [
    "DEFAULT_QUOTE_TIMEFRAME",
    "MarketService",
    "display_code",
]
