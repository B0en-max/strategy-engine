"""绩效补充指标（Stage 029）：净值曲线 / 最大回撤 / 收益率（口径显式）。

定位
------------------------------------------------------------------
- 只做**纯计算**：从 pipeline 的逐笔成交明细（含 `pnl` / `fee` / `fill_price` / `base_price`）
  推出净值曲线、最大回撤与收益率。
- **不重复** `backtest.metrics` 的胜率 / profit factor 等（那些直接复用）。

收益率口径（**先明确分母，不用不一致的分母制造收益率**）
------------------------------------------------------------------
- `net_pnl`（净盈亏）与 `max_drawdown`（最大回撤，货币单位）**不需要初始资金**即可定义：
  净值曲线的起点为 0，峰值回撤 = max(峰值 − 当前净值)。
- `return_pct`（收益率）**只有**在调用方显式提供正数 `initial_capital` 时才计算：
  `return_pct = 最终净值 / initial_capital`，回撤百分比以 `initial_capital` 为分母。
  未提供初始资金时 `return_pct = None` 且 `return_basis` 明确标注 unknown，**不猜**。
- 手续费：每笔成交的 `fee` 作为成本计入净值；滑点已体现在 `fill_price` 中，另单独汇总。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

BPS: float = 1e-4

RETURN_BASIS_UNKNOWN: str = "unknown（未提供初始资金 → 不计算收益率，避免用不一致的分母）"
RETURN_BASIS_CAPITAL: str = "net_pnl / initial_capital（初始资金由调用方显式提供）"


@dataclass(frozen=True)
class PerformanceSummary:
    """一次运行的补充绩效（净值 / 回撤 / 收益率）。"""

    net_pnl: float
    gross_realized_pnl: float
    fee_total: float
    slippage_total: float
    #: 净值曲线（起点 0）上的最大回撤（货币单位，>= 0）
    max_drawdown: float
    #: 以 initial_capital 为分母的最大回撤比例；未提供初始资金时为 None
    max_drawdown_pct: float | None
    #: 净值曲线终点（= net_pnl）
    final_net_equity: float
    initial_capital: float | None
    #: 收益率；未提供初始资金时为 None
    return_pct: float | None
    #: 收益率口径说明
    return_basis: str
    #: 净值曲线点数（= 成交笔数 + 1，含起点）
    equity_points: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_pnl": self.net_pnl,
            "gross_realized_pnl": self.gross_realized_pnl,
            "fee_total": self.fee_total,
            "slippage_total": self.slippage_total,
            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "final_net_equity": self.final_net_equity,
            "initial_capital": self.initial_capital,
            "return_pct": self.return_pct,
            "return_basis": self.return_basis,
            "equity_points": self.equity_points,
        }


def summarize_performance(
    trades: Sequence[Mapping[str, Any]],
    *,
    fee_total: float | None = None,
    net_pnl: float | None = None,
    initial_capital: float | None = None,
) -> PerformanceSummary:
    """从逐笔成交明细计算净值曲线 / 最大回撤 / 收益率。

    参数：
        trades          逐笔成交明细；每笔需含 `pnl`（已实现盈亏）、`fee`、
                        `fill_price`、`base_price`、`quantity`
        fee_total       手续费合计；不传则按 trades 的 fee 求和
        net_pnl         净盈亏；不传则按 已实现盈亏 − 手续费 计算
        initial_capital 初始资金；提供时计算 return_pct 与回撤百分比，否则为 None

    失败时：
        initial_capital 提供但不为正数 → ValueError（拒绝用非法分母）。
    """
    if initial_capital is not None:
        if isinstance(initial_capital, bool) or not isinstance(initial_capital, (int, float)):
            raise ValueError(f"initial_capital 必须是数字或 None，实际为 {initial_capital!r}")
        if float(initial_capital) <= 0:
            raise ValueError(f"initial_capital 必须为正数，实际为 {initial_capital!r}")

    gross = round(sum(float(t["pnl"]) for t in trades), 6)
    fees = (
        round(sum(float(t["fee"]) for t in trades), 2)
        if fee_total is None
        else round(float(fee_total), 2)
    )
    slippage = round(
        sum(
            abs(float(t["fill_price"]) - float(t["base_price"])) * float(t["quantity"])
            for t in trades
        ),
        2,
    )
    net = round(gross - fees, 6) if net_pnl is None else round(float(net_pnl), 6)

    # 净值曲线：起点 0，逐笔累加（已实现盈亏 − 手续费）；同时跟踪峰值与回撤
    equity = 0.0
    peak_net = 0.0
    max_drawdown = 0.0
    max_drawdown_pct: float | None = None if initial_capital is None else 0.0
    capital = None if initial_capital is None else float(initial_capital)

    for trade in trades:
        equity += float(trade["pnl"]) - float(trade["fee"])
        peak_net = max(peak_net, equity)
        drawdown = peak_net - equity
        if drawdown > max_drawdown:
            max_drawdown = drawdown
        if capital is not None:
            peak_equity = capital + peak_net
            pct = drawdown / peak_equity if peak_equity > 0 else 0.0
            if max_drawdown_pct is None or pct > max_drawdown_pct:
                max_drawdown_pct = pct

    return PerformanceSummary(
        net_pnl=net,
        gross_realized_pnl=gross,
        fee_total=fees,
        slippage_total=slippage,
        max_drawdown=round(max_drawdown, 6),
        max_drawdown_pct=None if max_drawdown_pct is None else round(max_drawdown_pct, 10),
        final_net_equity=round(equity, 6),
        initial_capital=capital,
        return_pct=None if capital is None else round(net / capital, 10),
        return_basis=RETURN_BASIS_UNKNOWN if capital is None else RETURN_BASIS_CAPITAL,
        equity_points=len(trades) + 1,
    )


__all__ = [
    "RETURN_BASIS_CAPITAL",
    "RETURN_BASIS_UNKNOWN",
    "PerformanceSummary",
    "summarize_performance",
]
