"""Stage 05：strategies（策略定义与策略版本）测试。

覆盖内容：
    Schema      表存在、字段完整、类型 / NOT NULL / 默认值 / 主键正确
    Unique      (name, version) 重复被拒绝（含绕过 Repository 的原始 SQL）
    Version     同 name 不同 version 允许；不同 name 同 version 允许
    Status      四个合法状态可用；非法状态失败（Python 层 ValueError、数据库 CHECK）
    Config      {} / 嵌套 / list / bool / float / 中文 / JSON round trip
    Config 类型 None / [] / "abc" / 123 等非法输入在 Python 层拒绝
    Update      update_strategy_config / update_strategy_status 不得改动其他字段
    时间        created_at 不变，updated_at 只增不减
    持久化      重开连接后数据仍在
    隔离        全部使用临时数据库，不污染 data/strategy_engine.db

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import (  # noqa: E402  (测试需要先保证项目根目录在 sys.path 中)
    DEFAULT_DB_PATH,
    StrategyRepository,
    initialize_database,
)
from database.models import SCHEMA_VERSION, STRATEGY_STATUSES  # noqa: E402

STRATEGIES_TABLE = "strategies"

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

SAMPLE_CONFIG = {
    "entry": {"type": "support_retest", "lookback": 20},
    "exit": {"stop_loss": 0.02, "take_profit": 0.04},
    "position": {"max_position": 0.1},
}


class StrategyTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_strategies_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "strategies_test.db"
        initialize_database(self.db_path)
        self.repo = StrategyRepository(self.db_path)

    # ------------------------------------------------------------------ 工具
    def create(self, name: str = "mean_reversion", config: object = None, **kwargs) -> int:
        return self.repo.create_strategy(
            name, SAMPLE_CONFIG if config is None else config, **kwargs
        )

    def raw_insert(self, columns: dict[str, object]) -> int:
        """直接用 SQL 插入（列由 dict 的键决定），用于验证数据库层约束。"""
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {STRATEGIES_TABLE} ({names}) VALUES ({placeholders})"
        return self.repo.execute(sql, tuple(columns.values()))

    def total_rows(self) -> int:
        return self.repo.query_one(f"SELECT COUNT(*) AS n FROM {STRATEGIES_TABLE}")["n"]


class StrategySchemaTest(StrategyTestCase):
    """Schema。"""

    def test_01_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.repo.table_exists(STRATEGIES_TABLE))
        # 数据库随阶段演进会继续增加业务表（Stage 06 起还有 signals），
        # 这里只断言 strategies 存在；"没有多余/越界表" 由 test_database.py 负责
        self.assertIn(STRATEGIES_TABLE, self.repo.list_tables())

    def test_02_columns_types_notnull_defaults_and_primary_key(self) -> None:
        columns = {
            row["name"]: row
            for row in self.repo.query_all(f"PRAGMA table_info({STRATEGIES_TABLE})")
        }
        self.assertEqual(
            list(columns.keys()),
            # Stage 19：strategies 增加 strategy_id（该版本属于哪个 Strategy），
            # 由 initialize_database() 的幂等迁移（ALTER TABLE ADD COLUMN）补上
            [
                "id",
                "name",
                "description",
                "status",
                "version",
                "config",
                "created_at",
                "updated_at",
                "strategy_id",
            ],
        )

        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["id"]["pk"], 1)

        for field in ("name", "status", "version", "config", "created_at", "updated_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")
        self.assertEqual(columns["description"]["notnull"], 0, "description 应允许 NULL")
        # strategy_id 允许为 NULL（老数据 / 未建立 Strategy 身份时）
        self.assertEqual(columns["strategy_id"]["notnull"], 0, "strategy_id 应允许 NULL")

        self.assertEqual(columns["name"]["type"].upper(), "TEXT")
        self.assertEqual(columns["description"]["type"].upper(), "TEXT")
        self.assertEqual(columns["status"]["type"].upper(), "TEXT")
        self.assertEqual(columns["version"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["config"]["type"].upper(), "TEXT")
        self.assertEqual(columns["created_at"]["type"].upper(), "TEXT")
        self.assertEqual(columns["updated_at"]["type"].upper(), "TEXT")

        self.assertEqual(columns["status"]["dflt_value"], "'draft'")
        self.assertEqual(columns["version"]["dflt_value"], "1")
        self.assertIsNotNone(columns["created_at"]["dflt_value"])
        self.assertIsNotNone(columns["updated_at"]["dflt_value"])

    def test_03_unique_index_covers_name_and_version(self) -> None:
        indexes = self.repo.query_all(f"PRAGMA index_list({STRATEGIES_TABLE})")
        self.assertEqual(len(indexes), 1, "应只有一个索引（来自 UNIQUE 约束）")
        index = indexes[0]
        self.assertEqual(index["unique"], 1)
        self.assertEqual(index["origin"], "u")

        index_columns = [
            row["name"] for row in self.repo.query_all(f"PRAGMA index_info({index['name']})")
        ]
        self.assertEqual(index_columns, ["name", "version"])

    def test_04_check_constraints_are_declared(self) -> None:
        sql = self.repo.query_one(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (STRATEGIES_TABLE,),
        )["sql"]
        upper = sql.upper()
        self.assertIn("CHECK", upper)
        self.assertIn("CK_STRATEGIES_VERSION", upper)
        self.assertIn("CK_STRATEGIES_STATUS", upper)
        self.assertIn("UQ_STRATEGIES_NAME_VERSION", upper)
        for status in STRATEGY_STATUSES:
            self.assertIn(f"'{status}'", sql)

    def test_05_schema_version_is_recorded(self) -> None:
        # 断言数据库里的 user_version 与代码里的 SCHEMA_VERSION 一致（防止漏初始化/漏迁移）
        self.assertEqual(self.repo.query_one("PRAGMA user_version")[0], SCHEMA_VERSION)


class StrategyCreateTest(StrategyTestCase):
    """创建 / 默认值 / 必填字段。"""

    def test_01_create_and_read_back(self) -> None:
        strategy_id = self.create(description="均值回归策略", status="testing", version=2)
        self.assertIsInstance(strategy_id, int)
        self.assertGreater(strategy_id, 0)

        strategy = self.repo.get_strategy(strategy_id)
        self.assertEqual(strategy["id"], strategy_id)
        self.assertEqual(strategy["name"], "mean_reversion")
        self.assertEqual(strategy["description"], "均值回归策略")
        self.assertEqual(strategy["status"], "testing")
        self.assertEqual(strategy["version"], 2)
        self.assertEqual(strategy["config"], SAMPLE_CONFIG)
        self.assertRegex(strategy["created_at"], TIMESTAMP_PATTERN)
        self.assertRegex(strategy["updated_at"], TIMESTAMP_PATTERN)

    def test_02_defaults_from_python_layer(self) -> None:
        strategy_id = self.create()
        strategy = self.repo.get_strategy(strategy_id)
        self.assertEqual(strategy["status"], "draft")
        self.assertEqual(strategy["version"], 1)
        self.assertIsNone(strategy["description"])

    def test_03_defaults_from_database_definition(self) -> None:
        # 绕过 Repository（不写 status / version 列），验证 DDL 的 DEFAULT 生效
        self.raw_insert({"name": "db_defaults", "config": "{}"})
        strategy = self.repo.get_strategy_by_name_version("db_defaults", 1)
        self.assertEqual(strategy["status"], "draft")
        self.assertEqual(strategy["version"], 1)
        self.assertRegex(strategy["created_at"], TIMESTAMP_PATTERN)

    def test_04_config_and_name_are_required_by_database(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": "no_config"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"config": "{}"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": None, "config": "{}"})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": "x", "config": None})

    def test_05_version_check_constraint(self) -> None:
        self.assertEqual(self.create(version=1), 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": "bad_version", "config": "{}", "version": 0})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": "bad_version", "config": "{}", "version": -1})

    def test_06_python_parameter_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.create_strategy("", {"a": 1})
        with self.assertRaises(ValueError):
            self.repo.create_strategy("   ", {"a": 1})
        with self.assertRaises(ValueError):
            self.repo.create_strategy("ok", {"a": 1}, version=0)
        with self.assertRaises(ValueError):
            self.repo.create_strategy("ok", {"a": 1}, version="1")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            self.repo.create_strategy("ok", {"a": 1}, description=123)  # type: ignore[arg-type]
        self.assertEqual(self.total_rows(), 0, "非法参数不应写入任何数据")

    def test_07_name_is_trimmed(self) -> None:
        strategy_id = self.create(name="  spaced_name  ")
        self.assertEqual(self.repo.get_strategy(strategy_id)["name"], "spaced_name")


class StrategyUniqueTest(StrategyTestCase):
    """Unique + Version。"""

    def test_01_duplicate_name_version_is_rejected_by_repository(self) -> None:
        self.create(version=1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(version=1, config={"changed": True}, status="active")

        self.assertEqual(self.total_rows(), 1, "重复创建不应覆盖旧记录")

    def test_02_duplicate_is_rejected_by_raw_sql_too(self) -> None:
        # 绕过 Repository 直接写 SQL，证明约束来自数据库
        self.raw_insert({"name": "raw_dup", "config": "{}", "version": 1})
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": "raw_dup", "config": "{}", "version": 1})

    def test_03_duplicate_with_default_version_is_rejected(self) -> None:
        self.create()  # 默认 version = 1
        with self.assertRaises(sqlite3.IntegrityError):
            self.create()  # 仍然是默认 version = 1

    def test_04_same_name_different_versions_are_allowed(self) -> None:
        ids = [self.create(name="mean_reversion", version=v) for v in (1, 2, 3)]
        self.assertEqual(len(set(ids)), 3, "每个版本都应是独立记录")
        self.assertEqual(self.total_rows(), 3)

        self.repo.update_strategy_status(ids[0], "retired")
        self.repo.update_strategy_status(ids[1], "testing")

        versions = [
            (s["version"], s["status"])
            for s in self.repo.list_strategies(name="mean_reversion")
        ]
        self.assertEqual(versions, [(1, "retired"), (2, "testing"), (3, "draft")])

    def test_05_different_name_same_version_is_allowed(self) -> None:
        self.create(name="strategy_a", version=1)
        self.create(name="strategy_b", version=1)
        self.assertEqual(self.total_rows(), 2)

    def test_06_versions_are_independent_records(self) -> None:
        v1 = self.create(version=1, config={"threshold": 1.0})
        v2 = self.create(version=2, config={"threshold": 2.0})

        self.assertNotEqual(v1, v2)
        self.assertEqual(self.repo.get_strategy(v1)["config"], {"threshold": 1.0})
        self.assertEqual(self.repo.get_strategy(v2)["config"], {"threshold": 2.0})

    def test_07_get_by_name_version_returns_none_when_missing(self) -> None:
        self.create(version=1)
        self.assertIsNone(self.repo.get_strategy_by_name_version("mean_reversion", 99))
        self.assertIsNone(self.repo.get_strategy_by_name_version("no_such_name", 1))
        self.assertIsNone(self.repo.get_strategy(999999))


class StrategyStatusTest(StrategyTestCase):
    """Status。"""

    def test_01_all_four_statuses_are_allowed(self) -> None:
        for index, status in enumerate(STRATEGY_STATUSES):
            with self.subTest(status=status):
                strategy_id = self.repo.create_strategy(
                    f"strategy_{index}", {"a": 1}, status=status
                )
                self.assertEqual(self.repo.get_strategy(strategy_id)["status"], status)

    def test_02_invalid_status_is_rejected_by_python_layer(self) -> None:
        for bad in ("ACTIVE", "running", "unknown", "", None, 1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.repo.create_strategy("s", {"a": 1}, status=bad)  # type: ignore[arg-type]
        self.assertEqual(self.total_rows(), 0)

    def test_03_invalid_status_is_rejected_by_database_check(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({"name": "bad", "config": "{}", "status": "running"})
        self.assertEqual(self.total_rows(), 0)

    def test_04_update_status(self) -> None:
        strategy_id = self.create()
        self.assertEqual(self.repo.get_strategy(strategy_id)["status"], "draft")

        self.assertTrue(self.repo.update_strategy_status(strategy_id, "testing"))
        self.assertEqual(self.repo.get_strategy(strategy_id)["status"], "testing")

        self.assertTrue(self.repo.update_strategy_status(strategy_id, "active"))
        self.assertTrue(self.repo.update_strategy_status(strategy_id, "retired"))
        self.assertEqual(self.repo.get_strategy(strategy_id)["status"], "retired")

    def test_05_update_status_with_invalid_value_fails_and_keeps_old_value(self) -> None:
        strategy_id = self.create(status="testing")
        with self.assertRaises(ValueError):
            self.repo.update_strategy_status(strategy_id, "running")
        with self.assertRaises(ValueError):
            self.repo.update_strategy_status(strategy_id, "ACTIVE")

        self.assertEqual(
            self.repo.get_strategy(strategy_id)["status"],
            "testing",
            "非法状态不应被静默转换成其他值",
        )

    def test_06_update_status_unknown_id_returns_false(self) -> None:
        self.assertFalse(self.repo.update_strategy_status(999999, "active"))

    def test_07_update_status_does_not_touch_other_fields(self) -> None:
        strategy_id = self.create(description="原始描述", config=SAMPLE_CONFIG, version=3)
        before = self.repo.get_strategy(strategy_id)

        self.repo.update_strategy_status(strategy_id, "active")
        after = self.repo.get_strategy(strategy_id)

        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["name"], before["name"])
        self.assertEqual(after["description"], before["description"])
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["config"], before["config"], "status 更新不应改动 config")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])


class StrategyConfigTest(StrategyTestCase):
    """Config 序列化 / round trip / 非法类型。"""

    def test_01_empty_dict_round_trip(self) -> None:
        strategy_id = self.repo.create_strategy("empty", {})
        self.assertEqual(self.repo.get_strategy(strategy_id)["config"], {})

    def test_02_nested_dict_round_trip(self) -> None:
        config = {"entry": {"type": "support_retest", "lookback": 20}}
        strategy_id = self.repo.create_strategy("nested", config)
        retrieved = self.repo.get_strategy(strategy_id)
        self.assertEqual(retrieved["config"], config)
        self.assertIsInstance(retrieved["config"]["entry"], dict)

    def test_03_mixed_types_round_trip(self) -> None:
        config = {
            "entry": {"threshold": 1.5, "enabled": True},
            "symbols": ["600519", "000001"],
            "note": "中文描述：支持重测",
            "empty_list": [],
            "nested_list": [{"k": [1, 2, 3]}],
            "nothing": None,
            "int_value": 42,
        }
        strategy_id = self.repo.create_strategy("mixed", config)
        retrieved = self.repo.get_strategy(strategy_id)
        self.assertEqual(retrieved["config"], config, "JSON round trip 必须语义完全一致")
        self.assertTrue(retrieved["config"]["entry"]["enabled"])
        self.assertAlmostEqual(retrieved["config"]["entry"]["threshold"], 1.5)
        self.assertEqual(retrieved["config"]["symbols"], ["600519", "000001"])

    def test_04_config_is_stored_as_json_text(self) -> None:
        strategy_id = self.repo.create_strategy("text_check", {"中文": "值", "n": 1})
        raw = self.repo.query_one(
            f"SELECT config, typeof(config) AS t FROM {STRATEGIES_TABLE} WHERE id = ?",
            (strategy_id,),
        )
        self.assertEqual(raw["t"], "text", "config 列应以文本形式存储 JSON")
        self.assertIsInstance(raw["config"], str)
        self.assertEqual(json.loads(raw["config"]), {"中文": "值", "n": 1})
        self.assertIn("中文", raw["config"], "中文不应被转义成 \\uXXXX")

    def test_05_invalid_config_types_are_rejected_on_create(self) -> None:
        for bad in (None, [], "abc", 123, 1.5, True, ("a", 1)):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.repo.create_strategy("bad_config", bad)  # type: ignore[arg-type]
        self.assertEqual(self.total_rows(), 0, "非法 config 不应写入数据库")

    def test_06_invalid_config_types_are_rejected_on_update(self) -> None:
        strategy_id = self.create()
        for bad in (None, [], "abc", 123):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.repo.update_strategy_config(strategy_id, bad)  # type: ignore[arg-type]
        self.assertEqual(
            self.repo.get_strategy(strategy_id)["config"], SAMPLE_CONFIG, "原配置不应被破坏"
        )

    def test_07_non_serializable_config_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.create_strategy("unserializable", {"bad": {1, 2, 3}})
        self.assertEqual(self.total_rows(), 0)

    def test_08_update_config_round_trip(self) -> None:
        strategy_id = self.create()
        new_config = {"entry": {"type": "breakout", "lookback": 55}, "weights": [0.5, 0.5]}

        self.assertTrue(self.repo.update_strategy_config(strategy_id, new_config))
        self.assertEqual(self.repo.get_strategy(strategy_id)["config"], new_config)

    def test_09_update_config_unknown_id_returns_false(self) -> None:
        self.assertFalse(self.repo.update_strategy_config(999999, {"a": 1}))

    def test_10_update_config_does_not_touch_other_fields(self) -> None:
        strategy_id = self.create(description="描述", status="testing", version=2)
        before = self.repo.get_strategy(strategy_id)

        self.repo.update_strategy_config(strategy_id, {"changed": True})
        after = self.repo.get_strategy(strategy_id)

        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["name"], before["name"])
        self.assertEqual(after["description"], before["description"])
        self.assertEqual(after["status"], before["status"], "config 更新不应改动 status")
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["config"], {"changed": True})
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])


class StrategyQueryTest(StrategyTestCase):
    """list / count / 排序 / limit。"""

    def setUp(self) -> None:
        super().setUp()
        self.a1 = self.create(name="alpha", version=1, status="draft")
        self.a2 = self.create(name="alpha", version=2, status="testing")
        self.b1 = self.create(name="beta", version=1, status="active")
        self.c1 = self.create(name="gamma", version=1, status="retired")

    def test_01_list_returns_all_sorted_by_created_at(self) -> None:
        strategies = self.repo.list_strategies()
        self.assertEqual(len(strategies), 4)
        self.assertEqual(
            [(s["name"], s["version"]) for s in strategies],
            [("alpha", 1), ("alpha", 2), ("beta", 1), ("gamma", 1)],
        )
        created = [s["created_at"] for s in strategies]
        self.assertEqual(created, sorted(created), "应按 created_at 升序")

    def test_02_list_filtered_by_name(self) -> None:
        strategies = self.repo.list_strategies(name="alpha")
        self.assertEqual([s["version"] for s in strategies], [1, 2])

    def test_03_list_filtered_by_status(self) -> None:
        self.assertEqual(len(self.repo.list_strategies(status="draft")), 1)
        self.assertEqual(len(self.repo.list_strategies(status="testing")), 1)
        self.assertEqual(len(self.repo.list_strategies(status="active")), 1)
        self.assertEqual(len(self.repo.list_strategies(status="retired")), 1)
        self.assertEqual(self.repo.list_strategies(status="draft")[0]["name"], "alpha")

    def test_04_list_filtered_by_name_and_status(self) -> None:
        strategies = self.repo.list_strategies(name="alpha", status="testing")
        self.assertEqual([s["version"] for s in strategies], [2])

    def test_05_list_invalid_filters_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.list_strategies(status="running")
        with self.assertRaises(ValueError):
            self.repo.list_strategies(name="")
        with self.assertRaises(ValueError):
            self.repo.list_strategies(limit=0)

    def test_06_list_limit_returns_most_recent_n_in_ascending_order(self) -> None:
        strategies = self.repo.list_strategies(limit=2)
        self.assertEqual(
            [(s["name"], s["version"]) for s in strategies],
            [("beta", 1), ("gamma", 1)],
            "limit 表示'最近的 N 条'，返回时仍按升序",
        )

    def test_07_count_strategies(self) -> None:
        self.assertEqual(self.repo.count_strategies(), 4)
        self.assertEqual(self.repo.count_strategies(name="alpha"), 2)
        self.assertEqual(self.repo.count_strategies(status="draft"), 1)
        self.assertEqual(self.repo.count_strategies(name="alpha", status="draft"), 1)
        self.assertEqual(self.repo.count_strategies(name="no_such"), 0)

    def test_08_get_strategy_by_name_version(self) -> None:
        strategy = self.repo.get_strategy_by_name_version("alpha", 2)
        self.assertEqual(strategy["id"], self.a2)
        self.assertEqual(strategy["status"], "testing")


class StrategyTimeTest(StrategyTestCase):
    """时间字段行为。"""

    def test_01_timestamps_exist_on_create_and_are_utc(self) -> None:
        strategy_id = self.create()
        strategy = self.repo.get_strategy(strategy_id)
        self.assertRegex(strategy["created_at"], TIMESTAMP_PATTERN)
        self.assertRegex(strategy["updated_at"], TIMESTAMP_PATTERN)

        created_at = datetime.strptime(strategy["created_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        delta = abs((datetime.now(timezone.utc) - created_at).total_seconds())
        self.assertLess(delta, 300, "created_at 应为 UTC 时间（偏差过大说明用了本地时区）")

    def test_02_created_at_never_changes(self) -> None:
        strategy_id = self.create()
        created_at = self.repo.get_strategy(strategy_id)["created_at"]

        self.repo.update_strategy_status(strategy_id, "active")
        self.repo.update_strategy_config(strategy_id, {"x": 1})
        self.repo.update_strategy_status(strategy_id, "retired")

        self.assertEqual(self.repo.get_strategy(strategy_id)["created_at"], created_at)

    def test_03_updated_at_never_decreases(self) -> None:
        strategy_id = self.create()
        previous = self.repo.get_strategy(strategy_id)["updated_at"]

        for action in (
            lambda: self.repo.update_strategy_config(strategy_id, {"step": 1}),
            lambda: self.repo.update_strategy_status(strategy_id, "testing"),
            lambda: self.repo.update_strategy_config(strategy_id, {"step": 2}),
        ):
            action()
            current = self.repo.get_strategy(strategy_id)["updated_at"]
            self.assertGreaterEqual(current, previous, "updated_at 不应回退")
            self.assertRegex(current, TIMESTAMP_PATTERN)
            previous = current


class StrategyPersistenceTest(StrategyTestCase):
    """持久化 + 不污染正式数据库。"""

    def test_01_persists_after_reconnect(self) -> None:
        strategy_id = self.create(name="persist_me", version=2, status="active")
        del self.repo

        reopened = StrategyRepository(self.db_path)
        strategy = reopened.get_strategy(strategy_id)
        self.assertIsNotNone(strategy)
        self.assertEqual(strategy["name"], "persist_me")
        self.assertEqual(strategy["version"], 2)
        self.assertEqual(strategy["status"], "active")
        self.assertEqual(strategy["config"], SAMPLE_CONFIG)

    def test_02_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.repo.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.repo.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
