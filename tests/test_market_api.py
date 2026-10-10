"""Stage 20 Step 2：/market/* API 测试。

覆盖：
    /market/groups（+ group_type / market / active_only 过滤、symbol_count）
    /market/groups/{id}/symbols
    /market/symbols（+ group_id / market 过滤）
    /market/symbols/{id}
    /market/symbols/{id}/klines（limit 语义与既有 Repository 一致）
    display_code 由后端生成；行情缺失返回 null（不伪造）
    404（group / symbol 不存在）；既有 /symbols、/klines 语义不变
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from api.app import create_app
from database import (
    KlineRepository,
    SymbolGroupRepository,
    SymbolRepository,
    initialize_database,
)

MARKET_FIELDS = {
    "id",
    "symbol",
    "name",
    "market",
    "asset_type",
    "is_active",
    "display_code",
    "timeframe",
    "latest_price",
    "previous_close",
    "change",
    "change_percent",
    "volume",
    "amount",
    "timestamp",
}


class MarketApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_market_api_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "market_api.db"
        initialize_database(self.db_path)

        symbols = SymbolRepository(self.db_path)
        self.groups_repo = SymbolGroupRepository(self.db_path)
        klines = KlineRepository(self.db_path)

        self.maotai = symbols.create_symbol("600519", "贵州茅台", "CN", "STOCK")
        self.catl = symbols.create_symbol("300750", "宁德时代", "CN", "STOCK")
        self.pingan = symbols.create_symbol("000001", "平安银行", "CN", "STOCK")
        self.btc = symbols.create_symbol("BTCUSDT", "Bitcoin", "CRYPTO", "CRYPTO")
        self.inactive = symbols.create_symbol("688111", "金山办公", "CN", "STOCK")
        symbols.deactivate_symbol("CN", "688111")

        self._bulk(
            klines,
            self.maotai,
            "1d",
            [("2026-09-28 00:00:00", 100.0), ("2026-09-29 00:00:00", 105.0), ("2026-09-30 00:00:00", 110.0)],
            amount=None,
        )
        self._bulk(
            klines,
            self.catl,
            "1d",
            [("2026-09-29 00:00:00", 200.0), ("2026-09-30 00:00:00", 210.0)],
            amount=12345.0,
        )
        self._bulk(klines, self.pingan, "15m", [("2026-09-30 14:45:00", 12.5)], amount=None)

        self.semi = self.groups_repo.create_group("半导体", "industry", market="CN")
        self.watchlist = self.groups_repo.create_group("我的自选", "watchlist")
        self.groups_repo.add_symbol_to_group(self.semi, self.maotai)
        self.groups_repo.add_symbol_to_group(self.semi, self.catl)
        self.groups_repo.add_symbol_to_group(self.watchlist, self.maotai)
        self.groups_repo.add_symbol_to_group(self.watchlist, self.btc)

        self.client = TestClient(create_app(db_path=self.db_path))

    def _bulk(self, klines, symbol_id, timeframe, series, amount):
        klines.bulk_upsert_klines(
            [
                {
                    "symbol_id": symbol_id,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "open": close - 1.0,
                    "high": close + 2.0,
                    "low": close - 2.0,
                    "close": close,
                    "volume": 1000.0,
                    "amount": amount,
                }
                for timestamp, close in series
            ]
        )

    # ------------------------------------------------------------------ 分组
    def test_01_groups_include_required_fields_and_counts(self) -> None:
        response = self.client.get("/market/groups")
        self.assertEqual(response.status_code, 200)
        groups = {group["name"]: group for group in response.json()}
        self.assertEqual(set(groups), {"半导体", "我的自选"})
        for group in groups.values():
            self.assertTrue(
                {"id", "name", "group_type", "market", "description", "is_active", "symbol_count"}
                <= set(group)
            )
            self.assertEqual(group["is_active"], 1)
        self.assertEqual(groups["半导体"]["group_type"], "industry")
        self.assertEqual(groups["半导体"]["market"], "CN")
        self.assertEqual(groups["半导体"]["symbol_count"], 2)
        self.assertEqual(groups["我的自选"]["symbol_count"], 2)
        self.assertIsNone(groups["我的自选"]["market"])

    def test_02_groups_filters(self) -> None:
        self.assertEqual(
            [g["name"] for g in self.client.get("/market/groups?group_type=industry").json()],
            ["半导体"],
        )
        self.assertEqual(
            [g["name"] for g in self.client.get("/market/groups?group_type=watchlist").json()],
            ["我的自选"],
        )
        self.assertEqual(
            [g["name"] for g in self.client.get("/market/groups?market=CN").json()], ["半导体"]
        )
        self.assertEqual(self.client.get("/market/groups?market=US").json(), [])

        retired = self.groups_repo.create_group("已停用", "custom")
        self.groups_repo.deactivate_group(retired)
        self.assertEqual(len(self.client.get("/market/groups").json()), 2, "默认 active_only=true")
        self.assertEqual(len(self.client.get("/market/groups?active_only=false").json()), 3)

    # ------------------------------------------------------------ 分组内标的
    def test_03_group_symbols_with_quotes(self) -> None:
        response = self.client.get(f"/market/groups/{self.semi}/symbols")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual([row["symbol"] for row in rows], ["300750", "600519"])
        for row in rows:
            self.assertTrue(MARKET_FIELDS <= set(row))

        maotai = rows[1]
        self.assertEqual(maotai["display_code"], "600519.SH")
        self.assertEqual(maotai["name"], "贵州茅台")
        self.assertEqual(maotai["timeframe"], "1d")
        self.assertEqual(maotai["latest_price"], 110.0)
        self.assertEqual(maotai["previous_close"], 105.0)
        self.assertAlmostEqual(maotai["change"], 5.0)
        self.assertAlmostEqual(maotai["change_percent"], 5.0 / 105.0 * 100.0)
        self.assertEqual(maotai["volume"], 1000.0)
        self.assertIsNone(maotai["amount"])
        self.assertEqual(maotai["timestamp"], "2026-09-30 00:00:00")

    # ---------------------------------------------------------------- 标的列表
    def test_04_market_symbols_default_excludes_inactive(self) -> None:
        symbol_ids = [row["id"] for row in self.client.get("/market/symbols").json()]
        self.assertNotIn(self.inactive, symbol_ids)
        self.assertEqual(len(symbol_ids), 4)
        self.assertIn(
            self.inactive, [row["id"] for row in self.client.get("/market/symbols?active_only=false").json()]
        )

    def test_05_market_symbols_group_filter(self) -> None:
        rows = self.client.get(f"/market/symbols?group_id={self.watchlist}").json()
        self.assertEqual([row["symbol"] for row in rows], ["600519", "BTCUSDT"])
        self.assertEqual([row["display_code"] for row in rows], ["600519.SH", "BTCUSDT"])

    def test_06_market_symbols_market_filter(self) -> None:
        crypto = self.client.get("/market/symbols?market=CRYPTO").json()
        self.assertEqual([row["symbol"] for row in crypto], ["BTCUSDT"])
        cn = self.client.get("/market/symbols?market=CN").json()
        self.assertEqual(sorted(row["symbol"] for row in cn), ["000001", "300750", "600519"])

    def test_07_display_code_is_generated_by_backend(self) -> None:
        expected = {"600519": "600519.SH", "300750": "300750.SZ", "000001": "000001.SZ", "BTCUSDT": "BTCUSDT"}
        rows = {row["symbol"]: row for row in self.client.get("/market/symbols").json()}
        for symbol, display_code in expected.items():
            self.assertEqual(rows[symbol]["display_code"], display_code)

    def test_08_quote_from_two_bars(self) -> None:
        row = self.client.get(f"/market/symbols/{self.catl}").json()
        self.assertEqual(row["latest_price"], 210.0)
        self.assertEqual(row["previous_close"], 200.0)
        self.assertAlmostEqual(row["change"], 10.0)
        self.assertAlmostEqual(row["change_percent"], 5.0)
        self.assertEqual(row["amount"], 12345.0)

    def test_09_single_bar_returns_null_change(self) -> None:
        row = self.client.get(f"/market/symbols/{self.pingan}?timeframe=15m").json()
        self.assertEqual(row["latest_price"], 12.5)
        self.assertIsNone(row["previous_close"])
        self.assertIsNone(row["change"])
        self.assertIsNone(row["change_percent"])
        # 该标的没有日线 → 行情字段全为 null，且不报错
        daily = self.client.get(f"/market/symbols/{self.pingan}").json()
        self.assertIsNone(daily["latest_price"])
        self.assertIsNone(daily["timestamp"])

    def test_10_missing_amount_is_null_not_zero(self) -> None:
        response = self.client.get(f"/market/symbols/{self.maotai}")
        self.assertEqual(response.status_code, 200)
        self.assertIn('"amount":null', response.text.replace(" ", ""))
        self.assertIsNone(response.json()["amount"])

    def test_11_no_klines_symbol_has_null_quote(self) -> None:
        row = self.client.get(f"/market/symbols/{self.btc}").json()
        for field in ("latest_price", "previous_close", "change", "change_percent", "volume", "amount", "timestamp"):
            self.assertIsNone(row[field], f"{field} 应为 null")

    # ------------------------------------------------------------------ K 线
    def test_12_symbol_klines_projection_and_order(self) -> None:
        response = self.client.get(f"/market/symbols/{self.maotai}/klines?timeframe=1d")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            list(rows[0].keys()), ["timestamp", "open", "high", "low", "close", "volume", "amount"]
        )
        self.assertEqual(
            [row["timestamp"] for row in rows],
            ["2026-09-28 00:00:00", "2026-09-29 00:00:00", "2026-09-30 00:00:00"],
        )
        self.assertIsNone(rows[0]["amount"])
        self.assertEqual(rows[-1]["close"], 110.0)

    def test_13_klines_limit_and_range_match_repository(self) -> None:
        limited = self.client.get(f"/market/symbols/{self.maotai}/klines?timeframe=1d&limit=2").json()
        repository_rows = KlineRepository(self.db_path).list_klines(self.maotai, "1d", limit=2)
        self.assertEqual(
            [(row["timestamp"], row["close"]) for row in limited],
            [(row["timestamp"], row["close"]) for row in repository_rows],
        )
        self.assertEqual(len(limited), 2)
        self.assertEqual(limited[0]["timestamp"], "2026-09-29 00:00:00", "limit 取最近 N 根且保持升序")

        ranged = self.client.get(
            f"/market/symbols/{self.maotai}/klines?timeframe=1d&start=2026-09-29%2000:00:00"
        ).json()
        self.assertEqual([row["timestamp"] for row in ranged], ["2026-09-29 00:00:00", "2026-09-30 00:00:00"])

        self.assertEqual(self.client.get(f"/market/symbols/{self.maotai}/klines?limit=0").status_code, 422)
        self.assertEqual(self.client.get(f"/market/symbols/{self.maotai}/klines?limit=2001").status_code, 422)
        self.assertEqual(
            self.client.get(f"/market/symbols/{self.maotai}/klines?timeframe=1d&limit=1").json()[0]["close"],
            110.0,
        )

    # ---------------------------------------------------------------- 错误处理
    def test_14_unknown_group_or_symbol_returns_404(self) -> None:
        for path in (
            "/market/groups/9999/symbols",
            "/market/symbols?group_id=9999",
            "/market/symbols/9999",
            "/market/symbols/9999/klines",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404, path)
                body = response.json()
                self.assertIn("detail", body)
                self.assertNotIn("Traceback", response.text)

    def test_15_existing_endpoints_unchanged(self) -> None:
        # /symbols 语义不变：仍然是标的表行，不含行情字段
        symbols = self.client.get("/symbols").json()
        self.assertEqual(len(symbols), 5)
        self.assertNotIn("display_code", symbols[0])
        self.assertNotIn("latest_price", symbols[0])
        self.assertEqual(self.client.get("/symbols?market=CN").status_code, 200)
        self.assertEqual(self.client.get("/symbols?active_only=true").status_code, 200)

        # /klines 语义不变：仍然是 klines 表行（含 id / symbol_id / timeframe）
        klines = self.client.get(f"/klines?symbol_id={self.maotai}&timeframe=1d&limit=2").json()
        self.assertEqual(len(klines), 2)
        self.assertIn("symbol_id", klines[0])
        self.assertIn("id", klines[0])

        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/strategies").status_code, 200)

    # --------------------------------------------------- 板块聚合（Stage 22）
    def test_16_groups_include_quote_aggregates(self) -> None:
        """板块列表附带涨跌聚合：等权平均、涨跌家数、行情覆盖率。"""
        groups = {group["name"]: group for group in self.client.get("/market/groups").json()}
        semi = groups["半导体"]
        for field in (
            "avg_change_percent",
            "up_count",
            "down_count",
            "flat_count",
            "quote_coverage",
            "quoted_count",
            "timeframe",
        ):
            self.assertIn(field, semi)

        # 600519: 105 → 110 = +4.7619%；300750: 200 → 210 = +5.0%
        self.assertEqual(
            (semi["symbol_count"], semi["quoted_count"], semi["up_count"], semi["down_count"], semi["flat_count"]),
            (2, 2, 2, 0, 0),
        )
        self.assertAlmostEqual(semi["avg_change_percent"], (5.0 / 105.0 * 100.0 + 5.0) / 2)
        self.assertAlmostEqual(semi["quote_coverage"], 1.0)
        self.assertEqual(semi["timeframe"], "1d")

        # 我的自选 含 BTCUSDT（无 K 线）→ 覆盖率 0.5，涨跌统计只用有行情的成员
        watch = groups["我的自选"]
        self.assertEqual((watch["symbol_count"], watch["quoted_count"], watch["up_count"]), (2, 1, 1))
        self.assertAlmostEqual(watch["avg_change_percent"], 5.0 / 105.0 * 100.0)
        self.assertAlmostEqual(watch["quote_coverage"], 0.5)

    def test_17_aggregates_ignore_members_without_valid_quote(self) -> None:
        """没有有效行情的成员不计入涨跌统计（不补 0）；没有成员时覆盖率无定义 → None。"""
        groups = {group["name"]: group for group in self.client.get("/market/groups?timeframe=15m").json()}
        semi = groups["半导体"]
        self.assertEqual(
            (semi["symbol_count"], semi["quoted_count"], semi["up_count"], semi["flat_count"]),
            (2, 0, 0, 0),
        )
        self.assertIsNone(semi["avg_change_percent"])
        self.assertEqual(semi["quote_coverage"], 0.0)

        empty_id = self.groups_repo.create_group("空板块", "custom")
        empty = next(g for g in self.client.get("/market/groups").json() if g["id"] == empty_id)
        self.assertEqual(
            (empty["symbol_count"], empty["quoted_count"], empty["up_count"], empty["down_count"], empty["flat_count"]),
            (0, 0, 0, 0, 0),
        )
        self.assertIsNone(empty["quote_coverage"])
        self.assertIsNone(empty["avg_change_percent"])

    def test_18_flat_count_and_down_count(self) -> None:
        """平盘 / 下跌也要如实统计：up + down + flat === quoted_count。"""
        flat_id = SymbolRepository(self.db_path).create_symbol("601398", "工商银行", "CN", "STOCK")
        down_id = SymbolRepository(self.db_path).create_symbol("000858", "五粮液", "CN", "STOCK")
        self._bulk(
            KlineRepository(self.db_path),
            flat_id,
            "1d",
            [("2026-09-29 00:00:00", 5.0), ("2026-09-30 00:00:00", 5.0)],
            amount=None,
        )
        self._bulk(
            KlineRepository(self.db_path),
            down_id,
            "1d",
            [("2026-09-29 00:00:00", 120.0), ("2026-09-30 00:00:00", 114.0)],
            amount=None,
        )
        group_id = self.groups_repo.create_group("银行白酒", "industry", market="CN")
        self.groups_repo.add_symbol_to_group(group_id, flat_id)
        self.groups_repo.add_symbol_to_group(group_id, down_id)

        row = next(g for g in self.client.get("/market/groups").json() if g["id"] == group_id)
        self.assertEqual((row["up_count"], row["down_count"], row["flat_count"]), (0, 1, 1))
        self.assertEqual(row["up_count"] + row["down_count"] + row["flat_count"], row["quoted_count"])
        self.assertAlmostEqual(row["avg_change_percent"], (0.0 + (114.0 - 120.0) / 120.0 * 100.0) / 2)

    def test_19_aggregate_uses_one_query_for_all_groups(self) -> None:
        """N 个板块的聚合必须由一条 SQL 完成（禁止 N+1）。"""
        for index in range(5):
            group_id = self.groups_repo.create_group(f"批量板块{index}", "custom")
            self.groups_repo.add_symbol_to_group(group_id, self.maotai)

        calls: list[str] = []
        original = SymbolGroupRepository.query_all

        def counting(self, sql, params=None):  # noqa: ANN001
            calls.append(sql)
            return original(self, sql, params)

        SymbolGroupRepository.query_all = counting
        self.addCleanup(setattr, SymbolGroupRepository, "query_all", original)

        rows = self.groups_repo.list_groups_with_stats()
        self.assertEqual(len(rows), 7)
        self.assertEqual(len(calls), 1, f"板块聚合查询了 {len(calls)} 次，出现 N+1")


if __name__ == "__main__":
    unittest.main()
