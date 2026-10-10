"""Stage 21 Step 1：导入编排测试（market/data/importer.py）。

**不进行真实网络请求**：列表与 K 线数据源都是假实现（K 线用真实 KlineBar 结构）。

覆盖：
    股票同步  不存在→insert；已存在→名称变化才 update；重复运行不重复建；不删除历史股票；
              dry-run 不写库；limit；数据源坏行计入 skipped；单只失败不影响其它
    指定代码  缺失时按数据源查名称；查不到名称 → 用代码兜底并记备注；非法代码 → skipped
    日 K 导入 dry-run 不写库；正式写入；重复导入幂等（不产生重复记录）；
              数据源修正历史 → upsert 覆盖；时间范围本地裁切；无数据 / 单只失败被如实统计
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_source.base import KlineBar, KlineDataSource  # noqa: E402
from database import KlineRepository, SymbolRepository, initialize_database  # noqa: E402
from market.data.base import StockSymbol, SymbolListSource, SymbolPage  # noqa: E402
from market.data.codes import normalize_a_share_code  # noqa: E402
from market.data.importer import (  # noqa: E402
    KlineTarget,
    ensure_a_share_symbols,
    import_daily_klines,
    sync_a_share_symbols,
)


class FakeListSource(SymbolListSource):
    """假的股票列表数据源：按页返回预设数据（含坏行）。"""

    name = "fake"

    def __init__(self, pages: list[list[StockSymbol]], *, total: int | None = None, skipped: list[str] | None = None):
        self._pages = pages
        self._total = total
        self._skipped = skipped or []
        self.names: dict[str, str | None] = {}
        self.page_calls: list[tuple[int, int]] = []

    def fetch_page(self, *, page: int, page_size: int) -> SymbolPage:
        self.page_calls.append((page, page_size))
        if page > len(self._pages):
            return SymbolPage(page=page, page_size=page_size, items=())
        return SymbolPage(
            page=page,
            page_size=page_size,
            items=tuple(self._pages[page - 1]),
            skipped=tuple(self._skipped if page == 1 else []),
        )

    def count(self) -> int | None:
        return self._total

    def lookup_name(self, code: str) -> str | None:
        return self.names.get(code)


class FakeKlineSource(KlineDataSource):
    """假的日 K 数据源：按代码返回预设 K 线，或按配置抛错。"""

    def __init__(self, bars_by_symbol: dict[str, list[KlineBar]], *, errors: dict[str, Exception] | None = None):
        self._bars = bars_by_symbol
        self._errors = errors or {}
        self.calls: list[tuple[str, str]] = []

    def fetch_klines(self, *, symbol: str, timeframe: str, start_timestamp=None, end_timestamp=None):
        self.calls.append((symbol, timeframe))
        if symbol in self._errors:
            raise self._errors[symbol]
        return list(self._bars.get(symbol, []))


def make_bar(symbol: str, day: str, close: float, *, volume: float = 1000.0) -> KlineBar:
    return KlineBar(
        symbol=symbol,
        timeframe="1d",
        timestamp=f"{day} 00:00:00",
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=volume,
        amount=None,
    )


def stock(symbol: str, name: str, exchange: str) -> StockSymbol:
    return StockSymbol(symbol=symbol, name=name, exchange=exchange)


class ImportTestCase(unittest.TestCase):
    """公共夹具：临时库 + Repository。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_market_data_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "market_data.db"
        initialize_database(self.db_path)
        self.symbols = SymbolRepository(self.db_path)
        self.klines = KlineRepository(self.db_path)

    def symbol_symbols(self) -> list[str]:
        return [str(row["symbol"]) for row in self.symbols.list_symbols(market="CN")]


