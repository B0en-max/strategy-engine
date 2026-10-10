"""Stage 20 Step 1：市场分类数据层测试。

覆盖：
    - 新表 symbol_groups / symbol_group_members 存在，schema 版本 = 9，迁移幂等
    - group 创建 / 查询 / 更新 / 停用 / 唯一性与 group_type 校验
    - 成员添加（幂等）/ 重复拒绝（数据库层）/ 移除 / 查询 / 反向查询
    - MarketService：展示代码（A 股补后缀）、最新行情（缺失一律 None，不伪造）
    - KlineRepository.list_recent_klines（只读，不改既有语义）
    - 旧 symbols / klines 数据不受影响
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import (
    KlineRepository,
    SymbolGroupRepository,
    SymbolRepository,
    database_info,
    initialize_database,
)
from database import models
from market import MarketService, display_code

GROUPS_TABLE = models.SYMBOL_GROUPS_TABLE
MEMBERS_TABLE = models.SYMBOL_GROUP_MEMBERS_TABLE
TIMESTAMP_RE = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"


class MarketDataTestCase(unittest.TestCase):
    """公共夹具：临时库 + 4 个标的（含加密）+ K 线 + 1 个集合。"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_market_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "market.db"
        initialize_database(self.db_path)

        self.groups = SymbolGroupRepository(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)
        self.service = MarketService(self.db_path)

        self.maotai = self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        self.pingan = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.catl = self.symbols.create_symbol("300750", "宁德时代", "CN", "STOCK")
        self.btc = self.symbols.create_symbol("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO")

        # 600519：3 根日线，amount 为 NULL（与真实新浪数据一致）
        self._bulk_klines(self.maotai, "1d", [("2026-09-28 00:00:00", 100.0), ("2026-09-29 00:00:00", 105.0), ("2026-09-30 00:00:00", 110.0)])
        # 300750：2 根日线，amount 有值
        self._bulk_klines(self.catl, "1d", [("2026-09-29 00:00:00", 200.0), ("2026-09-30 00:00:00", 210.0)], amount=12345.0)
        # 000001：只有 1 根 15m（用于「不足两根 → 涨跌为 None」）
        self._bulk_klines(self.pingan, "15m", [("2026-09-30 14:45:00", 12.5)])

        self.watchlist = self.groups.create_group("我的自选", "watchlist")
        self.semi = self.groups.create_group("半导体", "industry", market="CN")

    def _bulk_klines(self, symbol_id, timeframe, series, amount=None):
        self.klines.bulk_upsert_klines(
            [
                {
                    "symbol_id": symbol_id,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "open": close - 1.0,
                    "high": close + 2.0,
                    "low": close - 2.0,
                    "close": close,
                    "volume": 1000.0,
                    "amount": amount,
                }
                for timestamp, close in series
            ]
        )


class SchemaTest(MarketDataTestCase):
    def test_01_new_tables_exist_and_are_registered(self):
        tables = database_info(self.db_path)["tables"]
        self.assertIn(GROUPS_TABLE, tables)
        self.assertIn(MEMBERS_TABLE, tables)
        self.assertIn(GROUPS_TABLE, models.BUSINESS_TABLES)
        self.assertIn(MEMBERS_TABLE, models.BUSINESS_TABLES)

    def test_02_schema_version_is_9(self):
        self.assertEqual(models.SCHEMA_VERSION, 9)
        self.assertEqual(database_info(self.db_path)["user_version"], 9)

    def test_03_group_table_structure_and_constraints(self):
        columns = {
            row["name"]: row for row in self.groups.query_all(f"PRAGMA table_info({GROUPS_TABLE})")
        }
        self.assertEqual(
            list(columns.keys()),
            [
                "id",
                "name",
                "group_type",
                "market",
                "description",
                "is_active",
                "created_at",
                "updated_at",
            ],
        )
        self.assertEqual(columns["name"]["notnull"], 1)
        self.assertEqual(columns["group_type"]["notnull"], 1)
        self.assertEqual(columns["market"]["notnull"], 0, "market 允许 NULL（跨市场集合）")
        self.assertEqual(columns["description"]["notnull"], 0)
        self.assertEqual(columns["is_active"]["notnull"], 1)

        sql = self.groups.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (GROUPS_TABLE,)
        )["sql"]
        upper = sql.upper()
        self.assertIn("UQ_SYMBOL_GROUPS_NAME", upper)
        self.assertIn("CK_SYMBOL_GROUPS_GROUP_TYPE", upper)
        for group_type in models.GROUP_TYPES:
            self.assertIn(f"'{group_type}'", sql)

    def test_04_members_table_structure_and_foreign_keys(self):
        columns = [row["name"] for row in self.groups.query_all(f"PRAGMA table_info({MEMBERS_TABLE})")]
        self.assertEqual(list(columns), ["id", "group_id", "symbol_id", "created_at"])
        foreign_keys = {
            row["from"]: row["table"]
            for row in self.groups.query_all(f"PRAGMA foreign_key_list({MEMBERS_TABLE})")
        }
        self.assertEqual(foreign_keys.get("group_id"), GROUPS_TABLE)
        self.assertEqual(foreign_keys.get("symbol_id"), models.SYMBOLS_TABLE)
        sql = self.groups.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (MEMBERS_TABLE,)
        )["sql"].upper()
        self.assertIn("UNIQUE", sql)

    def test_05_reinitialize_is_idempotent_and_keeps_existing_data(self):
        symbol_row = dict(self.symbols.get_symbol_by_id(self.maotai))
        klines_before = [dict(row) for row in self.klines.list_klines(self.maotai, "1d")]
        size_before = self.db_path.stat().st_size

        initialize_database(self.db_path)
        initialize_database(self.db_path)

        self.assertEqual(self.db_path.stat().st_size, size_before, "重复初始化不应改动数据库文件")
        self.assertEqual(dict(self.symbols.get_symbol_by_id(self.maotai)), symbol_row)
        self.assertEqual(
            [dict(row) for row in self.klines.list_klines(self.maotai, "1d")], klines_before
        )
        self.assertEqual(self.groups.count_groups(), 2)
        self.assertEqual(database_info(self.db_path)["user_version"], 9)


