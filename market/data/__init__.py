"""A 股真实数据接入层（Stage 21 Step 1）。

链路（第三方字段不直接进入数据库层）：

    新浪财经接口
        ↓  Adapter      market/data/sina.py     —— 请求 + 字段映射 + 容错
    标准化数据
        ↓  Service      market/data/importer.py —— 幂等 upsert + dry-run 统计
    Repository（SymbolRepository / KlineRepository）
        ↓
    SQLite（symbols / klines，schema 不变）

入口：
    python -m market.data.cli --symbols 600519,000001,300750 [--write]

边界（本阶段刻意不做）：
    - 不做实时行情 / WebSocket / 资金流 / 新闻 / 基本面 / 选股算法 / 自动交易
    - 不修改 Executor、Stage 18 逻辑；不加新依赖（只用标准库 + 现有依赖）
    - 不改数据库 schema；数据源只是「生产者」，不是数据库结构的定义者
"""

from .base import SINA_SOURCE_ID, StockSymbol, SymbolListSource, SymbolPage
from .boards import (
    BOARD_CODES,
    BOARD_BY_CODE,
    HS_MAIN_UNIVERSE_CODE,
    HS_MAIN_UNIVERSE_LABEL,
    TRADING_BOARDS,
    TradingBoard,
    classify_board,
    plan_memberships,
    sync_trading_boards,
)
from .codes import AStockCode, normalize_a_share_code
from .concepts import (
    CONCEPT_GROUP_TYPE,
    CONCEPT_TAG,
    Concept,
    ConceptSyncReport,
    SinaConceptSource,
    sync_concepts,
)
from .industries import (
    INDUSTRY_GROUP_TYPE,
    INDUSTRY_TAG,
    Industry,
    IndustrySyncReport,
    SinaIndustrySource,
    sync_industries,
)
from .importer import (
    DailyKlineReport,
    KlineTarget,
    SymbolSyncReport,
    ensure_a_share_symbols,
    import_daily_klines,
    sync_a_share_symbols,
)
from .sina import (
    SINA_A_SHARE_LIST_URL,
    SINA_DEFAULT_DAILY_BARS,
    SINA_MAX_DAILY_BARS,
    SINA_MAX_PAGE_SIZE,
    SinaAStockListSource,
    make_a_share_list_source,
    sina_daily_kline_config,
)

__all__ = [
    "AStockCode",
    "BOARD_BY_CODE",
    "BOARD_CODES",
    "CONCEPT_GROUP_TYPE",
    "CONCEPT_TAG",
    "Concept",
    "ConceptSyncReport",
    "DailyKlineReport",
    "HS_MAIN_UNIVERSE_CODE",
    "HS_MAIN_UNIVERSE_LABEL",
    "INDUSTRY_GROUP_TYPE",
    "INDUSTRY_TAG",
    "Industry",
    "IndustrySyncReport",
    "KlineTarget",
    "SINA_A_SHARE_LIST_URL",
    "SINA_DEFAULT_DAILY_BARS",
    "SINA_MAX_DAILY_BARS",
    "SINA_MAX_PAGE_SIZE",
    "SINA_SOURCE_ID",
    "SinaAStockListSource",
    "SinaConceptSource",
    "SinaIndustrySource",
    "StockSymbol",
    "SymbolListSource",
    "SymbolPage",
    "SymbolSyncReport",
    "TRADING_BOARDS",
    "TradingBoard",
    "classify_board",
    "ensure_a_share_symbols",
    "import_daily_klines",
    "make_a_share_list_source",
    "normalize_a_share_code",
    "plan_memberships",
    "sina_daily_kline_config",
    "sync_a_share_symbols",
    "sync_concepts",
    "sync_industries",
    "sync_trading_boards",
]
