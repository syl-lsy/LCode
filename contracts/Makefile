# contracts/ 自校验入口（独立 harness，也可在本目录内单独使用）
# 根 Makefile 已于 M1 阶段 2（2026-10-17）就位：根 `contracts` / `ci` / `ci-backend` 目标
# 挂 `make -C contracts check`，CI（.github/workflows/ci.yml）经 `make ci-backend` 调用。
# 口径见根 Makefile 与 contracts/README.md「五、阶段 2 接入挂点」。
.PHONY: check test gen ts-check clean

## 一条命令自校验：pytest（含 fixtures 正负例 + 生成物同步检查）+ TS 生成与 tsc 编译
check: test ts-check
	@echo "contracts check: all green"

## 仅 Python 侧校验（事件/API/数据模型/结构化输出/基线 fixtures + 生成物 in-sync）
test:
	uv run pytest tests -q

## 重新生成契约产物：generated/json/*.schema.json（+ node 可用时生成 generated/ts）
gen:
	uv run python tools/generate_json_schema.py
	bash tools/ts-check.sh

## TS 侧：node 探测 -> 生成 types/zod -> tsc --noEmit（node 不可用 = 显式失败，绝不静默跳过）
ts-check:
	bash tools/ts-check.sh

clean:
	rm -rf .pytest_cache src/lcode_contracts/__pycache__ tests/__pycache__
