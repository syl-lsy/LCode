import { useId, useState, type FormEvent } from 'react';

import type { ApprovalDecisionRequest } from '@contracts/rest';

import { decideApproval } from '@/api/approvals';
import { ApiError } from '@/api/http';

import { useApprovalStore } from '../store';

/**
 * 决策表单（A4 §8.1 DecisionForm 行；approval-card.html 四决策 + 第 5 档反馈框）。
 *
 * - 四决策：允许（approve scope=once）/ 本会话内始终允许（approve scope=session，
 *   对应 PRD「会话级免审」口径）/ 修改参数后放行（edit，展开 args JSON 编辑器）/
 *   拒绝（reject，反馈可选附言）；
 * - 第 5 档反馈框同时服务 respond：选 respond 时 feedback 必填非空（就地报错）；
 * - 提交中禁用全部控件、确认钮显示「提交中…」；失败经 ApiError 就地展示
 *   `决策提交失败（code）：message`，不裸抛（后端 APPROVAL_ALREADY_RESOLVED
 *   幂等 409 由该路径兜底）；
 * - 样式只用语义 token（accent/input/destructive/--text-ui-*），不写裸色值。
 */

/** 选项键（approve 拆成 once/session 两项；其余与契约 decision 字面量一一对应）。 */
export type DecisionOptionKey = 'approve-once' | 'approve-session' | 'edit' | 'reject' | 'respond';

export type ParsedArgsResult =
  | { readonly ok: true; readonly args: Record<string, unknown> }
  | { readonly ok: false; readonly error: string };

/**
 * 解析「修改参数后放行」的 args 编辑器文本（导出供单测直测）。
 * JSON 解析失败 / 数组 / null / 非对象一律返回就地报错文案，不抛异常。
 */
export function parseEditedArgs(text: string): ParsedArgsResult {
  const trimmed = text.trim();
  if (trimmed === '') {
    return { ok: false, error: '参数不能为空：请粘贴完整的 JSON 对象' };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return { ok: false, error: '不是合法 JSON：请检查逗号、引号或括号' };
  }
  if (parsed === null) {
    return { ok: false, error: '参数不能为 null：必须是 JSON 对象（{...}）' };
  }
  if (Array.isArray(parsed)) {
    return { ok: false, error: '参数必须是 JSON 对象（{...}），不能是数组' };
  }
  if (typeof parsed !== 'object') {
    return { ok: false, error: '参数必须是 JSON 对象（{...}），收到的是标量值' };
  }
  return { ok: true, args: parsed as Record<string, unknown> };
}

const OPTIONS: readonly { readonly key: DecisionOptionKey; readonly title: string; readonly desc: string }[] = [
  { key: 'approve-once', title: '允许', desc: '仅允许这一次' },
  { key: 'approve-session', title: '本会话内始终允许', desc: '会话内同类操作不再询问（升级为自动审批）' },
  { key: 'edit', title: '修改参数后放行', desc: '编辑参数后继续执行' },
  { key: 'reject', title: '拒绝', desc: '这次先拒绝，可在下方附说明' },
  { key: 'respond', title: '给出反馈后继续', desc: '不改放行决策，把指示反馈给模型' },
];

export interface DecisionFormProps {
  readonly approvalId: string;
  /** 原始 args（编辑器初值；提交 edit 时以编辑器当前文本为准） */
  readonly args: Readonly<Record<string, unknown>>;
}

