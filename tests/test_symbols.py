"""Stage 03：symbols（标的基础信息）测试。

覆盖内容：
    1. 建表                     初始化数据库后 symbols 存在
    2. 创建                     能创建并取回标的
    3. 查询                     按 market + symbol 查询
    4. 唯一性                   重复的 (market, symbol) 必须被**数据库**拒绝
    5. 必填字段                 NULL / 缺字段必须被数据库拒绝
    6. 默认值                   is_active 默认 1
    7. 更新                     name / asset_type / is_active
    8. 逻辑停用                 is_active = 0 后：全部查询仍可见，active 查询不可见
    9. 查询全部                 多市场标的查询结果正确
    10. 持久化                  重开连接后数据仍在
    11. SQL 约束                UNIQUE / NOT NULL / CHECK 真实生效

重要：
    - 所有测试都在临时目录里建库，**不会向 data/strategy_engine.db 写任何数据**
    - 唯一性等约束的验证使用真实 SQL 语句，而不是只在 Python 层判断

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import (  # noqa: E402  (测试需要先保证项目根目录在 sys.path 中)
    DEFAULT_DB_PATH,
    SymbolRepository,
    initialize_database,
)
from database.models import SCHEMA_VERSION  # noqa: E402

SYMBOLS_TABLE = "symbols"

# 统一时间格式：YYYY-MM-DD HH:MM:SS
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

# 注意：这些数据只写进临时数据库，绝不写进正式库
SAMPLE = [
    # (symbol, name, market, asset_type)
    ("000001", "平安银行", "CN", "STOCK"),
    ("600519", "贵州茅台", "CN", "STOCK"),
    ("00001", "长和", "HK", "STOCK"),
    ("AAPL", "Apple Inc.", "US", "STOCK"),
    ("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO"),
]


class SymbolTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_symbols_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "symbols_test.db"
        initialize_database(self.db_path)
        self.repo = SymbolRepository(self.db_path)

    def create_sample(self, symbol: str, name: str, market: str, asset_type: str) -> int:
        return self.repo.create_symbol(symbol, name, market, asset_type)


class SymbolsSchemaTest(SymbolTestCase):
    """1 / 11：表结构与约束。"""

    def test_01_symbols_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.repo.table_exists(SYMBOLS_TABLE))
        # 数据库随阶段演进会增加业务表（Stage 04 起还有 klines），
        # 这里只断言 symbols 存在；"没有多余/越界表" 由 test_database.py 负责
        self.assertIn(SYMBOLS_TABLE, self.repo.list_tables())

    def test_02_symbols_table_columns(self) -> None:
        columns = {
            row["name"]: row for row in self.repo.query_all(f"PRAGMA table_info({SYMBOLS_TABLE})")
        }
        expected = [
            "id",
            "symbol",
            "name",
            "market",
            "asset_type",
            "is_active",
            "created_at",
            "updated_at",
        ]
        self.assertEqual(list(columns.keys()), expected)

        self.assertEqual(columns["id"]["pk"], 1, "id 应是主键")
        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")

        for field in ("symbol", "name", "market", "asset_type", "is_active", "created_at", "updated_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")

        self.assertEqual(columns["is_active"]["dflt_value"], "1", "is_active 默认值应为 1")
        self.assertIsNotNone(columns["created_at"]["dflt_value"])
        self.assertIsNotNone(columns["updated_at"]["dflt_value"])

    def test_03_unique_index_covers_market_and_symbol(self) -> None:
        indexes = [
            row for row in self.repo.query_all(f"PRAGMA index_list({SYMBOLS_TABLE})")
            if row["unique"] == 1
        ]
        self.assertTrue(indexes, "应存在唯一索引")

        index_columns = [
            row["name"]
            for row in self.repo.query_all(f"PRAGMA index_info({indexes[0]['name']})")
        ]
        self.assertEqual(index_columns, ["market", "symbol"], "唯一约束应覆盖 (market, symbol)")

    def test_04_schema_version_is_recorded(self) -> None:
        # 断言数据库里的 user_version 与代码里的 SCHEMA_VERSION 一致（防止漏初始化/漏迁移）
        version = self.repo.query_one("PRAGMA user_version")[0]
        self.assertEqual(version, SCHEMA_VERSION)


class SymbolCreateTest(SymbolTestCase):
    """2 / 4 / 5 / 6：创建、唯一性、必填字段、默认值。"""

    def test_01_create_symbol_returns_id_and_row(self) -> None:
        symbol_id = self.create_sample("600519", "贵州茅台", "CN", "STOCK")
        self.assertIsInstance(symbol_id, int)
        self.assertGreater(symbol_id, 0)

        row = self.repo.get_symbol("CN", "600519")
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], symbol_id)
        self.assertEqual(row["symbol"], "600519")
        self.assertEqual(row["name"], "贵州茅台")
        self.assertEqual(row["market"], "CN")
        self.assertEqual(row["asset_type"], "STOCK")
        self.assertEqual(row["is_active"], 1)

    def test_02_create_symbol_trims_whitespace(self) -> None:
        symbol_id = self.create_sample("  AAPL  ", " Apple Inc. ", "US", "STOCK")
        row = self.repo.get_symbol_by_id(symbol_id)
        self.assertEqual(row["symbol"], "AAPL")
        self.assertEqual(row["name"], "Apple Inc.")
        self.assertEqual(row["market"], "US")

    def test_03_duplicate_market_symbol_rejected_by_database(self) -> None:
        self.create_sample("000001", "平安银行", "CN", "STOCK")

        with self.assertRaises(sqlite3.IntegrityError):
            self.create_sample("000001", "重复的平安银行", "CN", "STOCK")

        rows = self.repo.query_all(f"SELECT * FROM {SYMBOLS_TABLE} WHERE symbol = '000001'")
        self.assertEqual(len(rows), 1, "重复创建后不应产生第二条记录")

    def test_04_unique_constraint_enforced_by_raw_sql_too(self) -> None:
        # 绕过 Repository 直接写 SQL，证明是数据库在拒绝，而不是 Python 在判断
        self.repo.execute(
            f"INSERT INTO {SYMBOLS_TABLE} (symbol, name, market, asset_type) "
            "VALUES ('BTCUSDT', 'Bitcoin', 'CRYPTO', 'CRYPTO')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.execute(
                f"INSERT INTO {SYMBOLS_TABLE} (symbol, name, market, asset_type) "
                "VALUES ('BTCUSDT', 'Bitcoin again', 'CRYPTO', 'CRYPTO')"
            )

    def test_05_same_symbol_in_different_markets_is_allowed(self) -> None:
        # 设计选择：身份 = market + symbol，所以不同市场可以同名
        self.create_sample("SOL", "Solana", "CRYPTO", "CRYPTO")
        self.create_sample("SOL", "Solar Company", "US", "STOCK")

        self.assertIsNotNone(self.repo.get_symbol("CRYPTO", "SOL"))
        self.assertIsNotNone(self.repo.get_symbol("US", "SOL"))
        self.assertEqual(len(self.repo.list_symbols()), 2)

    def test_06_is_active_defaults_to_1(self) -> None:
        symbol_id = self.create_sample("ETHUSDT", "Ethereum", "CRYPTO", "CRYPTO")
        self.assertEqual(self.repo.get_symbol_by_id(symbol_id)["is_active"], 1)

        # 数据库默认值也必须生效（完全不写 is_active 列）
        self.repo.execute(
            f"INSERT INTO {SYMBOLS_TABLE} (symbol, name, market, asset_type) "
            "VALUES ('DEFAULT1', '默认值测试', 'CN', 'STOCK')"
        )
        row = self.repo.get_symbol("CN", "DEFAULT1")
        self.assertEqual(row["is_active"], 1, "数据库默认值应为 1")

    def test_07_missing_required_fields_rejected_by_database(self) -> None:
        table = SYMBOLS_TABLE
        cases = {
            "缺少 symbol": f"INSERT INTO {table} (name, market, asset_type) VALUES ('n', 'CN', 'STOCK')",
            "缺少 name": f"INSERT INTO {table} (symbol, market, asset_type) VALUES ('X', 'CN', 'STOCK')",
            "缺少 market": f"INSERT INTO {table} (symbol, name, asset_type) VALUES ('X', 'n', 'STOCK')",
            "缺少 asset_type": f"INSERT INTO {table} (symbol, name, market) VALUES ('X', 'n', 'CN')",
            "symbol 为 NULL": f"INSERT INTO {table} (symbol, name, market, asset_type) VALUES (NULL, 'n', 'CN', 'STOCK')",
            "name 为 NULL": f"INSERT INTO {table} (symbol, name, market, asset_type) VALUES ('X', NULL, 'CN', 'STOCK')",
            "market 为 NULL": f"INSERT INTO {table} (symbol, name, market, asset_type) VALUES ('X', 'n', NULL, 'STOCK')",
            "asset_type 为 NULL": f"INSERT INTO {table} (symbol, name, market, asset_type) VALUES ('X', 'n', 'CN', NULL)",
            "is_active 为 NULL": f"INSERT INTO {table} (symbol, name, market, asset_type, is_active) VALUES ('X', 'n', 'CN', 'STOCK', NULL)",
        }
        for label, sql in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.repo.execute(sql)

    def test_08_is_active_check_constraint(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.execute(
                f"INSERT INTO {SYMBOLS_TABLE} (symbol, name, market, asset_type, is_active) "
                "VALUES ('BAD', 'n', 'CN', 'STOCK', 2)"
            )

    def test_09_python_validation_rejects_empty_text(self) -> None:
        # Python 层只做"非空/类型"这类快速校验；唯一性仍由数据库负责
        with self.assertRaises(ValueError):
            self.repo.create_symbol("", "name", "CN", "STOCK")
        with self.assertRaises(ValueError):
            self.repo.create_symbol("X", "   ", "CN", "STOCK")
        with self.assertRaises(ValueError):
            self.repo.create_symbol("X", "name", None, "STOCK")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.repo.create_symbol("X", "name", "CN", "STOCK", is_active=7)

    def test_10_market_and_asset_type_are_not_hardcoded(self) -> None:
        # 不应把市场/资产类型写死：新的市场与资产类型也应能写入
        symbol_id = self.repo.create_symbol("7203", "Toyota", "JP", "STOCK")
        self.assertIsNotNone(self.repo.get_symbol_by_id(symbol_id))
        symbol_id = self.repo.create_symbol("XAUUSD", "Gold Spot", "GLOBAL", "COMMODITY")
        self.assertIsNotNone(self.repo.get_symbol_by_id(symbol_id))


class SymbolQueryTest(SymbolTestCase):
    """3 / 9：查询。"""

    def setUp(self) -> None:
        super().setUp()
        for args in SAMPLE:
            self.create_sample(*args)

    def test_01_get_symbol_by_market_and_symbol(self) -> None:
        row = self.repo.get_symbol("HK", "00001")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "长和")

        self.assertIsNone(self.repo.get_symbol("CN", "00001"), "市场不匹配时不应命中")
        self.assertIsNone(self.repo.get_symbol("US", "NOT_EXIST"))

    def test_02_list_symbols_returns_all_markets_sorted(self) -> None:
        rows = self.repo.list_symbols()
        self.assertEqual(len(rows), len(SAMPLE))
        self.assertEqual(
            [(r["market"], r["symbol"]) for r in rows],
            [("CN", "000001"), ("CN", "600519"), ("CRYPTO", "BTCUSDT"), ("HK", "00001"), ("US", "AAPL")],
        )

    def test_03_list_symbols_filtered_by_market(self) -> None:
        rows = self.repo.list_symbols(market="CN")
        self.assertEqual([r["symbol"] for r in rows], ["000001", "600519"])

    def test_04_list_active_symbols_returns_all_when_all_active(self) -> None:
        self.assertEqual(len(self.repo.list_active_symbols()), len(SAMPLE))
        self.assertEqual(len(self.repo.list_active_symbols(market="CN")), 2)

    def test_05_get_symbol_by_id(self) -> None:
        row = self.repo.get_symbol("US", "AAPL")
        self.assertEqual(self.repo.get_symbol_by_id(row["id"])["name"], "Apple Inc.")
        self.assertIsNone(self.repo.get_symbol_by_id(999999))


class SymbolUpdateTest(SymbolTestCase):
    """7 / 8：更新与逻辑停用。"""

    def setUp(self) -> None:
        super().setUp()
        self.create_sample("600519", "贵州茅台", "CN", "STOCK")

    def test_01_update_name(self) -> None:
        self.assertTrue(self.repo.update_symbol("CN", "600519", name="贵州茅台股份有限公司"))
        self.assertEqual(self.repo.get_symbol("CN", "600519")["name"], "贵州茅台股份有限公司")

    def test_02_update_asset_type(self) -> None:
        self.assertTrue(self.repo.update_symbol("CN", "600519", asset_type="ETF"))
        self.assertEqual(self.repo.get_symbol("CN", "600519")["asset_type"], "ETF")

    def test_03_update_is_active(self) -> None:
        self.assertTrue(self.repo.update_symbol("CN", "600519", is_active=0))
        self.assertEqual(self.repo.get_symbol("CN", "600519")["is_active"], 0)
        self.assertTrue(self.repo.update_symbol("CN", "600519", is_active=1))
        self.assertEqual(self.repo.get_symbol("CN", "600519")["is_active"], 1)

    def test_04_update_multiple_fields_at_once(self) -> None:
        self.assertTrue(
            self.repo.update_symbol("CN", "600519", name="新名字", asset_type="ETF", is_active=0)
        )
        row = self.repo.get_symbol("CN", "600519")
        self.assertEqual(row["name"], "新名字")
        self.assertEqual(row["asset_type"], "ETF")
        self.assertEqual(row["is_active"], 0)

    def test_05_update_unknown_symbol_returns_false(self) -> None:
        self.assertFalse(self.repo.update_symbol("US", "NOT_EXIST", name="x"))
        self.assertFalse(self.repo.deactivate_symbol("US", "NOT_EXIST"))

    def test_06_update_requires_at_least_one_field(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.update_symbol("CN", "600519")

    def test_07_update_refreshes_updated_at_and_keeps_created_at(self) -> None:
        before = self.repo.get_symbol("CN", "600519")
        self.repo.update_symbol("CN", "600519", name="改名")

        after = self.repo.get_symbol("CN", "600519")
        self.assertEqual(after["created_at"], before["created_at"], "created_at 不应被修改")
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])
        self.assertRegex(after["created_at"], TIMESTAMP_PATTERN)
        self.assertRegex(after["updated_at"], TIMESTAMP_PATTERN)

    def test_08_deactivate_is_logical_not_physical(self) -> None:
        self.create_sample("000001", "平安银行", "CN", "STOCK")

        self.assertTrue(self.repo.deactivate_symbol("CN", "000001"))

        # 记录仍然存在
        row = self.repo.get_symbol("CN", "000001")
        self.assertIsNotNone(row, "逻辑停用不应删除记录")
        self.assertEqual(row["is_active"], 0)

        # 全部查询仍能找到，active 查询不能找到
        all_symbols = self.repo.list_symbols()
        self.assertIn(("CN", "000001"), [(r["market"], r["symbol"]) for r in all_symbols])

        active = self.repo.list_active_symbols()
        self.assertNotIn(("CN", "000001"), [(r["market"], r["symbol"]) for r in active])
        self.assertEqual(len(active), 1, "只剩 600519 处于启用状态")

    def test_09_repository_has_no_physical_delete(self) -> None:
        # 设计选择：不提供物理删除，避免 K线/交易/回测数据出现孤儿引用
        for name in ("delete_symbol", "remove_symbol", "delete", "purge_symbol"):
            with self.subTest(method=name):
                self.assertFalse(hasattr(self.repo, name), f"不应提供 {name}")


class SymbolPersistenceTest(SymbolTestCase):
    """10：持久化 + 不污染正式数据库。"""

    def test_01_data_persists_after_reconnect(self) -> None:
        for args in SAMPLE:
            self.create_sample(*args)
        self.repo.deactivate_symbol("CN", "000001")

        # 完全丢弃当前 repository，重新建立连接（模拟程序重启）
        del self.repo
        reopened = SymbolRepository(self.db_path)

        rows = reopened.list_symbols()
        self.assertEqual(len(rows), len(SAMPLE))
        self.assertEqual(reopened.get_symbol("CN", "000001")["is_active"], 0)
        self.assertEqual(len(reopened.list_active_symbols()), len(SAMPLE) - 1)

    def test_02_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.repo.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.repo.db_path, self.db_path)

    def test_03_timestamps_use_unified_format(self) -> None:
        symbol_id = self.create_sample("AAPL", "Apple Inc.", "US", "STOCK")
        row = self.repo.get_symbol_by_id(symbol_id)
        self.assertRegex(row["created_at"], TIMESTAMP_PATTERN)
        self.assertRegex(row["updated_at"], TIMESTAMP_PATTERN)
        self.assertLessEqual(row["created_at"], row["updated_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
