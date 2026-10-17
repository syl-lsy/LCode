# LCode 根 Makefile —— 质量门禁命令的单一事实源
#
# 约定：
# - 一切命令从仓库根执行（基座铁律 6：env_file=".env" 相对运行时 cwd 解析）；
# - CI（.github/workflows/ci.yml）只调本文件的目标，不在 yml 里复制命令；
# - 兼容 GNU Make 3.81（macOS 自带），勿使用 4.x 特性；
# - `make ci` 系列目标按依赖顺序串行即可，不要加 -j；
# - env 自举：缺 .env / .env.test 时从入库模板 cp——干净 checkout（CI）与本地行为一致。

SERVICE := lcode-server
ENV_BOOTSTRAP := test -f .env || cp .env.example .env
ENV_TEST_BOOTSTRAP := test -f .env.test || cp .env.test.example .env.test

.PHONY: help dev debug dev-web \
        test test-unit test-integration test-e2e test-web test-changed \
        lint lint-py lint-web typecheck build-web install-web \
        contracts ci ci-backend ci-frontend \
        db-migrate db-upgrade db-downgrade clean

help: ## 列出全部目标（自文档）
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- 开发

dev: ## 后端 dev 服务器（fastapi dev 127.0.0.1:8080；缺 .env 自动从模板自举）
	@$(ENV_BOOTSTRAP)
	uv run --package $(SERVICE) fastapi dev apps/server/app/main.py --port 8080

debug: ## debugpy 断点调试：先跑本目标，IDE/VSCode 连 127.0.0.1:5678
	@$(ENV_BOOTSTRAP)
	export PYTHONDONTWRITEBYTECODE=1; \
	uv run --package $(SERVICE) python -m debugpy --listen 127.0.0.1:5678 --wait-for-client \
		-m fastapi dev apps/server/app/main.py --port 8000

dev-web: ## 前端 dev 服务器（Vite，/api 代理 127.0.0.1:8000；依赖未装先 make install-web）
	pnpm --dir web dev

# ---------------------------------------------------------------- 测试（三层环，口径见 docs/arch/test-plan-m2.md）

test: ## 后端门禁环：unit+integration，覆盖率 --cov-fail-under=80（e2e 不计覆盖）
	@$(ENV_TEST_BOOTSTRAP)
	uv run pytest apps/server/test/unit/ apps/server/test/integration/ -q \
		--cov=app --cov-report=term-missing --cov-fail-under=80

test-unit: ## 仅后端 unit（全 mock、零网络）
	uv run pytest apps/server/test/unit/ -q

test-integration: ## 仅后端 integration（骨架期不触库；requires_* 缺依赖显式跳过）
	@$(ENV_TEST_BOOTSTRAP)
	uv run pytest apps/server/test/integration/ -q

test-e2e: ## 后端 e2e：子进程真服务（:18000）；缺 .env.test 自动从模板自举
	@$(ENV_TEST_BOOTSTRAP)
	uv run pytest apps/server/test/e2e/ -q

test-web: ## 前端 vitest 全量（骨架期不挂覆盖率阈值；阈值随 M2 启用 test:coverage）
	pnpm --dir web test

# 开发内环（非门禁）：按 git 变更跑测试文件，语义 = 改了哪个测试跑哪个，源码改动不自动映射。
# 用法：make test-changed                    —— 工作区 staged/unstaged/untracked 变更
#       make test-changed SINCE=m1-start     —— 追加「自该 ref 起的已提交变更」（merge-base 比较）
#       注意是 make 变量 SINCE=<ref>：GNU make 不认 `--since` 命令行选项，会当未知选项报错。
test-changed: ## 按 git 变更跑关联测试（开发内环；替代不了 make test 的覆盖率门禁）
	@$(ENV_TEST_BOOTSTRAP)
	uv run python scripts/run_changed_tests.py $(if $(SINCE),--since $(SINCE),)

# ---------------------------------------------------------------- 静态检查

lint-py: ## 后端 lint：ruff check + ruff format --check + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

lint-web: ## 前端 lint：eslint
	pnpm --dir web lint

lint: lint-py lint-web ## lint 全量（后端 ruff+mypy / 前端 eslint）

typecheck: ## 前端类型：tsc --noEmit（后端 mypy 已含在 lint-py，此处不重复）
	pnpm --dir web typecheck

build-web: ## 前端生产构建：vite build
	pnpm --dir web build

install-web: ## 前端依赖安装：pnpm install --frozen-lockfile（lock 与 package.json 不一致即失败）
	pnpm --dir web install --frozen-lockfile

# ---------------------------------------------------------------- 契约

contracts: ## 契约单源自校验 = pytest harness + TS 生成/tsc/in-sync 守门（TS 侧需 node>=20）
	$(MAKE) -C contracts check

# ---------------------------------------------------------------- CI 里程碑环（yml 只调这两个聚合目标；方案 A「每次全跑」）

ci-backend: contracts lint-py test test-e2e ## CI 后端环 = contracts check + ruff/mypy + test(覆盖门禁) + e2e

ci-frontend: install-web lint-web typecheck test-web build-web ## CI 前端环 = frozen install + eslint + tsc + vitest + vite build

ci: ci-backend ci-frontend ## 全量里程碑环（本地与 CI 同一入口，差异只在配置不在代码）

# ---------------------------------------------------------------- 数据库
# M2 真用（integration/e2e 接 PostgreSQL 后）。本机 Docker 当前不可用，
# 以 docker-compose 起库的 db-up 类目标**留 M2 TODO**，此处不放红目标。
# 下列 alembic 目标只要求 Postgres 可达（DB_* 读 .env / .env.test），与 Docker 无关。

db-migrate: ## alembic 自动生成迁移：make db-migrate message="add table x"
	uv run --package $(SERVICE) alembic -c apps/server/alembic.ini revision --autogenerate -m "$(message)"

db-upgrade: ## alembic 升级到 head
	uv run --package $(SERVICE) alembic -c apps/server/alembic.ini upgrade head

db-downgrade: ## alembic 回退：make db-downgrade version=-1
	uv run --package $(SERVICE) alembic -c apps/server/alembic.ini downgrade $(version)

# ---------------------------------------------------------------- 清理

clean: ## 清理本地缓存与构建产物（不动 .venv / node_modules / .env*）
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov apps/server/.pytest_cache
	rm -rf web/coverage web/dist
	$(MAKE) -C contracts clean
