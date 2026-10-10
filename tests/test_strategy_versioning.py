"""Stage 19：Strategy 身份 / Strategy Version 的数据层测试。

覆盖：
    1. 创建 Strategy
    2. 创建 Strategy Version
    3. 一个 Strategy 可以存在多个 Version
    + schema（新表 / 新列 / 幂等迁移 / 老数据回填 / 版本号）
    + 严格创建与约束（name 唯一、status 合法、(name, version) 不重复）
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

from database import (  # noqa: E402
    StrategyDefinitionRepository,
    StrategyRepository,
    database_info,
    initialize_database,
)
from database import models  # noqa: E402

DEFINITIONS_TABLE = models.STRATEGY_DEFINITIONS_TABLE
STRATEGIES_TABLE = models.STRATEGIES_TABLE
SAMPLE_CONFIG = {"type": "close_direction", "min_move": 0.0}


class VersioningTestCase(unittest.TestCase):
    """基类：独立临时数据库 + 两个 Repository。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_versioning_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "versioning.db"
        initialize_database(self.db_path)
        self.definitions = StrategyDefinitionRepository(self.db_path)
        self.versions = StrategyRepository(self.db_path)


class SchemaTest(VersioningTestCase):
    """Schema：新表 / 新列 / 迁移 / 版本号。"""

    def test_01_strategy_definitions_table_exists(self) -> None:
        self.assertTrue(self.definitions.table_exists(DEFINITIONS_TABLE))
        self.assertIn(DEFINITIONS_TABLE, database_info(self.db_path)["tables"])

    def test_02_columns_and_constraints(self) -> None:
        columns = {
            row["name"]: row
            for row in self.definitions.query_all(f"PRAGMA table_info({DEFINITIONS_TABLE})")
        }
        self.assertEqual(
            list(columns.keys()),
            ["id", "name", "description", "status", "created_at", "updated_at"],
        )
        for field in ("name", "status", "created_at", "updated_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")
        self.assertEqual(columns["description"]["notnull"], 0, "description 应允许 NULL")

        sql = self.definitions.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (DEFINITIONS_TABLE,),
        )["sql"]
        upper = sql.upper()
        self.assertIn("UNIQUE", upper)
        self.assertIn("UQ_STRATEGY_DEFINITIONS_NAME", upper)
        self.assertIn("CK_STRATEGY_DEFINITIONS_STATUS", upper)
        for status in models.STRATEGY_STATUSES:
            self.assertIn(f"'{status}'", sql)

    def test_03_strategies_has_strategy_id_column_with_fk(self) -> None:
        columns = {
            row["name"]: row
            for row in self.versions.query_all(f"PRAGMA table_info({STRATEGIES_TABLE})")
        }
        self.assertIn("strategy_id", columns)
        self.assertEqual(columns["strategy_id"]["notnull"], 0, "strategy_id 应允许 NULL")

        foreign_keys = self.versions.query_all(f"PRAGMA foreign_key_list({STRATEGIES_TABLE})")
        self.assertIn(
            DEFINITIONS_TABLE,
            [row["table"] for row in foreign_keys],
            "strategies.strategy_id 应引用 strategy_definitions(id)",
        )

    def test_04_schema_version_and_idempotent_initialize(self) -> None:
        # Stage 19 引入时是 8；Stage 20 新增 symbol_groups / symbol_group_members 后为 9
        self.assertEqual(models.SCHEMA_VERSION, 9)
        self.assertEqual(
            self.versions.query_one("PRAGMA user_version")[0], models.SCHEMA_VERSION
        )

        # 重复初始化不应报错，也不应改动数据库（迁移幂等）
        size_before = self.db_path.stat().st_size
        initialize_database(self.db_path)
        initialize_database(self.db_path)
        self.assertEqual(self.db_path.stat().st_size, size_before)

    def test_05_backfill_links_legacy_versions(self) -> None:
        # 模拟 Stage 19 之前写入的版本行（没有 strategy_id）
        self.versions.execute(
            f"INSERT INTO {STRATEGIES_TABLE} "
            "(name, description, status, version, config, created_at, updated_at) "
            "VALUES (?, NULL, 'active', 1, '{\"type\": \"close_direction\"}', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            ("legacy_strategy",),
        )
        self.assertIsNone(
            self.versions.query_one(
                f"SELECT strategy_id FROM {STRATEGIES_TABLE} WHERE name = ?",
                ("legacy_strategy",),
            )["strategy_id"]
        )

        initialize_database(self.db_path)  # 幂等回填

        definition = self.definitions.get_definition_by_name("legacy_strategy")
        self.assertIsNotNone(definition, "老数据应被补齐 Strategy 身份")
        version = self.versions.get_version_by_number(int(definition["id"]), 1)
        self.assertIsNotNone(version)
        self.assertEqual(version["strategy_id"], definition["id"], "老数据应被关联到 Strategy")


