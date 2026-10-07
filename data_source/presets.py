"""具体数据源的预设配置（Stage 12）。

当前接入的数据源：**新浪财经**（A 股 / ETF / 指数等历史 K 线）

为什么选它：
    - 公开 REST 接口，**免费、无需认证**（不需要 API Key，也就不存在密钥泄漏风险）
    - 支持历史 K 线（日内 5/15/30/60 分钟与日线）
    - 返回 JSON，字段具名，容易映射到本项目 klines 表

该数据源的特点（已在本项目的配置里如实处理）：
    - 字段：day / open / high / low / close / volume（**数字以字符串返回**，已容错解析）
    - 无成交额字段 → amount 为 NULL（klines.amount 本就允许为空）
    - 周期用 scale 表示（5/15/30/60 分钟，240 = 日线）→ 用 timeframe_mapping 映射
    - 时间标签是 K 线**结束**时间（日内周期）→ timestamp_semantics="end"，解析时回退一个周期
    - **不支持起止时间参数**，只能取最近 N 根（datalen）→ 时间范围由 import_klines 在本地裁切
    - 返回顺序是从旧到新
"""

from __future__ import annotations

import os
from typing import Final, Mapping

from config import ENV_BASE_URL, ENV_TIMEOUT, ConfigError, DataSourceConfig

#: 新浪财经 K 线接口地址（公开接口，无密钥；可用环境变量 STRATEGY_ENGINE_DATA_SOURCE_URL 覆盖）
SINA_KLINE_URL: Final[str] = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/CN_MarketData.getKLineData"
)

#: 本项目 timeframe → 新浪 scale
SINA_TIMEFRAME_MAPPING: Final[dict[str, str]] = {
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
    "1h": "60",
    "1d": "240",
}

#: 本项目字段 → 新浪响应字段（该接口不返回成交额，故不映射 amount）
SINA_FIELD_MAPPING: Final[dict[str, str]] = {
    "timestamp": "day",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
}

#: 单次请求最多返回的 K 线根数（接口限制）
SINA_MAX_DATALEN: Final[int] = 1023


def sina_cn_kline_config(
    *,
    env: Mapping[str, str] | None = None,
    datalen: int = 320,
    base_url: str | None = None,
    timeout: float | None = None,
) -> DataSourceConfig:
    """构造新浪财经 K 线数据源配置。

    地址优先级：显式 base_url > 环境变量 STRATEGY_ENGINE_DATA_SOURCE_URL > 公开地址常量。
    该接口无需 API Key，因此配置里不含任何密钥。

    参数：
        datalen   一次请求取最近多少根 K 线（1 ~ 1023，默认 320）
    """
    if isinstance(datalen, bool) or not isinstance(datalen, int) or not 1 <= datalen <= SINA_MAX_DATALEN:
        raise ConfigError(f"datalen 必须是 1~{SINA_MAX_DATALEN} 之间的整数，实际为 {datalen!r}")

    source = os.environ if env is None else env
    resolved_url = base_url or source.get(ENV_BASE_URL) or SINA_KLINE_URL
    if timeout is None:
        raw_timeout = source.get(ENV_TIMEOUT)
        try:
            timeout = float(raw_timeout) if raw_timeout else 10.0
        except ValueError as exc:
            raise ConfigError(f"{ENV_TIMEOUT} 必须是数字，实际为 {raw_timeout!r}") from exc

    return DataSourceConfig(
        base_url=resolved_url,
        api_key=None,
        timeout=timeout,
        extra_params={"ma": "no", "datalen": str(datalen)},
        symbol_param="symbol",
        timeframe_param="scale",
        start_param=None,  # 该接口不支持起止时间参数
        end_param=None,
        timeframe_mapping=dict(SINA_TIMEFRAME_MAPPING),
        request_timestamp_format="datetime",
        response_timestamp_format="datetime",
        timestamp_semantics="end",
        response_path=None,  # 响应本身就是 K 线数组
        field_mapping=dict(SINA_FIELD_MAPPING),
    )
