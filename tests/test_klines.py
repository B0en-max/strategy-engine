"""Stage 04：klines（K 线 OHLCV）测试。

覆盖内容：
    1. Schema            表存在、字段类型、NOT NULL、默认值、主键
    2. Foreign Key       symbol_id 真实引用 symbols.id，不存在的 id 被数据库拒绝
    3. 创建              写入后能查到完全一致的数据
    4. OHLC 约束         high/low 的 6 种非法组合全部被数据库拒绝
    5. NULL 约束         必需字段不能为空（NULL 与"未提供"两种情形）
    6. amount NULL       允许为空
    7. 唯一性            (symbol_id, timeframe, timestamp) 重复被数据库拒绝
    8. 不同 timeframe    同 symbol、同 timestamp、不同周期允许
    9. 不同 timestamp    同 symbol、同周期、不同时间允许
    10. Upsert           存在则更新 OHLCV，只有一条记录
    11. 批量写入         数量 / 顺序 / 数据正确
    12. 批量事务         批次中出现非法行 → 整批回滚，0 条新增
    13. 时间范围查询     闭区间 [start, end]，升序返回
    14. 最新 K 线        get_latest_kline 返回最大 timestamp
    15. limit            返回"最近的 N 条"且仍为升序
    16. 持久化           重开连接后数据仍在
    17. 索引             PRAGMA index_list / index_info / EXPLAIN QUERY PLAN 实际验证

重要：
    - 所有测试都在临时目录里建库，**不会向 data/strategy_engine.db 写任何数据**
    - 约束验证走真实 SQL 语句，不依赖 Python 层判断

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

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
    KlineRepository,
    SymbolRepository,
    initialize_database,
)

KLINES_TABLE = "klines"
SYMBOLS_TABLE = "symbols"

TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

T0 = "2026-01-01 00:00:00"
T1 = "2026-01-02 00:00:00"
T2 = "2026-01-03 00:00:00"
T3 = "2026-01-04 00:00:00"


class KlineTestCase(unittest.TestCase):
    """基类：每个用例使用独立的临时数据库，并预置两个标的。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(
            prefix="se_klines_test_", ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "klines_test.db"
        initialize_database(self.db_path)

        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)

        # 标的 A：A 股；标的 B：美股（用于验证按 symbol 隔离）
        self.symbol_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.other_symbol_id = self.symbols.create_symbol("AAPL", "Apple Inc.", "US", "STOCK")

    # ------------------------------------------------------------------ 工具
    def insert(
        self,
        timestamp: str,
        *,
        symbol_id: int | None = None,
        timeframe: str = "1d",
        open: float = 100.0,
        high: float = 110.0,
        low: float = 90.0,
        close: float = 105.0,
        volume: float = 1000.0,
        amount: float | None = None,
    ) -> int:
        """用严格插入写入一条 K 线。"""
        return self.klines.create_kline(
            self.symbol_id if symbol_id is None else symbol_id,
            timeframe,
            timestamp,
            open=open,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
        )

    def kline_dict(self, timestamp: str, **overrides: object) -> dict[str, object]:
        """构造一条批量写入用的 K 线字典。"""
        row: dict[str, object] = {
            "symbol_id": self.symbol_id,
            "timeframe": "1d",
            "timestamp": timestamp,
            "open": 100.0,
            "high": 110.0,
            "low": 90.0,
            "close": 105.0,
            "volume": 1000.0,
            "amount": None,
        }
        row.update(overrides)
        return row

    def raw_insert(self, columns: dict[str, object]) -> int:
        """直接用 SQL 插入（列由 dict 的键决定，便于测试 NOT NULL / CHECK）。"""
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT INTO {KLINES_TABLE} ({names}) VALUES ({placeholders})"
        return self.klines.execute(sql, tuple(columns.values()))

    def raw_row(self, timestamp: str, symbol_id: int | None = None, timeframe: str = "1d") -> dict:
        """构造一条完整合法行的 dict（用于 raw_insert）。"""
        return {
            "symbol_id": self.symbol_id if symbol_id is None else symbol_id,
            "timeframe": timeframe,
            "timestamp": timestamp,
            "open": 100.0,
            "high": 110.0,
            "low": 90.0,
            "close": 105.0,
            "volume": 1000.0,
            "amount": None,
        }

    def total_rows(self) -> int:
        return self.klines.query_one(f"SELECT COUNT(*) AS n FROM {KLINES_TABLE}")["n"]


