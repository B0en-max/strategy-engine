"""K 线数据采集层（Stage 11）。

结构：
    base.py         KlineBar / KlineDataSource（最小接口）/ DataSourceError
    http_source.py  可配置的 HTTP / JSON 数据源（不绑定具体交易所）
    importer.py     采集编排：数据源 → KlineRepository → klines 表

边界：
    - 只做**历史 K 线采集**：不接策略、不接执行器、不做实盘、不做实时行情推送
    - 数据源实现与 Repository 分离：本模块不写 SQL，执行器 / 策略不感知数据源
    - API 地址与密钥来自 config（环境变量），不硬编码、不入 Git
"""

from .base import DataSourceError, KlineBar, KlineDataSource
from .http_source import HttpKlineDataSource
from .importer import ImportResult, import_klines

__all__ = [
    "DataSourceError",
    "HttpKlineDataSource",
    "ImportResult",
    "KlineBar",
    "KlineDataSource",
    "import_klines",
]
