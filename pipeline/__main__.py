"""最小运行闭环的命令行入口（Stage 028）。

示例：

    # 用正式库的真实行情跑一次闭环（正式库只读，结果落在临时运行库）
    python -m pipeline --symbol 600519 --timeframe 1d --start 2026-01-01 --end 2026-09-30 \
        --strategy close_direction --quantity 100 --json run.json

失败时退出码 2 并在 stderr 打印明确原因（不会输出"看似成功的空结果"）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .costs import ExecutionCosts
from .loop import FILL_MODELS, FILL_NEXT_OPEN, RunError, RunSpec, run_pipeline


def _parse_params(raw: list[str]) -> dict[str, object]:
    """--param key=value（value 先尝试 JSON 解析，再退化为字符串）。"""
    params: dict[str, object] = {}
    for item in raw:
        if "=" not in item:
            raise SystemExit(f"--param 需要 key=value 形式，实际为 {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        try:
            params[key] = json.loads(value)
        except json.JSONDecodeError:
            params[key] = value.strip()
    return params


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline", description="量化系统最小运行闭环")
    parser.add_argument("--symbol", required=True, help="标的代码，如 600519")
    parser.add_argument("--timeframe", default="1d", help="K 线周期（默认 1d）")
    parser.add_argument("--start", dest="start_timestamp", default=None, help="起始时间（含）")
    parser.add_argument("--end", dest="end_timestamp", default=None, help="结束时间（含）")
    parser.add_argument("--strategy", default="close_direction", help="策略类型（config.type）")
    parser.add_argument("--param", action="append", default=[], metavar="K=V", help="策略参数，可重复")
    parser.add_argument("--strategy-name", default="adhoc", help="策略身份名（写入运行库）")
    parser.add_argument("--strategy-version", type=int, default=1, help="策略版本号")
    parser.add_argument("--quantity", type=float, default=100.0, help="每次成交数量（默认 100）")
    parser.add_argument("--fill-model", choices=list(FILL_MODELS), default=FILL_NEXT_OPEN,
                        help="成交模型（默认 t 决策、t+1 开盘成交）")
    parser.add_argument("--commission-bps", type=float, default=2.5, help="佣金（bps，双边）")
    parser.add_argument("--min-commission", type=float, default=5.0, help="单笔最低佣金（元）")
    parser.add_argument("--stamp-tax-bps", type=float, default=5.0, help="印花税（bps，仅卖出）")
    parser.add_argument("--slippage-bps", type=float, default=1.0, help="滑点（bps，双边）")
    parser.add_argument("--source-db", default=None, help="行情来源库（默认正式库，只读）")
    parser.add_argument("--run-db", default=None, help="运行库路径（默认临时库，跑完清理）")
    parser.add_argument("--json", dest="json_path", default=None, help="把完整报告写入该 JSON 文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    spec = RunSpec(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_timestamp=args.start_timestamp,
        end_timestamp=args.end_timestamp,
        strategy_type=args.strategy,
        strategy_config=_parse_params(args.param),
        strategy_name=args.strategy_name,
        strategy_version=args.strategy_version,
        quantity=args.quantity,
        costs=ExecutionCosts(
            commission_bps=args.commission_bps,
            min_commission=args.min_commission,
            stamp_tax_bps=args.stamp_tax_bps,
            slippage_bps=args.slippage_bps,
        ),
        fill_model=args.fill_model,
    )
    try:
        report = run_pipeline(spec, source_db_path=args.source_db, run_db_path=args.run_db)
    except RunError as exc:
        print(f"[run failed] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[invalid input] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    payload = report.to_dict()
    print(json.dumps(
        {
            "run_id": payload["run_id"],
            "symbol": payload["data"]["code"],
            "timeframe": payload["spec"]["timeframe"],
            "range": [payload["data"]["first_timestamp"], payload["data"]["last_timestamp"]],
            "bars_processed": payload["bars_processed"],
            "signals_generated": payload["signals_generated"],
            "trades_executed": payload["trades_executed"],
            "unfilled_signals": payload["unfilled_signals"],
            "metrics": payload["metrics"],
            "cost_totals": payload["cost_totals"],
            "net_pnl": payload["net_pnl"],
            "warnings": payload["data"]["warnings"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[report] {args.json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
