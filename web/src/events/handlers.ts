/**
 * 事件 → store 写入的映射实现（A4 §5.3 映射表逐行，dispatcher 的唯一落地端）。
 *
 * 纪律：
 * - 每个 handler 只碰自己领域的 store（写者唯一，§3.2「store 边界 = 写者边界」）；
 * - delta 类（message_delta）**不直接写 store 而是入缓冲**（§3.3 高频路径），由
 *   scheduler 每帧 drain 一次落 store；
 * - 事件类直达 store；run_status 按 §5.3 与 §4.7：started→running，
 *   finished/stopped/error 同为 run 终态→收敛 idle + 全通道终态冲刷（规则 10）；
 * - B 档容忍性（test-plan §3.4）：跨字段语义不变量（totals 求和、channel×phase、
 *   stdout⇒exec_id 等）前端不兜底——此处只保证不崩、不写坏 store；缺归属的终端
 *   输出落到 unattributed 桶而非静默丢弃。
 *
 * 类型来源：EventByType / HandlerMap 定义在 dispatcher.ts（从生成物 zod 推导），
 * 本文件只 `import type` ——类型导入不产生运行时依赖，故 handlers ↔ dispatcher
 * 不构成运行时循环（dispatcher 运行时导入 HANDLERS）。
 */

import { useApprovalStore } from '@/features/approval/store';
import { partKeyOf, useChatStore } from '@/features/chat/store';
import { useCostStore } from '@/features/costs/store';
import { useSessionStore } from '@/features/sessions/store';
import { useTodoStore } from '@/features/todo/store';
import { useDiffStore, useTerminalStore, useWorkbenchStore } from '@/features/workbench/store';

import { deltaBuffers, flushAllChannels, flushChannel, terminalBuffer } from './buffers';
import type { EventByType, HandlerMap } from './dispatcher';

/** 无 exec 归属的终端输出桶（B 档容忍路径：契约违规时不丢内容，如实标注来源缺失）。 */
export const UNATTRIBUTED_EXEC_ID = 'unattributed';

/** decision → approval status（与 contracts rest.py 的 ApprovalItem.status 枚举对应）。 */
const DECISION_STATUS: Record<
  EventByType['approval_resolved']['decision'],
  'approved' | 'edited' | 'rejected' | 'responded'
> = {
  approve: 'approved',
  edit: 'edited',
  reject: 'rejected',
  respond: 'responded',
};

/** compact 状态行条目 id（started 建条目、finished 原地更新；同前缀可推导）。 */
function compactItemId(seq: number): string {
  return `compact:${seq}`;
}

let browserActionWarned = false;

