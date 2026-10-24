import type { ApprovalStatus } from '../store';
import { useApprovalStore } from '../store';

import { DecisionForm, type ApprovalVariant } from './DecisionForm';
import { Icon } from '@/components/Icon';
import './approval-card.css';

/**
 * 审批卡片（A4 §8.1 ApprovalCard 行；approval-card.html 两变体 + F14 拒绝闭环）。
 *
 * - props 只收结构化字段（approvalId / tool / fallbackStatus），不跨 feature
 *   import chat 类型；完整载荷（args/riskLevel/reason/status）按 id 从
 *   approvalStore 读取——大载荷不进 chat item（§8.2 容器/卡片分工）；
 * - pending：`.need-p` 需要权限提示 + 完整卡（ap-head 工具 tag/等待确认/风险徽标 +
 *   reason + 变体主体 + DecisionForm）；
 *   - Bash 变体（execute 等命令类）：`.ap-cmd` = $ + args.command 原文；
 *   - 文件写入变体（write_file/edit_file）：`.ap-file` = 文件图标 + 文件名 +
 *     写入行数徽标 + 目录（目录取自目标路径事实）；
 *   - 两类特征都提取不到（如 grep 等其余工具）：保留 args JSON 原文块（approval-args）；
 *   store 载荷未加载（如刷新后 SSE 未回放）时退化为占位行，不出决策表单
 *   （缺载荷不可决策）；
 * - 终态：折叠为一行结果，auto_approved 文案「已自动通过」（不落到「已回复」）；
 *   **rejected 且带反馈**（F14 拒绝闭环，R3§E）：折叠为「终端」完成态工具行 +
 *   一行 agent 改道气泡（`.reroute`，通用改道文案「收到反馈：{feedback}」）；
 * - 根节点保留 data-testid="approval-row" + data-status（MessageList/replay
 *   既有定位契约）。
 */

const TERMINAL_LABELS: Readonly<Record<Exclude<ApprovalStatus, 'pending'>, string>> = {
  approved: '已允许',
  rejected: '已拒绝',
  edited: '已编辑后允许',
  responded: '已回复',
  auto_approved: '已自动通过',
};

/** 文件写入类工具 → 审批卡文件变体（approval-card.html 变体 B；与 ToolCard 写入族同口径）。 */
const WRITE_TOOLS: ReadonlySet<string> = new Set(['write_file', 'edit_file']);

/** Bash 变体命令探针：args.command 为字符串时取之。 */
function commandOf(args: Readonly<Record<string, unknown>>): string | null {
  const command = args['command'];
  return typeof command === 'string' ? command : null;
}

/** 文件变体目标路径探针（约定键；与 ToolCard 写入卡同口径）。 */
const FILE_PATH_KEYS = ['path', 'file_path', 'file'] as const;

function filePathOf(args: Readonly<Record<string, unknown>>): string | null {
  for (const key of FILE_PATH_KEYS) {
    const value = args[key];
    if (typeof value === 'string' && value !== '') return value;
  }
  return null;
}

/** 文件变体写入行数探针（写入内容行数；+N 徽标来自入参事实，F15 口径）。 */
const WRITE_CONTENT_KEYS = ['content', 'new_string', 'new_text', 'code'] as const;

function writeLinesOf(args: Readonly<Record<string, unknown>>): number | null {
  for (const key of WRITE_CONTENT_KEYS) {
    const value = args[key];
    if (typeof value === 'string') return value.split('\n').length;
  }
  return null;
}

/** 路径拆分：文件名 + 目录（含尾斜杠；无目录返回 null）。 */
function splitPath(path: string): { readonly name: string; readonly dir: string | null } {
  const idx = path.lastIndexOf('/');
  if (idx < 0) return { name: path, dir: null };
  return { name: path.slice(idx + 1), dir: path.slice(0, idx + 1) };
}

export interface ApprovalCardProps {
  readonly approvalId: string;
  /** 工具名（chat item 冗余的轻量字段；载荷缺失时仍可显示） */
  readonly tool: string;
  /** chat item 持有的状态快照；store 载荷存在时以 store 为准 */
  readonly fallbackStatus: ApprovalStatus;
}

