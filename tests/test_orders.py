"""Stage 09：orders（订单）测试。

覆盖（按要求保持精简）：
    建表与约束  表、字段、类型、NOT NULL、默认值、CHECK、外键、索引
    创建/查询   写入后能查到一致数据；signal_id / price 可为空
    market/limit
    状态更新    update_order_status
    外键        symbol_id 必填且必须存在；signal_id 可空但非空时必须存在
    基本筛选与计数
    持久化      重开连接后数据仍在
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
    OrderRepository,
    SignalRepository,
    StrategyRepository,
    SymbolRepository,
    initialize_database,
)
from database.models import (  # noqa: E402
    DEFAULT_ORDER_STATUS,
    ORDER_STATUSES,
    ORDER_TYPES,
    SCHEMA_VERSION,
)

ORDERS_TABLE = "orders"
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

T0 = "2026-01-01 09:30:00"
T1 = "2026-01-01 14:00:00"
T2 = "2026-01-02 09:30:00"

EXPECTED_COLUMNS = [
    "id",
    "signal_id",
    "symbol_id",
    "side",
    "quantity",
    "price",
    "order_type",
    "status",
    "created_at",
    "updated_at",
]


class OrderTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库，并预置标的与信号。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_orders_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "orders_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.strategies = StrategyRepository(self.db_path)
        self.signals = SignalRepository(self.db_path)
        self.orders = OrderRepository(self.db_path)

        self.symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.other_symbol_id = self.symbols.create_symbol("AAPL", "Apple Inc.", "US", "STOCK")
        self.strategy_id = self.strategies.create_strategy("mean_reversion", {"entry": {"lookback": 20}})
        self.signal_id = self.signals.create_signal(
            self.strategy_id, self.symbol_id, "1d", T0, "long", 100.0
        )

    # ------------------------------------------------------------------ 工具
    def create(
        self,
        *,
        symbol_id: int | None = None,
        side: str = "buy",
        quantity: float = 100.0,
        order_type: str = "limit",
        price: float | None = 100.0,
        signal_id: int | None = None,
        status: str | None = None,
        use_signal: bool = True,
    ) -> int:
        kwargs: dict[str, object] = {
            "order_type": order_type,
            "price": price,
            "signal_id": self.signal_id if (use_signal and signal_id is None) else signal_id,
        }
        if status is not None:
            kwargs["status"] = status
        return self.orders.create_order(
            self.symbol_id if symbol_id is None else symbol_id, side, quantity, **kwargs  # type: ignore[arg-type]
        )

    def raw_insert(self, columns: dict[str, object]) -> int:
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {ORDERS_TABLE} ({names}) VALUES ({placeholders})"
        return self.orders.execute(sql, tuple(columns.values()))

    def raw_row(self, created_at: str = T0) -> dict[str, object]:
        return {
            "symbol_id": self.symbol_id,
            "side": "buy",
            "quantity": 100.0,
            "order_type": "limit",
            "price": 100.0,
            "created_at": created_at,
            "updated_at": created_at,
        }

    def total_rows(self) -> int:
        return self.orders.query_one(f"SELECT COUNT(*) AS n FROM {ORDERS_TABLE}")["n"]


class OrderSchemaTest(OrderTestCase):
    """建表与约束。"""

    def test_01_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.orders.table_exists(ORDERS_TABLE))
        self.assertIn(ORDERS_TABLE, self.orders.list_tables())

    def test_02_columns_types_notnull_defaults_and_primary_key(self) -> None:
        columns = {
            row["name"]: row for row in self.orders.query_all(f"PRAGMA table_info({ORDERS_TABLE})")
        }
        self.assertEqual(list(columns.keys()), EXPECTED_COLUMNS, "字段应与设计完全一致（不多不少）")

        self.assertEqual(columns["id"]["pk"], 1)
        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["signal_id"]["notnull"], 0, "signal_id 应允许 NULL（手工下单）")
        self.assertEqual(columns["price"]["notnull"], 0, "price 应允许 NULL（市价单）")

        for field in ("symbol_id", "side", "quantity", "order_type", "status", "created_at", "updated_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")

        self.assertEqual(columns["quantity"]["type"].upper(), "REAL")
        self.assertEqual(columns["price"]["type"].upper(), "REAL")
        self.assertEqual(columns["status"]["dflt_value"], "'pending'")
        self.assertIsNotNone(columns["created_at"]["dflt_value"])
        self.assertIsNotNone(columns["updated_at"]["dflt_value"])

    def test_03_check_constraints_are_declared(self) -> None:
        sql = self.orders.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (ORDERS_TABLE,),
        )["sql"]
        upper = sql.upper()
        for constraint in ("CK_ORDERS_SIDE", "CK_ORDERS_ORDER_TYPE", "CK_ORDERS_STATUS"):
            with self.subTest(constraint=constraint):
                self.assertIn(constraint, upper)
        for value in ("buy", "sell", *ORDER_TYPES, *ORDER_STATUSES):
            self.assertIn(f"'{value}'", sql)

    def test_04_foreign_keys_are_declared(self) -> None:
        fks = {
            row["from"]: row["table"]
            for row in self.orders.query_all(f"PRAGMA foreign_key_list({ORDERS_TABLE})")
        }
        self.assertEqual(fks, {"signal_id": "signals", "symbol_id": "symbols"})

    def test_05_index_and_schema_version(self) -> None:
        indexes = self.orders.query_all(f"PRAGMA index_list({ORDERS_TABLE})")
        self.assertEqual(len(indexes), 1, f"应只有一个索引，实际: {[r['name'] for r in indexes]}")
        self.assertEqual(indexes[0]["name"], "idx_orders_symbol_created_at")
        index_columns = [
            row["name"] for row in self.orders.query_all(f"PRAGMA index_info({indexes[0]['name']})")
        ]
        self.assertEqual(index_columns, ["symbol_id", "created_at"])

        self.assertEqual(self.orders.query_one("PRAGMA user_version")[0], SCHEMA_VERSION)


class OrderCreateTest(OrderTestCase):
    """创建 / 查询 / 参数校验。"""

    def test_01_create_and_read_back(self) -> None:
        order_id = self.create(side="sell", quantity=250.0, price=88.5)
        self.assertGreater(order_id, 0)

        row = self.orders.get_order(order_id)
        self.assertEqual(row["id"], order_id)
        self.assertEqual(row["signal_id"], self.signal_id)
        self.assertEqual(row["symbol_id"], self.symbol_id)
        self.assertEqual(row["side"], "sell")
        self.assertAlmostEqual(row["quantity"], 250.0)
        self.assertAlmostEqual(row["price"], 88.5)
        self.assertEqual(row["order_type"], "limit")
        self.assertEqual(row["status"], "pending")
        self.assertRegex(row["created_at"], TIMESTAMP_PATTERN)
        self.assertRegex(row["updated_at"], TIMESTAMP_PATTERN)

    def test_02_defaults_and_nullable_fields_from_database(self) -> None:
        # 绕过 Repository（不写 signal_id / price / status），验证 DDL 的可空与默认值
        self.raw_insert({"symbol_id": self.symbol_id, "side": "buy", "quantity": 1.0, "order_type": "market"})
        row = self.orders.query_one(f"SELECT * FROM {ORDERS_TABLE}")
        self.assertIsNone(row["signal_id"])
        self.assertIsNone(row["price"])
        self.assertEqual(row["status"], DEFAULT_ORDER_STATUS)
        self.assertRegex(row["created_at"], TIMESTAMP_PATTERN)

    def test_03_get_order_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.orders.get_order(999999))

    def test_04_python_parameter_validation(self) -> None:
        base: dict[str, object] = {
            "symbol_id": self.symbol_id,
            "signal_id": self.signal_id,
            "side": "buy",
            "quantity": 100.0,
            "order_type": "limit",
            "price": 100.0,
        }
        cases = {
            "symbol_id 非正": {"symbol_id": 0},
            "signal_id 非正": {"signal_id": -1},
            "side 非法": {"side": "long"},
            "quantity 非数字": {"quantity": "100"},
            "order_type 非法": {"order_type": "stop"},
            "price 非数字": {"price": "100"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                kwargs: dict[str, object] = {**base, **overrides}
                with self.assertRaises(ValueError):
                    self.orders.create_order(
                        kwargs["symbol_id"],  # type: ignore[arg-type]
                        kwargs["side"],  # type: ignore[arg-type]
                        kwargs["quantity"],  # type: ignore[arg-type]
                        order_type=kwargs["order_type"],  # type: ignore[arg-type]
                        price=kwargs["price"],  # type: ignore[arg-type]
                        signal_id=kwargs["signal_id"],  # type: ignore[arg-type]
                    )
        self.assertEqual(self.total_rows(), 0)


class OrderTypeTest(OrderTestCase):
    """market / limit。"""

    def test_01_market_and_limit_are_allowed(self) -> None:
        limit_id = self.create(order_type="limit", price=100.0)
        market_id = self.create(order_type="market", price=None)

        self.assertEqual(self.orders.get_order(limit_id)["order_type"], "limit")
        self.assertEqual(self.orders.get_order(market_id)["order_type"], "market")
        self.assertIsNone(self.orders.get_order(market_id)["price"])

    def test_02_invalid_order_type_is_rejected_by_both_layers(self) -> None:
        with self.assertRaises(ValueError):
            self.create(order_type="stop")  # Python 层
        with self.assertRaises(sqlite3.IntegrityError):  # 数据库 CHECK 兜底
            self.raw_insert({**self.raw_row(), "order_type": "stop"})
        self.assertEqual(self.total_rows(), 0)

    def test_03_data_layer_does_not_require_price_for_limit_orders(self) -> None:
        # "limit 必须有 price" 属于执行层业务规则，数据层不强制
        order_id = self.create(order_type="limit", price=None)
        self.assertIsNone(self.orders.get_order(order_id)["price"])


class OrderStatusTest(OrderTestCase):
    """状态与状态更新。"""

    def test_01_all_statuses_are_allowed_and_default_is_pending(self) -> None:
        for status in ORDER_STATUSES:
            with self.subTest(status=status):
                order_id = self.create(status=status)
                self.assertEqual(self.orders.get_order(order_id)["status"], status)
        self.assertEqual(self.orders.get_order(self.create())["status"], "pending")

    def test_02_invalid_status_is_rejected_by_both_layers(self) -> None:
        with self.assertRaises(ValueError):
            self.create(status="open")  # Python 层
        with self.assertRaises(sqlite3.IntegrityError):  # 数据库 CHECK 兜底
            self.raw_insert({**self.raw_row(), "status": "open"})
        self.assertEqual(self.total_rows(), 0)

    def test_03_update_order_status(self) -> None:
        order_id = self.create()
        self.assertTrue(self.orders.update_order_status(order_id, "submitted"))
        self.assertEqual(self.orders.get_order(order_id)["status"], "submitted")
        self.assertTrue(self.orders.update_order_status(order_id, "filled"))
        self.assertEqual(self.orders.get_order(order_id)["status"], "filled")

    def test_04_update_with_invalid_status_keeps_old_value(self) -> None:
        order_id = self.create(status="submitted")
        for bad in ("open", "FILLED", ""):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.orders.update_order_status(order_id, bad)
        self.assertEqual(self.orders.get_order(order_id)["status"], "submitted")

    def test_05_update_unknown_id_returns_false(self) -> None:
        self.assertFalse(self.orders.update_order_status(999999, "filled"))

    def test_06_update_status_does_not_change_other_columns(self) -> None:
        order_id = self.create(side="sell", quantity=10.0, price=5.5, order_type="limit")
        before = dict(self.orders.get_order(order_id))

        self.orders.update_order_status(order_id, "cancelled")
        after = dict(self.orders.get_order(order_id))

        self.assertEqual(after["status"], "cancelled")
        for field in ("id", "signal_id", "symbol_id", "side", "quantity", "price", "order_type", "created_at"):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field], f"{field} 不应被状态更新改动")
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])


class OrderForeignKeyTest(OrderTestCase):
    """外键与必填字段。"""

    def test_01_unknown_references_are_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(symbol_id=999999)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(signal_id=999999)
        with self.assertRaises(sqlite3.IntegrityError):  # 绕过 Repository 同样被拒
            self.raw_insert({**self.raw_row(), "symbol_id": 999999})
        self.assertEqual(self.total_rows(), 0)

    def test_02_symbol_id_is_required_signal_id_is_not(self) -> None:
        row = self.raw_row()
        del row["symbol_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert(row)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(), "symbol_id": None})

        self.raw_insert(self.raw_row())  # 不写 signal_id 可以
        self.assertEqual(self.total_rows(), 1)

    def test_03_deleting_referenced_symbol_or_signal_is_rejected(self) -> None:
        self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.orders.execute("DELETE FROM symbols WHERE id = ?", (self.symbol_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.orders.execute("DELETE FROM signals WHERE id = ?", (self.signal_id,))


class OrderQueryTest(OrderTestCase):
    """基本筛选与计数。"""

    def setUp(self) -> None:
        super().setUp()
        self.o1 = self.create(status="pending")
        self.o2 = self.create(status="filled")
        self.o3 = self.create(
            symbol_id=self.other_symbol_id, use_signal=False, order_type="market", price=None
        )
        # 给 o1 / o2 / o3 设置可筛选的 created_at
        for order_id, created in ((self.o1, T0), (self.o2, T1), (self.o3, T2)):
            self.orders.execute(
                f"UPDATE {ORDERS_TABLE} SET created_at = ? WHERE id = ?", (created, order_id)
            )

    def test_01_list_filters(self) -> None:
        self.assertEqual([r["id"] for r in self.orders.list_orders()], [self.o1, self.o2, self.o3])
        self.assertEqual(
            [r["id"] for r in self.orders.list_orders(symbol_id=self.other_symbol_id)], [self.o3]
        )
        self.assertEqual([r["id"] for r in self.orders.list_orders(signal_id=self.signal_id)], [self.o1, self.o2])
        self.assertEqual([r["id"] for r in self.orders.list_orders(status="filled")], [self.o2])
        # 时间范围（针对 created_at，闭区间）
        self.assertEqual(
            [r["id"] for r in self.orders.list_orders(start_created_at=T0, end_created_at=T1)],
            [self.o1, self.o2],
        )
        self.assertEqual([r["id"] for r in self.orders.list_orders(start_created_at=T2)], [self.o3])
        # 组合 / 无匹配 / limit（最近的 N 条，仍升序）
        self.assertEqual(
            [r["id"] for r in self.orders.list_orders(symbol_id=self.symbol_id, status="pending")],
            [self.o1],
        )
        self.assertEqual(self.orders.list_orders(symbol_id=999999), [])
        self.assertEqual([r["id"] for r in self.orders.list_orders(limit=2)], [self.o2, self.o3])

    def test_02_count_orders(self) -> None:
        self.assertEqual(self.orders.count_orders(), 3)
        self.assertEqual(self.orders.count_orders(status="pending"), 2)
        self.assertEqual(self.orders.count_orders(symbol_id=self.symbol_id), 2)
        self.assertEqual(self.orders.count_orders(signal_id=self.signal_id), 2)
        self.assertEqual(self.orders.count_orders(start_created_at=T1), 2)
        self.assertEqual(self.orders.count_orders(symbol_id=999999), 0)

    def test_03_invalid_filters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.orders.list_orders(status="open")
        with self.assertRaises(ValueError):
            self.orders.list_orders(start_created_at="2026-01-01")
        with self.assertRaises(ValueError):
            self.orders.list_orders(limit=0)


class OrderPersistenceTest(OrderTestCase):
    """持久化 + 不污染正式数据库。"""

    def test_01_persists_after_reconnect(self) -> None:
        order_id = self.create(order_type="market", price=None, status="submitted")
        del self.orders

        reopened = OrderRepository(self.db_path)
        row = reopened.get_order(order_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["order_type"], "market")
        self.assertIsNone(row["price"])
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(reopened.count_orders(), 1)

    def test_02_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.orders.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.orders.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
