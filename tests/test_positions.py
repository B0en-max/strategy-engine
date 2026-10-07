"""Stage 08：positions（当前持仓）测试。

覆盖（按要求保持精简）：
    建表与约束  表、字段、类型、NOT NULL、默认值、UNIQUE、外键、索引
    创建/查询/更新
    symbol 唯一性  同一 symbol 只能有一条持仓（数据库 UNIQUE 强制）
    外键        symbol_id 必填且必须存在
    持久化      重开连接后数据仍在
    基本列表查询
    隔离        全部使用临时数据库，不污染 data/strategy_engine.db

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
    PositionRepository,
    SymbolRepository,
    initialize_database,
)
from database.models import SCHEMA_VERSION  # noqa: E402

POSITIONS_TABLE = "positions"
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

EXPECTED_COLUMNS = ["id", "symbol_id", "quantity", "avg_price", "updated_at"]


class PositionTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库，并预置标的。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_positions_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "positions_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.positions = PositionRepository(self.db_path)

        self.symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.other_symbol_id = self.symbols.create_symbol("AAPL", "Apple Inc.", "US", "STOCK")

    # ------------------------------------------------------------------ 工具
    def create(self, symbol_id: int | None = None, quantity: float = 100.0, avg_price: float = 10.5) -> int:
        return self.positions.create_position(
            self.symbol_id if symbol_id is None else symbol_id, quantity, avg_price
        )

    def raw_insert(self, columns: dict[str, object]) -> int:
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {POSITIONS_TABLE} ({names}) VALUES ({placeholders})"
        return self.positions.execute(sql, tuple(columns.values()))

    def total_rows(self) -> int:
        return self.positions.query_one(f"SELECT COUNT(*) AS n FROM {POSITIONS_TABLE}")["n"]


class PositionSchemaTest(PositionTestCase):
    """建表与约束。"""

    def test_01_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.positions.table_exists(POSITIONS_TABLE))
        self.assertIn(POSITIONS_TABLE, self.positions.list_tables())

    def test_02_columns_types_notnull_defaults_and_primary_key(self) -> None:
        columns = {
            row["name"]: row for row in self.positions.query_all(f"PRAGMA table_info({POSITIONS_TABLE})")
        }
        self.assertEqual(list(columns.keys()), EXPECTED_COLUMNS, "字段应与设计完全一致（不多不少）")

        self.assertEqual(columns["id"]["pk"], 1)
        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["symbol_id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["quantity"]["type"].upper(), "REAL")
        self.assertEqual(columns["avg_price"]["type"].upper(), "REAL")
        self.assertEqual(columns["updated_at"]["type"].upper(), "TEXT")

        for field in EXPECTED_COLUMNS[1:]:
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")
        self.assertIsNotNone(columns["updated_at"]["dflt_value"])

    def test_03_unique_constraint_on_symbol_id(self) -> None:
        indexes = self.positions.query_all(f"PRAGMA index_list({POSITIONS_TABLE})")
        self.assertEqual(len(indexes), 1, f"应只有一个索引（来自 UNIQUE），实际: {[r['name'] for r in indexes]}")
        self.assertEqual(indexes[0]["unique"], 1)
        self.assertEqual(indexes[0]["origin"], "u")

        index_columns = [
            row["name"] for row in self.positions.query_all(f"PRAGMA index_info({indexes[0]['name']})")
        ]
        self.assertEqual(index_columns, ["symbol_id"])

    def test_04_foreign_key_is_declared(self) -> None:
        fks = self.positions.query_all(f"PRAGMA foreign_key_list({POSITIONS_TABLE})")
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0]["table"], "symbols")
        self.assertEqual(fks[0]["from"], "symbol_id")
        self.assertEqual(fks[0]["to"], "id")

    def test_05_schema_version(self) -> None:
        self.assertEqual(self.positions.query_one("PRAGMA user_version")[0], SCHEMA_VERSION)


class PositionCreateTest(PositionTestCase):
    """创建 / 查询 / 参数校验。"""

    def test_01_create_and_read_back(self) -> None:
        position_id = self.create(quantity=200.0, avg_price=12.75)
        self.assertGreater(position_id, 0)

        row = self.positions.get_position(position_id)
        self.assertEqual(row["id"], position_id)
        self.assertEqual(row["symbol_id"], self.symbol_id)
        self.assertAlmostEqual(row["quantity"], 200.0)
        self.assertAlmostEqual(row["avg_price"], 12.75)
        self.assertRegex(row["updated_at"], TIMESTAMP_PATTERN)

        by_symbol = self.positions.get_position_by_symbol(self.symbol_id)
        self.assertEqual(by_symbol["id"], position_id)

    def test_02_getters_return_none_when_missing(self) -> None:
        self.assertIsNone(self.positions.get_position(999999))
        self.assertIsNone(self.positions.get_position_by_symbol(self.symbol_id))

    def test_03_python_parameter_validation(self) -> None:
        cases = {
            "symbol_id 非正": {"symbol_id": 0},
            "symbol_id 非整数": {"symbol_id": "1"},
            "quantity 非数字": {"quantity": "100"},
            "avg_price 为 None": {"avg_price": None},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                kwargs = {"symbol_id": self.symbol_id, "quantity": 100.0, "avg_price": 10.0}
                kwargs.update(overrides)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    self.positions.create_position(
                        kwargs["symbol_id"], kwargs["quantity"], kwargs["avg_price"]
                    )
        self.assertEqual(self.total_rows(), 0)

    def test_04_quantity_sign_is_not_restricted(self) -> None:
        # 0 表示已清仓，负数表示做空，都属于本数据层允许的表示
        zero_id = self.create(quantity=0.0)
        short_id = self.create(self.other_symbol_id, quantity=-50.0, avg_price=100.0)
        self.assertAlmostEqual(self.positions.get_position(zero_id)["quantity"], 0.0)
        self.assertAlmostEqual(self.positions.get_position(short_id)["quantity"], -50.0)


class PositionUniqueTest(PositionTestCase):
    """同一 symbol 只能有一条持仓。"""

    def test_01_duplicate_symbol_is_rejected(self) -> None:
        original_id = self.create(quantity=100.0, avg_price=10.0)

        with self.assertRaises(sqlite3.IntegrityError):
            self.create(quantity=999.0, avg_price=999.0)
        # 绕过 Repository 直接写 SQL 同样被拒（证明约束来自数据库）
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"symbol_id": self.symbol_id, "quantity": 1.0, "avg_price": 1.0})

        self.assertEqual(self.total_rows(), 1, "同一 symbol 不应出现第二条持仓")
        row = self.positions.get_position(original_id)
        self.assertAlmostEqual(row["quantity"], 100.0, "原有数据不应被失败的新建改动")

    def test_02_different_symbols_are_allowed_and_update_keeps_single_row(self) -> None:
        first = self.create()
        second = self.create(self.other_symbol_id, quantity=5.0, avg_price=200.0)
        self.assertNotEqual(first, second)
        self.assertEqual(self.total_rows(), 2)

        self.positions.update_position(first, quantity=150.0)
        self.assertEqual(self.total_rows(), 2, "更新持仓不应产生新记录")


class PositionForeignKeyTest(PositionTestCase):
    """外键与必填字段。"""

    def test_01_unknown_symbol_id_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(symbol_id=999999)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"symbol_id": 999999, "quantity": 1.0, "avg_price": 1.0})
        self.assertEqual(self.total_rows(), 0)

    def test_02_symbol_id_is_required(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"quantity": 1.0, "avg_price": 1.0})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"symbol_id": None, "quantity": 1.0, "avg_price": 1.0})

    def test_03_deleting_referenced_symbol_is_rejected(self) -> None:
        self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.positions.execute("DELETE FROM symbols WHERE id = ?", (self.symbol_id,))


class PositionUpdateTest(PositionTestCase):
    """更新。"""

    def test_01_update_fields_and_timestamp(self) -> None:
        position_id = self.create(quantity=100.0, avg_price=10.0)
        before = dict(self.positions.get_position(position_id))

        self.assertTrue(self.positions.update_position(position_id, quantity=120.0))
        after_quantity = dict(self.positions.get_position(position_id))
        self.assertAlmostEqual(after_quantity["quantity"], 120.0)
        self.assertAlmostEqual(after_quantity["avg_price"], 10.0, "未传的字段不应被改动")
        self.assertGreaterEqual(after_quantity["updated_at"], before["updated_at"])

        self.assertTrue(self.positions.update_position(position_id, avg_price=11.5))
        after_price = dict(self.positions.get_position(position_id))
        self.assertAlmostEqual(after_price["avg_price"], 11.5)
        self.assertAlmostEqual(after_price["quantity"], 120.0)

        # id / symbol_id 不应被更新改动
        self.assertEqual(after_price["id"], before["id"])
        self.assertEqual(after_price["symbol_id"], before["symbol_id"])
        self.assertRegex(after_price["updated_at"], TIMESTAMP_PATTERN)

    def test_02_update_requires_a_field_and_unknown_id_returns_false(self) -> None:
        position_id = self.create()
        with self.assertRaises(ValueError):
            self.positions.update_position(position_id)
        self.assertFalse(self.positions.update_position(999999, quantity=1.0))


class PositionListTest(PositionTestCase):
    """基本列表查询 + 持久化 + 隔离。"""

    def test_01_list_positions_sorted_by_symbol(self) -> None:
        self.assertEqual(self.positions.list_positions(), [])

        self.create(self.other_symbol_id, quantity=5.0, avg_price=200.0)
        self.create(quantity=100.0, avg_price=10.0)

        rows = self.positions.list_positions()
        self.assertEqual([r["symbol_id"] for r in rows], sorted([self.symbol_id, self.other_symbol_id]))
        self.assertEqual(len(rows), 2)

    def test_02_persists_after_reconnect(self) -> None:
        position_id = self.create(quantity=88.0, avg_price=3.25)
        del self.positions

        reopened = PositionRepository(self.db_path)
        row = reopened.get_position(position_id)
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["quantity"], 88.0)
        self.assertAlmostEqual(row["avg_price"], 3.25)
        self.assertEqual(len(reopened.list_positions()), 1)

    def test_03_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.positions.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.positions.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
