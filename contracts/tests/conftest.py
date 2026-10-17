"""harness 公共工具：fixtures 路径与加载。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(rel: str) -> dict:
    """读取 wire 格式 fixture JSON（相对 tests/fixtures/ 的路径）。"""
    return json.loads((FIXTURES / rel).read_text(encoding="utf-8"))
