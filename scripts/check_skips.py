#!/usr/bin/env python3
"""pytest skip 预算门禁（M2 收口台账 G3）：解析 junitxml，杜绝无标记 skip 与 skip 增长。

行为约定：
- 输入 = pytest ``--junitxml`` 产物（默认 ``tmp/junit.xml``，由 ``make test`` 生成）；
- 统计全部 ``<testcase><skipped type="pytest.skip">`` 记录（xfail 的
  ``type="pytest.xfail"`` 不计——现状全仓库无 xfail，引入时须先扩本策略再合入）；
- 每条 skip 的 reason（``<skipped message=...>``）必须命中「允许清单」中某家族的
  **reason 特征子串**，且该家族计数不超预算；否则违规：
  - 不命中任何家族 → **无标记/未登记 skip** 违规（负控用例 ``pytest.skip("probe")`` 走此路）；
  - 命中但超预算 → skip 增长违规（明细含同 reason 已登记数量）；
- 任何违规 → 打印明细并 exit 1；全绿 → 打印家族计数摘要 exit 0；
- 仅标准库（xml.etree / argparse），不依赖第三方；``make test`` 经 pytest 退出码 0
  后调用本脚本，两环都绿才算门禁过。

预算基线（双态口径；FAMILIES 预算不变）：
- **无凭证态**（2026-10-19 实测 ``make test`` = unit+integration，共 12 skipped；
  2026-10-24 S3 新增 OpenSandbox adopt 真冒烟后为 13）：
  unit 7（requires_llm：test_llm_factory 1 + test_llm_real_gateway 6）；
  integration 6（requires_sandbox 4：test_sandbox_real 百炼 +
  requires_sandbox_opensandbox 2：OpenSandbox 冒烟 + S3 adopt 重连）；
- **凭证齐备态**（2026-10-20 实测，共 1 skipped；2026-10-24 S3 后为 2）：
  仅 `requires_sandbox_opensandbox` 跳过（smoke + adopt 重连两条；requires_llm 与
  真百炼冒烟均已实际执行）；
- requires_db 家族（integration/conftest 缺库显式 skip 通道）：预算 = 当前 requires_db
  用例总数 60——本机常态库可用、不出现；CI 已配 postgres service 同理。该通道若真被
  触发（环境缺库），覆盖率 --cov-fail-under=80 环会先红兜底（60 用例 skip 后 app 覆盖
  必不足），本预算只保证「skip 不越过用例总量无限增长」；
- local-git 家族（test_sandbox_local_git 的 git 守卫，无 marker、reason 明确）：预算 4。

同族不同 reason 变体共享预算（如百炼缺密钥与缺模板是同 4 个用例的两种缺因），
所以按家族而非逐条 reason 计。新增 requires_* 豁免 = 加家族条目 + 同步预算，
走评审；未登记的 skip 一律红。
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

# (家族名/marker, reason 特征子串列表, 预算)
FAMILIES: list[tuple[str, list[str], int]] = [
    (
        "requires_llm",
        ["真模型冒烟需要 LLM_BASE_URL", "真网关用例需要 LLM_BASE_URL"],
        7,
    ),
    (
        "requires_sandbox",
        [
            "requires_sandbox 显式跳过",
            "SANDBOX_BAILIAN_TEMPLATE 为空",
            "SANDBOX_BAILIAN_API_URL 为空",
        ],
        4,
    ),
    (
        "requires_sandbox_opensandbox",
        [
            "自托管 OpenSandbox 未部署",
            "requires_sandbox_opensandbox 显式跳过",
            "SANDBOX_OPENSANDBOX_IMAGE 未设置",
        ],
        # 2 = test_opensandbox_smoke（U4 冒烟）+ test_opensandbox_restart_reconnect_by_id
        #   （S3 adopt 真沙箱回归，评审项 1 补位；2026-10-24 预算 1→2）
        2,
    ),
    (
        "requires_db",
        # 覆盖 integration conftest 三种「缺库不红」reason：未配置 DB_NAME /
        # 安全闸 DB_NAME 非 _test / 测试库不可用（均以「requires_db 用例跳过」结尾，
        # 安全闸那条除外——其文本特征为「安全闸：DB_NAME」）
        ["requires_db 用例跳过", "安全闸：DB_NAME"],
        60,
    ),
    (
        "local-git",
        ["本机无 git"],
        4,
    ),
]


def classify(message: str) -> str | None:
    """reason 文本 → 家族名；未命中任何允许清单返回 None（= 违规候选）。"""
    for family, patterns, _budget in FAMILIES:
        if any(p in message for p in patterns):
            return family
    return None


def collect_skips(junit_path: Path) -> list[tuple[str, str, str]]:
    """解析 junit XML，返回 [(classname::name, 家族名或'<UNREGISTERED>', reason)]。"""
    root = ET.parse(junit_path).getroot()  # 解析失败让异常直接冒红（文件坏=门禁红）
    skips: list[tuple[str, str, str]] = []
    for testcase in root.iter("testcase"):
        skipped = testcase.find("skipped")
        if skipped is None or skipped.get("type") != "pytest.skip":
            continue
        reason = skipped.get("message") or (skipped.text or "").strip()
        where = f"{testcase.get('classname', '?')}::{testcase.get('name', '?')}"
        skips.append((where, classify(reason) or "<UNREGISTERED>", reason))
    return skips


def main() -> None:
    parser = argparse.ArgumentParser(description="pytest skip 预算门禁（G3）")
    parser.add_argument(
        "junit",
        nargs="?",
        default="tmp/junit.xml",
        help="pytest --junitxml 产物路径（默认 tmp/junit.xml）",
    )
    args = parser.parse_args()

    junit_path = Path(args.junit)
    if not junit_path.is_file():
        print(
            f"错误: junit 产物不存在：{junit_path}（make test 须带 --junitxml 生成）",
            file=sys.stderr,
        )
        sys.exit(1)

    skips = collect_skips(junit_path)
    counts: Counter[str] = Counter(family for _where, family, _reason in skips)

    # 违规一：未登记 skip（逐条列明细）
    violations: list[str] = [
        f"无标记/未登记 skip: {where}\n    reason: {reason}"
        for where, _family, reason in skips
        if _family == "<UNREGISTERED>"
    ]
    # 违规二：家族超预算（每族报一次，附超出部分的用例位置）
    for family, _patterns, budget in FAMILIES:
        if counts[family] > budget:
            members = [w for w, fam, _r in skips if fam == family]
            violations.append(
                f"skip 超预算: 家族 {family} 计数 {counts[family]} > 预算 {budget}\n"
                f"    用例: {', '.join(members)}"
            )

    summary = ", ".join(f"{f}={counts[f]}" for f, _p, _b in FAMILIES if counts[f]) or "0 skipped"
    if violations:
        print(f"skip 预算检查失败：共 {len(skips)} 条 skipped（{summary}）", file=sys.stderr)
        for v in violations:
            print(v, file=sys.stderr)
        print(
            "提示: 新增豁免须同步 scripts/check_skips.py 的 FAMILIES（家族+预算），走评审；"
            "禁止裸 pytest.skip 无 reason 登记。",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"skip 预算检查通过：共 {len(skips)} 条 skipped（{summary}）")


if __name__ == "__main__":
    main()
