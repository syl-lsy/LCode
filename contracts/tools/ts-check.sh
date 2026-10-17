#!/usr/bin/env bash
# TS 侧契约校验：node 探测 -> 生成 types/zod -> fixture↔zod 实测 -> tsc --noEmit
#                  -> generated/ts git in-sync 守门。
# node 不可用时显式失败（绝不静默跳过）—— 见 contracts/README.md「TODO-TS」。
set -euo pipefail

cd "$(dirname "$0")/ts"

if ! command -v node >/dev/null 2>&1; then
  echo "FAIL: node 不可用 —— TS 类型/zod 生成与 tsc 编译无法执行。" >&2
  echo "      这是契约自校验的显式失败（不静默跳过）。" >&2
  echo "      处理：安装 node >= 20 后重跑 make -C contracts check；" >&2
  echo "      或按 contracts/README.md「TODO-TS」记录的接入点在阶段 2 补挂。" >&2
  exit 1
fi

if [ ! -d node_modules ]; then
  echo "installing ts toolchain (first run, needs network)..."
  npm install --no-fund --no-audit
fi

node gen_ts.mjs
node validate_fixtures.mjs
npx tsc --noEmit -p tsconfig.json

# ---- generated/ts in-sync 守门（P3-5）----
# 生成后必须与版本库（索引）一致：防"改了单源忘提交生成物"被合入。
# 本地"已生成未提交"窗口内本守门会红——预期行为（生成物与提交必须同步），
# 暂存/提交生成物后即绿。无 git 环境显式提示跳过（不许静默）。
GENERATED_GIT_PATH="../../generated/ts"
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git diff --exit-code -- "$GENERATED_GIT_PATH" >/dev/null 2>&1; then
    echo "FAIL: generated/ts 与版本库索引不一致（已重新生成但未暂存/未提交）。" >&2
    echo "      正确姿势：改单源 → make -C contracts gen → 提交生成物 → check。" >&2
    git diff --stat -- "$GENERATED_GIT_PATH" | tail -20 >&2
    exit 1
  fi
  untracked="$(git ls-files --others --exclude-standard -- "$GENERATED_GIT_PATH")"
  if [ -n "$untracked" ]; then
    echo "FAIL: generated/ts 存在未入库文件（生成物必须入库）：" >&2
    echo "$untracked" | sed 's/^/      /' >&2
    exit 1
  fi
  echo "generated/ts in-sync: OK"
else
  echo "skip: 非 git 环境 —— generated/ts in-sync 守门显式跳过（原因：无法读取版本库状态）" >&2
fi

echo "ts-check: OK"
