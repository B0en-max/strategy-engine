"""Stage 17：API 跨域（CORS）配置测试。

为什么需要这层：Stage 17 的 React 前端跑在 http://127.0.0.1:5173，
浏览器会以「跨域」方式调用 http://127.0.0.1:8000 的 API。
后端必须显式允许该来源，否则前端一行请求都发不出去。

本文件只验证**装配层**（允许哪些来源 / 默认值 / 环境变量覆盖），
不修改 api/app.py 里的任何业务逻辑，也不触碰正式数据库。

运行：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from api import create_app, resolve_cors_origins  # noqa: E402
from config import ENV_CORS_ORIGINS  # noqa: E402
from database import initialize_database  # noqa: E402

FRONTEND_ORIGIN = "http://127.0.0.1:5173"
OTHER_ORIGIN = "http://evil.example.com"


class ResolveCorsOriginsTestCase(unittest.TestCase):
    """resolve_cors_origins：默认值 / 字符串 / 序列 / 环境变量，一律走同一个来源。"""

    def test_default_origins_are_local_dev_only(self) -> None:
        with mock.patch.dict(os.environ, {ENV_CORS_ORIGINS: ""}, clear=False):
            self.assertEqual(
                resolve_cors_origins(),
                ["http://127.0.0.1:5173", "http://localhost:5173"],
            )

    def test_explicit_string_and_sequence_are_supported(self) -> None:
        self.assertEqual(resolve_cors_origins("http://a.test:1, http://b.test:2"), [
            "http://a.test:1",
            "http://b.test:2",
        ])
        self.assertEqual(resolve_cors_origins(["http://a.test:1,http://b.test:2"]), [
            "http://a.test:1",
            "http://b.test:2",
        ])

    def test_environment_variable_wins_over_default_and_blanks_are_dropped(self) -> None:
        with mock.patch.dict(
            os.environ, {ENV_CORS_ORIGINS: "http://localhost:3000, , http://localhost:3000"}, clear=False
        ):
            self.assertEqual(resolve_cors_origins(), ["http://localhost:3000"])


class CorsApplicationTestCase(unittest.TestCase):
    """真实走一遍 HTTP：默认允许前端来源，未列出的来源不放行。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(prefix="se_cors_test_", ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.db_path = Path(self._tmpdir.name) / "cors_test.db"
        initialize_database(self.db_path)

    def _client(self, **kwargs: object) -> TestClient:
        with mock.patch.dict(os.environ, {ENV_CORS_ORIGINS: ""}, clear=False):
            app = create_app(db_path=self.db_path, **kwargs)  # type: ignore[arg-type]
        return TestClient(app)

    def test_frontend_origin_is_allowed_on_simple_request(self) -> None:
        response = self._client().get("/health", headers={"Origin": FRONTEND_ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), FRONTEND_ORIGIN)
        self.assertEqual(response.json()["status"], "ok")

    def test_preflight_request_is_answered(self) -> None:
        response = self._client().options(
            "/execution/signals/1",
            headers={
                "Origin": FRONTEND_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), FRONTEND_ORIGIN)
        self.assertIn("POST", response.headers.get("access-control-allow-methods", ""))

    def test_unknown_origin_is_not_echoed(self) -> None:
        response = self._client().get("/health", headers={"Origin": OTHER_ORIGIN})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("access-control-allow-origin"))

    def test_explicit_origins_override_default(self) -> None:
        client = self._client(cors_origins=["http://127.0.0.1:4321"])
        response = client.get("/health", headers={"Origin": "http://127.0.0.1:4321"})
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://127.0.0.1:4321")
        # 默认来源被显式值取代，不再放行
        other = client.get("/health", headers={"Origin": FRONTEND_ORIGIN})
        self.assertIsNone(other.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
