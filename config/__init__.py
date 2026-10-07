"""集中配置管理（Stage 11）。

当前包含：外部行情数据源的配置（API 地址、认证信息、字段映射、时间格式）。

安全约定：
    - **API Key 一律来自环境变量**（或调用方显式传入），不写进代码、不入 Git
    - 配置对象只保存密钥值，不做任何日志输出；错误信息里也不会带上密钥

环境变量：
    STRATEGY_ENGINE_DATA_SOURCE_URL        数据源 API 地址（必填）
    STRATEGY_ENGINE_DATA_SOURCE_API_KEY    认证密钥（可选）
    STRATEGY_ENGINE_DATA_SOURCE_TIMEOUT    超时秒数（可选）
    STRATEGY_ENGINE_DB_PATH                数据库路径覆盖（可选；API 层等入口统一从这里读）
"""

from .settings import (
    DEFAULT_FIELD_MAPPING,
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_CORS_ORIGINS,
    ENV_DB_PATH,
    ENV_TIMEOUT,
    TIMESTAMP_FORMATS,
    DataSourceConfig,
    ConfigError,
)

__all__ = [
    "DEFAULT_FIELD_MAPPING",
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_CORS_ORIGINS",
    "ENV_DB_PATH",
    "ENV_TIMEOUT",
    "TIMESTAMP_FORMATS",
    "ConfigError",
    "DataSourceConfig",
]