class SymbolSyncTest(ImportTestCase):
    """股票基础数据同步。"""

    def test_dry_run_writes_nothing(self) -> None:
        source = FakeListSource([[stock("600519", "贵州茅台", "SH"), stock("000001", "平安银行", "SZ")]])
        report = sync_a_share_symbols(source, self.symbols, dry_run=True)

        self.assertEqual((report.inserted, report.updated, report.unchanged), (2, 0, 0))
        self.assertEqual(report.written, 0)
        self.assertEqual(self.symbol_symbols(), [])  # 一条都没写

    def test_write_inserts_then_is_idempotent(self) -> None:
        source = FakeListSource([[stock("600519", "贵州茅台", "SH"), stock("300750", "宁德时代", "SZ")]])

        first = sync_a_share_symbols(source, self.symbols, dry_run=False)
        self.assertEqual((first.inserted, first.updated, first.unchanged), (2, 0, 0))
        self.assertEqual(first.written, 2)
        self.assertEqual(sorted(self.symbol_symbols()), ["300750", "600519"])

        second = sync_a_share_symbols(source, self.symbols, dry_run=False)
        self.assertEqual((second.inserted, second.updated, second.unchanged), (0, 0, 2))
        self.assertEqual(second.written, 0)
        self.assertEqual(len(self.symbol_symbols()), 2)  # 不重复创建

    def test_name_change_updates_only_that_symbol(self) -> None:
        sync_a_share_symbols(
            FakeListSource([[stock("600519", "贵州茅台", "SH"), stock("000001", "平安银行", "SZ")]]),
            self.symbols,
            dry_run=False,
        )
        report = sync_a_share_symbols(
            FakeListSource([[stock("600519", "贵州茅台", "SH"), stock("000001", "平安银行股份", "SZ")]]),
            self.symbols,
            dry_run=False,
        )
        self.assertEqual((report.inserted, report.updated, report.unchanged), (0, 1, 1))
        self.assertEqual(str(self.symbols.get_symbol("CN", "000001")["name"]), "平安银行股份")
        self.assertEqual(str(self.symbols.get_symbol("CN", "600519")["name"]), "贵州茅台")

    def test_never_deletes_or_deactivates_missing_symbols(self) -> None:
        self.symbols.create_symbol("999999", "历史遗留", "CN", "STOCK")
        sync_a_share_symbols(FakeListSource([[stock("600519", "贵州茅台", "SH")]]), self.symbols, dry_run=False)

        legacy = self.symbols.get_symbol("CN", "999999")
        self.assertIsNotNone(legacy)
        self.assertEqual(int(legacy["is_active"]), 1)  # 不因为不在列表里就停用
        self.assertIn("999999", self.symbol_symbols())

    def test_limit_stops_after_n_symbols(self) -> None:
        page = [stock("600519", "贵州茅台", "SH"), stock("000001", "平安银行", "SZ"), stock("300750", "宁德时代", "SZ")]
        report = sync_a_share_symbols(FakeListSource([page]), self.symbols, dry_run=False, limit=2)
        self.assertEqual(report.inserted, 2)
        self.assertEqual(sorted(self.symbol_symbols()), ["000001", "600519"])

    def test_paging_and_total(self) -> None:
        pages = [[stock("600519", "贵州茅台", "SH")], [stock("000001", "平安银行", "SZ")]]
        source = FakeListSource(pages, total=2)
        report = sync_a_share_symbols(source, self.symbols, dry_run=False, page_size=1)
        self.assertEqual(report.pages, 2)
        self.assertEqual(report.inserted, 2)

    def test_source_bad_rows_are_counted_as_skipped(self) -> None:
        source = FakeListSource([[stock("600519", "贵州茅台", "SH")]], skipped=["代码无法标准化('hk00700'): 不是合法的 A 股代码"])
        report = sync_a_share_symbols(source, self.symbols, dry_run=False)
        self.assertEqual((report.fetched, report.skipped, report.inserted), (1, 1, 1))
        self.assertTrue(report.errors[0].startswith("代码无法标准化"))

    def test_single_symbol_failure_does_not_break_the_rest(self) -> None:
        original = SymbolRepository.create_symbol

        def flaky(self, symbol, name, market, asset_type, **kwargs):  # noqa: ANN001
            if symbol == "000001":
                raise RuntimeError("模拟写库失败")
            return original(self, symbol, name, market, asset_type, **kwargs)

        SymbolRepository.create_symbol = flaky
        self.addCleanup(setattr, SymbolRepository, "create_symbol", original)

        report = sync_a_share_symbols(
            FakeListSource([[stock("600519", "贵州茅台", "SH"), stock("000001", "平安银行", "SZ"), stock("300750", "宁德时代", "SZ")]]),
            self.symbols,
            dry_run=False,
        )
        self.assertEqual((report.inserted, report.failed), (2, 1))
        self.assertEqual(sorted(self.symbol_symbols()), ["300750", "600519"])
        self.assertIn("000001", report.errors[0])


