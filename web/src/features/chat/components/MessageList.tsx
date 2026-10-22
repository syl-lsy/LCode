import { Fragment, useMemo, useState } from 'react';

import { useChatStore, type ChatItem, type ChatToolCardItem } from '../store';
import { useTodoStore } from '@/features/todo/store';
import { TodoPanel } from '@/features/todo/components/TodoPanel';
import { useWorkbenchStore } from '@/features/workbench/store';
import { formatTokens } from '@/lib/formatTokens';
// 容器例外（A4 §2.2 唯一）：chat 容器可 import 各领域卡片组件
import { ApprovalCard } from '@/features/approval/components/ApprovalCard';

import { useAutoFollow, useStickToBottom } from '../hooks/useStickToBottom';

import { EmptyState } from '@/components/EmptyState';

import { MessageBubble } from './MessageBubble';
import {
  SubagentCard,
  agentOf,
  collectSubagentActivity,
  type SubagentActivity,
} from './SubagentCard';
import { TimerRow } from './TimerRow';
import { ToolCard } from './ToolCard';

/**
 * 消息流容器（A4 §8.1 MessageList 行 / §8.2 items 渲染模型）。
 *
 * - items 顺序表按 kind 分发：message → MessageBubble；toolcard → 连续段收敛进
 *   TimerRow 总折叠区（F3：收起时思考卡+工具卡藏入该行，行文案不变），task 卡
 *   下方挂 SubagentCard 扩展区块（REQ-016）；approval → ApprovalCard（M3：四决策
 *   卡片 + 终态折叠；载荷按 id 取自 approvalStore）；diffcard/compact_note → 就地行
 *   渲染（compact 数值 k/M 人读格式，AC-010-1）；
 * - TodoPanel 悬浮于流区域右上（对齐 zcode-ui 12-todo-panel.png，非独立右栏 tab），
 *   空快照由组件自身零渲染（AC-005-3）；
 * - 贴底四件套（§6.4）：唯一滚动入口 follow()；脱离显示「回到底部 ↓N」FAB；
 * - diffcard 点击 → workbenchStore.openTab（UI 编排 action，A4 §2.2 唯一例外）。
 */

interface ToolGroup {
  readonly kind: 'tools';
  readonly key: string;
  readonly cards: readonly ChatToolCardItem[];
  readonly startedAt: string;
}

interface SingleItem {
  readonly kind: 'single';
  readonly key: string;
  readonly item: ChatItem;
}

type RenderGroup = ToolGroup | SingleItem;

/** 连续 toolcard 段收敛（F3 总折叠的分组单位）；其余条目单发。 */
export function groupItems(items: readonly ChatItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  let currentCards: ChatToolCardItem[] = [];
  const flush = (): void => {
    if (currentCards.length === 0) return;
    groups.push({
      kind: 'tools',
      key: `tools:${currentCards[0]?.id ?? ''}`,
      cards: currentCards,
      startedAt: currentCards[0]?.createdAt ?? '',
    });
    currentCards = [];
  };
  for (const item of items) {
    if (item.kind === 'toolcard') {
      currentCards.push(item);
      continue;
    }
    flush();
    groups.push({ kind: 'single', key: item.id, item });
  }
  flush();
  return groups;
}

function DiffCardRow({ item }: { item: Extract<ChatItem, { kind: 'diffcard' }> }) {
  const openTab = useWorkbenchStore((s) => s.openTab);
  return (
    <button
      type="button"
      data-testid="diff-card"
      data-verdict={item.verdict}
      className="mb-2.5 flex w-full items-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-left text-[length:var(--text-ui-xs)] hover:border-border-hover"
      onClick={() =>
        openTab({ kind: 'review', id: item.id, title: `审查 · 第 ${item.round} 轮` })
      }
    >
      <span aria-hidden="true">🗂</span>
      <span className="flex-none text-foreground">改动文件（第 {item.round} 轮）</span>
      <span className="badge flex-none border-diff-added/40 text-diff-added">+{item.additionsTotal}</span>
      <span className="badge flex-none border-diff-removed/40 text-diff-removed">-{item.deletionsTotal}</span>
      <span className="ml-auto flex-none text-foreground-subtlest">
        {item.fileCount} 个文件 · {item.verdict === 'pending' ? '待裁决' : item.verdict === 'accepted' ? '已接受' : '已驳回'} · 点击打开审查标签
      </span>
    </button>
  );
}

function CompactNoteRow({ item }: { item: Extract<ChatItem, { kind: 'compact_note' }> }) {
  return (
    <div
      data-testid="compact-note"
      data-phase={item.phase}
      className="mb-2.5 flex items-center gap-2 rounded-md border border-border bg-card px-3 py-1.5 text-[length:var(--text-ui-xs)] text-foreground"
    >
      {item.phase === 'started' ? (
        <span className="spinner flex-none" aria-hidden="true" />
      ) : (
        <span aria-hidden="true" className="flex-none text-success">
          ✓
        </span>
      )}
      {item.phase === 'started' ? '正在压缩上下文…' : '上下文已压缩'}
      {item.phase === 'finished' && item.beforeTokens !== null && item.afterTokens !== null && (
        <span className="ml-auto text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
          约 {formatTokens(item.beforeTokens)} → {formatTokens(item.afterTokens)} tokens
        </span>
      )}
    </div>
  );
}

