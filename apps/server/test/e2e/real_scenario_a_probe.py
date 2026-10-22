"""M3 真机取证：场景 A 最小主干（真模型 + 真百炼沙箱 + 真 PG），**手动运行，不是门禁**。

用法（仓库根）::

    uv run --package lcode-server python apps/server/test/e2e/real_scenario_a_probe.py

它做什么：

1. 只读 `.env`（真凭证；绝不回显密钥值），在**独立测试库** `lcode_real_probe_test`
   上重建 schema（不碰开发库 `lcode`）；
2. 进程内起真 app（lifespan 装配真 `AsyncPostgresSaver`），经 ASGI 走真 REST；
3. 预置 git 仓库（`calc.py` 带 bug + README，初始提交）→ 发一条「修 bug 并验证」的
   自然语言任务 → Agent 每弹一次审批就用 REST 批准（scope=once）→ 等 run 收口；
4. 收集事实：run 终态 / 审批行 / diff 审查（round/files/totals）/ git_baselines /
   沙箱内 tag / 事件计数，写 `.scratch/m3/real-demo-<timestamp>.json`（只含事实与
   计数，**不含任何密钥**，最终回复只截前 500 字）；
5. 删除会话（kill 沙箱）后退出。失败也写证据并给出归类（environment / network /
   model / sandbox / product）。

marker 惯例（`requires_llm` + `requires_sandbox`）：本文件**不命名为 test_*.py**，
避免被 `test/e2e/conftest.py`（强制假模型/假沙箱/e2e 库）接管；就绪检查与显式 skip
语义在 `_readiness()` 里按同一口径实现——缺 LLM_BASE_URL / 模型密钥 / 沙箱凭证时打印
skip 原因并以 0 退出（不产生失败）。凭据存在时**会真跑**（可能耗时数分钟）。

安全纪律（NFR-004）：本文件只打印变量名与 SET/EMPTY，不打印值；证据 JSON 不含密钥、
不含完整模型输出、不含环境变量表。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROBE_DB = "lcode_real_probe_test"
"""专用取证库（`_test` 结尾：与开发库物理隔离，跑完保留供人工复查）。"""

ROOT = Path(__file__).resolve().parents[4]
SERVER_DIR = Path(__file__).resolve().parents[2]
EVIDENCE_DIR = ROOT / ".scratch" / "m3"

TASK_PROMPT = (
    "这是 LCode 场景 A 的真机冒烟。仓库根目录的绝对路径是 /home/user/lcode-demo 。"
    "请完成三件事：1) 查看 /home/user/lcode-demo/calc.py，把 add 函数的实现从 a - b "
    "修正为 a + b（工具参数请使用这个绝对路径，不要用相对路径）；"
    '2) 运行 `cd /home/user/lcode-demo && python3 -c "import calc; print(calc.add(2, 3))"`'
    " 验证输出 5；3) 简要汇报结果。不要改动其他文件。"
)

WORKSPACE = "/home/user/lcode-demo"
"""取证仓库的沙箱绝对路径（百炼 code 模板的 home 为 /home/user；模块文档已注明）。"""

SEED_FILES = {
    "lcode-demo/calc.py": "def add(a, b):\n    return a - b  # BUG: should be a + b\n",
    "lcode-demo/README.md": "# Demo\n\nA tiny calculator demo.\n",
}
"""预置仓库内容（相对路线上传到沙箱默认 cwd 下；绝对路径 /x 会落到容器根 /）。"""

_PYTHON = sys.executable


# --------------------------------------------------------------------------- 只读就绪检查


def _read_env_file() -> dict[str, str]:
    """解析 `.env` 为 dict（不打印值；真实环境变量优先）。"""
    values: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _readiness() -> tuple[bool, list[str]]:
    """真模型 + 真沙箱就绪判定（只报变量名与 SET/EMPTY，不回显值）。"""
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
    file_values = _read_env_file()

    def env_of(name: str) -> str:
        return os.environ.get(name, "") or file_values.get(name, "")

    problems: list[str] = []
    llm_base = env_of("LLM_BASE_URL")
    key_env_name = env_of("LLM_API_KEY_ENV") or "LLM_API_KEY"
    llm_key = env_of(key_env_name)
    if not llm_base:
        problems.append("缺配置：LLM_BASE_URL 为空")
    if not llm_key:
        problems.append(f"缺凭证：{key_env_name} 未设置")

    sandbox_key_env = env_of("SANDBOX_BAILIAN_API_KEY_ENV") or "BAILIAN_API_KEY"
    if not env_of(sandbox_key_env):
        problems.append(f"缺凭证：{sandbox_key_env} 未设置")
    for name in ("SANDBOX_BAILIAN_API_URL", "SANDBOX_BAILIAN_TEMPLATE"):
        if not env_of(name):
            problems.append(f"缺配置：{name} 为空")

    facts = [
        f"LLM_BASE_URL={'SET' if llm_base else 'EMPTY'}",
        f"{key_env_name}={'SET' if llm_key else 'EMPTY'}",
        f"{sandbox_key_env}={'SET' if env_of(sandbox_key_env) else 'EMPTY'}",
        f"SANDBOX_BAILIAN_API_URL={'SET' if env_of('SANDBOX_BAILIAN_API_URL') else 'EMPTY'}",
        f"SANDBOX_BAILIAN_TEMPLATE={'SET' if env_of('SANDBOX_BAILIAN_TEMPLATE') else 'EMPTY'}",
    ]
    print("[readiness] " + " / ".join(facts), flush=True)
    return not problems, problems


# --------------------------------------------------------------------------- 库准备


def _prepare_database() -> None:
    """重建专用取证库并跑迁移（env DB_NAME 覆盖 .env 的值）。"""
    import psycopg2  # type: ignore[import-untyped]  # 无官方 stubs（同 integration conftest 口径）

    env = dict(os.environ)
    env["DB_NAME"] = PROBE_DB
    connection = psycopg2.connect(
        host=env.get("DB_HOST", "127.0.0.1"),
        port=env.get("DB_PORT", "5432"),
        dbname="postgres",
        user=env.get("DB_USER", "postgres"),
        password=env.get("DB_PASSWORD", ""),
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP DATABASE IF EXISTS "{PROBE_DB}" WITH (FORCE)')
            cursor.execute(f'CREATE DATABASE "{PROBE_DB}"')
    finally:
        connection.close()

    result = subprocess.run(
        [
            "uv",
            "run",
            "--package",
            "lcode-server",
            "alembic",
            "-c",
            str(ROOT / "apps" / "server" / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head 失败（exit={result.returncode}）：{result.stderr[-800:]}"
        )


def _query(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    import psycopg2  # noqa: PLC0415 —— 延迟导入：app 导入前先完成就绪检查/建库

    connection = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
    )
    try:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            columns = [item[0] for item in cursor.description or ()]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    finally:
        connection.close()


# --------------------------------------------------------------------------- 沙箱预置


def _seed_sandbox(client: Any) -> dict[str, Any]:
    """在真沙箱里建一个**专用子目录仓库**（模拟已 clone）+ 初始提交。

    上传使用相对路径（`lcode-demo/...` → 沙箱默认 cwd `/home/user`）；绝对路径 `/x`
    会写到容器根 `/`（实测：上传语义是容器绝对路径）。
    """

    def run(command: str) -> str:
        result = client.execute(command, timeout=120)
        if result.exit_code != 0:
            raise RuntimeError(
                f"沙箱命令失败（exit={result.exit_code}）：{command}\n{result.output[:500]}"
            )
        return result.output

    responses = client.upload_files(
        [(name, content.encode("utf-8")) for name, content in SEED_FILES.items()]
    )
    failures = [response for response in responses if getattr(response, "error", None)]
    if failures:
        raise RuntimeError(f"预置文件上传失败：{[item.error for item in failures]}")
    run(f"git init -q {WORKSPACE}")
    run(f"git -C {WORKSPACE} add -A")
    run(f'git -C {WORKSPACE} -c user.name=lcode -c user.email=lcode@local commit -qm "initial"')
    head = run(f"git -C {WORKSPACE} rev-parse HEAD").strip().splitlines()[-1].strip()
    return {"workspace": WORKSPACE, "head": head}


# --------------------------------------------------------------------------- 主流程


async def _run_probe() -> dict[str, Any]:
    import httpx

    from app.dependencies import get_sandbox_manager
    from app.main import app

    evidence: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "database": PROBE_DB,
        "task_prompt": TASK_PROMPT,
        "status": "failed",
        "failure_class": None,
        "notes": [],
    }
    session_id: str | None = None
    run_id: str | None = None

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://probe", timeout=httpx.Timeout(60.0)
        ) as http:
            session = (await http.post("/api/v1/sessions", json={"title": "real probe"})).json()[
                "data"
            ]
            session_id = session["id"]
            evidence["session_id"] = session_id

            manager = get_sandbox_manager()
            client = manager.create(session_id, timeout_seconds=900)
            evidence["sandbox_id"] = client.id
            seed = _seed_sandbox(client)
            evidence["seed"] = seed

            ack = await http.post(
                f"/api/v1/sessions/{session_id}/messages", json={"content": TASK_PROMPT}
            )
            ack_data = ack.json()["data"]
            run_id = ack_data["run_id"]
            evidence["run_id"] = run_id

            deadline = time.monotonic() + 360
            approvals_seen: list[dict[str, Any]] = []
            terminal_state = "unknown"
            while time.monotonic() < deadline:
                detail = (await http.get(f"/api/v1/sessions/{session_id}")).json()["data"]
                terminal_state = detail["run_state"]
                if terminal_state == "idle":
                    break
                if terminal_state == "waiting_approval":
                    page = (
                        await http.get(
                            "/api/v1/approvals",
                            params={"status": "pending", "session_id": session_id},
                        )
                    ).json()["data"]
                    for item in page["items"]:
                        approvals_seen.append(
                            {"tool": item["tool"], "risk_level": item["risk_level"]}
                        )
                        decision = await http.post(
                            f"/api/v1/approvals/{item['id']}/decide",
                            json={"decision": "approve", "scope": "once"},
                        )
                        if decision.status_code not in (200, 409):
                            evidence["notes"].append(
                                f"审批决策异常 {decision.status_code}: {decision.text[:200]}"
                            )
                await asyncio.sleep(0.5)
            evidence["final_run_state"] = terminal_state
            evidence["approvals_approved_live"] = approvals_seen

            if terminal_state != "idle":
                evidence["failure_class"] = "product"
                evidence["notes"].append("run 未在 360s 内收敛到 idle")
                if run_id:
                    await http.post(f"/api/v1/runs/{run_id}/stop")

            # run 行 idle 只代表**运行**收口；diff 是收口后的后台产物（git 命令要时间）。
            # 等 diff_reviews 出现（最长 90s），否则会把「竞态」误判成「无 diff」。
            diff_started = time.monotonic()
            diff_deadline = diff_started + 90
            diff_count = 0
            while time.monotonic() < diff_deadline:
                rows = _query(
                    "SELECT count(*) AS n FROM diff_reviews WHERE session_id = %s",
                    (session_id,),
                )
                diff_count = int(rows[0]["n"]) if rows else 0
                if diff_count:
                    break
                await asyncio.sleep(0.5)
            evidence["diff_wait_seconds"] = round(time.monotonic() - diff_started, 1)
            if not diff_count:
                evidence["notes"].append("run 收口后 90s 内未产生 diff_reviews（diff 链未完成）")

            # 接受审查（AC-007-4 真机面）：推进基线 + 沙箱内 tag `lcode-baseline/round-N`。
            if diff_count:
                pending_review = _query(
                    "SELECT id::text AS id, round FROM diff_reviews "
                    "WHERE session_id = %s AND verdict = 'pending' ORDER BY round DESC LIMIT 1",
                    (session_id,),
                )
                if pending_review:
                    verdict = await http.post(
                        f"/api/v1/diff-reviews/{pending_review[0]['id']}/verdict",
                        json={"verdict": "accepted"},
                    )
                    evidence["verdict_response"] = {
                        "status_code": verdict.status_code,
                        "verdict": (verdict.json().get("data") or {}).get("verdict")
                        if verdict.status_code == 200
                        else None,
                    }
                    # 裁决回声是异步 emit；短暂等待后 tags/baselines 才可见（commit+tag 同步完成）
                    await asyncio.sleep(0.5)

            # ------------------------------------------------------------ 事实收集
            runs = _query(
                "SELECT id::text AS id, status, error_code, error_message, "
                "started_at, finished_at FROM runs WHERE session_id = %s "
                "ORDER BY started_at DESC LIMIT 1",
                (session_id,),
            )
            evidence["run"] = runs[0] if runs else None

            rows = _query(
                "SELECT tool, tool_call_id, status, decided_by, risk_level, args "
                "FROM approvals WHERE session_id = %s ORDER BY created_at",
                (session_id,),
            )
            evidence["approvals"] = [
                {
                    "tool": row["tool"],
                    "tool_call_id": row["tool_call_id"],
                    "status": row["status"],
                    "decided_by": row["decided_by"],
                    "risk_level": row["risk_level"],
                    "args": row["args"],
                }
                for row in rows
            ]

            reviews = _query(
                "SELECT id::text AS id, round, verdict, baseline_kind, baseline_ref, "
                "baseline_commit, additions_total, deletions_total, files "
                "FROM diff_reviews WHERE session_id = %s ORDER BY round",
                (session_id,),
            )
            evidence["diff_reviews"] = [
                {
                    "id": row["id"],
                    "round": row["round"],
                    "verdict": row["verdict"],
                    "baseline": {
                        "kind": row["baseline_kind"],
                        "ref": row["baseline_ref"],
                        "commit": row["baseline_commit"],
                    },
                    "additions_total": row["additions_total"],
                    "deletions_total": row["deletions_total"],
                    "files": [
                        {
                            "path": entry.get("path"),
                            "change": entry.get("change"),
                            "additions": entry.get("additions"),
                            "deletions": entry.get("deletions"),
                        }
                        for entry in (row["files"] or [])
                    ],
                }
                for row in reviews
            ]

            evidence["git_baselines"] = _query(
                "SELECT round, kind, ref, commit FROM git_baselines "
                "WHERE session_id = %s ORDER BY round",
                (session_id,),
            )

            event_counts = _query(
                "SELECT type, count(*) AS count FROM events WHERE session_id = %s "
                "GROUP BY type ORDER BY type",
                (session_id,),
            )
            evidence["events"] = {row["type"]: int(row["count"]) for row in event_counts}

            messages = _query(
                "SELECT role, content FROM messages WHERE session_id = %s "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            )
            evidence["final_message_excerpt"] = (
                (messages[0]["content"] or "")[:500] if messages else None
            )

            # 沙箱内 git 事实（真适配器执行；路径固定为取证仓库）
            try:
                tags = client.execute(f"git -C {WORKSPACE} tag --list", timeout=60)
                log = client.execute(f"git -C {WORKSPACE} log -1 --pretty=%s", timeout=60)
                calc = client.execute(f"cat {WORKSPACE}/calc.py", timeout=60)
                evidence["sandbox_git"] = {
                    "tags": tags.output.strip().splitlines(),
                    "last_commit_subject": log.output.strip(),
                    "calc_py": calc.output[:400],
                }
            except Exception as exc:  # noqa: BLE001 —— 取证失败如实记录
                evidence["sandbox_git"] = {"error": f"{type(exc).__name__}: {exc}"}

            tool_events = _query(
                "SELECT type, data FROM events WHERE session_id = %s "
                "AND type IN ('tool_call_started', 'tool_call_finished') ORDER BY seq",
                (session_id,),
            )
            evidence["tool_events"] = [
                {
                    "type": row["type"],
                    "tool": row["data"].get("tool"),
                    "tool_call_id": row["data"].get("tool_call_id"),
                    "status": row["data"].get("status"),
                    "args": row["data"].get("args"),
                    "result_preview": (row["data"].get("result_preview") or "")[:300],
                }
                for row in tool_events
            ]

            # diff 详情（REST 面，验证面板数据可读）
            if reviews:
                latest = reviews[-1]
                detail = await http.get(f"/api/v1/diff-reviews/{latest['id']}")
                if detail.status_code == 200:
                    payload = detail.json()["data"]
                    evidence["diff_detail"] = {
                        "round": payload["round"],
                        "verdict": payload["verdict"],
                        "files": [
                            {
                                "path": item["path"],
                                "change": item["change"],
                                "additions": item["additions"],
                                "deletions": item["deletions"],
                            }
                            for item in payload["files"]
                        ],
                        "additions_total": payload["additions_total"],
                        "deletions_total": payload["deletions_total"],
                    }

            # 诊断面（**只读**，不改索引）：diff 为空时记录工作区解析与 git 状态
            try:
                from app.core.config import sandbox_settings
                from app.sandbox.git import GitWorkspace

                layout = GitWorkspace(client).layout
                evidence["diff_diagnostic"] = {
                    "workspace_dir_setting": sandbox_settings.workspace_dir,
                    "layout": {
                        "root": layout.root,
                        "is_repo": layout.is_repo,
                        "head": layout.head,
                    },
                    "status_porcelain": client.execute(
                        f"git -C {WORKSPACE} status --porcelain", timeout=60
                    ).output,
                    "diff_stat": client.execute(
                        f"git -C {WORKSPACE} diff HEAD --stat", timeout=60
                    ).output,
                }
            except Exception as exc:  # noqa: BLE001
                evidence["diff_diagnostic"] = {"error": f"{type(exc).__name__}: {exc}"}

            # ------------------------------------------------------------ 判定
            run_row = evidence.get("run") or {}
            has_diff = bool(evidence["diff_reviews"])
            modified = any(
                entry["path"] == "calc.py" and entry["change"] == "modified"
                for review in evidence["diff_reviews"]
                for entry in review["files"]
            )
            if run_row.get("status") == "completed" and has_diff and modified:
                evidence["status"] = "ok"
            else:
                evidence["status"] = "failed"
                if evidence["failure_class"] is None:
                    if run_row.get("status") == "error":
                        evidence["failure_class"] = _classify(run_row.get("error_code"))
                    elif not has_diff:
                        evidence["failure_class"] = "model"
                        evidence["notes"].append("run 完成但没有 diff（模型未修改工作区）")
                    else:
                        evidence["failure_class"] = "product"
                        evidence["notes"].append("diff 存在但 calc.py 不是 modified")

            # 清理：删会话（kill 沙箱）；证据先落盘再删
            try:
                await http.delete(f"/api/v1/sessions/{session_id}")
            except Exception as exc:  # noqa: BLE001
                evidence["notes"].append(f"删除会话失败：{type(exc).__name__}: {exc}")
                session_id = session_id  # noqa: PLW0127 —— 保留 id 供人工清理
    return evidence


def _classify(error_code: str | None) -> str:
    """run 错误码 → 失败归类（粗粒度，人工复核用）。"""
    code = (error_code or "").lower()
    if "sandbox" in code:
        return "sandbox"
    if "llm" in code or "model" in code:
        return "model"
    if "network" in code or "timeout" in code:
        return "network"
    return "product"


def main() -> int:
    ready, problems = _readiness()
    if not ready:
        print("[skip] 真机用例显式跳过（requires_llm + requires_sandbox）：")
        for problem in problems:
            print(f"  - {problem}")
        print("（缺环境不算失败；配置齐备后重跑本脚本即可。）")
        return 0

    sys.path.insert(0, str(SERVER_DIR))
    os.environ["DB_NAME"] = PROBE_DB
    os.environ["AGENT_CHECKPOINTER"] = "postgres"
    # diff 工作区固定到取证仓库（沙箱 home 下还有镜像自带目录，不能让 GitWorkspace
    # 在「多个一级子目录且根不是 git」时拒绝解析——D7 的明确报错路径）。
    os.environ["SANDBOX_WORKSPACE_DIR"] = WORKSPACE
    print(f"[setup] 重建取证库 {PROBE_DB} 并迁移…", flush=True)
    _prepare_database()

    print("[run] 真模型 + 真百炼沙箱：场景 A 最小主干开始（可能耗时数分钟）…", flush=True)
    started = time.monotonic()
    evidence = asyncio.run(_run_probe())
    evidence["duration_seconds"] = round(time.monotonic() - started, 1)

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE_DIR / f"real-demo-{stamp}.json"
    path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print(f"[evidence] {path}")
    print(
        "[summary] status={status} class={failure_class} run={run_status} "
        "approvals={approvals} reviews={reviews} tags={tags}".format(
            status=evidence["status"],
            failure_class=evidence["failure_class"],
            run_status=(evidence.get("run") or {}).get("status"),
            approvals=len(evidence.get("approvals") or []),
            reviews=len(evidence.get("diff_reviews") or []),
            tags=(evidence.get("sandbox_git") or {}).get("tags"),
        )
    )
    for note in evidence.get("notes") or []:
        print(f"[note] {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
