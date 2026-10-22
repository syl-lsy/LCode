import type { AgentRef } from '@contracts/common';

import type { ChatItem, ChatToolCardItem } from '../store';

/**
 * 子智能体卡片（REQ-016；A4 §8.1）。
 *
 * 定位：主流中 `tool === 'task'` 工具卡的**扩展区块**（由 MessageList 挂在 ToolCard
 * 下方，不侵入 ToolCard 通用布局——ToolCard 保持单一职责）。展示三面信息：
 *
 * 1. 委派关系：role（activity → 卡片 envelope → task 入参 subagent_type 逐级回退）
 *    + agent_id 简写；
 * 2. 子智能体活动：按 `AgentRef.spawned_by_tool_call_id === task 卡 id` 关联该子智能体
 *    产生的主流条目（消息/工具调用计数、运行态、最近工具）——纯函数
 *    `collectSubagentActivity` 供 MessageList 每次渲染统一计算（O(n) 一次）；
 * 3. 结果摘要：task 出参摘要（既有 result_preview 字段，不自造）。
 *
 * 降级：事件缺失/关联不上（activity=null）时仍渲染卡片——计数如实为 0、角色回退到
 * 入参，不报错不空白（AC-016 优雅降级）。
 */

/** 子智能体活动摘要（从流内条目聚合；null = 没有任何关联事件）。 */
export interface SubagentActivity {
  readonly agentId: string | null;
  readonly role: string | null;
  readonly messageCount: number;
  readonly toolCount: number;
  readonly running: boolean;
  readonly lastTool: string | null;
}

/** 条目归属 envelope（仅消息/工具卡/子智能体条目携带 agent，其余联合成员无此字段）。 */
export function agentOf(item: ChatItem): AgentRef | null {
  return item.kind === 'message' || item.kind === 'toolcard' || item.kind === 'subagent'
    ? item.agent
    : null;
}

/** 按 spawned_by_tool_call_id 收集某 task 派生的子智能体条目；无匹配返回 null。 */
export function collectSubagentActivity(
  items: readonly ChatItem[],
  toolCallId: string,
): SubagentActivity | null {
  let agentId: string | null = null;
  let role: string | null = null;
  let messageCount = 0;
  let toolCount = 0;
  let running = false;
  let lastTool: string | null = null;

  for (const item of items) {
    const agent = agentOf(item);
    if (agent === null || agent.spawned_by_tool_call_id !== toolCallId) continue;
    agentId ??= agent.agent_id;
    role ??= agent.role ?? null;
    if (item.kind === 'message') {
      messageCount += 1;
    } else if (item.kind === 'toolcard') {
      toolCount += 1;
      lastTool = item.tool;
      if (item.status === 'running') running = true;
    }
  }

  if (messageCount === 0 && toolCount === 0) return null;
  return { agentId, role, messageCount, toolCount, running, lastTool };
}

/** task 入参里的角色声明（deepagents task 约定键；逐个探测，不假设固定形状）。 */
function roleFromArgs(args: Readonly<Record<string, unknown>>): string | null {
  for (const key of ['subagent_type', 'role', 'agent_role']) {
    const value = args[key];
    if (typeof value === 'string' && value !== '') return value;
  }
  return null;
}

/** agent_id 简写：超长截断（deepagents 派生线程 id 较长，卡片内只作辨识用）。 */
function shortIdOf(agentId: string): string {
  return agentId.length > 10 ? `${agentId.slice(0, 10)}…` : agentId;
}

export function SubagentCard({
  item,
  activity,
}: {
  item: ChatToolCardItem;
  activity: SubagentActivity | null;
}) {
  const role =
    activity?.role ?? item.agent?.role ?? roleFromArgs(item.args) ?? '子智能体';
  const agentId = activity?.agentId ?? item.agent?.agent_id ?? null;
  const running = activity?.running === true || item.status === 'running';
  const result = item.resultPreview ?? null;

  return (
    <div
      data-testid="subagent-card"
      data-running={running}
      className="mb-2 ml-6 rounded-md border border-border bg-surface px-3 py-1.5 text-[length:var(--text-ui-xs)]"
    >
      <div className="flex min-w-0 items-center gap-2">
        <span aria-hidden="true" className="flex-none text-foreground-subtlest">
          ⤷
        </span>
        <span className="flex-none text-foreground-subtle">子智能体</span>
        <span className="badge flex-none border-border-hover text-foreground">{role}</span>
        {agentId !== null && (
          <code className="flex-none font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
            {shortIdOf(agentId)}
          </code>
        )}
        <span className="ml-auto flex-none text-foreground-subtlest">
          {activity?.messageCount ?? 0} 条消息 · {activity?.toolCount ?? 0} 次工具
        </span>
        {running && <span className="spinner flex-none" aria-hidden="true" />}
      </div>
      {activity?.lastTool !== null && activity?.lastTool !== undefined && (
        <div className="mt-0.5 pl-6 text-foreground-subtlest">最近工具 {activity.lastTool}</div>
      )}
      {result !== null && result !== '' && (
        <div
          data-testid="subagent-result"
          title={result}
          className="mt-0.5 truncate pl-6 text-foreground-subtle"
        >
          结果 {result}
        </div>
      )}
    </div>
  );
}
