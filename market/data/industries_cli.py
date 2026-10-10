"""行业板块初始化命令行入口（Stage 025）。

**默认 dry-run**（不加 --write 只预演，不写库）。

用法（项目根目录执行）：

    # 预演：抓取行业分类并输出覆盖率报告（不写库）
    .venv\\Scripts\\python.exe -m market.data.industries_cli

    # 正式写入默认库（幂等，可重复执行）
    .venv\\Scripts\\python.exe -m market.data.industries_cli --write

    # 指定库
    .venv\\Scripts\\python.exe -m market.data.industries_cli --write --db <path>

只处理**行业板块**（证监会行业分类）；不处理交易板块（Stage 024）、概念板块（后续阶段）。
"""

from __future__ import annotations

import argparse
import sys

from database import SymbolGroupRepository, SymbolRepository, database_info, initialize_database, resolve_database_path
from market.data.industries import SinaIndustrySource, sync_industries

MAX_REPORTED = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m market.data.industries_cli",
        description="同步 A 股行业板块（证监会行业分类，来源：新浪财经；默认 dry-run）",
    )
    parser.add_argument("--write", action="store_true", help="真正写入数据库（不传则只预演）")
    parser.add_argument("--db", dest="db_path", default=None, help="数据库路径（默认取项目配置）")
    parser.add_argument("--quiet", action="store_true", help="不打印逐行业进度")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数（默认 15）")
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

    report = sync_industries(
        SymbolGroupRepository(db_path),
        SymbolRepository(db_path),
        SinaIndustrySource(timeout=args.timeout),
        dry_run=dry_run,
        progress=progress,
    )

    print("")
    print("================ 行业板块同步汇总 ================")
    print("数据源: 新浪财经 · 证监会行业分类（group_type=industry）")
    print(f"模式:   {'DRY-RUN（只预演，未写库）' if dry_run else 'WRITE（已写入数据库）'}")
    print(f"数据库: {database_info(db_path)['db_path']}")
    print(f"symbols 总数            : {report.symbols_total}")
    print(f"成功获得行业的 symbols  : {report.classified}")
    print(f"未获得行业的 symbols    : {report.unclassified}"
          + (f"（未分类 + 已分类 = {report.classified + report.unclassified}）" if report.unclassified else "（全覆盖 ✓）"))
    print(f"行业数量                : {report.industries_total}（新建 {report.industries_created} / 复用 {report.industries_reused}）")
    print(f"membership 数量(计划)   : {report.membership_planned}")
    print(f"membership 新增 / 已在  : {report.membership_added} / {report.membership_existing}")
    print(f"同一股票属于多行业(重复): {len(report.duplicates)}")
    print(f"数据源有但库中没有的代码: {report.source_codes_not_in_db}")
    print(f"跳过的行业              : {len(report.skipped_industries)}")

    if report.unclassified_codes:
        print(f"未分类代码示例（最多 {MAX_REPORTED} 个）: {report.unclassified_codes[:MAX_REPORTED]}")
    if report.duplicates:
        print(f"重复示例（最多 {MAX_REPORTED} 条）:")
        for line in report.duplicates[:MAX_REPORTED]:
            print(f"  - {line}")
    if report.skipped_industries:
        for line in report.skipped_industries[:MAX_REPORTED]:
            print(f"  - 跳过: {line}")
    if report.errors:
        print(f"错误（共 {len(report.errors)} 条，最多显示 {MAX_REPORTED} 条）:")
        for line in report.errors[:MAX_REPORTED]:
            print(f"  - {line}")
    print("==================================================")

    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
