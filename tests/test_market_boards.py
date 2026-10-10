"""Stage 024：交易板块（交易所层级）分类与初始化测试。

覆盖：
    代码段分类   沪深主板 / 创业板 / 科创板 / 北交所（含 430、83x、87x、920 段）
    不猜         非 6 位、非数字、沪B/深B、未知段 → None（未分类，如实上报）
    规则互斥     五个板块的前缀表两两不相交（因此一只股票最多属于一个板块）
    规划         纯函数 plan_memberships：分批计算结果 + 未分类列表
    同步         幂等（重复执行不重复插入）、只增不删、dry-run 不写库、
                 沪深主板 Universe = 沪市主板 ∪ 深市主板（仅成员关系）、
                 未分类与冲突如实报告（冲突不自动处理）
    不可破坏     不新增/不删除 symbol、不动 klines、不改 schema 版本
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import KlineRepository, SymbolGroupRepository, SymbolRepository, initialize_database  # noqa: E402
from market.data.boards import (  # noqa: E402
    BOARD_BY_CODE,
    BOARD_CODES,
    HS_MAIN_UNIVERSE_CODE,
    HS_MAIN_UNIVERSE_LABEL,
    TRADING_BOARDS,
    classify_board,
    plan_memberships,
    sync_trading_boards,
)


class ClassifyBoardTest(unittest.TestCase):
    """代码段 → 交易板块。"""

    def test_expected_boards_for_each_segment(self) -> None:
        cases = {
            "SSE_MAIN": ("600519", "601398", "603288", "605499"),
            "SZSE_MAIN": ("000001", "001979", "002594", "003816"),
            "SZSE_GEM": ("300750", "301269", "302132"),
            "SSE_STAR": ("688111", "689009"),
            "BSE": ("430047", "830799", "870204", "839999", "879999", "920000", "920999"),
        }
        for board, codes in cases.items():
            for code in codes:
                with self.subTest(board=board, code=code):
                    self.assertEqual(classify_board(code), board)

    def test_unreliable_codes_are_not_guessed(self) -> None:
        for code in (
            "BTCUSDT",  # 非 A 股
            "999999",  # 未知段
            "900901",  # 沪市 B 股（本模块只处理 A 股）
            "200011",  # 深市 B 股
            "60051",  # 位数不足
            "6005191",  # 位数超长
            "sh600519",  # 带前缀形态（库内不存这种）
            "880001",  # 88xxxx 段归属不确定 → 不猜
            "",
        ):
            with self.subTest(code=code):
                self.assertIsNone(classify_board(code))
        self.assertIsNone(classify_board(None))

    def test_board_prefixes_are_mutually_exclusive(self) -> None:
        """五个板块的前缀（含北交所区间）两两不相交 → 一只股票最多一个板块。"""
        seen: dict[str, str] = {}
        for board in TRADING_BOARDS:
            prefixes = list(board.prefixes)
            for low, high in (("830", "839"), ("870", "879")):
                if board.code == "BSE":
                    prefixes.extend(f"{n:03d}" for n in range(int(low), int(high) + 1))
            for prefix in prefixes:
                self.assertNotIn(prefix, seen, f"前缀 {prefix} 同时属于 {seen.get(prefix)} 与 {board.code}")
                seen[prefix] = board.code
        self.assertEqual(len(BOARD_CODES), 5)

    def test_board_metadata(self) -> None:
        self.assertEqual(BOARD_CODES, ("SSE_MAIN", "SZSE_MAIN", "SZSE_GEM", "SSE_STAR", "BSE"))
        self.assertEqual(BOARD_BY_CODE["SSE_MAIN"].label, "沪市主板")
        self.assertEqual(BOARD_BY_CODE["BSE"].label, "北交所")
        # 交易板块不是行业 / 概念板块（本阶段刻意不处理那两类）
        self.assertNotIn("industry", {b.code for b in TRADING_BOARDS})
        self.assertTrue(all("trading_board=" in b.group_description for b in TRADING_BOARDS))

    def test_plan_memberships(self) -> None:
        rows = [
            {"id": 1, "symbol": "600519"},
            {"id": 2, "symbol": "000001"},
            {"id": 3, "symbol": "300750"},
            {"id": 4, "symbol": "999999"},
        ]
        plan, unclassified = plan_memberships(rows)
        self.assertEqual(plan["SSE_MAIN"], [1])
        self.assertEqual(plan["SZSE_MAIN"], [2])
        self.assertEqual(plan["SZSE_GEM"], [3])
        self.assertEqual(plan["SSE_STAR"], [])
        self.assertEqual(unclassified, ["999999"])


class BoardSyncTest(unittest.TestCase):
    """初始化同步（临时库）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_boards_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "boards.db"
        initialize_database(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.groups = SymbolGroupRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)

        self.maotai = self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        self.pingan = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.catl = self.symbols.create_symbol("300750", "宁德时代", "CN", "STOCK")
        self.star = self.symbols.create_symbol("688111", "金山办公", "CN", "STOCK")
        self.bse = self.symbols.create_symbol("920000", "安庆凤", "CN", "STOCK")
        self.unknown = self.symbols.create_symbol("999999", "未知段", "CN", "STOCK")
        self.btc = self.symbols.create_symbol("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO")
        self.klines.bulk_upsert_klines(
            [
                {
                    "symbol_id": self.maotai,
                    "timeframe": "1d",
                    "timestamp": "2026-09-30 00:00:00",
                    "open": 1.0,
                    "high": 2.0,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 100.0,
                    "amount": None,
                }
            ]
        )

    def counts(self) -> tuple[int, int]:
        return len(self.symbols.list_symbols()), self.klines.count_klines(self.maotai, "1d")

    def members_of(self, name: str) -> list[str]:
        group = self.groups.get_group_by_name(name)
        return sorted(str(row["symbol"]) for row in self.groups.list_group_symbols(int(group["id"])))

    def test_dry_run_writes_nothing(self) -> None:
        report = sync_trading_boards(self.groups, self.symbols, dry_run=True)
        self.assertEqual(report.written, 0)
        self.assertEqual(report.groups_created, 6)  # 只统计“将要创建”
        self.assertEqual(self.groups.list_groups_with_counts(), [])
        self.assertEqual(self.groups.get_group_by_name("沪市主板"), None)
        self.assertEqual(self.counts(), (7, 1))

    def test_write_creates_boards_and_universe(self) -> None:
        report = sync_trading_boards(self.groups, self.symbols, dry_run=False)
        self.assertEqual(report.groups_created, 6)
        self.assertEqual(report.unclassified, 1)
        self.assertEqual(report.unclassified_codes, ["999999"])
        self.assertEqual(report.conflicts, [])
        self.assertEqual(report.failed, 0)

        self.assertEqual(self.members_of("沪市主板"), ["600519"])
        self.assertEqual(self.members_of("深市主板"), ["000001"])
        self.assertEqual(self.members_of("创业板"), ["300750"])
        self.assertEqual(self.members_of("科创板"), ["688111"])
        self.assertEqual(self.members_of("北交所"), ["920000"])
        # 沪深主板 = 沪市主板 ∪ 深市主板（仅成员关系，不复制股票数据）
        self.assertEqual(self.members_of("沪深主板"), ["000001", "600519"])
        self.assertEqual(report.per_board[HS_MAIN_UNIVERSE_CODE], 2)
        # 加密货币市场不受影响
        self.assertNotIn("BTCUSDT", self.members_of("沪市主板") + self.members_of("深市主板"))

    def test_each_symbol_belongs_to_at_most_one_board(self) -> None:
        sync_trading_boards(self.groups, self.symbols, dry_run=False)
        per_symbol: dict[str, list[str]] = {}
        for board in TRADING_BOARDS:
            for code in self.members_of(board.label):
                per_symbol.setdefault(code, []).append(board.code)
        self.assertTrue(per_symbol, "应当有成员")
        for code, boards in per_symbol.items():
            with self.subTest(code=code):
                self.assertEqual(len(boards), 1)

    def test_idempotent_second_run_adds_nothing(self) -> None:
        first = sync_trading_boards(self.groups, self.symbols, dry_run=False)
        second = sync_trading_boards(self.groups, self.symbols, dry_run=False)
        self.assertGreater(first.added, 0)
        self.assertEqual(second.added, 0)
        self.assertEqual(second.groups_created, 0)
        self.assertEqual(second.groups_existing, 6)
        self.assertEqual(self.members_of("沪深主板"), ["000001", "600519"])
        self.assertEqual(len(self.groups.list_groups()), 6)

    def test_group_metadata_marks_trading_boards(self) -> None:
        sync_trading_boards(self.groups, self.symbols, dry_run=False)
        sse = self.groups.get_group_by_name("沪市主板")
        self.assertEqual(sse["group_type"], "custom")  # schema 只允许四种 group_type
        self.assertEqual(sse["market"], "CN")
        self.assertIn("trading_board=SSE_MAIN", sse["description"])
        universe = self.groups.get_group_by_name(HS_MAIN_UNIVERSE_LABEL)
        self.assertIn("universe=HS_MAIN", universe["description"])

    def test_existing_groups_and_members_are_never_deleted(self) -> None:
        keeper = self.groups.create_group("我的人工自选", "watchlist")
        self.groups.add_symbol_to_group(keeper, self.btc)

        sync_trading_boards(self.groups, self.symbols, dry_run=False)
        sync_trading_boards(self.groups, self.symbols, dry_run=False)

        self.assertIsNotNone(self.groups.get_group_by_name("我的人工自选"))
        self.assertEqual(self.members_of("我的人工自选"), ["BTCUSDT"])
        self.assertEqual(self.counts(), (7, 1))  # 股票与 K 线都没被动过

    def test_conflicts_are_reported_not_removed(self) -> None:
        sync_trading_boards(self.groups, self.symbols, dry_run=False)
        # 人工制造冲突：把 600519 也加进「深市主板」
        self.groups.add_symbol_to_group(int(self.groups.get_group_by_name("深市主板")["id"]), self.maotai)

        report = sync_trading_boards(self.groups, self.symbols, dry_run=False)
        self.assertEqual(len(report.conflicts), 1)
        self.assertIn("SSE_MAIN", report.conflicts[0])
        # 不自动删除：两边的成员关系都还在
        self.assertEqual(self.members_of("沪市主板"), ["600519"])
        self.assertEqual(self.members_of("深市主板"), ["000001", "600519"])

    def test_report_serialises(self) -> None:
        report = sync_trading_boards(self.groups, self.symbols, dry_run=True)
        payload = report.as_dict()
        self.assertEqual(payload["dry_run"], True)
        self.assertEqual(payload["symbols_seen"], 6)  # 只统计 CN
        self.assertEqual(payload["written"], 0)
        self.assertEqual(set(payload["per_board"]), set(BOARD_CODES) | {HS_MAIN_UNIVERSE_CODE})


if __name__ == "__main__":
    unittest.main()
