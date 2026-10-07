"""Stage 01：项目骨架验收测试。

验证内容（只针对骨架，不涉及任何业务功能）：
  1. 目录结构是否建立
  2. 基础文件是否存在
  3. Python 包是否可以导入
  4. main.py 是否可以真实运行并输出启动信息
  5. .gitignore 是否包含必要的忽略规则、并真实生效
  6. 当前 Python 环境是否正常

运行方式（项目根目录下）：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
    .venv\\Scripts\\python.exe tests\\test_project_skeleton.py
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GIT_FALLBACK = Path(r"C:\Program Files\Git\cmd\git.exe")

# 如果项目根目录不在 sys.path 中，则加入，保证包可被导入
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

REQUIRED_DIRS = [
    "config",
    "database",
    "data",
    "data/raw",
    "data/processed",
    "data/cache",
    "strategies",
    "backtest",
    "execution",
    "review",
    "optimization",
    "api",
    "tests",
    "logs",
]

# 这些目录必须同时是 Python 包（含 __init__.py）
PACKAGE_DIRS = [
    "config",
    "database",
    "strategies",
    "backtest",
    "execution",
    "review",
    "optimization",
    "api",
]

REQUIRED_FILES = [
    "main.py",
    "requirements.txt",
    ".gitignore",
    "README.md",
    "tests/__init__.py",
]

REQUIRED_IGNORE_PATTERNS = [
    ".venv/",
    "__pycache__/",
    "*.pyc",
    ".env",
    "*.db",
    "*.sqlite",
    "*.sqlite3",
    "data/raw/",
    "data/processed/",
    "data/cache/",
    "logs/",
]


def _find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    if GIT_FALLBACK.exists():
        return str(GIT_FALLBACK)
    return None


class ProjectSkeletonTest(unittest.TestCase):
    """Stage 01 骨架验证。"""

    def test_01_required_directories_exist(self) -> None:
        missing = [d for d in REQUIRED_DIRS if not (PROJECT_ROOT / d).is_dir()]
        self.assertEqual(missing, [], f"缺少目录: {missing}")

    def test_02_package_dirs_have_init(self) -> None:
        missing = [d for d in PACKAGE_DIRS if not (PROJECT_ROOT / d / "__init__.py").is_file()]
        self.assertEqual(missing, [], f"这些包目录缺少 __init__.py: {missing}")

    def test_03_required_files_exist(self) -> None:
        missing = [f for f in REQUIRED_FILES if not (PROJECT_ROOT / f).is_file()]
        self.assertEqual(missing, [], f"缺少文件: {missing}")

    def test_04_packages_are_importable(self) -> None:
        for pkg in PACKAGE_DIRS:
            with self.subTest(package=pkg):
                module = importlib.import_module(pkg)
                self.assertEqual(module.__name__, pkg)

    def test_05_main_runs_and_prints_startup_info(self) -> None:
        proc = subprocess.run(
            [sys.executable, "main.py"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, f"main.py 退出码非 0\nstderr={proc.stderr}")
        stdout = proc.stdout
        self.assertIn("Strategy Engine", stdout)
        self.assertIn("Stage 01 - Project Skeleton", stdout)
        self.assertIn("Status: OK", stdout)
        self.assertEqual(proc.stderr.strip(), "", f"main.py 不应输出 stderr: {proc.stderr!r}")

    def test_06_gitignore_has_required_rules(self) -> None:
        content = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        missing = [p for p in REQUIRED_IGNORE_PATTERNS if p not in content]
        self.assertEqual(missing, [], f".gitignore 缺少规则: {missing}")
        # data 目录本身不应被整体忽略，否则目录结构无法保留
        lines = {ln.strip() for ln in content.splitlines()}
        self.assertNotIn("data/", lines, "不应整体忽略 data/ 目录")

    def test_07_gitignore_actually_works(self) -> None:
        git = _find_git()
        if git is None:
            self.skipTest("未找到 git，跳过实际忽略行为验证")
        if not (PROJECT_ROOT / ".git").is_dir():
            self.skipTest("尚未初始化 Git 仓库，跳过实际忽略行为验证")

        should_be_ignored = [
            ".venv/Scripts/python.exe",
            "data/raw/sample.csv",
            "data/processed/sample.csv",
            "data/cache/sample.pkl",
            "logs/engine.log",
            "engine.db",
            "engine.sqlite3",
            ".env",
            "config/__pycache__/x.pyc",
        ]
        for target in should_be_ignored:
            with self.subTest(target=target):
                proc = subprocess.run(
                    [git, "check-ignore", "-q", "--", target],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(
                    proc.returncode, 0, f"{target} 应被 .gitignore 忽略，但未被忽略"
                )

        # 这些文件必须能被 Git 跟踪
        should_be_tracked = ["main.py", "README.md", "requirements.txt", ".gitignore"]
        for target in should_be_tracked:
            with self.subTest(target=target):
                proc = subprocess.run(
                    [git, "check-ignore", "-q", "--", target],
                    cwd=str(PROJECT_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 1, f"{target} 不应被忽略")

    def test_08_python_environment_ok(self) -> None:
        self.assertGreaterEqual(sys.version_info, (3, 11), f"Python 版本过低: {sys.version}")
        self.assertTrue(sys.executable, "无法取得解释器路径")
        self.assertRegex(os.path.basename(sys.executable).lower(), r"^python(w)?\.exe$")

        import sqlite3  # noqa: PLC0415 - 环境验证需要

        self.assertRegex(sqlite3.sqlite_version, r"^\d+\.\d+\.\d+")

    def test_09_repo_root_is_expected(self) -> None:
        # 防止测试跑在错误的位置（例如被复制到别处）
        self.assertTrue((PROJECT_ROOT / "main.py").is_file())
        self.assertTrue((PROJECT_ROOT / "tests").is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
