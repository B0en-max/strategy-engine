"""Stage 16：执行器 API 层测试。

覆盖（按要求精简）：
    策略列表 / 标的列表 / K 线查询 / 策略运行 / Signal 查询 /
    Signal 模拟执行 / Position / Order / Trade 查询
    不存在资源 → 404；参数错误 → 422/400；非 pending 信号重复执行 → 409
    响应一律是 JSON（不泄漏 Repository / 连接对象）

全部使用**临时数据库**，不触碰 data/strategy_engine.db（仅只读校验行数未变）。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api import create_app  # noqa: E402
from database import (  # noqa: E402
    DEFAULT_DB_PATH,
    KlineRepository,
    SignalRepository,
    StrategyRepository,
    SymbolRepository,
    initialize_database,
)

T0 = "2026-01-01 09:30:00"
T1 = "2026-01-01 09:45:00"
T2 = "2026-01-01 10:00:00"
T3 = "2026-01-01 10:15:00"

PRODUCTION_TABLES = (
    "symbols",
    "klines",
    "strategies",
    "signals",
    "orders",
    "trades",
    "positions",
)


class ApiTestCase(unittest.TestCase):
    """基类：临时数据库 + TestClient。"""

    timeframe = "15m"

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_api_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "api_test.db"
        initialize_database(self.db_path)

        self.symbol_id = SymbolRepository(self.db_path).create_symbol(
            "600519", "贵州茅台", "CN", "STOCK"
        )
        self.strategy_id = StrategyRepository(self.db_path).create_strategy(
            "close_direction_v1", {"type": "close_direction"}
        )
        self.klines = KlineRepository(self.db_path)
        self.signals = SignalRepository(self.db_path)

        self.app = create_app(db_path=self.db_path)
        self.client = TestClient(self.app)

    # ------------------------------------------------------------------ 工具
    def add_closes(self, closes: dict[str, float]) -> None:
        for timestamp, close in closes.items():
            open_price = close - 1.0
            self.klines.create_kline(
                self.symbol_id,
                self.timeframe,
                timestamp,
                open=open_price,
                high=max(open_price, close) + 1.0,
                low=min(open_price, close) - 1.0,
                close=close,
                volume=100.0,
            )

    def run_strategy(self, **overrides) -> dict:
        body = {"symbol_id": self.symbol_id, "timeframe": self.timeframe}
        body.update(overrides)
        response = self.client.post(f"/strategies/{self.strategy_id}/run", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def first_signal_id(self) -> int:
        rows = self.signals.list_signals(strategy_id=self.strategy_id)
        self.assertTrue(rows, "预期已经产生信号")
        return int(rows[0]["id"])


class ApiBasicsTest(ApiTestCase):
    """健康检查、策略列表、标的列表、K 线查询。"""

    def test_01_health(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["db_path"], str(self.db_path))
        self.assertTrue(body["simulation_only"])
        self.assertFalse(body["broker_connected"])

    def test_02_strategy_list(self) -> None:
        response = self.client.get("/strategies")

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertIsInstance(rows, list)
        self.assertIsInstance(rows[0], dict)
        self.assertEqual(rows[0]["id"], self.strategy_id)
        self.assertEqual(rows[0]["name"], "close_direction_v1")
        self.assertEqual(rows[0]["config"], {"type": "close_direction"}, "config 以 dict 返回")
        self.assertEqual(rows[0]["status"], "draft")
        self.assertEqual(rows[0]["version"], 1)

    def test_03_strategy_list_filters(self) -> None:
        self.assertEqual(self.client.get("/strategies?status=active").json(), [])
        self.assertEqual(len(self.client.get("/strategies?status=draft").json()), 1)

    def test_04_symbol_list(self) -> None:
        SymbolRepository(self.db_path).create_symbol("000001", "平安银行", "CN", "STOCK", is_active=0)

        response = self.client.get("/symbols")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(
            [row["symbol"] for row in response.json()], ["000001", "600519"], "按 market, symbol 升序"
        )

        active_only = self.client.get("/symbols?active_only=true").json()
        self.assertEqual([row["symbol"] for row in active_only], ["600519"])

        self.assertEqual(self.client.get("/symbols?market=US").json(), [])

    def test_05_kline_query(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0})

        response = self.client.get(
            f"/klines?symbol_id={self.symbol_id}&timeframe={self.timeframe}"
        )

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["timestamp"] for row in rows], [T0, T1, T2], "按时间升序")
        self.assertAlmostEqual(rows[0]["close"], 100.0)
        self.assertIsInstance(rows[0], dict)

    def test_06_kline_query_limit_and_range(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0, T3: 130.0})

        limited = self.client.get(
            f"/klines?symbol_id={self.symbol_id}&timeframe={self.timeframe}&limit=2"
        ).json()
        self.assertEqual([row["timestamp"] for row in limited], [T2, T3], "limit 取最近 N 根")

        ranged = self.client.get(
            f"/klines?symbol_id={self.symbol_id}&timeframe={self.timeframe}"
            f"&start_timestamp={T1}&end_timestamp={T2}"
        ).json()
        self.assertEqual([row["timestamp"] for row in ranged], [T1, T2])

    def test_07_kline_query_errors(self) -> None:
        self.assertEqual(
            self.client.get("/klines?symbol_id=999999&timeframe=15m").status_code, 404
        )
        self.assertEqual(self.client.get("/klines?timeframe=15m").status_code, 422)
        self.assertEqual(self.client.get(f"/klines?symbol_id={self.symbol_id}").status_code, 422)
        self.assertEqual(
            self.client.get(f"/klines?symbol_id={self.symbol_id}&timeframe=15m&limit=0").status_code,
            422,
        )


class ApiStrategyRunTest(ApiTestCase):
    """运行策略接口。"""

    def test_01_run_strategy_creates_signals(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0})

        body = self.run_strategy()

        self.assertEqual(body["strategy_id"], self.strategy_id)
        self.assertEqual(body["symbol_id"], self.symbol_id)
        self.assertEqual(body["timeframe"], self.timeframe)
        self.assertEqual(body["bars"], 3)
        self.assertEqual(body["signals"], 2)
        self.assertEqual((body["long_signals"], body["short_signals"]), (1, 1))
        self.assertEqual(len(self.signals.list_signals(strategy_id=self.strategy_id)), 2)

    def test_02_run_strategy_with_time_range(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 120.0, T3: 130.0})

        body = self.run_strategy(start_timestamp=T1, end_timestamp=T2)

        self.assertEqual(body["bars"], 2)
        self.assertEqual(body["signals"], 1)
        self.assertEqual(body["first_bar_timestamp"], T1)
        self.assertEqual(body["last_bar_timestamp"], T2)

    def test_03_run_strategy_unknown_resources(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})

        body = {"symbol_id": self.symbol_id, "timeframe": self.timeframe}
        unknown_strategy = self.client.post("/strategies/999999/run", json=body)
        self.assertEqual(unknown_strategy.status_code, 404)
        self.assertIn("999999", unknown_strategy.json()["detail"])

        unknown_symbol = self.client.post(
            f"/strategies/{self.strategy_id}/run",
            json={"symbol_id": 999999, "timeframe": self.timeframe},
        )
        self.assertEqual(unknown_symbol.status_code, 404)

    def test_04_run_strategy_invalid_payload(self) -> None:
        # 缺少 symbol_id / timeframe
        self.assertEqual(
            self.client.post(f"/strategies/{self.strategy_id}/run", json={}).status_code, 422
        )
        self.assertEqual(
            self.client.post(
                f"/strategies/{self.strategy_id}/run",
                json={"symbol_id": self.symbol_id, "timeframe": ""},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(
                f"/strategies/{self.strategy_id}/run",
                json={"symbol_id": 0, "timeframe": self.timeframe},
            ).status_code,
            422,
        )

    def test_05_run_strategy_bad_timestamp_is_400(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})

        response = self.client.post(
            f"/strategies/{self.strategy_id}/run",
            json={
                "symbol_id": self.symbol_id,
                "timeframe": self.timeframe,
                "start_timestamp": "2026/01/01",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("timestamp", response.json()["detail"])


class ApiSignalTest(ApiTestCase):
    """信号查询。"""

    def test_01_list_signals_of_strategy(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0})
        self.run_strategy()

        response = self.client.get(f"/strategies/{self.strategy_id}/signals")

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["strategy_id"], self.strategy_id)
        self.assertEqual(first["symbol_id"], self.symbol_id)
        self.assertEqual(first["timeframe"], self.timeframe)
        self.assertEqual(first["direction"], "long")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["timestamp"], T1)
        self.assertAlmostEqual(first["price"], 110.0)
        self.assertIsNotNone(first["reason"])

    def test_02_signal_filters(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0, T2: 105.0})
        self.run_strategy()
        signal_id = self.first_signal_id()
        self.signals.update_signal_status(signal_id, "cancelled")

        pending = self.client.get(f"/strategies/{self.strategy_id}/signals?status=pending").json()
        cancelled = self.client.get(
            f"/strategies/{self.strategy_id}/signals?status=cancelled"
        ).json()
        short_only = self.client.get(
            f"/strategies/{self.strategy_id}/signals?timeframe=1h"
        ).json()

        self.assertEqual(len(pending), 1)
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(short_only, [])

    def test_03_unknown_strategy_is_404(self) -> None:
        response = self.client.get("/strategies/999999/signals")

        self.assertEqual(response.status_code, 404)
        self.assertIn("999999", response.json()["detail"])


class ApiExecutionTest(ApiTestCase):
    """模拟执行指定信号。"""

    def prepare_signal(self) -> int:
        self.add_closes({T0: 100.0, T1: 110.0})
        self.run_strategy()
        return self.first_signal_id()

    def test_01_execute_signal(self) -> None:
        signal_id = self.prepare_signal()

        response = self.client.post(
            f"/execution/signals/{signal_id}", json={"quantity": 1.0}
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["signal_id"], signal_id)
        self.assertEqual(body["side"], "buy")
        self.assertAlmostEqual(body["quantity"], 1.0)
        self.assertAlmostEqual(body["price"], 110.0, msg="默认用 signal 自带价格")
        self.assertAlmostEqual(body["position_quantity"], 1.0)
        self.assertAlmostEqual(body["position_avg_price"], 110.0)

        # 订单 / 成交 / 持仓落库状态，以及信号变为 executed
        orders = self.client.get("/orders").json()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "filled")
        self.assertEqual(self.signals.get_signal(signal_id)["status"], "executed")
        self.assertEqual(len(self.client.get("/trades").json()), 1)
        self.assertEqual(len(self.client.get("/positions").json()), 1)

    def test_02_execute_with_custom_quantity_and_price(self) -> None:
        signal_id = self.prepare_signal()

        body = self.client.post(
            f"/execution/signals/{signal_id}", json={"quantity": 2.5, "price": 100.0}
        ).json()

        self.assertAlmostEqual(body["quantity"], 2.5)
        self.assertAlmostEqual(body["price"], 100.0)
        self.assertAlmostEqual(body["position_quantity"], 2.5)
        self.assertAlmostEqual(body["position_avg_price"], 100.0)

    def test_03_non_pending_signal_is_conflict(self) -> None:
        signal_id = self.prepare_signal()
        first = self.client.post(f"/execution/signals/{signal_id}", json={"quantity": 1.0})
        self.assertEqual(first.status_code, 200)

        second = self.client.post(f"/execution/signals/{signal_id}", json={"quantity": 1.0})

        self.assertEqual(second.status_code, 409)
        self.assertIn("executed", second.json()["detail"])
        self.assertEqual(len(self.client.get("/trades").json()), 1, "不得重复成交")

    def test_04_unknown_signal_is_404(self) -> None:
        response = self.client.post("/execution/signals/999999", json={"quantity": 1.0})

        self.assertEqual(response.status_code, 404)
        self.assertIn("999999", response.json()["detail"])

    def test_05_invalid_payload_is_422(self) -> None:
        signal_id = self.prepare_signal()

        self.assertEqual(self.client.post(f"/execution/signals/{signal_id}", json={}).status_code, 422)
        self.assertEqual(
            self.client.post(f"/execution/signals/{signal_id}", json={"quantity": 0}).status_code,
            422,
        )
        self.assertEqual(
            self.client.post(f"/execution/signals/{signal_id}", json={"quantity": -1}).status_code,
            422,
        )


class ApiQueryTest(ApiTestCase):
    """持仓 / 订单 / 成交查询与数据库隔离。"""

    def execute_one(self) -> None:
        self.add_closes({T0: 100.0, T1: 110.0})
        self.run_strategy()
        signal_id = self.first_signal_id()
        self.assertEqual(
            self.client.post(f"/execution/signals/{signal_id}", json={"quantity": 1.0}).status_code,
            200,
        )

    def test_01_positions(self) -> None:
        self.assertEqual(self.client.get("/positions").json(), [])
        self.execute_one()

        rows = self.client.get("/positions").json()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol_id"], self.symbol_id)
        self.assertAlmostEqual(rows[0]["quantity"], 1.0)
        self.assertAlmostEqual(rows[0]["avg_price"], 110.0)

        filtered = self.client.get(f"/positions?symbol_id={self.symbol_id}").json()
        self.assertEqual(len(filtered), 1)
        self.assertEqual(self.client.get("/positions?symbol_id=999999").json(), [])

    def test_02_orders(self) -> None:
        self.execute_one()

        rows = self.client.get("/orders").json()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "buy")
        self.assertEqual(rows[0]["status"], "filled")
        self.assertAlmostEqual(rows[0]["quantity"], 1.0)
        self.assertEqual(self.client.get("/orders?status=filled").json().__len__(), 1)
        self.assertEqual(self.client.get("/orders?status=pending").json(), [])
        self.assertEqual(self.client.get("/orders?symbol_id=999999").json(), [])

    def test_03_trades(self) -> None:
        self.execute_one()

        rows = self.client.get("/trades").json()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "buy")
        self.assertAlmostEqual(rows[0]["price"], 110.0)
        self.assertAlmostEqual(rows[0]["fee"], 0.0)
        self.assertEqual(len(self.client.get("/trades?side=buy").json()), 1)
        self.assertEqual(self.client.get("/trades?side=sell").json(), [])
        self.assertEqual(self.client.get("/trades?limit=0").status_code, 422)

    def test_04_production_database_is_not_modified(self) -> None:
        production = KlineRepository(DEFAULT_DB_PATH)

        def snapshot() -> dict[str, int]:
            return {
                table: production.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
                for table in PRODUCTION_TABLES
            }

        before = snapshot()
        self.assertNotEqual(self.app.state.db_path, DEFAULT_DB_PATH)

        self.execute_one()

        self.assertEqual(snapshot(), before, "API 测试不得修改正式库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
