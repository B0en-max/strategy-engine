"""K 线（OHLCV）数据访问层（Stage 04）。

KlineRepository 建立在通用 Repository 之上，只负责 klines 表。

时间语义（必须牢记）：
    - timestamp 表示该根 K 线的**开始时间**，UTC，格式 'YYYY-MM-DD HH:MM:SS'
    - created_at 表示该条记录**进入数据库**的时间（UTC）
    - 不使用 datetime.now() 作为历史 K 线的时间戳

Python 层与数据库层的职责划分：
    Python 层（本模块）只做**格式校验**：字段类型、非空、时间格式、
        数字类型；以及区分「数据格式错误」与「市场上合法但特殊的数据」。
    数据库层负责**业务约束**：NOT NULL、UNIQUE(symbol_id, timeframe, timestamp)、
        OHLC 逻辑、FOREIGN KEY 引用 symbols(id)。
    因此：价格/成交量**不做正数限制**（不同市场存在合法但特殊的数据，
    例如负价格、零成交），唯一性也**不做** Python 层判断。

写入语义（不可混淆）：
    create_kline / bulk_create_klines  严格插入，重复 (symbol_id, timeframe, timestamp)
                                       会被数据库拒绝
    upsert_kline / bulk_upsert_klines  存在则更新 OHLCV，不存在则插入
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Iterable, Mapping

from . import models
from .repository import Repository, enrich_error

KLINES = models.KLINES_TABLE

#: 单条 K 线的输入形式：以字段名为键的映射（dict）
KlineInput = Mapping[str, object]

# 插入字段顺序（与 SQL 占位符一一对应）
_INSERT_COLUMNS: tuple[str, ...] = (
    "symbol_id",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

_REQUIRED_FIELDS: tuple[str, ...] = (
    "symbol_id",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)

_NUMERIC_FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume")

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

_INSERT_SQL = (
    f"INSERT INTO {KLINES} ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)})"
)

_UPSERT_SQL = (
    _INSERT_SQL
    + " ON CONFLICT (symbol_id, timeframe, timestamp) DO UPDATE SET"
    " open = excluded.open,"
    " high = excluded.high,"
    " low = excluded.low,"
    " close = excluded.close,"
    " volume = excluded.volume,"
    " amount = excluded.amount"
)

_SELECT_COLUMNS = "*"


# --------------------------------------------------------------------------
# 格式校验（只判断「格式错」，不判断「业务上限/下限」）
# --------------------------------------------------------------------------
def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串，实际为 {value!r}")
    return value.strip()


def _require_symbol_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"symbol_id 必须是整数，实际为 {value!r}")
    if value <= 0:
        raise ValueError(f"symbol_id 必须是正整数，实际为 {value!r}")
    return value


def _require_timeframe(value: object) -> str:
    """周期只做最小校验：非空、无空白字符、长度合理。

    故意**不**把周期限制成固定取值（1m/5m/1h/1d/1w ...），
    因为未来会扩展 2m/3m/10m/2h/6h/12h/1M 等。
    """
    text = _require_text(value, "timeframe")
    if any(ch.isspace() for ch in text):
        raise ValueError(f"timeframe 不应包含空白字符，实际为 {value!r}")
    if len(text) > 16:
        raise ValueError(f"timeframe 长度异常，实际为 {value!r}")
    return text


def _require_timestamp(value: object) -> str:
    """校验时间格式 'YYYY-MM-DD HH:MM:SS'（UTC），并确认是真实时间。

    注意：timestamp 是 K 线的**开始时间**。
    """
    text = _require_text(value, "timestamp")
    if not _TIMESTAMP_RE.match(text):
        raise ValueError(
            f"timestamp 格式必须是 'YYYY-MM-DD HH:MM:SS'（UTC），实际为 {value!r}"
        )
    try:
        datetime.strptime(text, models.TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"timestamp 不是有效时间 {value!r}: {exc}") from exc
    return text


def _require_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字，实际为 {value!r}")
    number = float(value)
    if number != number:  # NaN
        # SQLite 会把 NaN 静默存成 NULL，会让报错信息变得难以理解，这里提前拦住
        raise ValueError(f"{field} 不能是 NaN")
    return number


def _optional_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _require_number(value, field)


def _normalize_kline_input(row: KlineInput) -> tuple[object, ...]:
    """把一条 K 线输入校验并规范化为按 _INSERT_COLUMNS 排序的参数元组。

    只做格式校验；OHLC 逻辑、唯一性、外键是否存在都由数据库约束负责。
    """
    if not isinstance(row, Mapping):
        raise ValueError(
            f"K 线数据必须是以字段名为键的映射（dict），实际为 {type(row).__name__}"
        )
    missing = [field for field in _REQUIRED_FIELDS if field not in row]
    if missing:
        raise ValueError(f"K 线数据缺少必需字段: {missing}")

    symbol_id = _require_symbol_id(row["symbol_id"])
    timeframe = _require_timeframe(row["timeframe"])
    timestamp = _require_timestamp(row["timestamp"])
    numbers = [_require_number(row[field], field) for field in _NUMERIC_FIELDS]
    amount = _optional_number(row.get("amount"), "amount")
    return (symbol_id, timeframe, timestamp, *numbers, amount)


def _batch_params_description(count: int) -> str:
    return f"<批次共 {count} 条，已整体回滚>"


class KlineRepository(Repository):
    """klines 表的数据访问对象。"""

    # ------------------------------------------------------------ 单条写入
    def create_kline(
        self,
        symbol_id: int,
        timeframe: str,
        timestamp: str,
        *,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        amount: float | None = None,
    ) -> int:
        """严格插入一条 K 线，返回新记录 id。

        语义：**重复则失败**（sqlite3.IntegrityError），不偷偷更新已有数据。
        需要「存在则更新」请使用 upsert_kline()。
        """
        params = _normalize_kline_input(
            {
                "symbol_id": symbol_id,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "open": open,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        )
        with self.transaction() as conn:
            try:
                cursor = conn.execute(_INSERT_SQL, params)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, params) from exc
            return int(cursor.lastrowid)

    def upsert_kline(
        self,
        symbol_id: int,
        timeframe: str,
        timestamp: str,
        *,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        amount: float | None = None,
    ) -> int:
        """写入一条 K 线：已存在则更新 OHLCV，不存在则插入；返回记录 id。

        - 更新的字段：open / high / low / close / volume / amount
        - created_at 保持不变（记录首次入库时间）
        """
        params = _normalize_kline_input(
            {
                "symbol_id": symbol_id,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "open": open,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        )
        sql = _UPSERT_SQL + " RETURNING id"
        with self.transaction() as conn:
            try:
                row = conn.execute(sql, params).fetchone()
            except sqlite3.Error as exc:
                raise enrich_error(exc, _UPSERT_SQL, params) from exc
            if row is None:  # pragma: no cover - RETURNING 正常一定返回一行
                raise sqlite3.Error(f"upsert 未返回记录 id | SQL={sql!r}")
            return int(row[0])

    # ------------------------------------------------------------ 批量写入
    def bulk_create_klines(self, klines: Iterable[KlineInput]) -> int:
        """批量严格插入，返回写入条数。

        原子性：整个批次在一个事务里；任意一条违反约束（唯一性 / OHLC / 外键），
        **整批回滚**，不会出现「9999 条成功、1 条失败」的部分写入。
        """
        rows = [_normalize_kline_input(kline) for kline in klines]
        if not rows:
            return 0
        with self.transaction() as conn:
            try:
                conn.executemany(_INSERT_SQL, rows)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _INSERT_SQL, _batch_params_description(len(rows))) from exc
        return len(rows)

    def bulk_upsert_klines(self, klines: Iterable[KlineInput]) -> int:
        """批量 upsert，返回处理的条数。

        原子性同上：整批在一个事务里，任意一条失败则整批回滚。
        """
        rows = [_normalize_kline_input(kline) for kline in klines]
        if not rows:
            return 0
        with self.transaction() as conn:
            try:
                conn.executemany(_UPSERT_SQL, rows)
            except sqlite3.Error as exc:
                raise enrich_error(exc, _UPSERT_SQL, _batch_params_description(len(rows))) from exc
        return len(rows)

    # ------------------------------------------------------------------ 查询
    def get_kline(self, symbol_id: int, timeframe: str, timestamp: str) -> sqlite3.Row | None:
        """按 (symbol_id, timeframe, timestamp) 查询单条 K 线。"""
        symbol_id = _require_symbol_id(symbol_id)
        timeframe = _require_timeframe(timeframe)
        timestamp = _require_timestamp(timestamp)
        return self.query_one(
            f"SELECT {_SELECT_COLUMNS} FROM {KLINES} "
            "WHERE symbol_id = ? AND timeframe = ? AND timestamp = ?",
            (symbol_id, timeframe, timestamp),
        )

    def get_latest_kline(self, symbol_id: int, timeframe: str) -> sqlite3.Row | None:
        """返回 timestamp 最大（最新）的一条 K 线；没有数据时返回 None。"""
        symbol_id = _require_symbol_id(symbol_id)
        timeframe = _require_timeframe(timeframe)
        return self.query_one(
            f"SELECT {_SELECT_COLUMNS} FROM {KLINES} "
            "WHERE symbol_id = ? AND timeframe = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (symbol_id, timeframe),
        )

    def list_klines(
        self,
        symbol_id: int,
        timeframe: str,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
        *,
        limit: int | None = None,
    ) -> list[sqlite3.Row]:
        """按时间范围查询 K 线，**始终按 timestamp 升序返回**。

        范围语义（已固定）：start_timestamp <= timestamp <= end_timestamp，
        两端都是**闭区间**（timestamp 等于 start 或 end 的记录都会被返回）。

        limit 语义（注意与"按时间升序取前 N 条"完全不同）：
            limit = None  → 返回范围内全部数据
            limit = N     → 返回范围内**最近的 N 条**（timestamp 最大的 N 条），
                            返回结果仍然按 timestamp 升序排列
        """
        symbol_id = _require_symbol_id(symbol_id)
        timeframe = _require_timeframe(timeframe)
        where = "symbol_id = ? AND timeframe = ?"
        params: list[object] = [symbol_id, timeframe]
        if start_timestamp is not None:
            where += " AND timestamp >= ?"
            params.append(_require_timestamp(start_timestamp))
        if end_timestamp is not None:
            where += " AND timestamp <= ?"
            params.append(_require_timestamp(end_timestamp))

        if limit is None:
            sql = f"SELECT {_SELECT_COLUMNS} FROM {KLINES} WHERE {where} ORDER BY timestamp ASC"
        else:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError(f"limit 必须是正整数，实际为 {limit!r}")
            # 先用 DESC 取最近的 N 条，再在外层恢复升序，
            # 避免"ORDER BY ASC LIMIT N"被误当成"最近 N 条"
            sql = (
                f"SELECT {_SELECT_COLUMNS} FROM ("
                f"  SELECT {_SELECT_COLUMNS} FROM {KLINES} WHERE {where} "
                "   ORDER BY timestamp DESC LIMIT ?"
                ") ORDER BY timestamp ASC"
            )
            params.append(limit)

        return self.query_all(sql, tuple(params))

    def count_klines(
        self,
        symbol_id: int,
        timeframe: str,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
    ) -> int:
        """统计时间范围内的 K 线数量（范围语义与 list_klines 一致，闭区间）。"""
        symbol_id = _require_symbol_id(symbol_id)
        timeframe = _require_timeframe(timeframe)
        where = "symbol_id = ? AND timeframe = ?"
        params: list[object] = [symbol_id, timeframe]
        if start_timestamp is not None:
            where += " AND timestamp >= ?"
            params.append(_require_timestamp(start_timestamp))
        if end_timestamp is not None:
            where += " AND timestamp <= ?"
            params.append(_require_timestamp(end_timestamp))
        row = self.query_one(f"SELECT COUNT(*) AS n FROM {KLINES} WHERE {where}", tuple(params))
        return int(row["n"]) if row is not None else 0
