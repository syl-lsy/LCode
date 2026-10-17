# contracts/ 自校验入口（M1 阶段 1：根 Makefile 尚未建立，本文件是唯一命令入口）
# 阶段 2 根 Makefile 就位后，根 `make ci` 挂 `make -C contracts check`（见 contracts/README.md）
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
