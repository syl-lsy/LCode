// @vitest-environment jsdom
/**
 * SubagentCard 单测（REQ-016）：
 * collectSubagentActivity 按 AgentRef.spawned_by_tool_call_id 关联子智能体事件
 * （计数/运行态/最近工具），卡片展示委派关系与结果摘要，无关联时优雅降级。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AgentRef } from '@contracts/common';

import type { ChatItem, ChatToolCardItem } from '../store';
import '../../../tests/test-utils';

import { collectSubagentActivity, SubagentCard } from './SubagentCard';

const TASK_ID = 'call_task_1';
const SPAWN: AgentRef = {
  agent_id: 'agent-0192f0a1-abcd',
  role: 'coder',
  spawned_by_tool_call_id: TASK_ID,
};
const OTHER_SPAWN: AgentRef = { agent_id: 'agent-other', role: 'explorer', spawned_by_tool_call_id: 'call_other' };

function toolCard(id: string, tool: string, agent: AgentRef | null, status: 'running' | 'success' = 'success'): ChatToolCardItem {
  return { kind: 'toolcard', id, tool, args: {}, status, riskLevel: null, agent, createdAt: 't' };
}

function message(id: string, agent: AgentRef | null): ChatItem {
  return { kind: 'message', id, role: 'assistant', createdAt: 't', runId: null, agent };
}

describe('collectSubagentActivity（spawned_by_tool_call_id 关联）', () => {
  it('按 tool_call_id 归属计数：主流程与其它子智能体的事件不算进来', () => {
    const items: ChatItem[] = [
      message('m0', null), // 主智能体回复
      toolCard('t0', 'execute', null), // 主流程工具
      message('m1', SPAWN), // 子智能体回复 1
      toolCard('t1', 'write_file', SPAWN),
      message('m2', OTHER_SPAWN), // 其它 task 的子智能体
    ];

    const activity = collectSubagentActivity(items, TASK_ID);

    expect(activity).not.toBeNull();
    expect(activity?.messageCount).toBe(1);
    expect(activity?.toolCount).toBe(1);
    expect(activity?.agentId).toBe(SPAWN.agent_id);
    expect(activity?.role).toBe('coder');
    expect(activity?.running).toBe(false);
  });

  it('无匹配事件 → null（优雅降级判据）', () => {
    expect(collectSubagentActivity([message('m0', null)], TASK_ID)).toBeNull();
  });

  it('running=任一子智能体工具卡 running；lastTool=最后一个工具卡的工具名', () => {
    const items: ChatItem[] = [
      message('m1', SPAWN),
      toolCard('t1', 'read_file', SPAWN),
      toolCard('t2', 'execute', SPAWN, 'running'),
    ];

    const activity = collectSubagentActivity(items, TASK_ID);

    expect(activity?.running).toBe(true);
    expect(activity?.lastTool).toBe('execute');
  });
});

describe('SubagentCard 渲染', () => {
  const taskCard: ChatToolCardItem = {
    kind: 'toolcard',
    id: TASK_ID,
    tool: 'task',
    args: { description: '修超时根因' },
    status: 'success',
    riskLevel: null,
    agent: null,
    createdAt: 't',
  };

  it('有关联事件：role 徽标 + agent_id 简写 + 计数', () => {
    const activity = collectSubagentActivity(
      [message('m1', SPAWN), toolCard('t1', 'write_file', SPAWN)],
      TASK_ID,
    );
    render(<SubagentCard item={taskCard} activity={activity} />);

    expect(screen.getByTestId('subagent-card')).toHaveTextContent('coder');
    expect(screen.getByTestId('subagent-card')).toHaveTextContent('agent-0192…');
    expect(screen.getByTestId('subagent-card')).toHaveTextContent('1 条消息');
    expect(screen.getByTestId('subagent-card')).toHaveTextContent('1 次工具');
  });

  it('运行中显示进行中标记；结束后不显示', () => {
    const running = collectSubagentActivity([toolCard('t1', 'execute', SPAWN, 'running')], TASK_ID);
    const view = render(<SubagentCard item={taskCard} activity={running} />);
    expect(screen.getByTestId('subagent-card')).toHaveAttribute('data-running', 'true');

    view.rerender(
      <SubagentCard
        item={taskCard}
        activity={collectSubagentActivity([toolCard('t1', 'execute', SPAWN)], TASK_ID)}
      />,
    );
    expect(screen.getByTestId('subagent-card')).toHaveAttribute('data-running', 'false');
  });

  it('无关联事件：降级只显示 task 本身信息（args 角色回退 + 出参摘要），不报错不空白', () => {
    render(
      <SubagentCard
        item={{ ...taskCard, agent: null, resultPreview: '已修复 timeout 配置' }}
        activity={null}
      />,
    );

    const card = screen.getByTestId('subagent-card');
    expect(card).toBeInTheDocument();
    expect(card).toHaveTextContent('已修复 timeout 配置');
    expect(card).toHaveTextContent('0 条消息'); // 计数为 0 也是事实陈述
    expect(screen.queryByText(/agent-/)).not.toBeInTheDocument();
  });

  it('非 task 出参为空且无事件：仍渲染委派行（role 取 args 回退）', () => {
    render(
      <SubagentCard
        item={{ ...taskCard, args: { subagent_type: 'reviewer' }, resultPreview: null }}
        activity={null}
      />,
    );
    expect(screen.getByTestId('subagent-card')).toHaveTextContent('reviewer');
  });
});