class GroupTest(MarketDataTestCase):
    def test_01_create_and_read_back(self):
        group = self.groups.get_group(self.semi)
        self.assertEqual(group["name"], "半导体")
        self.assertEqual(group["group_type"], "industry")
        self.assertEqual(group["market"], "CN")
        self.assertEqual(group["is_active"], 1)
        self.assertIsNone(group["description"])
        self.assertRegex(group["created_at"], TIMESTAMP_RE)
        self.assertRegex(group["updated_at"], TIMESTAMP_RE)
        self.assertIsNone(self.groups.get_group(9999))

    def test_02_name_is_unique(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.groups.create_group("半导体", "industry")
        self.assertEqual(self.groups.get_group_by_name("半导体")["id"], self.semi)

    def test_03_group_type_is_validated(self):
        for group_type in models.GROUP_TYPES:
            group_id = self.groups.create_group(f"集合-{group_type}", group_type)
            self.assertEqual(self.groups.get_group(group_id)["group_type"], group_type)

        with self.assertRaises(ValueError):
            self.groups.create_group("非法类型", "sector")
        with self.assertRaises(ValueError):
            self.groups.list_groups(group_type="nope")
        # 数据库层同样拒绝（CHECK 约束）
        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO {GROUPS_TABLE} (name, group_type, is_active, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("raw-非法", "sector", 1, models.utc_now_str(), models.utc_now_str()),
                )

    def test_04_list_and_count_with_filters(self):
        self.groups.create_group("自选2", "watchlist", market="CRYPTO")
        self.groups.create_group("已停用", "custom")
        self.groups.deactivate_group(self.groups.get_group_by_name("已停用")["id"])

        self.assertEqual(self.groups.count_groups(), 4)
        self.assertEqual(
            [group["name"] for group in self.groups.list_groups(group_type="watchlist")],
            ["我的自选", "自选2"],
        )
        self.assertEqual([g["name"] for g in self.groups.list_groups(market="CN")], ["半导体"])
        self.assertEqual(self.groups.count_groups(active_only=True), 3)
        # 排序：group_type 升序（industry < watchlist）→ name 升序（SQLite 按字节序比较中文）
        self.assertEqual(
            [g["name"] for g in self.groups.list_groups(active_only=True)],
            ["半导体", "我的自选", "自选2"],
        )

    def test_05_update_group_partial_fields(self):
        self.assertTrue(self.groups.update_group(self.semi, description="A 股半导体"))
        self.assertEqual(self.groups.get_group(self.semi)["description"], "A 股半导体")

        self.assertTrue(self.groups.update_group(self.semi, name="半导体板块", market=None))
        updated = self.groups.get_group(self.semi)
        self.assertEqual(updated["name"], "半导体板块")
        self.assertIsNone(updated["market"], "显式传 None 应清空 market")

        # 未传的字段保持不变
        self.groups.update_group(self.semi, description="改描述")
        self.assertEqual(self.groups.get_group(self.semi)["name"], "半导体板块")

        with self.assertRaises(ValueError):
            self.groups.update_group(self.semi)
        self.assertFalse(self.groups.update_group(9999, description="不存在"))

    def test_06_deactivate_keeps_group_and_members(self):
        self.groups.add_symbol_to_group(self.semi, self.catl)
        self.assertTrue(self.groups.deactivate_group(self.semi))
        group = self.groups.get_group(self.semi)
        self.assertEqual(group["is_active"], 0)
        self.assertEqual(self.groups.count_group_symbols(self.semi), 1, "停用不删除成员关系")
        self.assertEqual(self.groups.count_groups(active_only=True), 1)


class MembershipTest(MarketDataTestCase):
    def test_01_add_and_list_group_symbols(self):
        member_id = self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        self.assertIsInstance(member_id, int)
        self.groups.add_symbol_to_group(self.watchlist, self.btc)

        rows = self.groups.list_group_symbols(self.watchlist)
        self.assertEqual([row["symbol"] for row in rows], ["600519", "BTCUSDT"])
        self.assertEqual(self.groups.count_group_symbols(self.watchlist), 2)
        self.assertEqual(rows[0]["name"], "贵州茅台")
        self.assertEqual([g["name"] for g in self.groups.list_groups_of_symbol(self.maotai)], ["我的自选"])

    def test_02_duplicate_member_is_idempotent_at_repository_and_rejected_by_db(self):
        first = self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        second = self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        self.assertEqual(first, second, "重复加入应返回既有成员 id，不重复插入")
        self.assertEqual(self.groups.count_group_symbols(self.watchlist), 1)

        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO {MEMBERS_TABLE} (group_id, symbol_id, created_at) VALUES (?, ?, ?)",
                    (self.watchlist, self.maotai, models.utc_now_str()),
                )

    def test_03_unknown_group_or_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            self.groups.add_symbol_to_group(9999, self.maotai)
        with self.assertRaises(ValueError):
            self.groups.add_symbol_to_group(self.watchlist, 9999)

    def test_04_remove_member(self):
        self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        self.assertTrue(self.groups.remove_symbol_from_group(self.watchlist, self.maotai))
        self.assertFalse(self.groups.remove_symbol_from_group(self.watchlist, self.maotai))
        self.assertEqual(self.groups.list_group_symbols(self.watchlist), [])

    def test_05_active_only_filter_and_empty_group(self):
        inactive = self.symbols.create_symbol("688111", "金山办公", "CN", "STOCK")
        self.symbols.deactivate_symbol("CN", "688111")
        self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        self.groups.add_symbol_to_group(self.watchlist, inactive)

        self.assertEqual(len(self.groups.list_group_symbols(self.watchlist)), 2)
        self.assertEqual(
            [row["symbol"] for row in self.groups.list_group_symbols(self.watchlist, active_only=True)],
            ["600519"],
        )
        self.assertEqual(self.groups.list_group_symbols(9999), [])

    def test_06_membership_does_not_touch_symbols_or_klines(self):
        symbols_before = [dict(row) for row in self.symbols.list_symbols()]
        klines_before = self.klines.count_klines(self.maotai, "1d")
        self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        self.groups.remove_symbol_from_group(self.watchlist, self.maotai)
        self.assertEqual([dict(row) for row in self.symbols.list_symbols()], symbols_before)
        self.assertEqual(self.klines.count_klines(self.maotai, "1d"), klines_before)


