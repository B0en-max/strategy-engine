"""执行器 API 层（Stage 16）。

用途：把 `StrategyRunner` / `ExecutionEngine` 包装成 HTTP 接口，供前端调用。

用法：
    from api import create_app
    app = create_app(db_path=None)      # 不传则读环境变量 / 项目默认路径

启动（本地开发）：
    .venv\\Scripts\\python.exe -m uvicorn api.app:app --reload --port 8000

边界：仅本地开发使用；没有登录 / 用户 / 权限 / HTTPS；**未连接真实交易所**。
本层不实现策略、订单、成交、持仓逻辑，一律复用已有组件。
"""

from .app import API_VERSION, create_app, resolve_api_database_path
from .schemas import ExecuteSignalRequest, RunStrategyRequest

__all__ = [
    "API_VERSION",
    "ExecuteSignalRequest",
    "RunStrategyRequest",
    "create_app",
    "resolve_api_database_path",
]
