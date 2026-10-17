#!/usr/bin/env bash
# TS 侧契约校验：node 探测 -> 生成 types/zod -> tsc --noEmit。
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
npx tsc --noEmit -p tsconfig.json
echo "ts-check: OK"
