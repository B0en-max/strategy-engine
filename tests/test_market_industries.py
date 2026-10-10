"""Stage 025：行业板块（证监会行业分类）数据源与同步测试。

覆盖：
    数据源    行业清单解析（JS 变量 / GBK）、分页取成员、非 6 位代码过滤、异常 → DataSourceError
    批量写入  SymbolGroupRepository.bulk_add_symbols_to_group：幂等、只计新增、单事务原子
    同步      dry-run 不写库、正式写入建 industry 集合、幂等复跑、未分类如实上报、
              多行业重复上报、数据源里有但库中没有的代码计数、
              不改动交易板块 / 其它集合、同名非行业集合跳过不碰
    无破坏    不新增/不删除 symbol，不动 klines
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
from market.data.industries import (  # noqa: E402
    INDUSTRY_GROUP_TYPE,
    SinaIndustrySource,
    sync_industries,
)

#: 真实接口的行业清单形态（GBK、JS 变量、逗号分隔字段）
INDUSTRY_LIST_TEXT = (
    'var S_Finance_bankuai_industry = {'
    '"hangye_ZA01":"hangye_ZA01,农业,16,9.82,0.34,3.67,772991490,5723859279,sh601118,10.017,6.370,0.580,海南橡胶",'
    '"hangye_I65":"hangye_I65,软件和信息技术服务业,364,20.1,0.5,1.2,1,2,sz300750,1,2,3,示例",'
    '"hangye_J66":"hangye_J66,货币金融服务,42,8.1,0.2,0.3,1,2,sh600000,1,2,3,示例"'
    "};"
)


class FakeOpener:
    """假的 opener：按 URL 返回预设响应，记录请求。"""

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


def members_payload(count: int, prefix: str = "0") -> bytes:
    rows = [
        {"symbol": f"sh{600000 + i}", "code": f"{600000 + i}", "name": f"股票{i}"}
        for i in range(count)
    ]
    if prefix:
        rows.insert(0, {"symbol": prefix, "name": "噪声行"})
    return json.dumps(rows, ensure_ascii=False).encode("utf-8")


class SourceTest(unittest.TestCase):
    """行业数据源解析。"""

    def test_fetch_industries_parses_js_var(self) -> None:
        opener = FakeOpener({"param=industry": INDUSTRY_LIST_TEXT.encode("gbk")})
        industries = SinaIndustrySource(opener=opener).fetch_industries()
        self.assertEqual([i.code for i in industries], ["hangye_ZA01", "hangye_I65", "hangye_J66"])
        self.assertEqual(industries[0].name, "农业")
        self.assertEqual(industries[1].name, "软件和信息技术服务业")
        self.assertEqual(industries[1].declared_count, 364)

    def test_fetch_industries_invalid_payload(self) -> None:
        with self.assertRaises(DataSourceError):
            SinaIndustrySource(opener=FakeOpener({"param=industry": b"<html>oops</html>"})).fetch_industries()

    def test_fetch_members_paginates_and_filters_noise(self) -> None:
        # 用 page=N 作为匹配键（URL 里同时含 node=，用 node 做键会命中同一响应导致死循环）
        opener = FakeOpener(
            {
                "page=2": json.dumps([{"code": "000001"}, {"code": "BTCUSDT"}]).encode("utf-8"),
                "page=1": members_payload(100),
            }
        )
        source = SinaIndustrySource(opener=opener)
        members = source.fetch_members("hangye_ZA01")
        self.assertEqual(len(members), 101)  # 100（第一页，含 1 条噪声被过滤）+ 1（第二页，BTCUSDT 被过滤）
        self.assertEqual(members[0], "600000")
        self.assertIn("000001", members)
        self.assertNotIn("BTCUSDT", members)
        self.assertIn("node=hangye_ZA01", opener.calls[0])
        self.assertIn("page=1", opener.calls[0])
        self.assertIn("page=2", opener.calls[1])

    def test_http_error_becomes_data_source_error(self) -> None:
        opener = FakeOpener({}, error=HTTPError("http://x", 503, "boom", {}, None))
        with self.assertRaises(DataSourceError):
            SinaIndustrySource(opener=opener).fetch_industries()
        with self.assertRaises(DataSourceError):
            SinaIndustrySource(opener=opener).fetch_members("hangye_ZA01")


class BulkMembershipTest(unittest.TestCase):
    """仓储批量成员写入。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_industry_repo_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "industry.db"
        initialize_database(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.groups = SymbolGroupRepository(self.db_path)
        self.ids = [self.symbols.create_symbol(f"60000{i}", f"股票{i}", "CN", "STOCK") for i in range(5)]
        self.group_id = self.groups.create_group("农业", "industry")

    def members(self) -> int:
        return len(self.groups.list_group_symbols(self.group_id))

    def test_bulk_insert_is_idempotent(self) -> None:
        self.assertEqual(self.groups.bulk_add_symbols_to_group(self.group_id, self.ids), 5)
        self.assertEqual(self.members(), 5)
        # 重复写入：一条都不新增
        self.assertEqual(self.groups.bulk_add_symbols_to_group(self.group_id, self.ids), 0)
        self.assertEqual(self.members(), 5)
        # 部分重叠：只新增缺的那一个
        self.assertEqual(self.groups.bulk_add_symbols_to_group(self.group_id, [self.ids[0], self.ids[1]]), 0)

    def test_bulk_insert_batches_large_input(self) -> None:
        many = [self.symbols.create_symbol(f"{700000 + i}", f"批量{i}", "CN", "STOCK") for i in range(1200)]
        added = self.groups.bulk_add_symbols_to_group(self.group_id, many)
        self.assertEqual(added, 1200)
        self.assertEqual(self.members(), 1200)

    def test_missing_group_or_symbol(self) -> None:
        with self.assertRaises(ValueError):
            self.groups.bulk_add_symbols_to_group(999999, self.ids)
        # 不存在的 symbol_id → 整批失败（外键约束 + 单事务回滚），不留半批数据
        with self.assertRaises(Exception):
            self.groups.bulk_add_symbols_to_group(self.group_id, [self.ids[0], 999999])
        self.assertEqual(self.members(), 0)


class FakeIndustrySource(SinaIndustrySource):
    """用预设字典替代网络的行业数据源。"""

    def __init__(self, industries: dict[str, tuple[str, list[str]]], *, fail: set[str] | None = None):
        super().__init__()
        self._industries = industries
        self._fail = fail or set()

    def fetch_industries(self):  # noqa: ANN201
        from market.data.industries import Industry

        return [Industry(code=code, name=name, declared_count=len(codes)) for code, (name, codes) in self._industries.items()]

    def fetch_members(self, code: str) -> list[str]:
        if code in self._fail:
            raise DataSourceError(f"模拟行业 {code} 抓取失败")
        return list(self._industries[code][1])


class SyncTest(unittest.TestCase):
    """行业同步（临时库）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_industry_sync_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "industry_sync.db"
        initialize_database(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.groups = SymbolGroupRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)

        self.codes = ["600519", "000001", "300750", "688111", "920000", "999999"]
        for index, code in enumerate(self.codes):
            self.symbols.create_symbol(code, f"股票{index}", "CN", "STOCK")
        self.btc = self.symbols.create_symbol("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO")

        # 预设：两只在「酒、饮料和精制茶制造业」，两只在「软件和信息技术服务业」，
        # 920000 在「其它」（用于验证重复上报），999999 不属于任何行业（未分类）
        self.source = FakeIndustrySource(
            {
                "hangye_C15": ("酒、饮料和精制茶制造业", ["600519", "300750"]),
                "hangye_I65": ("软件和信息技术服务业", ["000001", "688111"]),
                "hangye_X": ("综合", ["920000", "300750", "700001"]),  # 300750 重复；700001 不在库中
            }
        )
        # 交易板块（Stage 024 的产物）：必须原样保留
        self.board_group = self.groups.create_group("沪市主板", "custom", market="CN", description="trading_board=SSE_MAIN")
        self.groups.add_symbol_to_group(self.board_group, int(self.symbols.get_symbol("CN", "600519")["id"]))

    def test_dry_run_writes_nothing(self) -> None:
        report = sync_industries(self.groups, self.symbols, self.source, dry_run=True)
        self.assertEqual(report.written, 0)
        self.assertEqual(report.industries_created, 3)
        self.assertEqual(report.membership_planned, 6)  # 600519/300750/000001/688111/920000 + 300750（重复数）
        self.assertIsNone(self.groups.get_group_by_name("综合"))
        self.assertEqual(len(self.groups.list_groups()), 1)  # 只有交易板块

    def test_write_creates_industry_groups_and_memberships(self) -> None:
        report = sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(report.membership_added, 6)  # 700001 不在库中，不计入
        self.assertEqual(report.errors, [])

        group = self.groups.get_group_by_name("酒、饮料和精制茶制造业")
        self.assertEqual(group["group_type"], INDUSTRY_GROUP_TYPE)
        self.assertEqual(group["market"], "CN")
        self.assertIn("industry=hangye_C15", group["description"])
        self.assertEqual(
            sorted(str(r["symbol"]) for r in self.groups.list_group_symbols(int(group["id"]))), ["300750", "600519"]
        )

    def test_coverage_and_unclassified(self) -> None:
        report = sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(report.symbols_total, 6)  # 只统计 CN
        self.assertEqual(report.classified, 5)
        self.assertEqual(report.unclassified, 1)
        self.assertEqual(report.unclassified_codes, ["999999"])
        self.assertEqual(report.classified + report.unclassified, report.symbols_total)
        # 数据源里有但库里没有的代码（700001）被单独计数，不混进分类率
        self.assertEqual(report.source_codes_not_in_db, 1)

    def test_duplicates_reported(self) -> None:
        report = sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(len(report.duplicates), 1)
        self.assertIn("300750", report.duplicates[0])

    def test_idempotent_second_run(self) -> None:
        first = sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        second = sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(first.membership_added, 6)
        self.assertEqual(second.membership_added, 0)
        self.assertEqual(second.membership_existing, 6)
        self.assertEqual(second.industries_created, 0)
        self.assertEqual(second.industries_reused, 3)
        self.assertEqual(len(self.groups.list_groups()), 4)  # 1 交易板块 + 3 行业

    def test_trading_boards_and_other_groups_untouched(self) -> None:
        sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        board = self.groups.get_group_by_name("沪市主板")
        self.assertIsNotNone(board)
        self.assertEqual(board["group_type"], "custom")
        self.assertEqual(board["description"], "trading_board=SSE_MAIN")
        self.assertEqual(len(self.groups.list_group_symbols(self.board_group)), 1)
        # 股票与 K 线数量不变
        self.assertEqual(len(self.symbols.list_symbols()), 7)
        self.assertEqual(self.klines.count_klines(1, "1d"), 0)

    def test_same_name_non_industry_group_is_skipped(self) -> None:
        taken = self.groups.create_group("综合", "custom", description="我的人工集合")
        report = sync_industries(self.groups, self.symbols, self.source, dry_run=False)
        self.assertEqual(len(report.skipped_industries), 1)
        self.assertIn("综合", report.skipped_industries[0])
        after = self.groups.get_group_by_name("综合")
        self.assertEqual(after["group_type"], "custom")  # 类型没被改
        self.assertEqual(after["description"], "我的人工集合")
        self.assertEqual(len(self.groups.list_group_symbols(int(taken))), 0)  # 没被塞成员

    def test_single_industry_failure_does_not_break_others(self) -> None:
        source = FakeIndustrySource(
            {
                "hangye_C15": ("酒、饮料和精制茶制造业", ["600519"]),
                "hangye_I65": ("软件和信息技术服务业", ["000001"]),
            },
            fail={"hangye_I65"},
        )
        report = sync_industries(self.groups, self.symbols, source, dry_run=False)
        self.assertEqual(len(report.errors), 1)
        self.assertIsNotNone(self.groups.get_group_by_name("酒、饮料和精制茶制造业"))
        self.assertIsNone(self.groups.get_group_by_name("软件和信息技术服务业"))

    def test_report_serialises(self) -> None:
        payload = sync_industries(self.groups, self.symbols, self.source, dry_run=True).as_dict()
        self.assertEqual(payload["dry_run"], True)
        self.assertEqual(payload["industries_total"], 3)
        self.assertEqual(payload["written"], 0)


if __name__ == "__main__":
    unittest.main()