export const HANDLERS: HandlerMap = {
  message_created: (event) => {
    useChatStore.getState().appendUserMessage({
      id: event.message_id,
      content: event.content,
      createdAt: event.created_at,
      runId: event.run_id ?? null,
      agent: event.agent ?? null,
    });
    // 落库即回声（多端一致 + 纯事件回放可重建）→ 会话活跃度 + 运行徽标
    const sessions = useSessionStore.getState();
    sessions.touchActivity(event.session_id, event.created_at);
    sessions.setRunState(event.session_id, 'running');
  },

  message_delta: (event) => {
    const kind = event.kind;
    const chat = useChatStore.getState();
    // assistant 消息条目由首个 delta 建立（message_created 只承载 user 回声）
    chat.ensureAssistantMessage({
      id: event.message_id,
      createdAt: event.created_at,
      runId: event.run_id ?? null,
      agent: event.agent ?? null,
    });
    if (event.delta !== '') deltaBuffers.push(partKeyOf(event.message_id, kind), kind, event.delta);
  },

  message_finished: (event) => {
    useChatStore.getState().finishMessage({
      id: event.message_id,
      finishReason: event.finish_reason,
      structured: event.structured ?? null,
    });
    // usage 累计入 costStore（AC-015-1 口径：思考 token 计入 output）
    useCostStore.getState().addUsage(event.usage ?? null);
    // 终态冲刷：已收内容不丢（规则 10，不再等帧）
    flushChannel(partKeyOf(event.message_id, 'text'));
    flushChannel(partKeyOf(event.message_id, 'thinking'));
  },

  tool_call_started: (event) => {
    useChatStore.getState().appendToolCard({
      id: event.tool_call_id,
      tool: event.tool,
      args: event.args,
      riskLevel: event.risk_level ?? null,
      agent: event.agent ?? null,
      createdAt: event.created_at,
    });
  },

  tool_call_finished: (event) => {
    useChatStore.getState().completeToolCall({
      id: event.tool_call_id,
      status: event.status,
      durationMs: event.duration_ms,
      error: event.error ?? null,
      exitCode: event.exit_code ?? null,
      resultPreview: event.result_preview ?? null,
      resultRef: event.result_ref ?? null,
      truncated: event.truncated ?? false,
    });
    const todos = event.todos;
    if (todos == null || todos.length === 0) {
      // null/空数组都不动快照（REQ-005：store 无「清空」语义，单步任务不产出 todos）
      return;
    }
    // 契约口径：todos 仅由 write_todos 携带；ToolCallFinishedEvent 无 tool 字段，
    // 借流内 started 卡核对工具名（卡缺失——如重放只截到 finished——按 todos 放行）。
    const started = useChatStore.getState().byId.get(event.tool_call_id);
    if (started?.kind === 'toolcard' && started.tool !== 'write_todos') return;
    // todo 面板数据源 = 最近一次 write_todos 全量快照（REQ-005；单步任务不产出）
    useTodoStore.getState().setSnapshot(todos, event.created_at);
  },

  approval_request: (event) => {
    useApprovalStore.getState().upsertPending({
      id: event.approval_id,
      sessionId: event.session_id,
      tool: event.tool,
      args: event.args,
      riskLevel: event.risk_level,
      reason: event.reason ?? null,
      createdAt: event.created_at,
      toolCallId: event.tool_call_id,
      runId: event.run_id ?? null,
    });
    useChatStore.getState().appendApprovalItem({
      id: event.approval_id,
      tool: event.tool,
      status: 'pending',
      createdAt: event.created_at,
    });
    useChatStore.getState().noteApprovalPending();
    useSessionStore.getState().setRunState(event.session_id, 'waiting_approval');
  },

  approval_resolved: (event) => {
    useApprovalStore.getState().applyResolved({
      approvalId: event.approval_id,
      decision: event.decision,
      scope: event.scope ?? null,
      editedArgs: event.edited_args ?? null,
      feedback: event.feedback ?? null,
      decidedBy: event.decided_by,
    });
    useChatStore
      .getState()
      .setApprovalItemStatus(event.approval_id, DECISION_STATUS[event.decision]);
    // 批次守门（D3 batched interrupt）：同 run 多条 pending 时，逐条 resolved 只
    // 更新该卡；pendingIds 清空才把 run 拉回 running（否则保持 waiting_approval）。
    const allSettled = useApprovalStore.getState().pendingIds.length === 0;
    useChatStore.getState().noteApprovalSettled(allSettled);
    useSessionStore
      .getState()
      .setRunState(event.session_id, allSettled ? 'running' : 'waiting_approval');
  },

  diff_ready: (event) => {
    useDiffStore.getState().upsertFromEvent({
      reviewId: event.review_id,
      sessionId: event.session_id,
      round: event.round,
      baseline: event.baseline,
      files: event.files,
      additionsTotal: event.additions_total,
      deletionsTotal: event.deletions_total,
      patchRef: event.patch_ref,
      createdAt: event.created_at,
    });
    useChatStore.getState().appendDiffCard({
      id: event.review_id,
      round: event.round,
      fileCount: event.files.length,
      additionsTotal: event.additions_total,
      deletionsTotal: event.deletions_total,
      createdAt: event.created_at,
    });
    // M3：自动打开/聚焦审查标签（useWorkbenchStore 是 UI 编排 store，action 允许
    // 跨 feature 调用，§2.2 唯一例外）。已存在同 id 标签时只同步标题——不重复开、
    // 不抢用户当前激活的其它标签、不强行展开收起的面板（openReviewTab 语义）。
    useWorkbenchStore
      .getState()
      .openReviewTab(event.review_id, `审查 · 第 ${event.round} 轮`);
  },

  diff_resolved: (event) => {
    useDiffStore.getState().applyVerdict({
      reviewId: event.review_id,
      verdict: event.verdict,
      feedback: event.feedback ?? null,
      decidedBy: event.decided_by,
    });
    useChatStore.getState().setDiffCardVerdict(event.review_id, event.verdict);
  },

  sandbox_output: (event) => {
    if (event.channel === 'lifecycle') {
      const phase = event.phase ?? null;
      if (phase === null) return; // B 档容忍：lifecycle 缺 phase（契约违规）→ 不写坏 store
      useTerminalStore.getState().setLifecycle({
        phase,
        at: event.created_at,
        provider: event.provider ?? null,
        reason: event.reason ?? null,
        sandboxId: event.sandbox_id,
      });
      // 沙箱计时基准（§5.3：lifecycle → costStore）
      useCostStore.getState().noteSandboxPhase(phase, event.created_at);
      return;
    }
    const data = event.data ?? '';
    if (data === '') return;
    terminalBuffer.append(event.exec_id ?? UNATTRIBUTED_EXEC_ID, data);
  },

  run_status: (event) => {
    const chat = useChatStore.getState();
    if (event.subject === 'run') {
      chat.applyRunStatus({
        subject: event.subject,
        phase: event.phase,
        runId: event.run_id ?? null,
        createdAt: event.created_at,
        error: event.error ?? null,
      });
      useCostStore.getState().addUsage(event.usage_totals ?? null);
      const sessions = useSessionStore.getState();
      if (event.phase === 'started') {
        sessions.setRunState(event.session_id, 'running');
      } else {
        // finished / stopped / error 同为 run 终态 → 收敛 idle + 全通道终态冲刷（规则 10）。
        // error 即终局：后端 runtime 发完 run_status(error) 不再补发终态（契约 phase=error
        // 必带 RunErrorObj，run 行落 error、REST run_state 投影 idle）——等下去只会永久卡死
        sessions.setRunState(event.session_id, 'idle');
        flushAllChannels();
      }
      return;
    }
    // subject === 'compact'（AC-010-1 两态 + 前后窗口占用）
    if (event.phase === 'started') {
      chat.beginCompact({ id: compactItemId(event.seq), at: event.created_at });
      return;
    }
    if (event.phase === 'finished') {
      const active = useChatStore.getState().compactItemId;
      const id = active ?? compactItemId(event.seq);
      // 重放只见终态（缺 started）时补建条目，避免 AC-010-1 的「已压缩」无载体
      if (active === null) chat.beginCompact({ id, at: event.created_at });
      chat.finishCompact({
        id,
        at: event.created_at,
        beforeTokens: event.compact?.before_tokens ?? null,
        afterTokens: event.compact?.after_tokens ?? null,
      });
    }
  },

  browser_action: () => {
    // P1 占位：P0 服务端不得产出（A4 §5.3）；到达即记一次告警，不写任何 store
    if (browserActionWarned) return;
    browserActionWarned = true;
    console.warn('[handlers] browser_action 属 P1 事件（P0 契约不应产出）：已忽略');
  },
};

/** 测试隔离：重置 browser_action 告警闸门。 */
export function resetHandlerWarnings(): void {
  browserActionWarned = false;
}
