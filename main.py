"""Strategy Engine 最小可运行入口。

Stage 01 只负责让项目"能被启动"，因此这里故意不包含任何
数据库、数据采集、策略、回测、风控、执行等业务逻辑。
后续阶段会在这里把各模块装配成完整流程。

用法：
    python main.py
"""

from __future__ import annotations

import sys

PROJECT_NAME = "Strategy Engine"
CURRENT_STAGE = "Stage 01 - Project Skeleton"


def main() -> int:
    """打印项目启动信息并返回进程退出码。"""
    print(PROJECT_NAME)
    print(CURRENT_STAGE)
    print("Status: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
