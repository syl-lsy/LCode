"""git 基线约定（契约域 5，REQ-007 / AC-002-5 / AC-007-1/4）。

语义（diff 链）：
- 起点（round 0）：
  - git 输入：使用输入仓库自身历史（HEAD），**不做任何 git 操作**（AC-002-5），
    kind=head，仅记录 sha；
  - 非 git 输入（本地文件夹/空目录）：系统自动 `git init` 并创建标记为
    `[lcode-baseline]` 的基线提交，kind=init，打 tag `lcode-baseline/round-0`；
- 第 N 轮 diff（round N ≥ 1）：`git diff <baseline(N-1)>..工作区`，新建文件以
  `git add -N` 纳入（AC-007-1）；baseline_ref 记录对照点；
- 裁决 accepted：将全部改动提交（消息 `[lcode-baseline] round-N: ...`）并打 tag
  `lcode-baseline/round-N`，成为第 N+1 轮对照点（AC-007-4 多轮锚点）；
- 裁决 rejected：不推进基线；下一轮 diff 仍对照 baseline(N-1)。

命名空间隔离：tag 一律带 `lcode-baseline/` 前缀，避免与用户仓库自有 tag 冲突。
"""

import re
from typing import Literal

from pydantic import Field, model_validator

from .common import ContractModel

TAG_PREFIX = "lcode-baseline"
COMMIT_PREFIX = "[lcode-baseline]"

TAG_RE = re.compile(rf"^{TAG_PREFIX}/round-(\d+)$")
COMMIT_RE = re.compile(rf"^{re.escape(COMMIT_PREFIX)} (init|round-(\d+))(?:: (.*))?$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

BaselineKind = Literal["head", "init", "accepted"]


def tag_for_round(round_: int) -> str:
    """round -> tag 名。round 0 = init 起点（git 输入为 head、不打 tag）。"""
    return f"{TAG_PREFIX}/round-{round_}"


class BaselineTag(ContractModel):
    """合法 tag 的机器表达：kind=init 当且仅当 round=0。"""

    round: int = Field(ge=0)
    kind: Literal["init", "accepted"]

    @model_validator(mode="after")
    def _check_kind_round(self) -> "BaselineTag":
        if (self.kind == "init") != (self.round == 0):
            raise ValueError("kind=init 当且仅当 round=0；round>=1 必须为 accepted")
        return self

    @property
    def tag(self) -> str:
        return tag_for_round(self.round)


def parse_tag(tag: str) -> BaselineTag:
    """解析并校验 tag 名；不合法抛 ValueError。"""
    m = TAG_RE.match(tag)
    if not m:
        raise ValueError(f"非法基线 tag：{tag!r}（应为 {TAG_PREFIX}/round-<N>）")
    round_ = int(m.group(1))
    return BaselineTag(round=round_, kind="init" if round_ == 0 else "accepted")


class BaselineCommitMessage(ContractModel):
    """基线提交消息的机器表达与双向转换（render/parse）。"""

    kind: Literal["init", "accepted"]
    round: int = Field(ge=0)
    note: str | None = None

    @model_validator(mode="after")
    def _check_kind_round(self) -> "BaselineCommitMessage":
        if (self.kind == "init") != (self.round == 0):
            raise ValueError("kind=init 当且仅当 round=0")
        return self

    def render(self) -> str:
        label = "init" if self.kind == "init" else f"round-{self.round}"
        note = f": {self.note}" if self.note else ""
        return f"{COMMIT_PREFIX} {label}{note}"

    @classmethod
    def parse(cls, message: str) -> "BaselineCommitMessage":
        m = COMMIT_RE.match(message.strip())
        if not m:
            raise ValueError(f"非法基线提交消息：{message!r}（应为 {COMMIT_PREFIX} init[: 说明] 或 {COMMIT_PREFIX} round-N[: 说明]）")
        label, num, note = m.group(1), m.group(2), m.group(3)
        if label == "init":
            return cls(kind="init", round=0, note=note)
        return cls(kind="accepted", round=int(num), note=note)


def check_commit_sha(sha: str) -> bool:
    """40 位小写十六进制 sha 校验（events.BaselineRef.commit 同口径）。"""
    return bool(COMMIT_SHA_RE.match(sha))
