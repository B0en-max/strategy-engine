"""Stage 06：signals（交易信号）测试。

覆盖核心行为：
    Schema      表存在、字段完整（无多余字段）、类型 / NOT NULL / 默认值 / 主键
    外键        strategy_id → strategies(id)、symbol_id → symbols(id) 真实生效
    direction   long / short；非法值被拒绝
    status      pending / executed / cancelled；非法值被拒绝
    创建与查询  写入后能查到一致数据
    list 过滤   按 strategy / symbol / timeframe / status 筛选
    状态更新    update_signal_status
    count       计数（支持同样筛选）
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
    initialize_database,
)
from database.models import (  # noqa: E402
    DEFAULT_SIGNAL_STATUS,
    SCHEMA_VERSION,
    SIGNAL_DIRECTIONS,
    SIGNAL_STATUSES,
)

SIGNALS_TABLE = "signals"
TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

T0 = "2026-01-01 09:00:00"
T1 = "2026-01-01 10:00:00"
T2 = "2026-01-01 11:00:00"

EXPECTED_COLUMNS = [
    "id",
    "strategy_id",
    "symbol_id",
    "timeframe",
    "timestamp",
    "direction",
    "price",
    "reason",
    "status",
    "created_at",
]


class SignalTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库，并预置策略与标的。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_signals_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "signals_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.strategies = StrategyRepository(self.db_path)
        self.signals = SignalRepository(self.db_path)

        self.symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.other_symbol_id = self.symbols.create_symbol("AAPL", "Apple Inc.", "US", "STOCK")
        self.strategy_id = self.strategies.create_strategy("mean_reversion", {"entry": {"lookback": 20}})
        self.other_strategy_id = self.strategies.create_strategy("breakout", {})

    # ------------------------------------------------------------------ 工具
    def create(
        self,
        timestamp: str = T0,
        *,
        strategy_id: int | None = None,
        symbol_id: int | None = None,
        timeframe: str = "1d",
        direction: str = "long",
        price: float = 100.0,
        reason: str | None = None,
        status: str | None = None,
    ) -> int:
        kwargs = {"reason": reason}
        if status is not None:
            kwargs["status"] = status
        return self.signals.create_signal(
            self.strategy_id if strategy_id is None else strategy_id,
            self.symbol_id if symbol_id is None else symbol_id,
            timeframe,
            timestamp,
            direction,
            price,
            **kwargs,
        )

    def raw_insert(self, columns: dict[str, object]) -> int:
        """直接用 SQL 插入（列由 dict 的键决定），用于验证数据库层约束。"""
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {SIGNALS_TABLE} ({names}) VALUES ({placeholders})"
        return self.signals.execute(sql, tuple(columns.values()))

    def raw_row(self, timestamp: str = T0) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "symbol_id": self.symbol_id,
            "timeframe": "1d",
            "timestamp": timestamp,
            "direction": "long",
            "price": 100.0,
        }

    def total_rows(self) -> int:
        return self.signals.query_one(f"SELECT COUNT(*) AS n FROM {SIGNALS_TABLE}")["n"]


class SignalSchemaTest(SignalTestCase):
    """Schema。"""

    def test_01_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.signals.table_exists(SIGNALS_TABLE))
        self.assertIn(SIGNALS_TABLE, self.signals.list_tables())

    def test_02_columns_types_notnull_defaults_and_primary_key(self) -> None:
        columns = {
            row["name"]: row for row in self.signals.query_all(f"PRAGMA table_info({SIGNALS_TABLE})")
        }
        self.assertEqual(list(columns.keys()), EXPECTED_COLUMNS, "字段应与设计完全一致（不多不少）")

        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["id"]["pk"], 1)
        self.assertEqual(columns["strategy_id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["symbol_id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["price"]["type"].upper(), "REAL")

        for field in ("strategy_id", "symbol_id", "timeframe", "timestamp", "direction", "price", "status", "created_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")

        self.assertEqual(columns["reason"]["notnull"], 0, "reason 应允许 NULL")
        self.assertEqual(columns["status"]["dflt_value"], "'pending'")
        self.assertIsNotNone(columns["created_at"]["dflt_value"])

    def test_03_check_constraints_declared(self) -> None:
        sql = self.signals.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (SIGNALS_TABLE,),
        )["sql"]
        upper = sql.upper()
        self.assertIn("CK_SIGNALS_DIRECTION", upper)
        self.assertIn("CK_SIGNALS_STATUS", upper)
        for value in SIGNAL_DIRECTIONS + SIGNAL_STATUSES:
            self.assertIn(f"'{value}'", sql)

    def test_04_index_covers_query_columns(self) -> None:
        indexes = self.signals.query_all(f"PRAGMA index_list({SIGNALS_TABLE})")
        self.assertEqual(len(indexes), 1, f"应只有一个索引，实际: {[r['name'] for r in indexes]}")
        index = indexes[0]
        self.assertEqual(index["name"], "idx_signals_strategy_symbol_timeframe_timestamp")
        self.assertEqual(index["unique"], 0, "该索引不是唯一索引（本表没有唯一约束）")

        index_columns = [
            row["name"] for row in self.signals.query_all(f"PRAGMA index_info({index['name']})")
        ]
        self.assertEqual(index_columns, ["strategy_id", "symbol_id", "timeframe", "timestamp"])

    def test_05_query_plan_uses_index(self) -> None:
        plan = self.signals.query_all(
            f"EXPLAIN QUERY PLAN SELECT * FROM {SIGNALS_TABLE} "
            "WHERE strategy_id = ? AND symbol_id = ? AND timeframe = ? "
            "ORDER BY timestamp ASC, id ASC",
            (1, 1, "1d"),
        )
        detail = " ".join(row["detail"] for row in plan)
        self.assertIn("USING INDEX", detail.upper(), f"查询应使用索引，实际计划: {detail}")
        self.assertIn("idx_signals", detail)
        self.assertNotIn(
            "TEMP B-TREE FOR ORDER BY",
            detail.upper(),
            f"索引应已满足排序，不应产生临时排序: {detail}",
        )

    def test_06_schema_version(self) -> None:
        self.assertEqual(self.signals.query_one("PRAGMA user_version")[0], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 4)


class SignalForeignKeyTest(SignalTestCase):
    """外键（由数据库负责，不复制 Python 检查）。"""

    def test_01_valid_references_are_accepted(self) -> None:
        self.create()
        self.assertEqual(self.total_rows(), 1)

    def test_02_unknown_strategy_id_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(strategy_id=999999)
        self.assertEqual(self.total_rows(), 0)

    def test_03_unknown_symbol_id_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(symbol_id=999999)
        self.assertEqual(self.total_rows(), 0)

    def test_04_foreign_key_rejected_by_raw_sql_too(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(), "strategy_id": 999999})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(), "symbol_id": 999999})

    def test_05_foreign_keys_are_declared(self) -> None:
        fks = {
            row["from"]: row["table"]
            for row in self.signals.query_all(f"PRAGMA foreign_key_list({SIGNALS_TABLE})")
        }
        self.assertEqual(fks, {"strategy_id": "strategies", "symbol_id": "symbols"})

    def test_06_deleting_referenced_strategy_or_symbol_is_rejected(self) -> None:
        self.create()
        with self.assertRaises(sqlite3.IntegrityError):
            self.signals.execute("DELETE FROM strategies WHERE id = ?", (self.strategy_id,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.signals.execute("DELETE FROM symbols WHERE id = ?", (self.symbol_id,))

    def test_07_missing_reference_columns_are_rejected(self) -> None:
        row = self.raw_row()
        del row["strategy_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert(row)
        row = self.raw_row()
        del row["symbol_id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert(row)


class SignalDirectionTest(SignalTestCase):
    """direction。"""

    def test_01_long_and_short_are_allowed(self) -> None:
        for index, direction in enumerate(SIGNAL_DIRECTIONS):
            with self.subTest(direction=direction):
                signal_id = self.create(T0 if index == 0 else T1, direction=direction)
                self.assertEqual(self.signals.get_signal(signal_id)["direction"], direction)

    def test_02_invalid_direction_is_rejected_by_python_layer(self) -> None:
        for bad in ("LONG", "buy", "none", "", None, 1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.create(direction=bad)  # type: ignore[arg-type]
        self.assertEqual(self.total_rows(), 0)

    def test_03_invalid_direction_is_rejected_by_database_check(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(), "direction": "buy"})
        self.assertEqual(self.total_rows(), 0)


class SignalStatusTest(SignalTestCase):
    """status。"""

    def test_01_all_statuses_are_allowed(self) -> None:
        for index, status in enumerate(SIGNAL_STATUSES):
            with self.subTest(status=status):
                signal_id = self.create(T0 if index == 0 else T1, status=status)
                self.assertEqual(self.signals.get_signal(signal_id)["status"], status)

    def test_02_default_status_is_pending(self) -> None:
        self.assertEqual(self.signals.get_signal(self.create())["status"], DEFAULT_SIGNAL_STATUS)
        # 绕过 Repository（不写 status 列），验证 DDL 的 DEFAULT 生效
        self.raw_insert(self.raw_row(T1))
        self.assertEqual(self.signals.get_signal(2)["status"], "pending")

    def test_03_invalid_status_is_rejected_by_python_layer(self) -> None:
        # 这里直接调用 create_signal（不走 helper），确保 status 是被"显式传入非法值"
        for bad in ("PENDING", "filled", "open", "", None, 0):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.signals.create_signal(
                        self.strategy_id, self.symbol_id, "1d", T0, "long", 100.0, status=bad
                    )
        self.assertEqual(self.total_rows(), 0)

    def test_04_invalid_status_is_rejected_by_database_check(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(), "status": "filled"})

    def test_05_update_status(self) -> None:
        signal_id = self.create()
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "pending")

        self.assertTrue(self.signals.update_signal_status(signal_id, "executed"))
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "executed")

        self.assertTrue(self.signals.update_signal_status(signal_id, "cancelled"))
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "cancelled")

    def test_06_update_status_with_invalid_value_keeps_old_value(self) -> None:
        signal_id = self.create(status="executed")
        for bad in ("filled", "EXECUTED", ""):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.signals.update_signal_status(signal_id, bad)
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "executed")

    def test_07_update_status_unknown_id_returns_false(self) -> None:
        self.assertFalse(self.signals.update_signal_status(999999, "executed"))

    def test_08_update_status_does_not_change_other_columns(self) -> None:
        signal_id = self.create(reason="测试原因", price=123.45, direction="short")
        before = dict(self.signals.get_signal(signal_id))

        self.signals.update_signal_status(signal_id, "executed")
        after = dict(self.signals.get_signal(signal_id))

        self.assertEqual(after["status"], "executed")
        for field in ("id", "strategy_id", "symbol_id", "timeframe", "timestamp", "direction", "price", "reason", "created_at"):
            with self.subTest(field=field):
                self.assertEqual(after[field], before[field], f"{field} 不应被状态更新改动")


class SignalCreateQueryTest(SignalTestCase):
    """创建与查询。"""

    def test_01_create_and_read_back(self) -> None:
        signal_id = self.create(direction="short", price=88.5, reason="跌破支撑后回抽")
        self.assertIsInstance(signal_id, int)
        self.assertGreater(signal_id, 0)

        row = self.signals.get_signal(signal_id)
        self.assertEqual(row["id"], signal_id)
        self.assertEqual(row["strategy_id"], self.strategy_id)
        self.assertEqual(row["symbol_id"], self.symbol_id)
        self.assertEqual(row["timeframe"], "1d")
        self.assertEqual(row["timestamp"], T0)
        self.assertEqual(row["direction"], "short")
        self.assertAlmostEqual(row["price"], 88.5)
        self.assertEqual(row["reason"], "跌破支撑后回抽")
        self.assertEqual(row["status"], "pending")
        self.assertRegex(row["created_at"], TIMESTAMP_PATTERN)

    def test_02_reason_is_optional(self) -> None:
        self.assertIsNone(self.signals.get_signal(self.create())["reason"])

    def test_03_get_signal_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.signals.get_signal(999999))

    def test_04_create_is_strict_and_never_upserts(self) -> None:
        # 本表没有唯一约束，也不做自动 upsert：两条完全相同的信号都会各自入库
        first = self.create()
        second = self.create()
        self.assertNotEqual(first, second)
        self.assertEqual(self.total_rows(), 2)

    def test_05_python_parameter_validation(self) -> None:
        cases = {
            "strategy_id 非正": {"strategy_id": 0},
            "symbol_id 非正": {"symbol_id": -1},
            "timeframe 为空": {"timeframe": ""},
            "timestamp 格式错": {"timestamp": "2026/01/01 09:00:00"},
            "price 非数字": {"price": "100"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    self.create(**overrides)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.create(reason=123)  # type: ignore[arg-type]
        self.assertEqual(self.total_rows(), 0)

    def test_06_timestamp_is_kept_as_given(self) -> None:
        historical = "2015-06-15 09:30:00"
        signal_id = self.create(historical)
        self.assertEqual(self.signals.get_signal(signal_id)["timestamp"], historical)


class SignalListCountTest(SignalTestCase):
    """list 过滤 / count / limit。"""

    def setUp(self) -> None:
        super().setUp()
        self.s1 = self.create(T0, direction="long", status="pending")
        self.s2 = self.create(T1, direction="short", status="executed")
        self.s3 = self.create(T2, symbol_id=self.other_symbol_id, status="cancelled")
        self.s4 = self.create(T2, strategy_id=self.other_strategy_id, timeframe="1h")

    def test_01_list_sorted_by_timestamp_then_id(self) -> None:
        rows = self.signals.list_signals()
        self.assertEqual(len(rows), 4)
        self.assertEqual([r["id"] for r in rows], [self.s1, self.s2, self.s3, self.s4])
        timestamps = [r["timestamp"] for r in rows]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_02_list_filter_by_strategy(self) -> None:
        rows = self.signals.list_signals(strategy_id=self.strategy_id)
        self.assertEqual([r["id"] for r in rows], [self.s1, self.s2, self.s3])

    def test_03_list_filter_by_symbol(self) -> None:
        rows = self.signals.list_signals(symbol_id=self.other_symbol_id)
        self.assertEqual([r["id"] for r in rows], [self.s3])

    def test_04_list_filter_by_timeframe(self) -> None:
        rows = self.signals.list_signals(timeframe="1h")
        self.assertEqual([r["id"] for r in rows], [self.s4])
        self.assertEqual(len(self.signals.list_signals(timeframe="1d")), 3)

    def test_05_list_filter_by_status(self) -> None:
        # s1 与 s4 是 pending（s4 未指定 status），s2 是 executed，s3 是 cancelled
        self.assertEqual(
            [r["id"] for r in self.signals.list_signals(status="pending")], [self.s1, self.s4]
        )
        self.assertEqual([r["id"] for r in self.signals.list_signals(status="executed")], [self.s2])
        self.assertEqual([r["id"] for r in self.signals.list_signals(status="cancelled")], [self.s3])

    def test_06_list_combined_filters(self) -> None:
        rows = self.signals.list_signals(
            strategy_id=self.strategy_id, symbol_id=self.other_symbol_id, timeframe="1d"
        )
        self.assertEqual([r["id"] for r in rows], [self.s3])
        self.assertEqual(
            self.signals.list_signals(strategy_id=self.strategy_id, status="executed")[0]["id"],
            self.s2,
        )
        self.assertEqual(self.signals.list_signals(strategy_id=999999), [])

    def test_07_list_limit_returns_most_recent_n_in_ascending_order(self) -> None:
        rows = self.signals.list_signals(limit=2)
        self.assertEqual(
            [r["id"] for r in rows],
            [self.s3, self.s4],
            "limit 表示'最近的 N 条'，返回时仍应升序",
        )

    def test_08_list_invalid_filters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.signals.list_signals(strategy_id=0)
        with self.assertRaises(ValueError):
            self.signals.list_signals(timeframe="")
        with self.assertRaises(ValueError):
            self.signals.list_signals(status="filled")
        with self.assertRaises(ValueError):
            self.signals.list_signals(limit=0)

    def test_09_count(self) -> None:
        self.assertEqual(self.signals.count_signals(), 4)
        self.assertEqual(self.signals.count_signals(strategy_id=self.strategy_id), 3)
        self.assertEqual(self.signals.count_signals(symbol_id=self.other_symbol_id), 1)
        self.assertEqual(self.signals.count_signals(status="executed"), 1)
        self.assertEqual(self.signals.count_signals(status="pending"), 2)
        self.assertEqual(self.signals.count_signals(timeframe="1h"), 1)
        self.assertEqual(
            self.signals.count_signals(strategy_id=self.strategy_id, status="pending"), 1
        )
        self.assertEqual(self.signals.count_signals(strategy_id=999999), 0)


class SignalPersistenceTest(SignalTestCase):
    """持久化 + 不污染正式数据库。"""

    def test_01_persists_after_reconnect(self) -> None:
        signal_id = self.create(reason="需要持久化", status="executed")
        del self.signals

        reopened = SignalRepository(self.db_path)
        row = reopened.get_signal(signal_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["reason"], "需要持久化")
        self.assertEqual(row["status"], "executed")
        self.assertEqual(reopened.count_signals(), 1)

    def test_02_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.signals.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.signals.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
