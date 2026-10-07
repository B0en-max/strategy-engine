"""阶段 00：环境准备的最小验收测试。

目的只有一个：确认当前 Python 环境自带的 sqlite3 模块能够
真实地创建数据库文件、写入数据、读取数据、并且事务可回滚。

不涉及 Strategy Engine 的任何业务功能。

运行方式（在项目根目录下执行）：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
    .venv\\Scripts\\python.exe tests\\test_sqlite_minimal.py
"""

import os
import sqlite3
import sys
import tempfile
import unittest


class MinimalSQLiteTest(unittest.TestCase):
    """最小 SQLite 可用性验证。"""

    def setUp(self) -> None:
        # 使用临时目录，不污染项目目录；ignore_cleanup_errors 避免 Windows 上偶发占用报错
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_sqlite_", ignore_cleanup_errors=True
        )
        self.db_path = os.path.join(self._tmpdir.name, "minimal_test.db")
        self.conn = sqlite3.connect(self.db_path)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        try:
            self.conn.close()
        finally:
            self._tmpdir.cleanup()

    def test_01_create_table_and_file(self) -> None:
        tables_before = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        self.assertEqual(tables_before, [], "新库中不应存在任何表")

        self.conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        self.conn.commit()

        self.assertTrue(os.path.exists(self.db_path), "数据库文件应被真实创建")
        self.assertGreater(os.path.getsize(self.db_path), 0, "写入表结构后文件不应为空")
        tables_after = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        self.assertEqual(tables_after, [("t",)])

    def test_02_write_and_read_back(self) -> None:
        self.conn.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT, qty REAL)"
        )
        self.conn.executemany(
            "INSERT INTO trades (symbol, qty) VALUES (?, ?)",
            [("TEST-A", 1.5), ("TEST-B", 2.0)],
        )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT symbol, qty FROM trades ORDER BY id"
        ).fetchall()
        self.assertEqual(rows, [("TEST-A", 1.5), ("TEST-B", 2.0)])

    def test_03_data_survives_reconnect(self) -> None:
        self.conn.execute("CREATE TABLE t (v TEXT)")
        self.conn.execute("INSERT INTO t (v) VALUES ('persisted')")
        self.conn.commit()
        self.conn.close()

        conn2 = sqlite3.connect(self.db_path)
        try:
            got = conn2.execute("SELECT v FROM t").fetchone()
        finally:
            conn2.close()

        self.assertEqual(got[0], "persisted")

    def test_04_transaction_rollback(self) -> None:
        self.conn.execute("CREATE TABLE t (v INTEGER)")
        self.conn.commit()
        with self.assertRaises(RuntimeError):
            with self.conn:  # 异常时自动回滚
                self.conn.execute("INSERT INTO t (v) VALUES (1)")
                raise RuntimeError("intentional failure")

        count = self.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        self.assertEqual(count, 0, "事务失败后应回滚，表内不应有数据")

    def test_05_environment_versions_reported(self) -> None:
        version = sqlite3.sqlite_version
        self.assertRegex(version, r"^\d+\.\d+\.\d+")
        self.assertTrue(
            sys.version_info >= (3, 11),
            f"Python 版本过低: {sys.version}",
        )
        print(
            f"\n[info] Python {sys.version.split()[0]} ({sys.executable})"
            f" | SQLite {version}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
