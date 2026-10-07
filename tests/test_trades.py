"""Stage 07：trades（实际成交记录）测试。

覆盖（按本阶段要求保持精简）：
    建表        表与字段、约束、索引、外键声明
    创建/查询   写入后能查到一致数据，含 signal_id 可为空
    外键约束    symbol_id 必填且必须存在；signal_id 可空但非空时必须存在
    buy/sell    两种 side 可用；非法值被拒绝
    基本筛选    symbol_id / signal_id / side / 时间范围 + 计数
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
    SignalRepository,
    StrategyRepository,
    SymbolRepository,
    TradeRepository,
    initialize_database,
)
from database.models import SCHEMA_VERSION, TRADE_SIDES  # noqa: E402

TRADES_TABLE = "trades"
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
    "timestamp",
    "fee",
    "created_at",
]


class TradeTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库，并预置标的与信号。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_trades_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "trades_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.strategies = StrategyRepository(self.db_path)
        self.signals = SignalRepository(self.db_path)
        self.trades = TradeRepository(self.db_path)

        self.symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.other_symbol_id = self.symbols.create_symbol("AAPL", "Apple Inc.", "US", "STOCK")
        self.strategy_id = self.strategies.create_strategy("mean_reversion", {"entry": {"lookback": 20}})
        self.signal_id = self.signals.create_signal(
            self.strategy_id, self.symbol_id, "1d", T0, "long", 100.0
        )

    # ------------------------------------------------------------------ 工具
    def create(
        self,
        timestamp: str = T0,
        *,
        symbol_id: int | None = None,
        signal_id: int | None = None,
        side: str = "buy",
        quantity: float = 100.0,
        price: float = 100.0,
        fee: float = 0.0,
        use_signal: bool = True,
    ) -> int:
        return self.trades.create_trade(
            self.symbol_id if symbol_id is None else symbol_id,
            side,
            quantity,
            price,
            timestamp,
            signal_id=self.signal_id if (use_signal and signal_id is None) else signal_id,
            fee=fee,
        )

    def raw_insert(self, columns: dict[str, object]) -> int:
        """直接用 SQL 插入（列由 dict 的键决定），用于验证数据库层约束。"""
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {TRADES_TABLE} ({names}) VALUES ({placeholders})"
        return self.trades.execute(sql, tuple(columns.values()))

    def raw_row(self, timestamp: str = T0) -> dict[str, object]:
        return {
            "symbol_id": self.symbol_id,
            "side": "buy",
            "quantity": 100.0,
            "price": 100.0,
            "timestamp": timestamp,
        }

    def total_rows(self) -> int:
        return self.trades.query_one(f"SELECT COUNT(*) AS n FROM {TRADES_TABLE}")["n"]


class TradeSchemaTest(TradeTestCase):
    """建表。"""

    def test_01_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.trades.table_exists(TRADES_TABLE))
        self.assertIn(TRADES_TABLE, self.trades.list_tables())

    def test_02_columns_constraints_and_primary_key(self) -> None:
        columns = {
            row["name"]: row for row in self.trades.query_all(f"PRAGMA table_info({TRADES_TABLE})")
        }
        self.assertEqual(list(columns.keys()), EXPECTED_COLUMNS, "字段应与设计完全一致（不多不少）")

        self.assertEqual(columns["id"]["pk"], 1)
        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["signal_id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["signal_id"]["notnull"], 0, "signal_id 应允许 NULL（手工交易）")
        self.assertEqual(columns["side"]["type"].upper(), "TEXT")

        for field in ("symbol_id", "side", "quantity", "price", "timestamp", "fee", "created_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")
        for field in ("quantity", "price", "fee"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["type"].upper(), "REAL")

        self.assertEqual(columns["fee"]["dflt_value"], "0")
        self.assertIsNotNone(columns["created_at"]["dflt_value"])

    def test_03_check_constraint_for_side_is_declared(self) -> None:
        sql = self.trades.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (TRADES_TABLE,),
        )["sql"]
        self.assertIn("CK_TRADES_SIDE", sql.upper())
        for side in TRADE_SIDES:
            self.assertIn(f"'{side}'", sql)

    def test_04_foreign_keys_are_declared(self) -> None:
        fks = {
            row["from"]: row["table"]
            for row in self.trades.query_all(f"PRAGMA foreign_key_list({TRADES_TABLE})")
        }
        self.assertEqual(fks, {"signal_id": "signals", "symbol_id": "symbols"})

    def test_05_index_is_declared(self) -> None:
        indexes = self.trades.query_all(f"PRAGMA index_list({TRADES_TABLE})")
        self.assertEqual(len(indexes), 1, f"应只有一个索引，实际: {[r['name'] for r in indexes]}")
        self.assertEqual(indexes[0]["name"], "idx_trades_symbol_timestamp")
        index_columns = [
            row["name"] for row in self.trades.query_all(f"PRAGMA index_info({indexes[0]['name']})")
        ]
        self.assertEqual(index_columns, ["symbol_id", "timestamp"])

    def test_06_schema_version(self) -> None:
        self.assertEqual(self.trades.query_one("PRAGMA user_version")[0], SCHEMA_VERSION)


class TradeForeignKeyTest(TradeTestCase):
    """外键约束（由数据库负责）。"""

    def test_01_valid_references_are_accepted(self) -> None:
        self.create()  # 带 signal
        self.create(T1, use_signal=False)  # 手工交易，无 signal
        self.assertEqual(self.total_rows(), 2)

    def test_02_unknown_symbol_id_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(symbol_id=999999)
        with self.assertRaises(sqlite3.IntegrityError):  # 绕过 Repository 同样被拒
            self.raw_insert({**self.raw_row(), "symbol_id": 999999})
        self.assertEqual(self.total_rows(), 0)

    def test_03_unknown_signal_id_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(signal_id=999999)
        self.assertEqual(self.total_rows(), 0)

    def test_04_deleting_referenced_symbol_or_signal_is_rejected(self) -> None:
        self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.trades.execute("DELETE FROM symbols WHERE id = ?", (self.symbol_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.trades.execute("DELETE FROM signals WHERE id = ?", (self.signal_id,))

    def test_05_signal_id_nullable_symbol_id_required(self) -> None:
        # signal_id 缺省可以写入
        self.raw_insert(self.raw_row())
        self.assertEqual(self.total_rows(), 1)

        # symbol_id 缺省 / 为 NULL 都不行
        row = self.raw_row(T1)
        del row["symbol_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert(row)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(T1), "symbol_id": None})


class TradeSideTest(TradeTestCase):
    """buy / sell。"""

    def test_01_buy_and_sell_are_allowed(self) -> None:
        for index, side in enumerate(TRADE_SIDES):
            with self.subTest(side=side):
                trade_id = self.create(T0 if index == 0 else T1, side=side)
                self.assertEqual(self.trades.get_trade(trade_id)["side"], side)

    def test_02_invalid_side_is_rejected(self) -> None:
        for bad in ("BUY", "long", "short", "", None, 1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.create(side=bad)  # type: ignore[arg-type]
        with self.assertRaises(sqlite3.IntegrityError):  # 数据库 CHECK 兜底
            self.raw_insert({**self.raw_row(), "side": "long"})
        self.assertEqual(self.total_rows(), 0)


class TradeQueryTest(TradeTestCase):
    """创建 / 查询 / 筛选 / 计数。"""

    def test_01_create_and_read_back(self) -> None:
        trade_id = self.create(price=88.25, quantity=300.5, fee=1.5)
        self.assertGreater(trade_id, 0)

        row = self.trades.get_trade(trade_id)
        self.assertEqual(row["id"], trade_id)
        self.assertEqual(row["signal_id"], self.signal_id)
        self.assertEqual(row["symbol_id"], self.symbol_id)
        self.assertEqual(row["side"], "buy")
        self.assertAlmostEqual(row["quantity"], 300.5)
        self.assertAlmostEqual(row["price"], 88.25)
        self.assertEqual(row["timestamp"], T0)
        self.assertAlmostEqual(row["fee"], 1.5)
        self.assertRegex(row["created_at"], TIMESTAMP_PATTERN)

    def test_02_defaults(self) -> None:
        row = self.trades.get_trade(self.create(fee=0.0))
        self.assertAlmostEqual(row["fee"], 0.0)
        # 绕过 Repository（不写 fee / signal_id 列），验证 DDL 默认值与可空性
        self.raw_insert(self.raw_row(T1))
        raw = self.trades.query_one(
            f"SELECT * FROM {TRADES_TABLE} WHERE timestamp = ?", (T1,)
        )
        self.assertAlmostEqual(raw["fee"], 0.0)
        self.assertIsNone(raw["signal_id"])

    def test_03_get_trade_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.trades.get_trade(999999))

    def test_04_list_filters(self) -> None:
        t1 = self.create(T0, side="buy")
        t2 = self.create(T1, side="sell")
        t3 = self.create(T2, symbol_id=self.other_symbol_id, use_signal=False)

        self.assertEqual([r["id"] for r in self.trades.list_trades()], [t1, t2, t3])
        self.assertEqual([r["id"] for r in self.trades.list_trades(side="sell")], [t2])
        self.assertEqual(
            [r["id"] for r in self.trades.list_trades(symbol_id=self.other_symbol_id)], [t3]
        )
        self.assertEqual([r["id"] for r in self.trades.list_trades(signal_id=self.signal_id)], [t1, t2])
        # 时间范围是闭区间
        self.assertEqual(
            [r["id"] for r in self.trades.list_trades(start_timestamp=T0, end_timestamp=T1)],
            [t1, t2],
        )
        self.assertEqual([r["id"] for r in self.trades.list_trades(start_timestamp=T2)], [t3])
        # 组合筛选 / 无匹配
        self.assertEqual(
            [r["id"] for r in self.trades.list_trades(symbol_id=self.symbol_id, side="buy")], [t1]
        )
        self.assertEqual(self.trades.list_trades(symbol_id=999999), [])
        self.assertEqual([r["id"] for r in self.trades.list_trades(limit=2)], [t2, t3])

    def test_05_count(self) -> None:
        self.create(T0, side="buy")
        self.create(T1, side="sell")
        self.create(T2, side="buy")

        self.assertEqual(self.trades.count_trades(), 3)
        self.assertEqual(self.trades.count_trades(side="buy"), 2)
        self.assertEqual(self.trades.count_trades(symbol_id=self.symbol_id), 3)
        self.assertEqual(self.trades.count_trades(signal_id=self.signal_id), 3)
        self.assertEqual(self.trades.count_trades(side="sell", symbol_id=self.symbol_id), 1)
        self.assertEqual(self.trades.count_trades(start_timestamp=T1), 2)
        self.assertEqual(self.trades.count_trades(symbol_id=999999), 0)

    def test_06_python_parameter_validation(self) -> None:
        cases = {
            "symbol_id 非正": {"symbol_id": 0},
            "signal_id 非正": {"signal_id": -1},
            "quantity 非数字": {"quantity": "100"},
            "price 非数字": {"price": None},
            "timestamp 格式错": {"timestamp": "2026/01/01 09:30:00"},
            "fee 非数字": {"fee": "0"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    self.create(**overrides)  # type: ignore[arg-type]
        self.assertEqual(self.total_rows(), 0)

    def test_07_list_invalid_filters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.trades.list_trades(side="long")
        with self.assertRaises(ValueError):
            self.trades.list_trades(start_timestamp="2026-01-01")
        with self.assertRaises(ValueError):
            self.trades.list_trades(limit=0)

    def test_08_fee_sign_is_not_restricted(self) -> None:
        # 返佣可能让 fee 为负；quantity / price 也不在数据库层做正值限制
        row = self.trades.get_trade(self.create(fee=-0.75))
        self.assertAlmostEqual(row["fee"], -0.75)


class TradePersistenceTest(TradeTestCase):
    """持久化 + 不污染正式数据库。"""

    def test_01_persists_after_reconnect(self) -> None:
        trade_id = self.create(side="sell", quantity=42.0)
        del self.trades

        reopened = TradeRepository(self.db_path)
        row = reopened.get_trade(trade_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["side"], "sell")
        self.assertAlmostEqual(row["quantity"], 42.0)
        self.assertEqual(reopened.count_trades(), 1)

    def test_02_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.trades.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.trades.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
