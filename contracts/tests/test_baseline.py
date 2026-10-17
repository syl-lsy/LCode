"""git 基线约定 harness（契约域 5，REQ-007）。"""

import pytest
from pydantic import ValidationError

from lcode_contracts.baseline import (
    BaselineCommitMessage,
    BaselineTag,
    check_commit_sha,
    parse_tag,
    tag_for_round,
)


def test_tag_roundtrip() -> None:
    t = parse_tag("lcode-baseline/round-3")
    assert (t.round, t.kind) == (3, "accepted")
    assert t.tag == "lcode-baseline/round-3"
    assert parse_tag("lcode-baseline/round-0").kind == "init"
    assert tag_for_round(0) == "lcode-baseline/round-0"


@pytest.mark.parametrize("bad", [
    "lcode/round-1",
    "lcode-baseline/round/-1",
    "lcode-baseline/round-1/2",
    "baseline/round-1",
    "lcode-baseline/round-",
    "lcode-baseline/round-01a",
])
def test_bad_tag_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_tag(bad)


def test_commit_message_roundtrip() -> None:
    m = BaselineCommitMessage(kind="accepted", round=2, note="2 files, +17/-3")
    rendered = m.render()
    assert rendered == "[lcode-baseline] round-2: 2 files, +17/-3"
    assert BaselineCommitMessage.parse(rendered) == m
    init = BaselineCommitMessage.parse("[lcode-baseline] init")
    assert (init.kind, init.round, init.note) == ("init", 0, None)


@pytest.mark.parametrize("bad", [
    "[lcode] init",
    "[lcode-baseline] round-1x",
    "lcode-baseline round-1",
    "[lcode-baseline] round--1",
    "[lcode-baseline] round-",
])
def test_bad_commit_message_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        BaselineCommitMessage.parse(bad)


def test_kind_round_invariant() -> None:
    with pytest.raises(ValidationError):
        BaselineTag(round=2, kind="init")
    with pytest.raises(ValidationError):
        BaselineTag(round=0, kind="accepted")
    with pytest.raises(ValidationError):
        BaselineTag(round=-1, kind="init")


def test_commit_sha_format() -> None:
    assert check_commit_sha("e83c5163316f89b8de6d78a1e58a28f6a3d1a1d0")
    assert not check_commit_sha("xyz")
    assert not check_commit_sha("E83C5163316F89B8DE6D78A1E58A28F6A3D1A1D0")  # 大写不算