export function DecisionForm({ approvalId, args }: DecisionFormProps) {
  const deciding = useApprovalStore((s) => s.deciding.has(approvalId));
  const setDeciding = useApprovalStore((s) => s.setDeciding);
  const applyResolved = useApprovalStore((s) => s.applyResolved);

  const [choice, setChoice] = useState<DecisionOptionKey | null>(null);
  const [argsText, setArgsText] = useState(() => JSON.stringify(args, null, 2));
  const [feedback, setFeedback] = useState('');
  const [argsError, setArgsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const argsEditorId = useId();
  const feedbackId = useId();

  const selectOption = (key: DecisionOptionKey): void => {
    setChoice(key);
    setArgsError(null);
    setError(null);
  };

  const onSubmit = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (deciding) return;
    setError(null);
    setArgsError(null);

    if (choice === null) {
      setError('请先选择一个决策（选项 1-4 或 5 反馈）');
      return;
    }

    let body: ApprovalDecisionRequest;
    switch (choice) {
      case 'approve-once':
        body = { decision: 'approve', scope: 'once' };
        break;
      case 'approve-session':
        body = { decision: 'approve', scope: 'session' };
        break;
      case 'edit': {
        const parsed = parseEditedArgs(argsText);
        if (!parsed.ok) {
          setArgsError(parsed.error);
          return;
        }
        body = { decision: 'edit', edited_args: parsed.args };
        break;
      }
      case 'reject': {
        const text = feedback.trim();
        body = text === '' ? { decision: 'reject' } : { decision: 'reject', feedback: text };
        break;
      }
      case 'respond': {
        const text = feedback.trim();
        if (text === '') {
          setError('反馈不能为空：请填写要传达给模型的指示');
          return;
        }
        body = { decision: 'respond', feedback: text };
        break;
      }
    }

    setDeciding(approvalId, true);
    try {
      await decideApproval(approvalId, body);
      // 本地即时终态（decided_by='local' 与契约 fixtures 口径一致）；
      // SSE approval_resolved 回声同 id 再投递，applyResolved 幂等覆盖。
      applyResolved({
        approvalId,
        decision: body.decision,
        scope: body.scope ?? null,
        editedArgs: body.edited_args ?? null,
        feedback: body.feedback ?? null,
        decidedBy: 'local',
      });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`决策提交失败（${err.code}）：${err.message}`);
      } else {
        setError(`决策提交失败（UNKNOWN）：${err instanceof Error ? err.message : String(err)}`);
      }
    } finally {
      setDeciding(approvalId, false);
    }
  };

  return (
    <form
      data-testid="decision-form"
      onSubmit={(event) => {
        void onSubmit(event);
      }}
      aria-label="审批决策"
      className="flex flex-col"
    >
      <div role="group" aria-label="决策选项" className="flex flex-col gap-0.5 p-1.5">
        {OPTIONS.map((option, index) => {
          const selected = choice === option.key;
          return (
            <button
              key={option.key}
              type="button"
              data-testid={`decision-option-${option.key}`}
              aria-pressed={selected}
              disabled={deciding}
              onClick={() => selectOption(option.key)}
              className={`flex min-h-[40px] items-baseline gap-2.5 rounded-md border px-3 py-2 text-left transition-colors ${
                selected
                  ? 'border-accent-border bg-accent'
                  : 'border-transparent hover:bg-surface-hover'
              }`}
            >
              <span className="flex-none font-mono text-[length:var(--text-ui-xs)] text-foreground-subtlest">
                {index + 1}.
              </span>
              <span className="flex-none font-medium text-[length:var(--text-ui-sm)] text-foreground-strong">
                {option.title}
              </span>
              <span className="min-w-0 flex-1 truncate text-[length:var(--text-ui-xs)] text-foreground-subtle">
                {option.desc}
              </span>
              {selected && (
                <span aria-hidden="true" className="flex-none self-center text-brand">
                  ✓
                </span>
              )}
            </button>
          );
        })}
      </div>

      {choice === 'edit' && (
        <div className="flex flex-col gap-1.5 border-t border-border p-3">
          <label
            htmlFor={argsEditorId}
            className="text-[length:var(--text-ui-xs)] font-medium text-foreground-strong"
          >
            修改后的参数（JSON 对象）
          </label>
          <textarea
            id={argsEditorId}
            data-testid="decision-args-editor"
            value={argsText}
            disabled={deciding}
            spellCheck={false}
            rows={Math.min(12, Math.max(4, argsText.split('\n').length))}
            onChange={(event) => {
              setArgsText(event.target.value);
              setArgsError(null);
            }}
            className="w-full resize-y rounded-md border border-border bg-input px-2.5 py-2 font-mono text-[length:var(--code-font-size)] leading-relaxed text-foreground outline-none focus:border-accent-border"
          />
          {argsError !== null && (
            <p data-testid="decision-args-error" role="alert" className="text-[length:var(--text-ui-xs)] text-destructive">
              {argsError}
            </p>
          )}
        </div>
      )}

      <div className="flex flex-col gap-2 border-t border-border px-3 pt-2.5 pb-3">
        <div className="flex items-start gap-2.5">
          <span className="flex-none pt-2 font-mono text-[length:var(--text-ui-xs)] text-foreground-subtlest">
            5.
          </span>
          <textarea
            id={feedbackId}
            data-testid="decision-feedback"
            value={feedback}
            disabled={deciding}
            rows={2}
            placeholder={
              choice === 'respond'
                ? '必填：告诉模型接下来应该怎么做…'
                : '可选：附言 / 告诉模型接下来应该怎么做…'
            }
            onChange={(event) => {
              setFeedback(event.target.value);
              setError(null);
            }}
            className="min-h-[38px] flex-1 resize-none rounded-md border border-border bg-input px-2.5 py-2 text-[length:var(--text-ui-xs)] text-foreground outline-none placeholder:text-foreground-subtlest focus:border-accent-border"
          />
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
            Tab / 上下键选择，回车确认
          </span>
          <span className="flex-1" aria-hidden="true" />
          <button
            type="submit"
            data-testid="decision-submit"
            disabled={deciding}
            className="btn btn-primary btn-sm"
          >
            {deciding ? '提交中…' : '确认'}
          </button>
        </div>
        {error !== null && (
          <p data-testid="decision-error" role="alert" className="text-[length:var(--text-ui-xs)] text-destructive">
            {error}
          </p>
        )}
      </div>
    </form>
  );
}
