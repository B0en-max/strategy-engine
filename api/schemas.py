"""API 请求模型（Stage 16）。

只用 pydantic 校验**入参**；响应直接返回普通 dict（由 Repository 的行转成），
不把 Repository / 数据库连接等对象暴露出去。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RunStrategyRequest(BaseModel):
    """运行策略的请求体（对应 POST /strategies/{strategy_id}/run）。"""

    symbol_id: int = Field(..., gt=0, description="标的 id（symbols.id）")
    timeframe: str = Field(..., min_length=1, description="K 线周期，如 15m / 1d")
    start_timestamp: str | None = Field(
        None, description="起始时间（含），UTC 'YYYY-MM-DD HH:MM:SS'；不传表示从头"
    )
    end_timestamp: str | None = Field(None, description="结束时间（含），格式同上")


class ExecuteSignalRequest(BaseModel):
    """模拟执行信号的请求体（对应 POST /execution/signals/{signal_id}）。"""

    quantity: float = Field(..., gt=0, description="模拟成交数量（必须 > 0）")
    price: float | None = Field(
        None, description="指定成交价；不传则使用 signal 自带价格"
    )


class ExecuteStrategyRequest(BaseModel):
    """按策略版本执行一段 K 线的请求体（Stage 19，对应 POST /executor/run）。

    执行的是**指定的策略版本**：strategy_id 是策略身份，
    strategy_version_id 是策略版本（决定用哪套参数执行）。
    """

    strategy_id: int = Field(..., gt=0, description="Strategy 身份 id（strategy_definitions.id）")
    strategy_version_id: int = Field(..., gt=0, description="策略版本 id（strategies.id）")
    symbol_id: int = Field(..., gt=0, description="标的 id（symbols.id）")
    timeframe: str = Field(..., min_length=1, description="K 线周期，如 15m / 1d")
    start_timestamp: str | None = Field(
        None, description="起始时间（含），UTC 'YYYY-MM-DD HH:MM:SS'；不传表示从头"
    )
    end_timestamp: str | None = Field(None, description="结束时间（含），格式同上")
