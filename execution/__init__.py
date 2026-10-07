"""执行模块（Stage 10）。

包含最小可用的**模拟执行器**：

    ExecutionEngine.execute_signal()   Signal → Order → Trade → Position

边界：
    - 只做模拟执行，只使用已有 Repository，执行器内不写 SQL
    - 不接实时行情 / 交易所 / 券商 API，不做实盘交易
    - 不自动判断市场：side 由 Signal.direction 决定，数量由调用方给出
    - 不计算盈亏、不处理资金账户 / 杠杆 / 手续费结算
    - 不自动创建信号、不做回测
"""

from .engine import ExecutionEngine, ExecutionResult, apply_trade_to_position

__all__ = ["ExecutionEngine", "ExecutionResult", "apply_trade_to_position"]
