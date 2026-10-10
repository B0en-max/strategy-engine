"""Stage 026：概念板块（多对多）数据源与同步测试。

覆盖：
    数据源     概念清单解析（JS 变量 / GBK / 空名称丢弃）、分页取成员、无效代码过滤并计数、异常 → DataSourceError
    多对多     一只股票属于多个概念；一个概念多只股票；**不做一股一概念假设**
    清洗       同一概念内重复代码去重并计数、空概念如实留空、库中不存在的代码单独统计（不自动创建 symbol）
    同步       dry-run 不写库、正式写入、幂等复跑、同名非概念集合跳过不碰
    无破坏     不新增/不删除 symbol、不动 klines、不动 industry / trading_board 数据
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_source.base import DataSourceError  # noqa: E402
from database import KlineRepository, SymbolGroupRepository, SymbolRepository, initialize_database  # noqa: E402
from market.data.concepts import (  # noqa: E402
    CONCEPT_GROUP_TYPE,
    Concept,
    SinaConceptSource,
    sync_concepts,
)

#: 真实接口的概念清单形态（GBK / JS 变量 / 逗号分隔；含一条空名称用于清洗验证）
CONCEPT_LIST_TEXT = (
    'var S_Finance_bankuai_class = {'
    '"gn_bjgn":"gn_bjgn,白酒概念,19,12.3,0.5,1.2,1,2,sh600519,1,2,3,贵州茅台",'
    '"gn_jrrh":"gn_jrrh,金融融合,40,8.1,0.2,0.3,1,2,sz000001,1,2,3,平安银行",'
    '"gn_empty":"gn_empty,,5,1,1,1,1,2,sh600000,1,2,3,示例",'
    '"gn_gd":"gn_gd,固态电池,30,9.9,1.1,2.2,1,2,sz300750,1,2,3,宁德时代"'
    "};"
)


class FakeOpener:
    """假的 opener：按 URL 子串返回预设响应。"""

    def __init__(self, responses: dict[str, bytes], *, error: Exception | None = None) -> None:
        self._responses = responses
        self._error = error
        self.calls: list[str] = []

    def __call__(self, url: str, headers, timeout: float) -> bytes:
        self.calls.append(url)
        if self._error is not None:
            raise self._error
        for key, payload in self._responses.items():
            if key in url:
                return payload
        raise AssertionError(f"未预设的 URL: {url}")


def rows(*codes: str) -> bytes:
    return json.dumps([{"code": code} for code in codes], ensure_ascii=False).encode("utf-8")


class SourceTest(unittest.TestCase):
    """概念数据源解析。"""

    def test_fetch_concepts_parses_and_drops_empty_names(self) -> None:
        opener = FakeOpener({"param=class": CONCEPT_LIST_TEXT.encode("gbk")})
        concepts = SinaConceptSource(opener=opener).fetch_concepts()
        self.assertEqual([c.code for c in concepts], ["gn_bjgn", "gn_jrrh", "gn_gd"])  # 空名称的被丢弃
        self.assertEqual(concepts[0].name, "白酒概念")
        self.assertEqual(concepts[0].declared_count, 19)

    def test_invalid_payload(self) -> None:
        with self.assertRaises(DataSourceError):
            SinaConceptSource(opener=FakeOpener({"param=class": b"<html>x</html>"})).fetch_concepts()

    def test_members_pagination_and_invalid_code_counting(self) -> None:
        opener = FakeOpener(
            {
                "page=2": json.dumps([{"code": "000001"}, {"code": "BTCUSDT"}, {"code": "hk00700"}]).encode("utf-8"),
                "page=1": json.dumps([{"code": f"{600000 + i}"} for i in range(100)]).encode("utf-8"),
            }
        )
        source = SinaConceptSource(opener=opener)
        pages = list(source.iter_member_pages("gn_bjgn"))
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(pages[0][0]), 100)
        self.assertEqual(pages[1][0], ["000001"])
        self.assertEqual(pages[1][1], 2)  # BTCUSDT / hk00700 作为非 A 股代码被过滤并计数
        self.assertEqual(source.request_count, 2)
        self.assertEqual(source.failed_requests, 0)

    def test_members_dedupe_keeps_order(self) -> None:
        opener = FakeOpener({"page=1": rows("600519", "000001", "600519")})
        members = SinaConceptSource(opener=opener).fetch_members("gn_bjgn")
        self.assertEqual(members, ["600519", "000001"])

    def test_http_error_becomes_data_source_error_and_counts_failure(self) -> None:
        source = SinaConceptSource(opener=FakeOpener({}, error=HTTPError("http://x", 502, "boom", {}, None)))
        with self.assertRaises(DataSourceError):
            source.fetch_concepts()
        self.assertEqual(source.failed_requests, 1)


class FakeConceptSource(SinaConceptSource):
    """用预设字典替代网络的概念数据源（成员可含重复，用于清洗验证）。"""

    def __init__(self, concepts: dict[str, tuple[str, list[str]]], *, fail: set[str] | None = None):
        super().__init__()
        self._concepts = concepts
        self._fail = fail or set()

    def fetch_concepts(self) -> list[Concept]:
        return [
            Concept(code=code, name=name, declared_count=len(codes))
            for code, (name, codes) in self._concepts.items()
        ]

    def fetch_members(self, code: str) -> list[str]:
        if code in self._fail:
            raise DataSourceError(f"模拟概念 {code} 抓取失败")
        return list(self._concepts[code][1])

    def iter_member_pages(self, code: str):  # noqa: ANN201
        """按 100 条一页切分，保留重复项，便于验证「概念内重复」统计。"""
        members = self.fetch_members(code)
        for start in range(0, max(len(members), 1), 100):
            chunk = members[start:start + 100]
            yield [c for c in chunk if c.isdigit() and len(c) == 6], 0


class SyncTest(unittest.TestCase):
    """概念同步（临时库）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_concepts_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "concepts.db"
        initialize_database(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.groups = SymbolGroupRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)

        self.codes = ["600519", "000001", "300750", "688111", "999999"]
        for index, code in enumerate(self.codes):
            self.symbols.create_symbol(code, f"股票{index}", "CN", "STOCK")
        self.symbols.create_symbol("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO")

        # 多对多：600519 属于 3 个概念；300750 属于 2 个；999999 不在任何概念（未分类）
        self.source = FakeConceptSource(
            {
                "gn_a": ("白酒概念", ["600519", "300750"]),
                "gn_b": ("基金重仓", ["600519", "000001", "600519"]),  # 页内重复 → duplicate_memberships
                "gn_c": ("固态电池", ["300750", "688111", "888888"]),  # 888888 不在库中
            }
        )
        # 已有的行业集合（Stage 025 产物）必须原样保留
        self.industry_id = self.groups.create_group("白酒概念行业", "industry", market="CN", description="industry=hangye_C15")
        self.groups.add_symbol_to_group(self.industry_id, int(self.symbols.get_symbol("CN", "600519")["id"]))

    def members_of(self, name: str) -> list[str]:
        group = self.groups.get_group_by_name(name)
        return sorted(str(r["symbol"]) for r in self.groups.list_group_symbols(int(group["id"])))

    def test_dry_run_writes_nothing(self) -> None:
        report = sync_concepts(self.groups, self.symbols, self.source, dry_run=True)
        self.assertEqual(report.written, 0)
        self.assertEqual(report.groups_created, 3)
        self.assertIsNone(self.groups.get_group_by_name("白酒概念"))
        self.assertEqual(len(self.groups.list_groups()), 1)  # 只有行业集合

    def test_write_creates_concept_groups(self) -> None:
        report = sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.membership_added, 6)  # 2 + 3 + 3 - 1（重复） - 1（不在库中）
        group = self.groups.get_group_by_name("白酒概念")
        self.assertEqual(group["group_type"], CONCEPT_GROUP_TYPE)
        self.assertIn("concept=gn_a", group["description"])
        self.assertEqual(self.members_of("白酒概念"), ["300750", "600519"])

    def test_many_to_many_relations(self) -> None:
        sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(self.members_of("白酒概念"), ["300750", "600519"])
        self.assertEqual(self.members_of("基金重仓"), ["000001", "600519"])
        self.assertEqual(self.members_of("固态电池"), ["300750", "688111"])
        # 600519 同时属于「白酒概念」与「基金重仓」→ 多对多成立
        conn_codes = [
            name for name in ("白酒概念", "基金重仓", "固态电池") if "600519" in self.members_of(name)
        ]
        self.assertEqual(sorted(conn_codes), ["基金重仓", "白酒概念"])

    def test_duplicate_membership_and_unknown_code_accounting(self) -> None:
        report = sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(report.duplicate_memberships, 1)  # 基金重仓里 600519 出现两次
        self.assertEqual(report.source_symbols_not_in_db, 1)  # 888888
        self.assertEqual(self.members_of("基金重仓"), ["000001", "600519"])  # 只写了一条

    def test_coverage_equation(self) -> None:
        report = sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(report.symbols_total, 5)  # 只统计 CN
        self.assertEqual(report.symbols_classified, 4)
        self.assertEqual(report.symbols_unclassified, 1)
        self.assertEqual(report.symbols_classified + report.symbols_unclassified, report.symbols_total)
        self.assertEqual(report.unclassified_codes, ["999999"])
        # membership_count ≠ symbols_classified（多对多），这里是 6 vs 4
        self.assertEqual(report.membership_count, 6)
        self.assertNotEqual(report.membership_count, report.symbols_classified)

    def test_idempotent_second_run(self) -> None:
        first = sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        second = sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(first.membership_added, 6)
        self.assertEqual(second.membership_added, 0)
        self.assertEqual(second.membership_existing, 6)
        self.assertEqual(second.groups_created, 0)
        self.assertEqual(second.groups_reused, 3)
        self.assertEqual(len(self.groups.list_groups()), 4)  # 1 行业 + 3 概念

    def test_existing_industry_data_untouched(self) -> None:
        sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        industry = self.groups.get_group_by_name("白酒概念行业")
        self.assertEqual(industry["group_type"], "industry")
        self.assertEqual(industry["description"], "industry=hangye_C15")
        self.assertEqual(self.members_of("白酒概念行业"), ["600519"])
        self.assertEqual(len(self.symbols.list_symbols()), 6)
        self.assertEqual(self.klines.count_klines(1, "1d"), 0)

    def test_same_name_non_concept_group_is_skipped(self) -> None:
        self.groups.create_group("固态电池", "custom", description="我的人工集合")
        report = sync_concepts(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(len(report.skipped_concepts), 1)
        after = self.groups.get_group_by_name("固态电池")
        self.assertEqual(after["group_type"], "custom")
        self.assertEqual(after["description"], "我的人工集合")
        self.assertEqual(len(self.groups.list_group_symbols(int(after["id"]))), 0)

    def test_empty_concept_is_kept_empty(self) -> None:
        source = FakeConceptSource({"gn_x": ("空概念", [])})
        report = sync_concepts(self.groups, self.symbols, source, dry_run=False)
        self.assertEqual(report.empty_concepts, ["空概念"])
        self.assertEqual(self.members_of("空概念"), [])  # 不伪造成员

    def test_single_concept_failure_does_not_break_others(self) -> None:
        source = FakeConceptSource(
            {"gn_a": ("白酒概念", ["600519"]), "gn_b": ("基金重仓", ["000001"])},
            fail={"gn_b"},
        )
        report = sync_concepts(self.groups, self.symbols, source, dry_run=False)
        self.assertEqual(len(report.errors), 1)
        self.assertIsNotNone(self.groups.get_group_by_name("白酒概念"))
        self.assertIsNone(self.groups.get_group_by_name("基金重仓"))

    def test_report_fields(self) -> None:
        payload = sync_concepts(self.groups, self.symbols, self.source, dry_run=True).as_dict()
        for key in (
            "source",
            "concept_count",
            "symbols_total",
            "symbols_classified",
            "symbols_unclassified",
            "membership_count",
            "duplicate_memberships",
            "request_count",
            "failed_requests",
            "elapsed_seconds",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["concept_count"], 3)
        self.assertEqual(payload["written"], 0)


if __name__ == "__main__":
    unittest.main()