class StrategyDefinitionTest(VersioningTestCase):
    """Strategy 身份（strategy_definitions）。"""

    def test_01_create_and_read_back(self) -> None:
        strategy_id = self.definitions.create_definition(
            "Support_Rebound", description="支撑位反弹策略"
        )
        self.assertIsInstance(strategy_id, int)

        definition = self.definitions.get_definition(strategy_id)
        self.assertEqual(definition["name"], "Support_Rebound")
        self.assertEqual(definition["description"], "支撑位反弹策略")
        self.assertEqual(definition["status"], "draft")
        self.assertRegex(definition["created_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_02_name_is_unique_and_not_overwritten(self) -> None:
        self.definitions.create_definition("Support_Rebound")
        with self.assertRaises(sqlite3.IntegrityError):
            self.definitions.create_definition("Support_Rebound")

    def test_03_invalid_input_is_rejected(self) -> None:
        for bad in ("", "   ", None, 123):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    self.definitions.create_definition(bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.definitions.create_definition("ok", status="not-a-status")

    def test_04_ensure_definition_is_get_or_create(self) -> None:
        first = self.definitions.ensure_definition("close_direction_v1")
        second = self.definitions.ensure_definition("close_direction_v1", description="ignored")
        self.assertEqual(first, second, "同名 Strategy 不应重复创建")
        self.assertEqual(self.definitions.count_definitions(), 1)

    def test_05_list_count_and_status_update(self) -> None:
        a = self.definitions.create_definition("alpha")
        b = self.definitions.create_definition("beta", status="testing")
        self.assertEqual([d["id"] for d in self.definitions.list_definitions()], [a, b])
        self.assertEqual([d["name"] for d in self.definitions.list_definitions(status="testing")], ["beta"])

        self.assertTrue(self.definitions.update_definition_status(a, "active"))
        self.assertEqual(self.definitions.get_definition(a)["status"], "active")
        self.assertFalse(self.definitions.update_definition_status(999_999, "active"))
        with self.assertRaises(ValueError):
            self.definitions.update_definition_status(a, "bogus")


class StrategyVersionTest(VersioningTestCase):
    """Strategy Version（strategies 表，每版一行）。"""

    def test_01_create_version_auto_numbers(self) -> None:
        strategy_id = self.definitions.create_definition("Support_Rebound")

        v1 = self.versions.create_version(strategy_id, {"lookback": 20})
        v2 = self.versions.create_version(strategy_id, {"lookback": 30})
        v3 = self.versions.create_version(strategy_id, {"lookback": 40})

        self.assertEqual(
            [self.versions.get_version(v)["version"] for v in (v1, v2, v3)], [1, 2, 3]
        )
        for version_id in (v1, v2, v3):
            row = self.versions.get_version(version_id)
            self.assertEqual(row["strategy_id"], strategy_id, "版本必须关联 Strategy 身份")
            self.assertEqual(row["name"], "Support_Rebound", "版本继承 Strategy 名字")

    def test_02_one_strategy_can_have_multiple_versions(self) -> None:
        strategy_id = self.definitions.create_definition("Support_Rebound")
        self.versions.create_version(strategy_id, {"v": 1})
        self.versions.create_version(strategy_id, {"v": 2})

        versions = self.versions.list_versions(strategy_id)
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertEqual([row["config"] for row in versions], [{"v": 1}, {"v": 2}])

    def test_03_versions_of_different_strategies_are_independent(self) -> None:
        first = self.definitions.create_definition("strategy_a")
        second = self.definitions.create_definition("strategy_b")
        self.versions.create_version(first, {"who": "a"})
        self.versions.create_version(first, {"who": "a2"})
        self.versions.create_version(second, {"who": "b"})

        self.assertEqual([row["version"] for row in self.versions.list_versions(first)], [1, 2])
        self.assertEqual([row["version"] for row in self.versions.list_versions(second)], [1])

    def test_04_explicit_version_and_duplicate_rejection(self) -> None:
        strategy_id = self.definitions.create_definition("Support_Rebound")
        self.versions.create_version(strategy_id, {"v": 1}, version=5)
        self.assertEqual(self.versions.next_version(strategy_id), 6)

        with self.assertRaises(sqlite3.IntegrityError):
            self.versions.create_version(strategy_id, {"v": 1}, version=5)

    def test_05_unknown_strategy_or_invalid_version(self) -> None:
        with self.assertRaises(ValueError):
            self.versions.create_version(999_999, {"v": 1})
        with self.assertRaises(ValueError):
            self.versions.create_version(0, {"v": 1})

        strategy_id = self.definitions.create_definition("Support_Rebound")
        with self.assertRaises(ValueError):
            self.versions.create_version(strategy_id, {"v": 1}, version=0)
        self.assertIsNone(self.versions.get_version_by_number(strategy_id, 3))
        self.assertIsNone(self.versions.get_version(999_999))

    def test_06_legacy_create_strategy_still_works_and_links(self) -> None:
        # 旧调用方式（Stage 05 的签名）保持可用，并自动建立 / 复用 Strategy 身份
        v1 = self.versions.create_strategy("mean_reversion", SAMPLE_CONFIG, version=1)
        v2 = self.versions.create_strategy(
            "mean_reversion", {"type": "close_direction", "min_move": 0.01}, version=2
        )
        self.assertEqual(self.definitions.count_definitions(), 1)
        definition = self.definitions.get_definition_by_name("mean_reversion")
        self.assertEqual(self.versions.get_version(v1)["strategy_id"], definition["id"])
        self.assertEqual(self.versions.get_version(v2)["strategy_id"], definition["id"])

    def test_07_list_versions_status_filter(self) -> None:
        strategy_id = self.definitions.create_definition("Support_Rebound")
        self.versions.create_version(strategy_id, {"v": 1}, status="active")
        self.versions.create_version(strategy_id, {"v": 2}, status="retired")
        self.assertEqual(
            [row["version"] for row in self.versions.list_versions(strategy_id, status="active")],
            [1],
        )


if __name__ == "__main__":
    unittest.main()
