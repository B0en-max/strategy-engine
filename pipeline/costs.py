"""执行成本模型（Stage 028）：佣金 / 印花税 / 滑点。

定位
------------------------------------------------------------------
- 只做**成本计算**（纯函数）：滑点作用于成交价，佣金与印花税计入成交的 fee。
- 不接实时行情、不接券商、不做资金账户 / 杠杆 / 保证金。

⚠️ 诚实边界（**不得伪称已模拟**）
- 下面的默认费率是**假设值**，不是本项目已核实的现行官方费率；
  报告里会带 `cost_source="assumed"`，提醒使用者按实际券商/税率覆盖。
- 本模块**不模拟**：T+1 卖出限制、涨跌停价约束、一字板不可成交、停牌不可成交、
  历史 ST 状态筛选 —— 这些在 pipeline 的诊断信息里逐条标注为「未模拟」。

口径
------------------------------------------------------------------
- 佣金：`price * quantity * commission_bps/10000`，但不低于 `min_commission`（双边收取）。
- 印花税：仅卖出，`price * quantity * stamp_tax_bps/10000`。
- 滑点：买入上浮、卖出下压 `slippage_bps/10000`（对成交者不利）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from database.validation import require_choice, require_number

#: 万分之几 → 比例
BPS: Final[float] = 1e-4

TRADE_SIDES: Final[tuple[str, ...]] = ("buy", "sell")


@dataclass(frozen=True)
class ExecutionCosts:
    """一次运行使用的成本参数（单位：bps = 万分之一）。"""

    #: 佣金（双边），默认 2.5bps（万分之 2.5）
    commission_bps: float = 2.5
    #: 单笔最低佣金（元）
    min_commission: float = 5.0
    #: 印花税（仅卖出），默认 5bps
    stamp_tax_bps: float = 5.0
    #: 滑点（双边，按不利方向），默认 1bp
    slippage_bps: float = 1.0

    def __post_init__(self) -> None:
        for name in ("commission_bps", "min_commission", "stamp_tax_bps", "slippage_bps"):
            value = require_number(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} 不能为负数，实际为 {value!r}")

    # ------------------------------------------------------------------ 计算
    def fill_price(self, side: str, base_price: float) -> float:
        """把基准价（如次日开盘价）按滑点折算成成交价：买入上浮、卖出下压。"""
        side = require_choice(side, "side", TRADE_SIDES)
        base = require_number(base_price, "base_price")
        if base <= 0:
            raise ValueError(f"基准价必须为正数，实际为 {base!r}")
        drift = base * self.slippage_bps * BPS
        filled = base + drift if side == "buy" else base - drift
        return round(filled, 4)

    def fee(self, side: str, price: float, quantity: float) -> float:
        """本次成交的手续费（佣金 + 卖出印花税），四舍五入到分。"""
        side = require_choice(side, "side", TRADE_SIDES)
        price = require_number(price, "price")
        quantity = require_number(quantity, "quantity")
        turnover = price * quantity
        commission = max(turnover * self.commission_bps * BPS, self.min_commission)
        stamp = turnover * self.stamp_tax_bps * BPS if side == "sell" else 0.0
        return round(commission + stamp, 2)

    # ------------------------------------------------------------------ 序列化
    def to_dict(self) -> dict[str, Any]:
        """用于报告 / 指纹的稳定表示。"""
        return {
            "commission_bps": self.commission_bps,
            "min_commission": self.min_commission,
            "stamp_tax_bps": self.stamp_tax_bps,
            "slippage_bps": self.slippage_bps,
        }


__all__ = ["BPS", "ExecutionCosts", "TRADE_SIDES"]