class DisplayCodeTest(MarketDataTestCase):
    def test_01_cn_suffix_by_rules(self):
        cases = [
            ("CN", "600519", "600519.SH"),
            ("CN", "688111", "688111.SH"),
            ("CN", "900901", "900901.SH"),
            ("CN", "000001", "000001.SZ"),
            ("CN", "300750", "300750.SZ"),
            ("CN", "200011", "200011.SZ"),
            ("CN", "830799", "830799.BJ"),
            ("CN", "920001", "920001.BJ"),
            ("cn", "600519", "600519.SH"),
        ]
        for market, symbol, expected in cases:
            with self.subTest(symbol=symbol):
                self.assertEqual(display_code(market, symbol), expected)

    def test_02_other_markets_and_edge_cases_pass_through(self):
        cases = [
            ("CRYPTO", "BTCUSDT", "BTCUSDT"),
            ("CN", "600519.SH", "600519.SH"),
            ("CRYPTO", "ETH-USDT", "ETH-USDT"),
            (None, "BTCUSDT", "BTCUSDT"),
            ("CN", "12345", "12345"),
            ("CN", "", ""),
        ]
        for market, symbol, expected in cases:
            with self.subTest(symbol=symbol):
                self.assertEqual(display_code(market, symbol), expected)
                self.assertEqual(MarketService.display_code(market, symbol), expected)

    def test_03_symbol_view_adds_display_code(self):
        view = self.service.symbol_view(self.symbols.get_symbol_by_id(self.maotai))
        self.assertEqual(view["display_code"], "600519.SH")
        self.assertEqual(view["symbol"], "600519")
        self.assertEqual(view["name"], "贵州茅台")
        self.assertEqual(view["market"], "CN")
        crypto_view = self.service.symbol_view(self.symbols.get_symbol_by_id(self.btc))
        self.assertEqual(crypto_view["display_code"], "BTCUSDT")