class EnsureByCodesTest(ImportTestCase):
    """--symbols 指定代码的补充逻辑。"""

    def test_missing_symbol_uses_source_name(self) -> None:
        source = FakeListSource([])
        source.names = {"600519": "贵州茅台"}
        report = ensure_a_share_symbols(source, self.symbols, ["600519"], dry_run=False)

        self.assertEqual((report.inserted, report.failed), (1, 0))
        row = self.symbols.get_symbol("CN", "600519")
        self.assertEqual(str(row["name"]), "贵州茅台")
        self.assertEqual(str(row["asset_type"]), "STOCK")

    def test_existing_symbol_is_unchanged(self) -> None:
        self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        report = ensure_a_share_symbols(FakeListSource([]), self.symbols, ["sh600519"], dry_run=False)
        self.assertEqual((report.inserted, report.unchanged), (0, 1))

    def test_name_lookup_failure_falls_back_to_code_with_note(self) -> None:
        report = ensure_a_share_symbols(FakeListSource([]), self.symbols, ["300750"], dry_run=False)
        self.assertEqual(report.inserted, 1)
        self.assertEqual(str(self.symbols.get_symbol("CN", "300750")["name"]), "300750")
        self.assertTrue(any("未返回名称" in note for note in report.errors))

    def test_invalid_code_is_skipped(self) -> None:
        report = ensure_a_share_symbols(FakeListSource([]), self.symbols, ["BTCUSDT", "600519"], dry_run=True)
        self.assertEqual((report.skipped, report.inserted), (1, 1))

    def test_dry_run_does_not_write(self) -> None:
        ensure_a_share_symbols(FakeListSource([]), self.symbols, ["600519"], dry_run=True)
        self.assertIsNone(self.symbols.get_symbol("CN", "600519"))