export function ApprovalCard({ approvalId, tool, fallbackStatus }: ApprovalCardProps) {
  const entry = useApprovalStore((s) => s.byId.get(approvalId));
  const status: ApprovalStatus = entry?.status ?? fallbackStatus;
  const variant: ApprovalVariant = WRITE_TOOLS.has(entry?.tool ?? tool) ? 'file' : 'bash';

  if (status !== 'pending') {
    // F14 拒绝闭环：拒绝且带反馈 → 折叠为「终端」完成态工具行 + 改道气泡
    //（反馈载荷以事件/本地决策为准；REST 列表不投影 feedback，刷新后回退普通终态行）
    const feedback = status === 'rejected' ? (entry?.feedback ?? null) : null;
    if (feedback !== null && feedback !== '') {
      const command = entry !== undefined ? commandOf(entry.args) : null;
      return (
        <div data-testid="approval-row" data-status={status} data-feedback="true" className="mb-2.5">
          <div
            data-testid="approval-folded-tool"
            className="tool-card"
            title="已拒绝的命令（审批闭环折叠态）"
          >
            <span className="tic" aria-hidden="true">
              <Icon name="terminal" size={12} />
            </span>
            <span className="lb">终端</span>
            <code>{command ?? (entry?.tool ?? tool)}</code>
            <span className="chev" aria-hidden="true">
              <Icon name="chevron-down" size={11} />
            </span>
          </div>
          <div className="reroute" data-testid="approval-reroute">
            收到反馈：{feedback}
          </div>
        </div>
      );
    }
    return (
      <div
        data-testid="approval-row"
        data-status={status}
        className="mb-2.5 flex items-center gap-2 rounded-md border border-border bg-card px-3 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle"
      >
        <span aria-hidden="true" className="flex-none text-success">
          ✓
        </span>
        <span className="flex-none">权限审批 ·</span>
        <span className="flex-none font-mono text-foreground">{tool}</span>
        <span className="ml-auto flex-none">{TERMINAL_LABELS[status]}</span>
      </div>
    );
  }

  if (entry === undefined) {
    // 载荷未加载（store 缺条目）：占位行不出决策表单（AC-006-5 恢复由 App 拉
    // GET /approvals?status=pending 补齐，补上后原地升级为完整卡）
    return (
      <div
        data-testid="approval-row"
        data-status="pending"
        className="mb-2.5 flex items-center gap-2 rounded-md border border-accent-border bg-accent px-3 py-2 text-[length:var(--text-ui-xs)] text-accent-foreground"
      >
        <span className="flex-none">权限审批 ·</span>
        <span className="flex-none font-mono">{tool}</span>
        <span className="flex-none">等待确认</span>
        <span className="ml-auto flex-none text-foreground-subtlest">载入中…</span>
      </div>
    );
  }

  // pending 完整卡：变体主体探针（Bash 命令 / 文件目标路径 / 兜底 args JSON）
  const command = variant === 'bash' ? commandOf(entry.args) : null;
  const filePath = variant === 'file' ? filePathOf(entry.args) : null;
  const showCmd = command !== null;
  const showFile = filePath !== null;
  const tagLabel = showFile ? '写入' : showCmd ? 'Bash' : entry.tool;

  return (
    <div
      data-testid="approval-row"
      data-status="pending"
      data-variant={variant}
      className="mb-2.5"
    >
      {/* .need-p（approval-card.css，来源 approval-card.html 审批卡前置提示行） */}
      <p className="need-p">
        <Icon name="triangle-alert" size={12} /> 需要权限
      </p>
      <div className="approval">
        <div className="ap-head">
          <span className="tag">{tagLabel}</span>
          <span className="wait">等待确认</span>
          <span className="ml-auto flex-none">
            {entry.riskLevel === 'high' ? (
              <span data-testid="approval-risk" data-risk="high" className="badge border-destructive/40 text-destructive">
                高风险
              </span>
            ) : (
              <span data-testid="approval-risk" data-risk="medium" className="badge border-warning/40 text-warning">
                中风险
              </span>
            )}
          </span>
        </div>

        {entry.reason !== null && (
          <p
            data-testid="approval-reason"
            className="border-b border-border px-3 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle"
          >
            {entry.reason}
          </p>
        )}

        {showCmd && (
          <div className="ap-cmd" data-testid="approval-cmd">
            <span className="dollar" aria-hidden="true">
              $
            </span>
            <code>{command}</code>
          </div>
        )}
        {showFile && filePath !== null && (() => {
          const { name, dir } = splitPath(filePath);
          const lines = writeLinesOf(entry.args);
          return (
            <div className="ap-file" data-testid="approval-file">
              <Icon name="file-text" size={14} className="text-foreground-subtlest" />
              <span className="fn">{name}</span>
              {lines !== null && lines > 0 && (
                <span data-testid="approval-file-add" className="badge badge-add flex-none">
                  +{lines}
                </span>
              )}
              {dir !== null && <span className="pth">{dir}</span>}
            </div>
          );
        })()}
        {!showCmd && !showFile && (
          <pre
            data-testid="approval-args"
            className="overflow-x-auto bg-terminal-bg px-3 py-2.5 font-mono text-[length:var(--code-font-size)] leading-relaxed break-all whitespace-pre-wrap text-terminal-fg"
          >
            {JSON.stringify(entry.args, null, 2)}
          </pre>
        )}

        <DecisionForm approvalId={approvalId} args={entry.args} variant={variant} />
      </div>
    </div>
  );
}