class LatestQuoteTest(MarketDataTestCase):
    def test_01_quote_from_two_bars(self):
        quote = self.service.latest_quote(self.catl, "1d")
        self.assertEqual(quote["timestamp"], "2026-09-30 00:00:00")
        self.assertEqual(quote["close"], 210.0)
        self.assertEqual(quote["previous_close"], 200.0)
        self.assertAlmostEqual(quote["change"], 10.0)
        self.assertAlmostEqual(quote["change_percent"], 5.0)
        self.assertEqual(quote["volume"], 1000.0)
        self.assertEqual(quote["amount"], 12345.0)

    def test_02_missing_amount_is_none_not_faked(self):
        quote = self.service.latest_quote(self.maotai, "1d")
        self.assertEqual(quote["close"], 110.0)
        self.assertAlmostEqual(quote["change"], 5.0)
        self.assertIsNone(quote["amount"], "amount 为 NULL 时必须返回 None，不补 0")
        self.assertEqual(quote["volume"], 1000.0)

    def test_03_single_bar_has_no_change(self):
        quote = self.service.latest_quote(self.pingan, "15m")
        self.assertEqual(quote["close"], 12.5)
        self.assertIsNone(quote["previous_close"])
        self.assertIsNone(quote["change"])
        self.assertIsNone(quote["change_percent"])

    def test_04_symbol_without_klines_returns_all_none(self):
        quote = self.service.latest_quote(self.btc, "1d")
        self.assertEqual(quote["symbol_id"], self.btc)
        for field in ("timestamp", "close", "previous_close", "change", "change_percent", "volume", "amount"):
            self.assertIsNone(quote[field], f"{field} 应为 None")

    def test_05_list_symbols_with_quotes_and_filters(self):
        self.groups.add_symbol_to_group(self.watchlist, self.maotai)
        all_rows = self.service.list_symbols_with_quotes(timeframe="1d")
        self.assertEqual(len(all_rows), 4)
        self.assertTrue(all("quote" in row and "display_code" in row for row in all_rows))

        cn_rows = self.service.list_symbols_with_quotes(market="CN", timeframe="1d")
        self.assertEqual(sorted(row["symbol"] for row in cn_rows), ["000001", "300750", "600519"])

        group_rows = self.service.list_symbols_with_quotes(group_id=self.watchlist, timeframe="1d")
        self.assertEqual([row["symbol"] for row in group_rows], ["600519"])
        self.assertEqual(group_rows[0]["display_code"], "600519.SH")
        self.assertEqual(group_rows[0]["quote"]["close"], 110.0)

        self.assertEqual(self.service.list_symbols_with_quotes(group_id=9999), [])


class RecentKlinesTest(MarketDataTestCase):
    def test_01_returns_recent_n_in_ascending_order(self):
        rows = self.klines.list_recent_klines(self.maotai, "1d", 2)
        self.assertEqual([row["timestamp"] for row in rows], ["2026-09-29 00:00:00", "2026-09-30 00:00:00"])
        self.assertEqual(self.klines.list_recent_klines(self.maotai, "1d", 1)[0]["close"], 110.0)
        self.assertEqual(self.klines.list_recent_klines(self.maotai, "1d", 10)[0]["close"], 100.0)

    def test_02_same_semantics_as_list_klines_limit(self):
        recent = self.klines.list_recent_klines(self.maotai, "1d", 2)
        limited = self.klines.list_klines(self.maotai, "1d", limit=2)
        self.assertEqual([dict(row) for row in recent], [dict(row) for row in limited])

    def test_03_invalid_count_is_rejected(self):
        for count in (0, -1):
            with self.assertRaises(ValueError):
                self.klines.list_recent_klines(self.maotai, "1d", count)


if __name__ == "__main__":
    unittest.main()
