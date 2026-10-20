#!/usr/bin/env python3
"""alembic 漂移门禁（M2 收口台账 G5）：独立临时库上 全量迁移 + `alembic check`。

行为约定（幂等、可回滚、任何一步失败即非 0、离场必清理临时库）：

1. 连接参数优先级：**真实环境变量 > .env > .env.test**（与 pydantic-settings 的
   「环境变量压过 env_file」同口径；CI 由 workflow job env 注入 DB_*，本地取 .env）。
   缺 DB_HOST/DB_USER → 清晰报错 exit 1（db-check 是门禁目标，不做静默跳过）；
2. `dropdb --if-exists --force lcode_dbcheck`（清理上次残留，保证幂等）；
3. `createdb lcode_dbcheck`（临时库名**固定**，不碰 lcode_test / lcode_e2e_test）；
4. `uv run --package lcode-server alembic -c apps/server/alembic.ini upgrade head`
   —— 子进程 env 显式注入 DB_NAME=lcode_dbcheck + 连接参数（压过 .env/.env.test）；
5. `alembic check` —— 模型 metadata 与已迁移库结构比对，有漂移（如模型加列没建迁移）
   非 0 退出即门禁红；
6. finally：`dropdb --if-exists --force lcode_dbcheck` 清理（失败仅告警不吞主码）。

安全：库名常量 + argv 全字面量（shell=False）；createdb/dropdb 连接参数走 libpq
标准环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE），密码不进 argv、不打日志
（NFR-004）。alembic 子进程拿的是 DB_* 环境变量（app.core.db 统一拼串口径）。

Makefile 入口：``make db-check``；已并入 ``make ci-backend`` 链。
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_NAME = "lcode_dbcheck"  # 门禁固定临时库（硬性约束：只允许动这一个）

# libpq 管理命令用的既有库：连 postgres 维护库即可（drop/create 目标库本身不要求已连接）
_ADMIN_DB = "postgres"

_DOTENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _dotenv_values(path: Path) -> dict[str, str]:
    """极简 .env 解析（KEY=VALUE、剥首尾成对引号），只为取连接参数，不求全语义等价。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _DOTENV_LINE.match(line)
        if not m:
            continue
        value = m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[m.group(1)] = value
    return values


def _cfg(key: str, fallback: str = "") -> str:
    """连接参数取值：真实环境变量（非空）> .env > .env.test > fallback。"""
    env_val = os.environ.get(key, "")
    if env_val:
        return env_val
    for file in (ROOT / ".env", ROOT / ".env.test"):
        value = _dotenv_values(file).get(key, "")
        if value:
            return value
    return fallback


def _libpq_env(database: str) -> dict[str, str]:
    """createdb/dropdb 子进程环境：密码经 PGPASSWORD，argv 全字面量。"""
    env = dict(os.environ)
    env["PGHOST"] = _cfg("DB_HOST")
    env["PGPORT"] = _cfg("DB_PORT", "5432")
    env["PGUSER"] = _cfg("DB_USER")
    password = _cfg("DB_PASSWORD")
    if password:
        env["PGPASSWORD"] = password
    else:
        env.pop("PGPASSWORD", None)
    env["PGDATABASE"] = database
    return env


def _alembic_env() -> dict[str, str]:
    """alembic 子进程环境：DB_ 配置组显式覆写（pydantic-settings 环境变量优先）。"""
    env = dict(os.environ)
    env["DB_HOST"] = _cfg("DB_HOST")
    env["DB_PORT"] = _cfg("DB_PORT", "5432")
    env["DB_NAME"] = DB_NAME
    env["DB_USER"] = _cfg("DB_USER")
    env["DB_PASSWORD"] = _cfg("DB_PASSWORD")
    return env


def _run(cmd: list[str], env: dict[str, str], stage: str, *, quiet_ok: bool = False) -> int:
    """执行一步命令并回显输出；返回退出码。stage 用于失败定位（一眼看出败在哪步）。"""
    print(f"[db-check] {stage}: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0 and not quiet_ok:
        print(f"[db-check] 失败于阶段「{stage}」（exit={result.returncode}）", file=sys.stderr)
    return result.returncode


def _alembic(*args: str, env: dict[str, str]) -> int:
    cmd = [
        "uv",
        "run",
        "--package",
        "lcode-server",
        "alembic",
        "-c",
        "apps/server/alembic.ini",
        *args,
    ]
    return _run(cmd, env, "alembic " + " ".join(args))


def main() -> None:
    parser = argparse.ArgumentParser(description="alembic 漂移门禁（G5，临时库 lcode_dbcheck）")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="跑完保留临时库用于人工排查（默认离场清理；CI 恒清理）",
    )
    args = parser.parse_args()

    host, user = _cfg("DB_HOST"), _cfg("DB_USER")
    if not host or not user:
        print(
            "[db-check] 错误：缺 DB_HOST/DB_USER（环境变量或 .env/.env.test），"
            "db-check 为门禁目标不做静默跳过。",
            file=sys.stderr,
        )
        sys.exit(1)

    admin_env = _libpq_env(_ADMIN_DB)
    alembic_env = _alembic_env()
    # sys.exit 置于 try 内：SystemExit 照常触发 finally 清理，退出码不被吞
    try:
        # 1. 前置清理（幂等：上次中断残留的临时库先删掉）
        if _run(
            ["dropdb", "--if-exists", "--force", DB_NAME], admin_env, "前置清理", quiet_ok=True
        ):
            sys.exit(1)
        # 2. 建临时库
        if _run(["createdb", DB_NAME], admin_env, "createdb"):
            sys.exit(1)
        # 3. 全量迁移 + 4. 漂移检查（模型加列没建迁移 → check 非 0）
        if _alembic("upgrade", "head", env=alembic_env):
            sys.exit(1)
        if _alembic("check", env=alembic_env):
            sys.exit(1)
        print("[db-check] 通过：upgrade head + alembic check 均无漂移")
    finally:
        # 5. 离场清理（--keep 仅供本地排查；CI 恒清理；清理失败仅告警，不改主退出码）
        if args.keep:
            print(f"[db-check] --keep：保留临时库 {DB_NAME}（排查完请手动 dropdb {DB_NAME}）")
        elif _run(
            ["dropdb", "--if-exists", "--force", DB_NAME], admin_env, "离场清理", quiet_ok=True
        ):
            print(f"[db-check] 警告：临时库清理失败，请手动 dropdb {DB_NAME}", file=sys.stderr)


if __name__ == "__main__":
    main()
