"""数据库 schema 定义（Stage 02）。

本阶段**没有任何正式业务表**：
    symbols / klines / strategies / strategy_versions / signals / orders /
    trades / positions / backtests / performance / reviews /
    optimization_candidates  —— 全部尚未创建，属于后续阶段。

这里只提供数据库初始化所需的最小结构：
    - schema 版本号（存放在 SQLite 原生 PRAGMA user_version 中）
    - 初始化语句列表

使用 PRAGMA user_version 而不是自建的元数据表，是为了让初始化后的数据库
保持“零业务表”的干净状态，同时仍然具备后续做版本迁移的基础。
"""

from __future__ import annotations

from typing import Final

# 当前 schema 版本。后续阶段新增表结构时应递增，并补上迁移逻辑。
SCHEMA_VERSION: Final[int] = 0

# 本阶段明确不创建任何业务表
BUSINESS_TABLES: Final[tuple[str, ...]] = ()


def initialization_statements() -> list[tuple[str, tuple[object, ...]]]:
    """返回数据库初始化需要执行的 (SQL, 参数) 列表。

    全部语句都必须是幂等的：
    重复执行 initialize_database() 不能报错。
    """
    return [
        (f"PRAGMA user_version = {SCHEMA_VERSION:d}", ()),
    ]
