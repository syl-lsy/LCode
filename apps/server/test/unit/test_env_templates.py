"""unit 层：env 模板 ↔ settings 字段的防漂移守门。

`.env.example` / `.env.test.example` 是配置的唯一对外入口（基座铁律 5：新增配置变量
必须同步模板），但两侧不一致**没有任何运行时症状**——写错前缀、拼错字段名、加了字段
忘进模板，都要等到某台机器上才发现。本测试把模板键集合与 `app/core/config.py` 的
分组字段钉在一起，两个模板共用同一套口径（同一解析函数 + 同一组断言）：

- 正向：模板里每个键都能映射到某个 settings 组的字段（`<前缀><字段名大写>` 规则，
  无前缀的键归 `CommonSettings`）；
- 反向：每个 settings 字段都有对应的模板键；确有意不进模板的字段必须显式登记到
  `_EXEMPT_FIELDS`（避免"默默放过"变成新的漂移面）。

解析口径与 pydantic-settings 的 dotenv 读取一致：忽略空行与 `#` 注释行，取首个 `=`
左侧为键（值部分含引号/空格不参与断言）。
"""

import re
from pathlib import Path

import pytest
from pydantic_settings import BaseSettings

from app.core.config import (
    AgentSettings,
    CommonSettings,
    DBSettings,
    LLMSettings,
    LogSettings,
    SandboxSettings,
    SSESettings,
    WebSettings,
)

# 本文件位于 apps/server/test/unit/ 下：parents[4] 即仓库根
REPO_ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = [".env.example", ".env.test.example"]

# 前缀 → 分组：与 core/config.py 各组的 env_prefix 一一对应（"" = 无前缀组）
GROUPS: dict[str, type[BaseSettings]] = {
    "": CommonSettings,
    "WEB_": WebSettings,
    "DB_": DBSettings,
    "LLM_": LLMSettings,
    "SANDBOX_": SandboxSettings,
    "SSE_": SSESettings,
    "AGENT_": AgentSettings,
    "LOG_": LogSettings,
}

# 反向断言的豁免登记位（存 env 键名）：确有意不进模板的字段写在这里并注明理由
_EXEMPT_FIELDS: set[str] = set()

_KEY_LINE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


def _template_keys(file_name: str) -> set[str]:
    """模板里声明的键集合（忽略空行与 `#` 注释行）。"""
    keys: set[str] = set()
    for raw_line in (REPO_ROOT / file_name).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _KEY_LINE.match(line)
        if match:
            keys.add(match.group("key"))
    return keys


def _resolve(key: str) -> tuple[type[BaseSettings], str]:
    """env 键 → (settings 分组, 字段名)：按最长前缀匹配（无匹配归 `CommonSettings`）。"""
    for prefix in sorted(GROUPS, key=len, reverse=True):
        if key.startswith(prefix):  # "" 兜底：始终命中
            return GROUPS[prefix], key[len(prefix) :].lower()
    raise AssertionError("unreachable")  # pragma: no cover - 兜底分支不可达


@pytest.mark.parametrize("file_name", TEMPLATES)
def test_template_is_parsable_and_not_empty(file_name):
    """解析口径自检：模板必须能解析出键（防止解析器失效导致空集"假绿"）。"""
    keys = _template_keys(file_name)

    assert len(keys) >= 20, f"{file_name} 只解析到 {len(keys)} 个键，解析口径可能已失效"


@pytest.mark.parametrize("file_name", TEMPLATES)
def test_every_template_key_maps_to_a_settings_field(file_name):
    """正向守门：模板里不能出现 settings 不认的键（前缀/字段名拼错在这里拦住）。"""
    for key in sorted(_template_keys(file_name)):
        group, field = _resolve(key)

        assert field in group.model_fields, (
            f"{file_name} 的 {key} 映射不到 {group.__name__} 的字段"
            f"（可用字段：{sorted(group.model_fields)}）"
        )


@pytest.mark.parametrize("file_name", TEMPLATES)
def test_every_settings_field_is_covered_by_template(file_name):
    """反向守门：settings 新增字段必须同步进模板（基座铁律 5），否则只在用时才暴露。"""
    keys = _template_keys(file_name)

    missing = [
        env_key
        for prefix, group in GROUPS.items()
        for field in group.model_fields
        if (env_key := f"{prefix}{field.upper()}") not in keys and env_key not in _EXEMPT_FIELDS
    ]

    assert not missing, f"{file_name} 缺少 settings 字段对应的键：{sorted(missing)}"