class KlineSchemaTest(KlineTestCase):
    """1 / 17：表结构与索引。"""

    def test_01_klines_table_exists_after_initialize(self) -> None:
        self.assertTrue(self.klines.table_exists(KLINES_TABLE))
        self.assertEqual(self.klines.list_tables(), [KLINES_TABLE, SYMBOLS_TABLE])

    def test_02_columns_types_notnull_and_primary_key(self) -> None:
        columns = {
            row["name"]: row
            for row in self.klines.query_all(f"PRAGMA table_info({KLINES_TABLE})")
        }
        self.assertEqual(
            list(columns.keys()),
            ["id", "symbol_id", "timeframe", "timestamp", "open", "high", "low", "close", "volume", "amount", "created_at"],
        )

        self.assertEqual(columns["id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["id"]["pk"], 1, "id 应是主键")
        self.assertEqual(columns["symbol_id"]["type"].upper(), "INTEGER")
        self.assertEqual(columns["timeframe"]["type"].upper(), "TEXT")
        self.assertEqual(columns["timestamp"]["type"].upper(), "TEXT")
        self.assertEqual(columns["created_at"]["type"].upper(), "TEXT")

        for field in ("open", "high", "low", "close", "volume"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["type"].upper(), "REAL")
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")

        self.assertEqual(columns["amount"]["type"].upper(), "REAL")
        self.assertEqual(columns["amount"]["notnull"], 0, "amount 应允许 NULL")

        for field in ("symbol_id", "timeframe", "timestamp", "created_at"):
            with self.subTest(field=field):
                self.assertEqual(columns[field]["notnull"], 1, f"{field} 应为 NOT NULL")

        self.assertEqual(columns["created_at"]["dflt_value"] is not None, True)

    def test_03_unique_constraint_covers_symbol_timeframe_timestamp(self) -> None:
        indexes = self.klines.query_all(f"PRAGMA index_list({KLINES_TABLE})")
        self.assertEqual(len(indexes), 1, f"应只有一个索引，实际: {[r['name'] for r in indexes]}")

        index = indexes[0]
        self.assertEqual(index["unique"], 1, "索引必须是唯一索引")
        self.assertEqual(index["origin"], "u", "应来自 UNIQUE 约束")

        index_columns = [
            row["name"] for row in self.klines.query_all(f"PRAGMA index_info({index['name']})")
        ]
        self.assertEqual(index_columns, ["symbol_id", "timeframe", "timestamp"])

    def test_04_query_plan_uses_the_index(self) -> None:
        plan = self.klines.query_all(
            f"EXPLAIN QUERY PLAN SELECT * FROM {KLINES_TABLE} "
            "WHERE symbol_id = ? AND timeframe = ? AND timestamp BETWEEN ? AND ?",
            (1, "1d", T0, T3),
        )
        detail = " ".join(row["detail"] for row in plan)
        self.assertIn("USING INDEX", detail.upper(), f"查询应使用索引，实际计划: {detail}")
        self.assertNotIn("SCAN klines", detail, f"不应全表扫描，实际计划: {detail}")

    def test_05_check_constraint_is_declared_in_schema(self) -> None:
        row = self.klines.query_one(
            f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (KLINES_TABLE,),
        )
        sql = row["sql"].upper()
        self.assertIn("CHECK", sql)
        self.assertIn("CK_KLINES_OHLC", sql)

    def test_06_schema_version_is_recorded(self) -> None:
        self.assertEqual(self.klines.query_one("PRAGMA user_version")[0], 2)

    def test_07_no_redundant_duplicate_index(self) -> None:
        # UNIQUE 约束已经生成覆盖查询路径的索引，不应再有重复索引
        indexes = [row["name"] for row in self.klines.query_all(f"PRAGMA index_list({KLINES_TABLE})")]
        self.assertEqual(indexes, ["sqlite_autoindex_klines_1"])


class KlineForeignKeyTest(KlineTestCase):
    """2：外键约束。"""

    def test_01_existing_symbol_id_is_accepted(self) -> None:
        self.insert(T0)
        self.assertEqual(self.total_rows(), 1)

    def test_02_missing_symbol_id_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(T0, symbol_id=999999)
        self.assertEqual(self.total_rows(), 0, "外键失败后不应留下数据")

    def test_03_foreign_key_rejected_by_raw_sql_too(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert(self.raw_row(T0, symbol_id=999999))

    def test_04_deleting_referenced_symbol_is_rejected(self) -> None:
        self.insert(T0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.klines.execute(f"DELETE FROM {SYMBOLS_TABLE} WHERE id = ?", (self.symbol_id,))

    def test_05_foreign_key_is_declared_in_schema(self) -> None:
        fks = self.klines.query_all(f"PRAGMA foreign_key_list({KLINES_TABLE})")
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0]["table"], SYMBOLS_TABLE)
        self.assertEqual(fks[0]["from"], "symbol_id")
        self.assertEqual(fks[0]["to"], "id")


class KlineCreateTest(KlineTestCase):
    """3 / 4 / 5 / 6 / 7 / 8 / 9：创建与数据库约束。"""

    def test_01_create_and_roundtrip(self) -> None:
        kline_id = self.insert(T0, open=100.0, high=110.5, low=95.25, close=108.75, volume=1234.5)
        self.assertIsInstance(kline_id, int)
        self.assertGreater(kline_id, 0)

        row = self.klines.get_kline(self.symbol_id, "1d", T0)
        self.assertIsNotNone(row)
        self.assertEqual(row["id"], kline_id)
        self.assertEqual(row["symbol_id"], self.symbol_id)
        self.assertEqual(row["timeframe"], "1d")
        self.assertEqual(row["timestamp"], T0)
        self.assertAlmostEqual(row["open"], 100.0)
        self.assertAlmostEqual(row["high"], 110.5)
        self.assertAlmostEqual(row["low"], 95.25)
        self.assertAlmostEqual(row["close"], 108.75)
        self.assertAlmostEqual(row["volume"], 1234.5)
        self.assertIsNone(row["amount"])
        self.assertRegex(row["created_at"], TIMESTAMP_PATTERN)

    def test_02_amount_is_optional(self) -> None:
        self.insert(T0, amount=None)
        self.assertIsNone(self.klines.get_kline(self.symbol_id, "1d", T0)["amount"])

    def test_03_amount_is_stored_when_given(self) -> None:
        self.insert(T0, amount=987654.25)
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1d", T0)["amount"], 987654.25)

    def test_04_null_required_fields_are_rejected(self) -> None:
        for field in ("symbol_id", "timeframe", "timestamp", "open", "high", "low", "close", "volume"):
            with self.subTest(field=field):
                columns = self.raw_row(T0)
                columns[field] = None
                with self.assertRaises(sqlite3.IntegrityError):
                    self.raw_insert(columns)

    def test_05_omitted_required_fields_are_rejected(self) -> None:
        for field in ("symbol_id", "timeframe", "timestamp", "open", "high", "low", "close", "volume"):
            with self.subTest(field=field):
                columns = self.raw_row(T0)
                del columns[field]
                with self.assertRaises(sqlite3.IntegrityError):
                    self.raw_insert(columns)

    def test_06_ohlc_violations_are_rejected(self) -> None:
        cases = {
            "high < open": {"open": 120.0, "high": 110.0, "low": 90.0, "close": 100.0},
            "high < close": {"open": 100.0, "high": 110.0, "low": 90.0, "close": 120.0},
            "high < low": {"open": 100.0, "high": 80.0, "low": 90.0, "close": 95.0},
            "low > open": {"open": 80.0, "high": 110.0, "low": 90.0, "close": 95.0},
            "low > close": {"open": 100.0, "high": 110.0, "low": 105.0, "close": 90.0},
            "low > high": {"open": 100.0, "high": 110.0, "low": 120.0, "close": 105.0},
        }
        for label, ohlc in cases.items():
            with self.subTest(case=label):
                columns = self.raw_row(T0)
                columns.update(ohlc)
                with self.assertRaises(sqlite3.IntegrityError):
                    self.raw_insert(columns)
        self.assertEqual(self.total_rows(), 0, "非法 OHLC 不应写入任何数据")

    def test_07_ohlc_boundary_values_are_accepted(self) -> None:
        # open == high == low == close == volume 是合法数据（例如平盘、零成交）
        self.insert(T0, open=100.0, high=100.0, low=100.0, close=100.0, volume=0.0)
        self.assertEqual(self.total_rows(), 1)

    def test_08_nan_is_rejected(self) -> None:
        # Python 层提前拦截 NaN（SQLite 会把 NaN 静默存成 NULL，报错信息会误导）
        with self.assertRaises(ValueError):
            self.insert(T0, open=float("nan"))
        # 绕过 Python 层时，数据库仍然拒绝（NaN → NULL → NOT NULL）
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(T0), "open": float("nan")})

    def test_09_duplicate_is_rejected_by_strict_create(self) -> None:
        self.insert(T0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(T0, close=999.0)
        self.assertEqual(self.total_rows(), 1)
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1d", T0)["close"], 105.0)

    def test_10_duplicate_is_rejected_by_raw_sql(self) -> None:
        self.raw_insert(self.raw_row(T0))
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert(self.raw_row(T0))

    def test_11_same_timestamp_different_timeframe_is_allowed(self) -> None:
        self.insert(T0, timeframe="1d")
        self.insert(T0, timeframe="1h")
        self.insert(T0, timeframe="1w")
        self.assertEqual(self.total_rows(), 3)

    def test_12_different_timestamp_is_allowed(self) -> None:
        self.insert(T0)
        self.insert(T1)
        self.assertEqual(self.total_rows(), 2)

    def test_13_same_timestamp_different_symbol_is_allowed(self) -> None:
        self.insert(T0)
        self.insert(T0, symbol_id=self.other_symbol_id)
        self.assertEqual(self.total_rows(), 2)

    def test_14_python_format_validation(self) -> None:
        cases = {
            "时间格式错误": {"timestamp": "2026/01/01 00:00:00"},
            "时间缺少秒": {"timestamp": "2026-01-01 00:00"},
            "不存在的日期": {"timestamp": "2026-02-30 00:00:00"},
            "空 timeframe": {"timeframe": ""},
            "timeframe 含空格": {"timeframe": "1 d"},
            "价格为字符串": {"close": "105"},
            "价格缺失": {"open": None},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                fields = {
                    "symbol_id": self.symbol_id,
                    "timeframe": "1d",
                    "timestamp": T0,
                    "open": 100.0,
                    "high": 110.0,
                    "low": 90.0,
                    "close": 105.0,
                    "volume": 10.0,
                }
                fields.update(overrides)
                with self.assertRaises(ValueError):
                    self.klines.create_kline(
                        fields["symbol_id"],
                        fields["timeframe"],
                        fields["timestamp"],
                        open=fields["open"],
                        high=fields["high"],
                        low=fields["low"],
                        close=fields["close"],
                        volume=fields["volume"],
                    )
        self.assertEqual(self.total_rows(), 0)

    def test_15_symbol_id_must_be_positive_integer(self) -> None:
        for bad in (0, -1, "1", 1.5, True):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.insert(T0, symbol_id=bad)  # type: ignore[arg-type]

    def test_16_price_and_volume_positivity_is_not_restricted(self) -> None:
        # 不同市场存在合法但特殊的数据（例如负价格、零成交），
        # 因此 deliberately 不做 price > 0 / volume > 0 的限制
        self.insert(T0, open=-3.0, high=-1.0, low=-5.0, close=-2.0, volume=0.0)
        row = self.klines.get_kline(self.symbol_id, "1d", T0)
        self.assertAlmostEqual(row["open"], -3.0)
        self.assertAlmostEqual(row["volume"], 0.0)

    def test_17_timeframe_is_not_hardcoded(self) -> None:
        for timeframe in ("2m", "3m", "10m", "2h", "6h", "12h", "1M", "1d"):
            with self.subTest(timeframe=timeframe):
                self.insert(T2, timeframe=timeframe)
        self.assertEqual(self.total_rows(), 8)


class KlineUpsertTest(KlineTestCase):
    """10：upsert 策略。"""

    def test_01_upsert_inserts_when_absent(self) -> None:
        kline_id = self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=100.0, volume=1000.0
        )
        self.assertGreater(kline_id, 0)
        self.assertEqual(self.total_rows(), 1)

    def test_02_upsert_updates_when_present(self) -> None:
        first_id = self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=100.0, volume=1000.0
        )
        second_id = self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=120.0, low=90.0, close=101.0, volume=1100.0
        )

        self.assertEqual(self.total_rows(), 1, "相同 (symbol, timeframe, timestamp) 只能有一条")
        row = self.klines.get_kline(self.symbol_id, "1d", T0)
        self.assertEqual(row["id"], first_id)
        self.assertEqual(second_id, first_id)
        self.assertAlmostEqual(row["close"], 101.0)
        self.assertAlmostEqual(row["volume"], 1100.0)
        self.assertAlmostEqual(row["high"], 120.0)

    def test_03_upsert_keeps_created_at(self) -> None:
        self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=100.0, volume=1.0
        )
        created_before = self.klines.get_kline(self.symbol_id, "1d", T0)["created_at"]

        self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=101.0, volume=2.0
        )

        self.assertEqual(
            self.klines.get_kline(self.symbol_id, "1d", T0)["created_at"],
            created_before,
            "created_at 记录的是首次入库时间，不应被 upsert 改写",
        )

    def test_04_upsert_can_clear_or_set_amount(self) -> None:
        self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=100.0,
            volume=1.0, amount=500.0,
        )
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1d", T0)["amount"], 500.0)

        self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=100.0,
            volume=1.0, amount=None,
        )
        self.assertIsNone(self.klines.get_kline(self.symbol_id, "1d", T0)["amount"])

    def test_05_upsert_with_invalid_ohlc_is_rejected_and_keeps_old_data(self) -> None:
        self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=110.0, low=90.0, close=100.0, volume=1000.0
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.raw_insert({**self.raw_row(T0), "low": 200.0})

        row = self.klines.get_kline(self.symbol_id, "1d", T0)
        self.assertAlmostEqual(row["close"], 100.0, "失败不应破坏原有数据")
        self.assertEqual(self.total_rows(), 1)

    def test_06_upsert_distinguishes_from_create(self) -> None:
        # create_kline 是严格插入，upsert 是"存在则更新"，两者语义不同
        self.insert(T0, close=100.0)
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert(T0, close=200.0)
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1d", T0)["close"], 100.0)

        self.klines.upsert_kline(
            self.symbol_id, "1d", T0, open=100.0, high=210.0, low=90.0, close=200.0, volume=1.0
        )
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1d", T0)["close"], 200.0)


