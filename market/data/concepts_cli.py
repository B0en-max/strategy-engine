"""概念板块同步命令行入口（Stage 026）。

**默认 dry-run**（`--dry-run` 显式传入亦可，效果相同）；只有 `--write` 才会写库。

用法（项目根目录执行）：

    # 预演：抓取概念分类并输出覆盖率报告（不写库）
    .venv\\Scripts\\python.exe -m market.data.concepts_cli --dry-run

    # 正式写入默认库（幂等，可重复执行）
    .venv\\Scripts\\python.exe -m market.data.concepts_cli --write

    # 指定库
    .venv\\Scripts\\python.exe -m market.data.concepts_cli --write --db <path>

只处理**概念板块**（group_type='concept'）；不动交易板块（Stage 024）与行业板块（Stage 025）。
"""

from __future__ import annotations

import argparse
import sys

from database import SymbolGroupRepository, SymbolRepository, database_info, initialize_database, resolve_database_path
from market.data.concepts import SinaConceptSource, sync_concepts

MAX_REPORTED = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m market.data.concepts_cli",
        description="同步 A 股概念板块（新浪财经概念分类；默认 dry-run）",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="只预演不写库（默认行为）")
    group.add_argument("--write", action="store_true", help="真正写入数据库")
    parser.add_argument("--db", dest="db_path", default=None, help="数据库路径（默认取项目配置）")
    parser.add_argument("--quiet", action="store_true", help="不打印逐概念进度")
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

    report = sync_concepts(
        SymbolGroupRepository(db_path),
        SymbolRepository(db_path),
        SinaConceptSource(timeout=args.timeout),
        dry_run=dry_run,
        progress=progress,
    )

    print("")
    print("================ 概念板块同步汇总 ================")
    print("source                      :", report.source, "（新浪财经概念板块，group_type=concept，多对多）")
    print("mode                        :", "DRY-RUN（只预演，未写库）" if dry_run else "WRITE（已写入数据库）")
    print("db                          :", database_info(db_path)["db_path"])
    print("concept_count               :", report.concept_count)
    print("symbols_total               :", report.symbols_total)
    print("symbols_classified          :", report.symbols_classified)
    print("symbols_unclassified        :", report.symbols_unclassified,
          f"（classified + unclassified = {report.symbols_classified + report.symbols_unclassified}）")
    print("membership_count(概念×股票) :", report.membership_count)
    print("membership added / existing :", report.membership_added, "/", report.membership_existing)
    print("duplicate_memberships       :", report.duplicate_memberships)
    print("request_count               :", report.request_count)
    print("failed_requests             :", report.failed_requests)
    print("elapsed                     : %.1fs" % report.elapsed_seconds)
    print("groups created / reused     :", report.groups_created, "/", report.groups_reused)
    print("empty_concepts              :", len(report.empty_concepts))
    print("skipped_concepts            :", len(report.skipped_concepts))
    print("source_invalid_codes        :", report.source_invalid_codes)
    print("source_symbols_not_in_db    :", report.source_symbols_not_in_db)

    if report.unclassified_codes:
        print(f"未分类示例（最多 {MAX_REPORTED} 个）: {report.unclassified_codes[:MAX_REPORTED]}")
    for label, items in (("空概念", report.empty_concepts), ("跳过的概念", report.skipped_concepts), ("错误", report.errors)):
        if items:
            print(f"{label}（共 {len(items)} 条，最多显示 {MAX_REPORTED} 条）:")
            for line in items[:MAX_REPORTED]:
                print(f"  - {line}")
    print("==================================================")

    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
