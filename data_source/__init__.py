"""K 线数据采集层（Stage 11；Stage 032 / 032.1 新增 OKX 加密资产）。

结构：
    base.py         KlineBar / KlineDataSource（最小接口）/ DataSourceError
    http_source.py  可配置的 HTTP / JSON 数据源（不绑定具体交易所）
    presets.py      具体数据源的预设配置（当前：新浪财经）
    importer.py     采集编排：数据源 → KlineRepository → klines 表
    okx.py          OKX 公开现货行情数据源（分页 / 限流 / 有限重试 / 未完结过滤 / 周期派生）
    quality.py      K 线数据质量核对（排序 / 重复 / OHLC / 非法值 / 缺失区间明细）
    collector.py    采集编排（增量游标 / 全历史回补 / 5×9 组合矩阵 / 幂等写库 / 报告）
    cli.py          命令行入口（人工验收 / 日常补数据；自动测试不依赖网络）

边界：
    - 只做**历史 K 线采集**：不接策略、不接执行器、不做实盘、不做实时行情推送
    - 数据源实现与 Repository 分离：本模块不写 SQL，执行器 / 策略不感知数据源
    - API 地址与密钥来自 config（环境变量），不硬编码、不入 Git
"""

from .base import DataSourceError, KlineBar, KlineDataSource, timeframe_to_seconds
from .collector import (
    BackfillReport,
    CollectionReport,
    ComboResult,
    MatrixReport,
    backfill_matrix,
    backfill_okx_klines,
    collect_okx_klines,
)
from .http_source import HttpKlineDataSource
from .importer import ImportResult, import_klines, prepare_kline_rows
from .okx import (
    DERIVED_TIMEFRAME_BASE,
    OKX_BAR_BY_TIMEFRAME,
    OKX_BASE_URL,
    OKX_INSTRUMENTS,
    REQUIRED_TIMEFRAMES,
    OkxCandlePage,
    OkxFetchResult,
    OkxInstrument,
    OkxKlineSource,
    TimeframePlan,
    aggregate_bars,
    get_instrument,
    milliseconds_to_timestamp,
    timeframe_plan,
    timestamp_to_milliseconds,
)
from .presets import SINA_KLINE_URL, SINA_TIMEFRAME_MAPPING, sina_cn_kline_config
from .quality import KlineQualityReport, analyze_klines, deduplicate_rows

__all__ = [
    "BackfillReport",
    "CollectionReport",
    "ComboResult",
    "DERIVED_TIMEFRAME_BASE",
    "DataSourceError",
    "HttpKlineDataSource",
    "ImportResult",
    "KlineBar",
    "KlineDataSource",
    "KlineQualityReport",
    "MatrixReport",
    "OKX_BAR_BY_TIMEFRAME",
    "OKX_BASE_URL",
    "OKX_INSTRUMENTS",
    "OkxCandlePage",
    "OkxFetchResult",
    "OkxInstrument",
    "OkxKlineSource",
    "REQUIRED_TIMEFRAMES",
    "SINA_KLINE_URL",
    "SINA_TIMEFRAME_MAPPING",
    "TimeframePlan",
    "aggregate_bars",
    "analyze_klines",
    "backfill_matrix",
    "backfill_okx_klines",
    "collect_okx_klines",
    "deduplicate_rows",
    "get_instrument",
    "import_klines",
    "milliseconds_to_timestamp",
    "prepare_kline_rows",
    "sina_cn_kline_config",
    "timeframe_plan",
    "timeframe_to_seconds",
]
