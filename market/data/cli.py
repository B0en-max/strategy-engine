"""A 股数据导入命令行入口（Stage 21 Step 1）。

**默认 dry-run**：不加 --write 时只取数、校验、统计，绝不写数据库；
这样"随手跑一下看看"不会意外改动正式库。

用法（项目根目录执行）：

    # 1) 预演：3 只股票 + 最近 320 根日 K（不写库；**代码列表加引号**）
    .venv\\Scripts\\python.exe -m market.data.cli --symbols "600519,000001,300750"

    # 2) 正式写入（默认库）
    .venv\\Scripts\\python.exe -m market.data.cli --symbols "600519,000001,300750" --write

    # 3) 指定时间范围 / 更长历史 / 指定库
    .venv\\Scripts\\python.exe -m market.data.cli --symbols 600519 \\
        --start 2020-01-01 --end 2026-10-07 --datalen 2000 --write --db data\\x.db

    # 4) 全量 A 股股票列表（约 5600 只、56 页；先预演再写）
    .venv\\Scripts\\python.exe -m market.data.cli --all --no-klines
    .venv\\Scripts\\python.exe -m market.data.cli --all --no-klines --write
    .venv\\Scripts\\python.exe -m market.data.cli --all --limit 50 --write   # 先小批量试

    # 5) 代码也可以空格分隔（PowerShell 下不会踩数组解析的坑）
    .venv\\Scripts\\python.exe -m market.data.cli --symbols 600519 000001 300750 --write

数据库路径优先级：--db > 环境变量 STRATEGY_ENGINE_DB_PATH > 项目默认库。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from data_source import HttpKlineDataSource
from database import (
    KlineRepository,
    SymbolRepository,
    database_info,
    initialize_database,
    resolve_database_path,
)
from market.data.codes import A_SHARE_MARKET, normalize_a_share_code
from market.data.importer import (
    DailyKlineReport,
    KlineTarget,
    SymbolSyncReport,
    ensure_a_share_symbols,
    import_daily_klines,
    sync_a_share_symbols,
)
from market.data.sina import (
    SINA_DEFAULT_DAILY_BARS,
    SINA_MAX_DAILY_BARS,
    make_a_share_list_source,
    sina_daily_kline_config,
)

#: 报告里最多逐条打印多少条错误（其余只计数）
MAX_REPORTED_ERRORS = 20

_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_range(value: str | None, *, is_end: bool) -> str | None:
    """把 CLI 的日期参数规范化成内部时间戳 'YYYY-MM-DD HH:MM:SS'（UTC）。

    只给日期时：起始补 00:00:00，结束补 23:59:59（闭区间，含当天）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _DATE_ONLY_RE.match(text):
        return f"{text} {'23:59:59' if is_end else '00:00:00'}"
    return text  # 完整时间戳交给数据层校验（格式非法会报明确错误）


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m market.data.cli",
        description="导入 A 股股票基础数据与历史日 K（默认 dry-run，不写库）",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help=(
            "指定代码：加引号用逗号分隔（\"600519,000001,300750\"），或空格分隔（600519 000001 300750）。"
            "注意：Windows PowerShell 下不加引号的逗号列表会被当成数组，000001 会变成 1，务必加引号"
        ),
    )
    target.add_argument("--all", action="store_true", help="同步全量 A 股股票列表（约 5600 只）")

    parser.add_argument("--start", default=None, help="日 K 起始日期（YYYY-MM-DD，含当天）")
    parser.add_argument("--end", default=None, help="日 K 结束日期（YYYY-MM-DD，含当天）")
    parser.add_argument(
        "--datalen",
        type=int,
        default=SINA_DEFAULT_DAILY_BARS,
        help=f"向数据源请求最近多少根日 K（1~{SINA_MAX_DAILY_BARS}，默认 {SINA_DEFAULT_DAILY_BARS}）",
    )
    parser.add_argument("--limit", type=int, default=None, help="--all 时最多处理多少只股票")
    parser.add_argument("--no-klines", action="store_true", help="只同步股票基础数据，不导入日 K")
    parser.add_argument(
        "--write",
        action="store_true",
        help="真正写入数据库（不传则只预演：取数 + 校验 + 统计，不写库）",
    )
    parser.add_argument("--db", dest="db_path", default=None, help="数据库路径（默认取项目配置）")
    parser.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")
    parser.add_argument("--quiet", action="store_true", help="不打印逐页 / 逐只进度")
    return parser


