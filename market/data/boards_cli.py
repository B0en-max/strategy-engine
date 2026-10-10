"""交易板块初始化命令行入口（Stage 024）。

**默认 dry-run**（不加 --write 只预演，不写库），与 Stage 021 的导入 CLI 保持同一风格。

用法（项目根目录执行）：

    # 预演：只统计分类结果与将要写入的成员关系
    .venv\\Scripts\\python.exe -m market.data.boards_cli

    # 正式写入默认库（幂等，可重复执行）
    .venv\\Scripts\\python.exe -m market.data.boards_cli --write

    # 指定库（例如验收用的临时库）
    .venv\\Scripts\\python.exe -m market.data.boards_cli --write --db <path>

只处理交易板块（交易所层级）+「沪深主板」默认 Universe；不处理行业 / 概念板块。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from database import SymbolGroupRepository, SymbolRepository, database_info, initialize_database, resolve_database_path
from market.data.boards import HS_MAIN_UNIVERSE_CODE, TRADING_BOARDS, sync_trading_boards

MAX_REPORTED = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m market.data.boards_cli",
        description="初始化交易板块（沪/深主板、创业板、科创板、北交所）+ 沪深主板 Universe（默认 dry-run）",
    )
    parser.add_argument("--write", action="store_true", help="真正写入数据库（不传则只预演）")
    parser.add_argument("--db", dest="db_path", default=None, help="数据库路径（默认取项目配置）")
    parser.add_argument("--quiet", action="store_true", help="不打印逐板块进度")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = not args.write
    progress = None if args.quiet else (lambda line: print(f"  · {line}"))

    db_path = resolve_database_path(args.db_path)
    if dry_run and not db_path.exists():
        print(f"目标数据库不存在: {db_path}（预演不会创建数据库）", file=sys.stderr)
        return 2
    if not dry_run:
        initialize_database(db_path)

    report = sync_trading_boards(
        SymbolGroupRepository(db_path),
        SymbolRepository(db_path),
        dry_run=dry_run,
        progress=progress,
    )

    print("")
    print("================ 交易板块初始化汇总 ================")
    print(f"模式:   {'DRY-RUN（只预演，未写库）' if dry_run else 'WRITE（已写入数据库）'}")
    print(f"数据库: {database_info(db_path)['db_path']}")
    print(
        f"股票:   读取={report.symbols_seen} 已分类={report.classified} 未分类={report.unclassified}"
        f"（不做任何删除 / 不新建股票）"
    )
    print(f"集合:   新建={report.groups_created} 复用={report.groups_existing}")
    print(f"成员:   新增={report.added} 已在集合={report.already_member} 失败={report.failed}")
    print("板块成员规划:")
    for board in TRADING_BOARDS:
        print(f"  {board.code:<10} {board.label:<6} = {report.per_board.get(board.code, 0)}")
    print(f"  {HS_MAIN_UNIVERSE_CODE:<10} {'沪深主板':<6} = {report.per_board.get(HS_MAIN_UNIVERSE_CODE, 0)}（Universe，仅成员关系）")

    if report.unclassified_codes:
        print(f"未分类代码（最多显示 {MAX_REPORTED} 个）: {report.unclassified_codes[:MAX_REPORTED]}")
    if report.conflicts:
        print(f"冲突（同一股票属于多个交易板块，共 {len(report.conflicts)} 条，未自动处理）:")
        for line in report.conflicts[:MAX_REPORTED]:
            print(f"  - {line}")
    if report.errors:
        print(f"错误（共 {len(report.errors)} 条，最多显示 {MAX_REPORTED} 条）:")
        for line in report.errors[:MAX_REPORTED]:
            print(f"  - {line}")
    print("==================================================")

    return 1 if (report.failed or report.conflicts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
