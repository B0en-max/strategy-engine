"""行情数据源配置（Stage 11）。

设计目标：让「HTTP/JSON 数据源」的实现不绑定任何具体交易所——
地址、认证、查询参数名、响应路径、字段映射、时间格式全部来自配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final, Mapping

# 环境变量名（API Key 只从这里读，绝不硬编码）
ENV_BASE_URL: Final[str] = "STRATEGY_ENGINE_DATA_SOURCE_URL"
ENV_API_KEY: Final[str] = "STRATEGY_ENGINE_DATA_SOURCE_API_KEY"
ENV_TIMEOUT: Final[str] = "STRATEGY_ENGINE_DATA_SOURCE_TIMEOUT"

#: 数据库路径覆盖（可选）：API 层与其它入口都从这里读，不写死绝对路径
ENV_DB_PATH: Final[str] = "STRATEGY_ENGINE_DB_PATH"

#: 时间格式取值：秒级时间戳 / 毫秒级时间戳 / 'YYYY-MM-DD HH:MM:SS'（UTC）
TIMESTAMP_FORMATS: Final[tuple[str, ...]] = ("seconds", "milliseconds", "datetime")

#: 数据源时间标签的含义
#:   start —— 标签是 K 线**开始**时间（本项目内部约定）
#:   end   —— 标签是 K 线**结束**时间（部分数据源如此，如新浪财经的日内周期）；
#:            解析时会按周期长度回退到开始时间（仅日内周期回退，日线及以上的标签是日期）
TIMESTAMP_SEMANTICS: Final[tuple[str, ...]] = ("start", "end")

#: 标准字段 → 外部响应里的字段名（默认同名）
DEFAULT_FIELD_MAPPING: Final[dict[str, str]] = {
    "timestamp": "timestamp",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "amount": "amount",
}

#: 转换 K 线时必需的字段（amount 允许缺失）
REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


class ConfigError(ValueError):
    """配置缺失或非法。"""


@dataclass(frozen=True)
class DataSourceConfig:
    """HTTP/JSON 行情数据源配置。"""

    base_url: str
    api_key: str | None = None
    #: 认证信息以请求头传递，而不是拼进 URL（避免密钥出现在日志/错误信息里）
    api_key_header: str = "X-API-Key"
    timeout: float = 10.0
    #: 固定附加查询参数（例如 provider 要求的额外字段）
    extra_params: Mapping[str, str] = field(default_factory=dict)
    #: 查询参数名；start_param / end_param 为 None 表示该数据源不支持时间范围参数
    #: （此时调用方传入的时间范围由 import_klines 在本地裁切）
    symbol_param: str = "symbol"
    timeframe_param: str = "interval"
    start_param: str | None = "startTime"
    end_param: str | None = "endTime"
    #: 我方 timeframe → 数据源 timeframe 取值（例如新浪财经 "15m" → "15"，"1d" → "240"）
    #: None 表示直接透传
    timeframe_mapping: Mapping[str, str] | None = None
    #: 请求时间格式 / 响应时间格式
    request_timestamp_format: str = "seconds"
    response_timestamp_format: str = "seconds"
    #: 响应时间标签的含义（start / end）
    timestamp_semantics: str = "start"
    #: 响应 JSON 中 K 线数组的位置（点号路径，例如 "data.klines"）；None 表示响应本身就是数组
    response_path: str | None = None
    #: 标准字段 → 外部字段名
    field_mapping: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_FIELD_MAPPING))

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ConfigError("base_url 必须是非空字符串（例如 https://api.example.com/klines）")
        if self.timeout <= 0:
            raise ConfigError(f"timeout 必须为正数，实际为 {self.timeout!r}")
        for name, fmt in (
            ("request_timestamp_format", self.request_timestamp_format),
            ("response_timestamp_format", self.response_timestamp_format),
        ):
            if fmt not in TIMESTAMP_FORMATS:
                raise ConfigError(f"{name} 必须是 {list(TIMESTAMP_FORMATS)} 之一，实际为 {fmt!r}")
        if self.timestamp_semantics not in TIMESTAMP_SEMANTICS:
            raise ConfigError(
                f"timestamp_semantics 必须是 {list(TIMESTAMP_SEMANTICS)} 之一，实际为 {self.timestamp_semantics!r}"
            )
        if self.timeframe_mapping is not None:
            bad = [k for k, v in self.timeframe_mapping.items() if not isinstance(v, str) or not v]
            if bad:
                raise ConfigError(f"timeframe_mapping 的值必须是非空字符串，问题键: {bad}")
        missing = [name for name in REQUIRED_FIELDS if name not in self.field_mapping]
        if missing:
            raise ConfigError(f"field_mapping 缺少必需字段映射: {missing}")

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: object) -> "DataSourceConfig":
        """从环境变量构造配置；overrides 用于覆盖其它字段。

        API Key 只从环境变量读取，调用方也可以显式传入（例如测试）。
        """
        source = os.environ if env is None else env
        base_url = source.get(ENV_BASE_URL, "")
        if not base_url:
            raise ConfigError(
                f"未配置数据源地址，请设置环境变量 {ENV_BASE_URL}（不要写进代码）"
            )
        params: dict[str, object] = {"base_url": base_url}
        api_key = source.get(ENV_API_KEY)
        if api_key:
            params["api_key"] = api_key
        timeout = source.get(ENV_TIMEOUT)
        if timeout:
            try:
                params["timeout"] = float(timeout)
            except ValueError as exc:
                raise ConfigError(f"{ENV_TIMEOUT} 必须是数字，实际为 {timeout!r}") from exc
        params.update(overrides)
        return cls(**params)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ 工具
    def headers(self) -> dict[str, str]:
        """构造请求头；只有配置了 api_key 才附带认证头。"""
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers[self.api_key_header] = self.api_key
        return headers

    def map_timeframe(self, timeframe: str) -> str:
        """把我方 timeframe 转换成数据源要求的取值。"""
        if self.timeframe_mapping is None:
            return timeframe
        if timeframe not in self.timeframe_mapping:
            raise ConfigError(
                f"timeframe {timeframe!r} 未在该数据源的映射表中，"
                f"已支持: {sorted(self.timeframe_mapping)}"
            )
        return self.timeframe_mapping[timeframe]