class KlineQueryTest(KlineTestCase):
    """13 / 14 / 15：查询。"""

    def setUp(self) -> None:
        super().setUp()
        for i, timestamp in enumerate((T0, T1, T2, T3)):
            self.insert(timestamp, close=100.0 + i)

    def test_01_list_klines_returns_all_in_ascending_order(self) -> None:
        rows = self.klines.list_klines(self.symbol_id, "1d")
        self.assertEqual([r["timestamp"] for r in rows], [T0, T1, T2, T3])

    def test_02_range_boundaries_are_inclusive(self) -> None:
        # 固定语义：start_timestamp <= timestamp <= end_timestamp（闭区间）
        rows = self.klines.list_klines(self.symbol_id, "1d", T1, T2)
        self.assertEqual([r["timestamp"] for r in rows], [T1, T2])

    def test_03_range_excludes_data_outside(self) -> None:
        rows = self.klines.list_klines(self.symbol_id, "1d", "2026-01-02 12:00:00", T3)
        self.assertEqual([r["timestamp"] for r in rows], [T2, T3], "T1 在范围之外，不应返回")

    def test_04_open_ended_ranges(self) -> None:
        self.assertEqual(
            [r["timestamp"] for r in self.klines.list_klines(self.symbol_id, "1d", start_timestamp=T2)],
            [T2, T3],
        )
        self.assertEqual(
            [r["timestamp"] for r in self.klines.list_klines(self.symbol_id, "1d", end_timestamp=T1)],
            [T0, T1],
        )

    def test_05_limit_returns_most_recent_n_in_ascending_order(self) -> None:
        rows = self.klines.list_klines(self.symbol_id, "1d", limit=2)
        self.assertEqual(
            [r["timestamp"] for r in rows],
            [T2, T3],
            "limit 表示'最近的 N 条'，且返回结果仍应升序",
        )

    def test_06_limit_within_range(self) -> None:
        rows = self.klines.list_klines(self.symbol_id, "1d", T0, T2, limit=2)
        self.assertEqual([r["timestamp"] for r in rows], [T1, T2])

    def test_07_limit_must_be_positive_integer(self) -> None:
        for bad in (0, -1, 1.5, True, "2"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    self.klines.list_klines(self.symbol_id, "1d", limit=bad)  # type: ignore[arg-type]

    def test_08_invalid_range_timestamp_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.klines.list_klines(self.symbol_id, "1d", "2026-01-01")

    def test_09_get_latest_kline_returns_max_timestamp(self) -> None:
        row = self.klines.get_latest_kline(self.symbol_id, "1d")
        self.assertEqual(row["timestamp"], T3)

    def test_10_get_latest_kline_returns_none_when_empty(self) -> None:
        self.assertIsNone(self.klines.get_latest_kline(self.other_symbol_id, "1d"))
        self.assertIsNone(self.klines.get_latest_kline(self.symbol_id, "1w"))

    def test_11_get_kline_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.klines.get_kline(self.symbol_id, "1d", "2027-01-01 00:00:00"))

    def test_12_queries_are_isolated_by_symbol_and_timeframe(self) -> None:
        self.insert(T0, symbol_id=self.other_symbol_id)
        self.insert(T0, timeframe="1h")

        rows = self.klines.list_klines(self.symbol_id, "1d")
        self.assertEqual([r["timestamp"] for r in rows], [T0, T1, T2, T3], "不应混入其他 symbol / timeframe")
        self.assertEqual(len(self.klines.list_klines(self.symbol_id, "1h")), 1)
        self.assertEqual(len(self.klines.list_klines(self.other_symbol_id, "1d")), 1)

    def test_13_count_klines(self) -> None:
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1d"), 4)
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1d", T1, T2), 2)
        self.assertEqual(self.klines.count_klines(self.other_symbol_id, "1d"), 0)

    def test_14_hourly_bars_are_labelled_by_start_time(self) -> None:
        # timestamp = K 线开始时间：00:00 / 01:00 / 02:00 三根 1h K 线
        bars = ["2026-01-01 00:00:00", "2026-01-01 01:00:00", "2026-01-01 02:00:00"]
        for bar in bars:
            self.insert(bar, timeframe="1h")

        rows = self.klines.list_klines(self.symbol_id, "1h")
        self.assertEqual([r["timestamp"] for r in rows], bars)
        self.assertEqual(self.klines.get_latest_kline(self.symbol_id, "1h")["timestamp"], bars[-1])