class DailyKlineImportTest(ImportTestCase):
    """日 K 导入（幂等 / 更新 / 统计 / 恢复能力）。"""

    def setUp(self) -> None:
        super().setUp()
        self.symbol_id = self.symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        self.code = normalize_a_share_code("sh600519")
        self.target = KlineTarget(symbol_id=self.symbol_id, code=self.code, name="贵州茅台")

    def test_dry_run_writes_nothing_but_reports_counts(self) -> None:
        source = FakeKlineSource({"sh600519": [make_bar("sh600519", "2026-09-28", 1243.88)]})
        report = import_daily_klines(source, self.klines, [self.target], dry_run=True)

        self.assertEqual((report.inserted, report.updated, report.written), (1, 0, 0))
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1d"), 0)
        self.assertEqual(source.calls, [("sh600519", "1d")])

    def test_write_then_idempotent_second_run(self) -> None:
        bars = {"sh600519": [make_bar("sh600519", "2026-09-28", 1243.88), make_bar("sh600519", "2026-09-29", 1237.0)]}
        first = import_daily_klines(FakeKlineSource(bars), self.klines, [self.target], dry_run=False)
        self.assertEqual((first.inserted, first.updated), (2, 0))
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1d"), 2)

        second = import_daily_klines(FakeKlineSource(bars), self.klines, [self.target], dry_run=False)
        self.assertEqual((second.inserted, second.updated), (0, 2))
        self.assertEqual(self.klines.count_klines(self.symbol_id, "1d"), 2)  # 不产生重复记录

    def test_corrected_history_is_updated_in_place(self) -> None:
        import_daily_klines(
            FakeKlineSource({"sh600519": [make_bar("sh600519", "2026-09-29", 1237.0)]}),
            self.klines,
            [self.target],
            dry_run=False,
        )
        report = import_daily_klines(
            FakeKlineSource({"sh600519": [make_bar("sh600519", "2026-09-29", 1237.0, volume=999)]}),
            self.klines,
            [self.target],
            dry_run=False,
        )
        self.assertEqual((report.inserted, report.updated), (0, 1))
        row = self.klines.get_kline(self.symbol_id, "1d", "2026-09-29 00:00:00")
        self.assertEqual(Decimal(str(row["volume"])), Decimal("999"))

    def test_time_range_is_clipped_locally(self) -> None:
        bars = {"sh600519": [make_bar("sh600519", d, 100.0) for d in ("2026-09-01", "2026-09-15", "2026-09-30")]}
        report = import_daily_klines(
            FakeKlineSource(bars),
            self.klines,
            [self.target],
            start_timestamp="2026-09-14 00:00:00",
            end_timestamp="2026-09-30 23:59:59",
            dry_run=False,
        )
        self.assertEqual(report.fetched, 3)
        self.assertEqual(report.inserted, 2)
        self.assertEqual(
            [str(row["timestamp"]) for row in self.klines.list_klines(self.symbol_id, "1d")],
            ["2026-09-15 00:00:00", "2026-09-30 00:00:00"],
        )

    def test_empty_data_is_reported(self) -> None:
        report = import_daily_klines(FakeKlineSource({}), self.klines, [self.target], dry_run=False)
        self.assertEqual((report.symbols_empty, report.inserted), (1, 0))

    def test_single_symbol_failure_does_not_break_others(self) -> None:
        second_id = self.symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        second = KlineTarget(symbol_id=second_id, code=normalize_a_share_code("sz000001"), name="平安银行")
        source = FakeKlineSource(
            {"sz000001": [make_bar("sz000001", "2026-09-29", 11.0)]},
            errors={"sh600519": RuntimeError("模拟数据源故障")},
        )
        report = import_daily_klines(source, self.klines, [self.target, second], dry_run=False)

        self.assertEqual((report.symbols, report.symbols_failed, report.symbols_with_data), (2, 1, 1))
        self.assertIn("贵州茅台", report.errors[0])
        self.assertEqual(self.klines.count_klines(second_id, "1d"), 1)

    def test_failed_write_leaves_existing_rows_untouched(self) -> None:
        import_daily_klines(
            FakeKlineSource({"sh600519": [make_bar("sh600519", "2026-09-29", 1237.0)]}),
            self.klines,
            [self.target],
            dry_run=False,
        )
        original = KlineRepository.bulk_upsert_klines

        def boom(self, rows):  # noqa: ANN001
            raise RuntimeError("模拟整批回滚")

        KlineRepository.bulk_upsert_klines = boom
        self.addCleanup(setattr, KlineRepository, "bulk_upsert_klines", original)

        report = import_daily_klines(
            FakeKlineSource({"sh600519": [make_bar("sh600519", "2026-09-29", 1.0), make_bar("sh600519", "2026-09-30", 2.0)]}),
            self.klines,
            [self.target],
            dry_run=False,
        )
        self.assertEqual((report.symbols_failed, report.failed_rows), (1, 2))
        row = self.klines.get_kline(self.symbol_id, "1d", "2026-09-29 00:00:00")
        self.assertEqual(Decimal(str(row["close"])), Decimal("1237.0"))  # 旧数据没被破坏

    def test_reports_serialise_to_dict(self) -> None:
        symbol_report = sync_a_share_symbols(FakeListSource([[stock("600519", "贵州茅台", "SH")]]), self.symbols, dry_run=True)
        kline_report = import_daily_klines(FakeKlineSource({}), self.klines, [self.target], dry_run=True)
        self.assertEqual(symbol_report.as_dict()["dry_run"], True)
        self.assertEqual(kline_report.as_dict()["timeframe"], "1d")


if __name__ == "__main__":
    unittest.main()
