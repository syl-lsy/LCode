"""runtime 中断解析单测（m3-plan Phase 1 C / m3-spikes §1.3）。

锁定三件事：
- `parse_interrupts`：updates 通道 `__interrupt__` → `(namespace, Interrupt)`；其余忽略；
- 去重与根层优先：同一 interrupt 在根图 `()` 与子图各出现一次，只保留根层（审批唯一入口）；
- resume 输入形态与快照完整性：`resume_value` 非空 → `Command(resume=...)`；
  `session_cfg` 快照带 approval_mode / sandbox_provider（策略在 checkpoint 里可复现）。
"""

from typing import Any

from langgraph.types import Command, Interrupt

from app.agent.runtime import RunExecutor, parse_interrupts


class _Sink:
    """EventSink 替身：不消费（本文件只测解析/状态，不跑真图）。"""

    async def on_raw_event(self, raw: Any) -> None:  # pragma: no cover —— 未被调用
        return None


def _executor(**kwargs: Any) -> RunExecutor:
    return RunExecutor(session_id="sess-1", run_id="run-1", sink=_Sink(), **kwargs)


def _updates(payload: Any, namespace: tuple[str, ...] = ()) -> tuple[Any, ...]:
    if namespace:
        return (namespace, "updates", payload)
    return ("updates", payload)


def test_parse_interrupts_reads_updates_payload() -> None:
    interrupt = Interrupt({"action_requests": []}, id="int-1")
    parsed = parse_interrupts(_updates({"__interrupt__": (interrupt,)}))

    assert parsed == [((), interrupt)]


def test_parse_interrupts_carries_namespace_for_subgraphs() -> None:
    interrupt = Interrupt({"action_requests": []}, id="int-1")
    parsed = parse_interrupts(_updates({"__interrupt__": (interrupt,)}, ("tools:task-1",)))

    assert parsed == [(("tools:task-1",), interrupt)]


def test_parse_interrupts_ignores_other_modes_and_shapes() -> None:
    assert parse_interrupts(("messages", (object(), {}))) == []
    assert parse_interrupts(_updates({"model": {"messages": []}})) == []
    assert parse_interrupts(_updates({"__interrupt__": "not-a-tuple"})) == []
    assert parse_interrupts({"unexpected": True}) == []


def test_executor_keeps_root_interrupt_and_drops_subgraph_duplicate() -> None:
    """同一 interrupt 两层各出现一次：只保留根层（按 id 去重，先到先得后根层覆盖）。"""
    executor = _executor()
    interrupt = Interrupt({"action_requests": []}, id="int-1")

    executor._observe(_updates({"__interrupt__": (interrupt,)}, ("tools:task-1",)), _usage())
    assert executor.interrupts == ()  # 子图条目不作为映射入口

    executor._observe(_updates({"__interrupt__": (interrupt,)}), _usage())
    entries = executor.interrupts
    assert len(entries) == 1
    assert entries[0][0] == ()
    assert executor.interrupt_pending is True


def test_executor_dedupes_repeated_root_interrupt_by_id() -> None:
    """同一 interrupt id 在根层重复到达（多节点回声）只记一条。"""
    executor = _executor()
    interrupt = Interrupt({"action_requests": []}, id="int-1")
    executor._observe(_updates({"__interrupt__": (interrupt,)}), _usage())
    executor._observe(_updates({"__interrupt__": (interrupt,)}), _usage())
    assert len(executor.interrupts) == 1


def test_input_state_uses_command_on_resume() -> None:
    """resume 段输入 = `Command(resume=...)`（同 thread 顶层 resume，不重放 entry）。"""
    executor = _executor()
    state = executor._input_state([], resume_value={"decisions": [{"type": "approve"}]})
    assert isinstance(state, Command)
    assert state.resume == {"decisions": [{"type": "approve"}]}


def test_snapshot_carries_approval_mode_and_provider() -> None:
    """会话快照（checkpoint state）带 approval_mode / sandbox_provider（resume 段复现策略）。"""
    executor = _executor(approval_mode="auto", sandbox_provider="bailian")
    executor._session_cfg = {"model": "m", "reasoning_effort": "high"}

    snapshot = executor._snapshot()

    assert snapshot == {
        "model": "m",
        "reasoning_effort": "high",
        "approval_mode": "auto",
        "sandbox_provider": "bailian",
    }


def test_waiting_segment_maps_interrupt_to_waiting_status() -> None:
    """段末 interrupt_pending 为真 → waiting_approval（不当 finished，m3-plan D4）。"""
    executor = _executor()
    executor.interrupt_pending = True
    outcome = executor._outcome("waiting_approval", _usage(), None, 0.0)
    assert outcome.status == "waiting_approval"
    assert outcome.error is None


def _usage() -> Any:
    from lcode_contracts.common import Usage

    return Usage()