function ToolGroupBlock({
  group,
  activities,
}: {
  group: ToolGroup;
  activities: ReadonlyMap<string, SubagentActivity>;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const running = group.cards.some((card) => card.status === 'running');
  if (collapsed) {
    return (
      <div data-testid="tool-group-collapsed">
        <TimerRow running={running} startedAt={group.startedAt} collapsed onToggle={() => setCollapsed(false)} />
      </div>
    );
  }
  return (
    <div data-testid="tool-group">
      <TimerRow
        running={running}
        startedAt={group.startedAt}
        collapsed={false}
        onToggle={() => setCollapsed(true)}
      />
      {group.cards.map((card) => (
        <Fragment key={card.id}>
          <ToolCard item={card} />
          {/* task 卡的子智能体扩展区块（REQ-016）：挂在卡下，不改 ToolCard 布局 */}
          {card.tool === 'task' && (
            <SubagentCard item={card} activity={activities.get(card.id) ?? null} />
          )}
        </Fragment>
      ))}
    </div>
  );
}

export interface MessageListProps {
  sessionId: string;
}

export function MessageList({ sessionId }: MessageListProps) {
  const items = useChatStore((s) => s.items);
  const byId = useChatStore((s) => s.byId);
  const generation = useChatStore((s) => s.generation);

  const stick = useStickToBottom();
  const changeKey = `${sessionId}:${items.length}:${generation}`;
  useAutoFollow(stick, changeKey);

  const resolved = useMemo(() => {
    const list: ChatItem[] = [];
    for (const id of items) {
      const item = byId.get(id);
      if (item !== undefined) list.push(item);
    }
    return list;
  }, [items, byId]);

  const groups = useMemo(() => groupItems(resolved), [resolved]);

  // 子智能体活动索引（REQ-016）：对整条流一次 O(n) 聚合，task 卡按 id 取用——
  // 避免每张 task 卡各自遍历（子智能体事件量大时的渲染成本控制）。
  const subagentActivities = useMemo(() => {
    const map = new Map<string, SubagentActivity>();
    for (const item of resolved) {
      const agent = agentOf(item);
      const spawnedBy = agent?.spawned_by_tool_call_id;
      if (spawnedBy === null || spawnedBy === undefined) continue;
      const current = map.get(spawnedBy);
      const next = collectSubagentActivity([item], spawnedBy);
      if (next === null) continue;
      if (current === undefined) {
        map.set(spawnedBy, next);
        continue;
      }
      map.set(spawnedBy, {
        agentId: current.agentId ?? next.agentId,
        role: current.role ?? next.role,
        messageCount: current.messageCount + next.messageCount,
        toolCount: current.toolCount + next.toolCount,
        running: current.running || next.running,
        lastTool: next.lastTool ?? current.lastTool,
      });
    }
    return map;
  }, [resolved]);

  const hasTodos = useTodoStore((s) => s.snapshot.length > 0);

  return (
    <div className="relative min-h-0 flex-1">
      {hasTodos && (
        <div className="absolute right-5 top-3 z-10">
          <TodoPanel />
        </div>
      )}
      <div
        ref={stick.containerRef}
        onScroll={stick.onScroll}
        data-testid="message-scroll"
        className="scroll-thin h-full overflow-y-auto"
      >
        {resolved.length === 0 ? (
          <div className="flex h-full items-center justify-center">
            <EmptyState
              title="还没有消息"
              description="在下方输入框发送第一条消息；Agent 的回复与工具执行过程会实时出现在这里。"
            />
          </div>
        ) : (
          <div className="mx-auto max-w-[760px] px-5 pt-4 pb-2">
            {groups.map((group) =>
              group.kind === 'tools' ? (
                <ToolGroupBlock key={group.key} group={group} activities={subagentActivities} />
              ) : (
                <Fragment key={group.key}>
                  {group.item.kind === 'message' && <MessageBubble item={group.item} />}
                  {group.item.kind === 'approval' && (
                    <ApprovalCard
                      approvalId={group.item.id}
                      tool={group.item.tool}
                      fallbackStatus={group.item.status}
                    />
                  )}
                  {group.item.kind === 'diffcard' && <DiffCardRow item={group.item} />}
                  {group.item.kind === 'compact_note' && <CompactNoteRow item={group.item} />}
                  {group.item.kind === 'subagent' && (
                    <div data-testid="subagent-row" className="mb-2.5 text-[length:var(--text-ui-xs)] text-foreground-subtle">
                      子智能体 · <span className="font-mono">{group.item.agent.role ?? group.item.agent.agent_id}</span>
                    </div>
                  )}
                </Fragment>
              ),
            )}
          </div>
        )}
      </div>

      {!stick.atBottom && (
        <button
          type="button"
          data-testid="to-bottom-fab"
          className="absolute right-6 bottom-3 z-10 flex h-8 w-8 items-center justify-center rounded-full border border-border-hover bg-popover text-foreground-subtle shadow-md hover:text-foreground-strong"
          onClick={stick.scrollToBottom}
          aria-label={`回到底部（${stick.unreadCount} 条新内容）`}
        >
          ↓{stick.unreadCount > 0 ? stick.unreadCount : ''}
        </button>
      )}
    </div>
  );
}
