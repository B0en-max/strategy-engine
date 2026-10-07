"""Stage 02：SQLite 数据库基础设施测试。

覆盖内容：
    1. 数据库文件能否创建（以及初始化幂等）
    2. SQLite connection 能否正常连接 / 关闭
    3. row_factory：查询结果能否按列名访问
    4. PRAGMA foreign_keys 是否启用，以及外键约束是否真正生效
    5. 基础 CRUD（CREATE / INSERT / SELECT / UPDATE / DELETE）
    6. 事务：正常提交、异常回滚
    7. Repository 基础执行与查询方法

重要：
    - 所有测试都在临时目录里建库，**不会触碰正式库** data/strategy_engine.db
    - 测试用表名里的 "test" 表明它只是基础设施验证用途，不是业务表
    - 测试结束后临时数据库由 TemporaryDirectory 自动清理

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
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

from database import (  # noqa: E402  (测试需要先保证项目根目录在 sys.path 中)
    DEFAULT_DB_PATH,
    PROJECT_ROOT as DB_PROJECT_ROOT,
    Repository,
    close,
    connect,
    connection,
    database_info,
    get_database_path,
    initialize_database,
    resolve_database_path,
    transaction,
)

# 仅用于验证基础设施的测试表（不是业务表）
TEST_TABLE = "stage02_infrastructure_test"

# 本阶段明确不应存在的业务表
FORBIDDEN_BUSINESS_TABLES = [
    "symbols",
    "klines",
    "strategies",
    "strategy_versions",
    "signals",
    "orders",
    "trades",
    "positions",
    "backtests",
    "performance",
    "reviews",
    "optimization_candidates",
]


class TempDatabaseTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库，绝不污染正式数据库。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_db_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "test.db"
        self.repo = Repository(self.db_path)

    def create_test_table(self, name: str = TEST_TABLE) -> None:
        """创建一个仅用于基础设施验证的表。"""
        self.repo.execute(
            f"CREATE TABLE IF NOT EXISTS {name} ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  label TEXT NOT NULL,"
            "  amount REAL"
            ")"
        )


class DatabaseFileTest(TempDatabaseTestCase):
    """1. 数据库文件与初始化。"""

    def test_01_database_file_is_created(self) -> None:
        self.assertFalse(self.db_path.exists(), "初始化前不应存在数据库文件")

        returned = initialize_database(self.db_path)

        self.assertEqual(Path(returned), self.db_path)
        self.assertTrue(self.db_path.is_file(), "数据库文件应被真实创建")
        self.assertGreater(self.db_path.stat().st_size, 0, "初始化后数据库文件不应为空")

    def test_02_parent_directory_is_created(self) -> None:
        nested = Path(self._tmpdir.name) / "level1" / "level2" / "nested.db"
        self.assertFalse(nested.parent.exists())

        initialize_database(nested)

        self.assertTrue(nested.is_file())
        self.assertTrue(nested.parent.is_dir())

    def test_03_initialize_is_idempotent(self) -> None:
        initialize_database(self.db_path)
        size_after_first = self.db_path.stat().st_size

        # 重复执行不应报错（第二次、第三次）
        initialize_database(self.db_path)
        initialize_database(self.db_path)

        self.assertTrue(self.db_path.is_file())
        self.assertEqual(self.db_path.stat().st_size, size_after_first)

    def test_04_initialize_creates_no_business_tables(self) -> None:
        initialize_database(self.db_path)

        info = database_info(self.db_path)
        self.assertEqual(info["tables"], [], f"本阶段不应创建任何表，却有: {info['tables']}")

        for name in FORBIDDEN_BUSINESS_TABLES:
            with self.subTest(table=name):
                self.assertFalse(
                    self.repo.table_exists(name),
                    f"业务表 {name} 属于后续阶段，本阶段不应存在",
                )

    def test_05_default_database_path_is_under_data_dir(self) -> None:
        self.assertEqual(get_database_path(), DEFAULT_DB_PATH)
        self.assertEqual(DB_PROJECT_ROOT, PROJECT_ROOT)
        self.assertEqual(DEFAULT_DB_PATH, PROJECT_ROOT / "data" / "strategy_engine.db")

    def test_06_relative_path_resolves_against_project_root(self) -> None:
        resolved = resolve_database_path("data/strategy_engine.db")
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved, DEFAULT_DB_PATH)


class ConnectionTest(TempDatabaseTestCase):
    """2 / 3 / 4：连接、row_factory、foreign key。"""

    def test_01_connection_can_be_created_and_closed(self) -> None:
        conn = connect(self.db_path)
        try:
            self.assertIsInstance(conn, sqlite3.Connection)
            row = conn.execute("SELECT 1 AS one").fetchone()
            self.assertEqual(row["one"], 1)
        finally:
            close(conn)

        # 关闭后继续使用该连接应报错（说明 close 真的生效了）
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    def test_02_close_is_idempotent(self) -> None:
        conn = connect(self.db_path)
        close(conn)
        close(conn)  # 重复关闭不应报错

    def test_03_connection_context_closes_connection(self) -> None:
        with connection(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS t (v INTEGER)")
            conn.execute("INSERT INTO t (v) VALUES (1)")
            inner = conn

        with self.assertRaises(sqlite3.ProgrammingError):
            inner.execute("SELECT * FROM t")

        with connection(self.db_path) as conn2:
            count = conn2.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        self.assertEqual(count, 1, "connection() 正常结束时应提交")

    def test_04_row_factory_allows_access_by_column_name(self) -> None:
        self.create_test_table()
        self.repo.execute(
            f"INSERT INTO {TEST_TABLE} (label, amount) VALUES (?, ?)", ("alpha", 12.5)
        )

        with connection(self.db_path) as conn:
            self.assertIs(conn.row_factory, sqlite3.Row)
            row = conn.execute(f"SELECT id, label, amount FROM {TEST_TABLE}").fetchone()

        # 按列名访问
        self.assertEqual(row["label"], "alpha")
        self.assertAlmostEqual(row["amount"], 12.5)
        # 同时也支持按序号访问
        self.assertEqual(row[1], "alpha")
        self.assertIn("label", row.keys())

    def test_05_foreign_keys_pragma_is_enabled(self) -> None:
        with connection(self.db_path) as conn:
            pragma_value = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(pragma_value, 1, "PRAGMA foreign_keys 应为 1")

        self.assertTrue(database_info(self.db_path)["foreign_keys_enabled"])

    def test_06_foreign_key_constraint_actually_applies(self) -> None:
        self.repo.execute(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        self.repo.execute(
            "CREATE TABLE child ("
            "  id INTEGER PRIMARY KEY,"
            "  parent_id INTEGER NOT NULL REFERENCES parent(id)"
            ")"
        )
        self.repo.execute("INSERT INTO parent (id, name) VALUES (1, 'p1')")
        self.repo.execute("INSERT INTO child (id, parent_id) VALUES (1, 1)")

        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.execute("INSERT INTO child (id, parent_id) VALUES (2, 999)")

        # 删除仍被引用的父行同样应被拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.execute("DELETE FROM parent WHERE id = 1")


class CrudTest(TempDatabaseTestCase):
    """5. 基础 CRUD 能力（使用测试专用表）。"""

    def setUp(self) -> None:
        super().setUp()
        self.create_test_table()

    def test_01_insert_and_select(self) -> None:
        affected = self.repo.execute(
            f"INSERT INTO {TEST_TABLE} (label, amount) VALUES (?, ?)", ("one", 1.0)
        )
        self.assertEqual(affected, 1)

        row = self.repo.query_one(
            f"SELECT label, amount FROM {TEST_TABLE} WHERE label = ?", ("one",)
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["label"], "one")
        self.assertAlmostEqual(row["amount"], 1.0)

    def test_02_select_many_and_empty_result(self) -> None:
        self.repo.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('b')")
        self.repo.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('a')")

        rows = self.repo.query_all(f"SELECT label FROM {TEST_TABLE} ORDER BY label")
        self.assertEqual([r["label"] for r in rows], ["a", "b"])

        empty = self.repo.query_all(f"SELECT label FROM {TEST_TABLE} WHERE label = 'zzz'")
        self.assertEqual(empty, [])
        self.assertIsNone(
            self.repo.query_one(f"SELECT label FROM {TEST_TABLE} WHERE label = 'zzz'")
        )

    def test_03_update(self) -> None:
        self.repo.execute(f"INSERT INTO {TEST_TABLE} (label, amount) VALUES ('x', 1.0)")

        affected = self.repo.execute(
            f"UPDATE {TEST_TABLE} SET amount = ? WHERE label = ?", (9.5, "x")
        )
        self.assertEqual(affected, 1)

        row = self.repo.query_one(f"SELECT amount FROM {TEST_TABLE} WHERE label = 'x'")
        self.assertAlmostEqual(row["amount"], 9.5)

    def test_04_delete(self) -> None:
        self.repo.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('gone')")

        affected = self.repo.execute(f"DELETE FROM {TEST_TABLE} WHERE label = ?", ("gone",))
        self.assertEqual(affected, 1)

        count = self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"]
        self.assertEqual(count, 0)

    def test_05_drop_test_table(self) -> None:
        self.assertTrue(self.repo.table_exists(TEST_TABLE))
        self.repo.execute(f"DROP TABLE {TEST_TABLE}")
        self.assertFalse(self.repo.table_exists(TEST_TABLE), "测试表应可被清理")


class TransactionTest(TempDatabaseTestCase):
    """6. 事务：提交与回滚。"""

    def setUp(self) -> None:
        super().setUp()
        self.create_test_table()

    def test_01_transaction_commits(self) -> None:
        with connection(self.db_path) as conn:
            with transaction(conn):
                conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('c1')")
                conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('c2')")

        rows = self.repo.query_all(f"SELECT label FROM {TEST_TABLE} ORDER BY label")
        self.assertEqual([r["label"] for r in rows], ["c1", "c2"])

    def test_02_transaction_rolls_back_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with connection(self.db_path) as conn:
                with transaction(conn):
                    conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('r1')")
                    conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('r2')")
                    raise RuntimeError("intentional failure")

        count = self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"]
        self.assertEqual(count, 0, "异常后应回滚，表内不应留下数据")

    def test_03_transaction_rollback_on_sql_error(self) -> None:
        with self.assertRaises(sqlite3.Error):
            with connection(self.db_path) as conn:
                with transaction(conn):
                    conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('s1')")
                    conn.execute("INSERT INTO not_existing_table (x) VALUES (1)")

        count = self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"]
        self.assertEqual(count, 0, "SQL 出错后应回滚")

    def test_04_connection_context_rolls_back_on_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with connection(self.db_path) as conn:
                conn.execute("BEGIN")
                conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('cx')")
                raise RuntimeError("intentional failure")

        count = self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"]
        self.assertEqual(count, 0, "connection() 异常时应回滚")

    def test_05_no_transaction_left_open_after_use(self) -> None:
        with connection(self.db_path) as conn:
            self.assertFalse(conn.in_transaction, "autocommit 模式下不应有隐式事务")

        with connection(self.db_path) as conn:
            with transaction(conn):
                self.assertTrue(conn.in_transaction, "transaction() 内应处于事务中")
            self.assertFalse(conn.in_transaction, "事务结束后不应残留事务")


class RepositoryTest(TempDatabaseTestCase):
    """7. Repository 基础能力。"""

    def setUp(self) -> None:
        super().setUp()
        self.create_test_table()

    def test_01_db_path_and_initialize(self) -> None:
        self.assertEqual(self.repo.db_path, self.db_path)

        self.db_path.unlink(missing_ok=True)
        returned = self.repo.initialize()
        self.assertEqual(Path(returned), self.db_path)
        self.assertTrue(self.db_path.is_file())

    def test_02_execute_returns_affected_rows(self) -> None:
        self.assertEqual(
            self.repo.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES (?)", ("a",)), 1
        )
        self.assertEqual(
            self.repo.execute(f"UPDATE {TEST_TABLE} SET label = 'b' WHERE label = 'a'"), 1
        )
        self.assertEqual(
            self.repo.execute(f"DELETE FROM {TEST_TABLE} WHERE label = 'b'"), 1
        )

    def test_03_executemany_in_one_transaction(self) -> None:
        affected = self.repo.executemany(
            f"INSERT INTO {TEST_TABLE} (label, amount) VALUES (?, ?)",
            [("m1", 1.0), ("m2", 2.0), ("m3", 3.0)],
        )
        self.assertEqual(affected, 3)
        self.assertEqual(
            self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"], 3
        )

    def test_04_executemany_rolls_back_whole_batch_on_error(self) -> None:
        with self.assertRaises(sqlite3.Error):
            self.repo.executemany(
                f"INSERT INTO {TEST_TABLE} (label, amount) VALUES (?, ?)",
                [("ok", 1.0), ("bad", "not-a-number", "too-many-columns")],
            )
        count = self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"]
        self.assertEqual(count, 0, "批量执行失败应整体回滚")

    def test_05_transaction_context_commits_and_rolls_back(self) -> None:
        with self.repo.transaction() as conn:
            conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('t1')")
        self.assertEqual(
            self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"], 1
        )

        with self.assertRaises(RuntimeError):
            with self.repo.transaction() as conn:
                conn.execute(f"INSERT INTO {TEST_TABLE} (label) VALUES ('t2')")
                raise RuntimeError("intentional failure")
        self.assertEqual(
            self.repo.query_one(f"SELECT COUNT(*) AS n FROM {TEST_TABLE}")["n"], 1
        )

    def test_06_query_returns_row_objects(self) -> None:
        self.repo.execute(f"INSERT INTO {TEST_TABLE} (label, amount) VALUES ('r', 3.5)")

        row = self.repo.query_one(f"SELECT label, amount FROM {TEST_TABLE}")
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual(row["label"], "r")

        rows = self.repo.query_all(f"SELECT label FROM {TEST_TABLE}")
        self.assertIsInstance(rows, list)
        self.assertIsInstance(rows[0], sqlite3.Row)

    def test_07_table_exists_and_list_tables(self) -> None:
        self.assertTrue(self.repo.table_exists(TEST_TABLE))
        self.assertFalse(self.repo.table_exists("definitely_missing_table"))
        self.assertEqual(self.repo.list_tables(), [TEST_TABLE])

    def test_08_execute_error_carries_sql_context(self) -> None:
        with self.assertRaises(sqlite3.Error) as ctx:
            self.repo.execute("SELECT * FROM missing_table WHERE id = ?", (1,))
        message = str(ctx.exception)
        self.assertIn("missing_table", message)
        self.assertIn("SQL=", message)
        self.assertIn("params=", message)

    def test_09_repository_uses_given_path_not_default_db(self) -> None:
        # 防止误连正式库：Repository 必须使用传入路径
        self.assertNotEqual(self.repo.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.repo.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
