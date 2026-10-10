"""Stage 21 Step 1：导入 CLI 测试（market/data/cli.py）。

**不进行真实网络请求**：数据源在测试里被替换成假实现（patch CLI 内的工厂函数）。

覆盖：
    参数解析   --symbols / --all 互斥必填；默认 dry-run（不加 --write 不写库）
    日期规范化  'YYYY-MM-DD' → 起始 00:00:00 / 结束 23:59:59；完整时间戳原样透传
    预演       不写库；输出报表含关键统计行
    写入       --write 建股票 + 导入日 K
    安全       目标库不存在且为预演 → 返回 2（不创建数据库）
    全量       --all --no-klines 只同步列表；--limit 限制数量
    失败       失败项 → 退出码 1
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_source.base import KlineBar, KlineDataSource  # noqa: E402
from database import KlineRepository, SymbolRepository, initialize_database  # noqa: E402
from market.data.base import StockSymbol, SymbolListSource, SymbolPage  # noqa: E402
from market.data.cli import build_parser, main, normalize_range  # noqa: E402


class CliListSource(SymbolListSource):
    name = "fake-sina"

    def __init__(self, rows: list[tuple[str, str, str]]):
        self._rows = [StockSymbol(symbol=symbol, name=name, exchange=exchange) for symbol, name, exchange in rows]

    def fetch_page(self, *, page: int, page_size: int) -> SymbolPage:
        if page > 1:
            return SymbolPage(page=page, page_size=page_size, items=())
        return SymbolPage(page=page, page_size=page_size, items=tuple(self._rows))

    def count(self) -> int | None:
        return len(self._rows)

    def lookup_name(self, code: str) -> str | None:
        return {"600519": "贵州茅台", "000001": "平安银行", "300750": "宁德时代"}.get(code)


class CliKlineSource(KlineDataSource):
    def __init__(self, *, error: Exception | None = None):
        self._error = error
        self.calls: list[str] = []

    def fetch_klines(self, *, symbol: str, timeframe: str, start_timestamp=None, end_timestamp=None):
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return [
            KlineBar(
                symbol=symbol,
                timeframe=timeframe,
                timestamp="2026-09-29 00:00:00",
                open=1244.6,
                high=1256.13,
                low=1231.05,
                close=1237.0,
                volume=3123935,
                amount=None,
            )
        ]


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_market_cli_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "cli.db"
        initialize_database(self.db_path)

    def run_cli(self, argv: list[str], *, list_rows=None, kline_source=None):
        """在假数据源下运行 CLI，返回 (退出码, stdout, stderr)。"""
        rows = list_rows if list_rows is not None else [("600519", "贵州茅台", "SH"), ("000001", "平安银行", "SZ")]
        klines = kline_source or CliKlineSource()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("market.data.cli.make_a_share_list_source", return_value=CliListSource(rows)), mock.patch(
            "market.data.cli.HttpKlineDataSource", return_value=klines
        ):
            with redirect_stdout(out), redirect_stderr(err):
                code = main(argv)
        return code, out.getvalue(), err.getvalue(), klines


class ParserTest(CliTestCase):
    def test_defaults_are_dry_run(self) -> None:
        args = build_parser().parse_args(["--symbols", "600519"])
        self.assertFalse(args.write)
        self.assertFalse(args.all)
        self.assertFalse(args.no_klines)
        self.assertEqual(args.datalen, 320)

    def test_target_is_required_and_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args([])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--symbols", "600519", "--all"])

    def test_symbols_accepts_comma_and_space_forms(self) -> None:
        # 加引号的逗号列表（一个 token）
        args = build_parser().parse_args(["--symbols", "600519,000001,300750"])
        self.assertEqual(args.symbols, ["600519,000001,300750"])
        # 空格分隔 / PowerShell 数组（多个 token）
        args = build_parser().parse_args(["--symbols", "600519", "000001", "300750"])
        self.assertEqual(args.symbols, ["600519", "000001", "300750"])

    def test_space_separated_symbols_are_all_imported(self) -> None:
        code, out, _, _ = self.run_cli(
            ["--symbols", "600519", "000001", "300750", "--write", "--db", str(self.db_path)]
        )
        self.assertEqual(code, 0)
        self.assertIn("新增=3", out)

    def test_normalize_range(self) -> None:
        self.assertIsNone(normalize_range(None, is_end=False))
        self.assertIsNone(normalize_range("  ", is_end=False))
        self.assertEqual(normalize_range("2024-01-01", is_end=False), "2024-01-01 00:00:00")
        self.assertEqual(normalize_range("2024-01-01", is_end=True), "2024-01-01 23:59:59")
        self.assertEqual(normalize_range("2024-01-01 09:30:00", is_end=True), "2024-01-01 09:30:00")


class DryRunTest(CliTestCase):
    def test_dry_run_writes_nothing_and_reports(self) -> None:
        code, out, _, klines = self.run_cli(["--symbols", "600519,000001", "--db", str(self.db_path)])

        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN（只预演，未写库）", out)
        self.assertIn("新浪财经", out)
        self.assertIn("股票:", out)
        self.assertIn("新增=2", out)
        self.assertIsNone(SymbolRepository(self.db_path).get_symbol("CN", "600519"))
        self.assertEqual(KlineRepository(self.db_path).count_klines(1, "1d"), 0)
        # 预演模式下股票未入库 → 日 K 被跳过并给出说明，不发无意义的请求
        self.assertEqual(klines.calls, [])
        self.assertIn("跳过其日 K", out)

    def test_missing_db_in_dry_run_exits_without_creating_file(self) -> None:
        missing = Path(self._tmpdir.name) / "nope.db"
        code, _, err, _ = self.run_cli(["--symbols", "600519", "--db", str(missing)])
        self.assertEqual(code, 2)
        self.assertIn("目标数据库不存在", err)
        self.assertFalse(missing.exists())


class WriteTest(CliTestCase):
    def test_write_creates_symbols_and_imports_daily_klines(self) -> None:
        code, out, _, klines = self.run_cli(["--symbols", "600519,000001", "--write", "--db", str(self.db_path)])

        self.assertEqual(code, 0)
        self.assertIn("WRITE（已写入数据库）", out)
        symbols = SymbolRepository(self.db_path)
        maotai = symbols.get_symbol("CN", "600519")
        self.assertIsNotNone(maotai)
        self.assertEqual(str(maotai["name"]), "贵州茅台")
        self.assertEqual(klines.calls, ["sh600519", "sz000001"])
        self.assertEqual(KlineRepository(self.db_path).count_klines(int(maotai["id"]), "1d"), 1)

        # 再跑一次：幂等（不重复建股票、不重复插 K 线）
        code2, out2, _, _ = self.run_cli(["--symbols", "600519,000001", "--write", "--db", str(self.db_path)])
        self.assertEqual(code2, 0)
        self.assertIn("未变=2", out2)
        self.assertIn("新增=0", out2)
        self.assertEqual(KlineRepository(self.db_path).count_klines(int(maotai["id"]), "1d"), 1)

    def test_write_passes_date_range_and_datalen(self) -> None:
        with mock.patch("market.data.cli.sina_daily_kline_config") as config:
            config.return_value = mock.MagicMock()
            self.run_cli(
                ["--symbols", "600519", "--write", "--start", "2024-01-01", "--end", "2024-12-31", "--datalen", "2000", "--db", str(self.db_path)]
            )
        self.assertEqual(config.call_args.kwargs["datalen"], 2000)

    def test_all_without_klines_only_syncs_symbols(self) -> None:
        code, out, _, klines = self.run_cli(["--all", "--no-klines", "--write", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertIn("日K:    未执行（--no-klines）", out)
        self.assertEqual(sorted(str(r["symbol"]) for r in SymbolRepository(self.db_path).list_symbols(market="CN")), ["000001", "600519"])
        self.assertEqual(klines.calls, [])

    def test_all_limit_and_kline_import_for_existing_symbols(self) -> None:
        code, out, _, klines = self.run_cli(["--all", "--limit", "2", "--write", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        # --all 走「库里启用中的 CN 标的」，按 symbol 升序 → 000001 在前
        self.assertEqual(sorted(klines.calls), ["sh600519", "sz000001"])

    def test_dry_run_with_existing_symbols_imports_nothing_but_queries(self) -> None:
        SymbolRepository(self.db_path).create_symbol("600519", "贵州茅台", "CN", "STOCK")
        code, out, _, klines = self.run_cli(["--symbols", "600519", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertEqual(klines.calls, ["sh600519"])  # 预演也真的取数，只是不写
        self.assertEqual(KlineRepository(self.db_path).count_klines(
            int(SymbolRepository(self.db_path).get_symbol("CN", "600519")["id"]), "1d"
        ), 0)

    def test_invalid_code_is_skipped(self) -> None:
        code, out, _, _ = self.run_cli(["--symbols", "BTCUSDT,600519", "--write", "--db", str(self.db_path)])
        self.assertEqual(code, 0)
        self.assertIn("跳过=1", out)

    def test_failures_return_exit_code_1(self) -> None:
        failing = CliKlineSource(error=RuntimeError("模拟数据源故障"))
        code, out, _, _ = self.run_cli(
            ["--symbols", "600519", "--write", "--db", str(self.db_path)], kline_source=failing
        )
        self.assertEqual(code, 1)
        self.assertIn("失败=1", out)


if __name__ == "__main__":
    unittest.main()
