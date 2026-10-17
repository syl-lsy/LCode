"""结构化输出 schema（Q6 定稿）：reviewer findings（REQ-011）。

用途：
1. LangChain structured output（ToolStrategy/with_structured_output）的目标
   schema —— pydantic 模型直接可用；
2. message_finished.structured.data 的载荷类型（事件协议内携带）；
3. diff_reviews / 汇总报告落库的 jsonb 结构。

失败路径（AC-011-3，PRD Q2 建议默认值）：校验失败重试 1 次，仍失败降级为
文本 —— 此时 message_finished.structured.status = "fallback"、data = null，
前端显式标注「未结构化」，绝不静默丢弃。
"""

from typing import Literal

from pydantic import Field, model_validator

from .common import ContractModel

SchemaVersion = Literal["1"]


class Finding(ContractModel):
    """单条发现。line/end_line 为 1 起始行号；文件级问题（无行号）两者皆 null。"""

    file: str
    line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal["security", "correctness", "performance", "maintainability", "style"]
    title: str
    description: str
    evidence: str | None = None
    suggestion: str | None = None
    confidence: Literal["high", "medium", "low"] = "medium"

    @model_validator(mode="after")
    def _check_line_window(self) -> "Finding":
        if self.end_line is not None:
            if self.line is None:
                raise ValueError("end_line 需要与 line 成对出现（文件级问题两者都应为 null）")
            if self.end_line < self.line:
                raise ValueError("end_line 必须 >= line")
        return self


class Coverage(ContractModel):
    """审查覆盖面（扇出场景下部分失败要如实呈现，AC-009-4）。"""

    files_reviewed: int = Field(default=0, ge=0)
    files_skipped: list[str] = Field(default_factory=list)
    notes: str | None = None


class ReviewerFindings(ContractModel):
    """reviewer 子智能体的结构化返回（汇总端机器可拼，AC-011-1）。"""

    schema_version: SchemaVersion = "1"
    summary: str  # 面向人的总结（AC-011-2：同时有可读文字，非裸 JSON）
    findings: list[Finding] = Field(default_factory=list)
    coverage: Coverage | None = None
