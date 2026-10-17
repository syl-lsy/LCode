"""契约 -> JSON Schema 导出（生成管线的 Python 侧）。

导出注册表 SCHEMA_EXPORTS 是"哪些模型对外出 Schema"的唯一清单；
tools/generate_json_schema.py 把它写成 generated/json/**.json（必须入库），
tools/ts/gen_ts.mjs 再从 JSON Schema 生成 TS 类型 + zod schema。

命名规则：<域>/<名称>.json（事件域名称 = type 字面量），如
events/message_delta.json、structured/reviewer_findings.json。
文件内 title = 模型类名（TS 侧接口名来源）。
"""

import json
from pathlib import Path
from typing import Any

from . import baseline as baseline_mod
from . import events as events_mod
from . import rest as rest_mod
from .common import AgentRef, CompactInfo, TodoItem, Usage
from .findings import Coverage, Finding, ReviewerFindings

GENERATED_DIR = Path(__file__).resolve().parents[2] / "generated" / "json"


def _snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


SCHEMA_EXPORTS: dict[str, type] = {
    # 事件域：每型一份（自包含 $defs）+ 联合类型一份（TS anyOf 联合）
    **{f"events/{t}": cls for t, cls in events_mod.EVENT_TYPES.items()},
    "events/event_union": events_mod.EventUnion,
    # REST 域：每 DTO 一份
    **{f"rest/{_snake(name)}": cls for name, cls in rest_mod.REST_DTOS.items()},
    # 结构化输出域
    "structured/reviewer_findings": ReviewerFindings,
    "structured/finding": Finding,
    "structured/coverage": Coverage,
    # 公共类型（事件/REST 内部引用的 $defs 顶层可见版）
    "common/agent_ref": AgentRef,
    "common/usage": Usage,
    "common/todo_item": TodoItem,
    "common/compact_info": CompactInfo,
    # git 基线域（tag/提交消息的机器表达）
    "baseline/baseline_tag": baseline_mod.BaselineTag,
    "baseline/baseline_commit_message": baseline_mod.BaselineCommitMessage,
}


def export_all() -> dict[str, dict[str, Any]]:
    """name -> JSON Schema（dict 形态，比较与写文件两用）。"""
    return {name: cls.model_json_schema() for name, cls in SCHEMA_EXPORTS.items()}


def write_all(target_dir: Path | None = None) -> list[Path]:
    """写出全部 Schema（pretty JSON，键排序保证 diff 稳定）；返回写入文件清单。"""
    target = target_dir or GENERATED_DIR
    written: list[Path] = []
    for name, schema in export_all().items():
        path = target / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        written.append(path)
    return written