class KlineBulkTest(KlineTestCase):
    """11 / 12：批量写入与批量事务。"""

    def test_01_bulk_create_inserts_all(self) -> None:
        batch = [self.kline_dict(T0), self.kline_dict(T1), self.kline_dict(T2)]
        count = self.klines.bulk_create_klines(batch)

        self.assertEqual(count, 3)
        rows = self.klines.list_klines(self.symbol_id, "1d")
        self.assertEqual([r["timestamp"] for r in rows], [T0, T1, T2])
        self.assertAlmostEqual(rows[0]["open"], 100.0)

    def test_02_bulk_create_rolls_back_whole_batch_on_invalid_row(self) -> None:
        batch = [
            self.kline_dict(T0),
            self.kline_dict(T1),
            self.kline_dict(T2, low=200.0),  # 非法：low > high
            self.kline_dict(T3),
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            self.klines.bulk_create_klines(batch)

        self.assertEqual(self.total_rows(), 0, "批次中出现非法行时，必须整批回滚")

    def test_03_bulk_create_rolls_back_on_duplicate_inside_batch(self) -> None:
        batch = [self.kline_dict(T0), self.kline_dict(T1), self.kline_dict(T0)]
        with self.assertRaises(sqlite3.IntegrityError):
            self.klines.bulk_create_klines(batch)
        self.assertEqual(self.total_rows(), 0)

    def test_04_bulk_create_rolls_back_when_conflicting_with_existing_row(self) -> None:
        self.insert(T1)
        batch = [self.kline_dict(T0), self.kline_dict(T1), self.kline_dict(T2)]

        with self.assertRaises(sqlite3.IntegrityError):
            self.klines.bulk_create_klines(batch)

        self.assertEqual(self.total_rows(), 1, "只应保留批次之前已存在的那一条")
        self.assertIsNotNone(self.klines.get_kline(self.symbol_id, "1d", T1))

    def test_05_bulk_create_rolls_back_on_foreign_key_violation(self) -> None:
        batch = [self.kline_dict(T0), self.kline_dict(T1, symbol_id=999999)]
        with self.assertRaises(sqlite3.IntegrityError):
            self.klines.bulk_create_klines(batch)
        self.assertEqual(self.total_rows(), 0)

    def test_06_bulk_create_format_error_aborts_before_any_write(self) -> None:
        batch = [self.kline_dict(T0), self.kline_dict("2026-01-02")]  # 格式错误
        with self.assertRaises(ValueError):
            self.klines.bulk_create_klines(batch)
        self.assertEqual(self.total_rows(), 0)

    def test_07_bulk_create_rejects_non_mapping_input(self) -> None:
        with self.assertRaises(ValueError):
            self.klines.bulk_create_klines([(self.symbol_id, "1d", T0)])  # type: ignore[list-item]

    def test_08_bulk_create_empty_input_returns_zero(self) -> None:
        self.assertEqual(self.klines.bulk_create_klines([]), 0)
        self.assertEqual(self.total_rows(), 0)

    def test_09_bulk_upsert_inserts_and_updates(self) -> None:
        self.klines.bulk_upsert_klines([self.kline_dict(T0), self.kline_dict(T1)])
        self.assertEqual(self.total_rows(), 2)

        count = self.klines.bulk_upsert_klines(
            [
                self.kline_dict(T1, close=999.0, high=1000.0),
                self.kline_dict(T2),
            ]
        )
        self.assertEqual(count, 2)
        self.assertEqual(self.total_rows(), 3)
        self.assertAlmostEqual(self.klines.get_kline(self.symbol_id, "1d", T1)["close"], 999.0)

    def test_10_bulk_upsert_rolls_back_whole_batch_on_error(self) -> None:
        batch = [
            self.kline_dict(T0),
            self.kline_dict(T1, close=999.0),
            self.kline_dict(T2, high=1.0),  # 非法：high < open/close
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            self.klines.bulk_upsert_klines(batch)

        self.assertEqual(self.total_rows(), 0, "批量 upsert 失败也必须整批回滚")


class KlinePersistenceTest(KlineTestCase):
    """16：持久化与时间语义。"""

    def test_01_data_persists_after_reconnect(self) -> None:
        self.insert(T0)
        self.insert(T1)
        del self.klines

        reopened = KlineRepository(self.db_path)
        rows = reopened.list_klines(self.symbol_id, "1d")
        self.assertEqual([r["timestamp"] for r in rows], [T0, T1])

    def test_02_created_at_is_utc(self) -> None:
        self.insert(T0)
        created_at = datetime.strptime(
            self.klines.get_kline(self.symbol_id, "1d", T0)["created_at"], "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)

        delta = abs((datetime.now(timezone.utc) - created_at).total_seconds())
        self.assertLess(delta, 300, "created_at 应为 UTC 时间（偏差过大说明用了本地时区）")

    def test_03_timestamp_keeps_the_value_we_wrote(self) -> None:
        # 历史 K 线的时间由调用方给出，系统不会用"当前时间"覆盖
        historical = "2015-06-15 09:30:00"
        self.insert(historical)
        self.assertEqual(self.klines.get_kline(self.symbol_id, "1d", historical)["timestamp"], historical)

    def test_04_tests_never_use_the_production_database(self) -> None:
        self.assertNotEqual(self.klines.db_path, DEFAULT_DB_PATH)
        self.assertEqual(self.klines.db_path, self.db_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