def _print_report(
    *,
    db_path: Path,
    dry_run: bool,
    symbols_report: SymbolSyncReport,
    kline_report: DailyKlineReport | None,
    notes: list[str],
) -> None:
    """打印导入汇总报表（口径固定，便于人工核对）。"""
    print("")
    print("================ A 股数据导入汇总 ================")
    print("数据源: 新浪财经（A 股列表 + 历史日 K，公开接口、无需 API Key）")
    print(f"模式:   {'DRY-RUN（只预演，未写库）' if dry_run else 'WRITE（已写入数据库）'}")
    print(f"数据库: {db_path}")
    print(
        "股票:   "
        f"读到={symbols_report.fetched} 新增={symbols_report.inserted} 更新={symbols_report.updated} "
        f"未变={symbols_report.unchanged} 跳过={symbols_report.skipped} 失败={symbols_report.failed}"
    )
    if kline_report is None:
        print("日K:    未执行（--no-klines）")
    else:
        print(
            "日K:   "
            f"标的={kline_report.symbols} 有数据={kline_report.symbols_with_data} "
            f"无数据={kline_report.symbols_empty} 失败={kline_report.symbols_failed} "
            f"抓取={kline_report.fetched} 根 新增={kline_report.inserted} 更新={kline_report.updated} "
            f"出错行={kline_report.failed_rows}"
        )

    problems = list(symbols_report.errors) + (list(kline_report.errors) if kline_report else []) + list(notes)
    if problems:
        print(f"错误 / 备注（共 {len(problems)} 条，最多显示 {MAX_REPORTED_ERRORS} 条）:")
        for line in problems[:MAX_REPORTED_ERRORS]:
            print(f"  - {line}")
        if len(problems) > MAX_REPORTED_ERRORS:
            print(f"  … 其余 {len(problems) - MAX_REPORTED_ERRORS} 条略")
    else:
        print("错误 / 备注: 无")
    print("==================================================")


def main(argv: list[str] | None = None) -> int:
    """命令行入口；返回进程退出码（有失败项返回 1）。"""
    args = build_parser().parse_args(argv)
    dry_run = not args.write
    progress = None if args.quiet else (lambda line: print(f"  · {line}"))

    try:
        start_timestamp = normalize_range(args.start, is_end=False)
        end_timestamp = normalize_range(args.end, is_end=True)
    except ValueError as exc:
        print(f"参数错误: {exc}", file=sys.stderr)
        return 2

    db_path = resolve_database_path(args.db_path)
    if dry_run and not db_path.exists():
        print(
            f"目标数据库不存在: {db_path}\n"
            "预演模式不会创建数据库；请先用 --write 写入，或先用项目方式初始化数据库。",
            file=sys.stderr,
        )
        return 2
    if not dry_run:
        initialize_database(db_path)
    print(f"数据库: {database_info(db_path)['db_path']}（{'预演' if dry_run else '写入'}模式）")

    symbols = SymbolRepository(db_path)
    list_source = make_a_share_list_source(timeout=args.timeout)

    # ------------------------------------------------------------- 股票基础数据
    notes: list[str] = []
    codes: list[str] = []
    if args.symbols:
        # 兼容两种写法：加引号的逗号列表 / 空格分隔的多个参数
        codes = [part.strip() for token in args.symbols for part in str(token).split(",") if part.strip()]
        if not codes:
            print("参数错误: --symbols 为空", file=sys.stderr)
            return 2
        symbols_report = ensure_a_share_symbols(list_source, symbols, codes, dry_run=dry_run, progress=progress)
    else:
        symbols_report = sync_a_share_symbols(
            list_source,
            symbols,
            dry_run=dry_run,
            limit=args.limit,
            progress=progress,
        )

    # ------------------------------------------------------------- 历史日 K
    kline_report: DailyKlineReport | None = None
    if not args.no_klines:
        targets = _build_targets(
            symbols,
            codes=codes,
            all_symbols=args.all,
            dry_run=dry_run,
            notes=notes,
        )
        kline_source = HttpKlineDataSource(sina_daily_kline_config(datalen=args.datalen, timeout=args.timeout))
        kline_report = import_daily_klines(
            kline_source,
            KlineRepository(db_path),
            targets,
            timeframe="1d",
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            dry_run=dry_run,
            progress=progress,
        )

    _print_report(
        db_path=db_path,
        dry_run=dry_run,
        symbols_report=symbols_report,
        kline_report=kline_report,
        notes=notes,
    )
    failed = symbols_report.failed + (kline_report.symbols_failed if kline_report else 0)
    return 1 if failed else 0


def _build_targets(
    symbols: SymbolRepository,
    *,
    codes: list[str],
    all_symbols: bool,
    dry_run: bool,
    notes: list[str],
) -> list[KlineTarget]:
    """把「待导入日 K 的标的」解析成 KlineTarget 列表（symbol_id 必须已存在）。"""
    requested: list[str]
    if all_symbols:
        requested = [str(row["symbol"]) for row in symbols.list_active_symbols(market=A_SHARE_MARKET)]
    else:
        requested = list(codes)

    targets: list[KlineTarget] = []
    for raw in requested:
        try:
            code = normalize_a_share_code(raw)
        except (TypeError, ValueError) as exc:
            notes.append(f"{raw!r} 无法标准化，跳过日 K: {exc}")
            continue
        row = symbols.get_symbol(code.market, code.symbol)
        if row is None:
            notes.append(
                f"{code.display_code} 尚未入库，跳过其日 K"
                + ("（预演模式不会建股票，加 --write 即可）" if dry_run else "")
            )
            continue
        targets.append(KlineTarget(symbol_id=int(row["id"]), code=code, name=str(row["name"])))
    return targets


if __name__ == "__main__":
    raise SystemExit(main())
