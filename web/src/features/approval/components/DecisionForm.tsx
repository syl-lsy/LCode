import { useId, useState, type FormEvent } from 'react';

import type { ApprovalDecisionRequest } from '@contracts/rest';

import { decideApproval } from '@/api/approvals';
import { ApiError } from '@/api/http';
import { Icon } from '@/components/Icon';

import { useApprovalStore } from '../store';

/**
 * 决策表单（A4 §8.1 DecisionForm 行；approval-card.html 决策选项 + 第 5 档反馈框）。
 *
 * - 选项文案两变体（approval-card.html §8/§9，逐字校对）：
 *   - Bash 变体：允许 / 始终允许此命令 / 修改参数后放行 / 拒绝；
 *   - 文件变体：允许 / 始终允许本项目 / 修改参数后放行 / 拒绝；
 *   - 原型「完全访问」档不渲染：契约 ApprovalDecisionRequest 注明 scope=session
 *     对应原型「完全访问/始终允许」的会话级持续免审——两者提交体相同，渲染两档
 *     会造成同提交体的重复选项（差异说明见 docs/arch 与本批汇报）；
 * - 第 3 档 edit（AC-006-3）与第 5 档 respond（AC-006-4）为契约决策，原型无对应
 *   档，保留在列表中保证契约可达；第 5 档反馈框同时服务 reject 附言与 respond：
 *   选 respond 时 feedback 必填非空（就地报错）；
 * - 提交中禁用全部控件、确认钮显示「提交中…」；失败经 ApiError 就地展示
 *   `决策提交失败（code）：message`，不裸抛（后端 APPROVAL_ALREADY_RESOLVED
 *   幂等 409 由该路径兜底）；
 * - 样式走 approval-card.css 的 .ap-* 族（来源 approval-card.html）+ 语义 token，
 *   不写裸色值。
 */

/** 决策变体（approval-card.html 两变体；Bash 命令类 / 文件写入类）。 */
export type ApprovalVariant = 'bash' | 'file';

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

interface DecisionOption {
  readonly key: DecisionOptionKey;
  readonly title: string;
  readonly desc: string;
}

/**
 * 变体感知选项表：第 1/2/4 档文案与原型逐字一致；第 3 档 edit 与第 5 档 respond
 * 为契约决策（AC-006-3/4），原型无对应档。
 */
function optionsOf(variant: ApprovalVariant): readonly DecisionOption[] {
  const alwaysAllow: DecisionOption =
    variant === 'file'
      ? { key: 'approve-session', title: '始终允许本项目', desc: '后续相同文件操作不再询问' }
      : { key: 'approve-session', title: '始终允许此命令', desc: '项目范围内，后续相同命令不再询问' };
  return [
    { key: 'approve-once', title: '允许', desc: '仅允许这一次' },
    alwaysAllow,
    { key: 'edit', title: '修改参数后放行', desc: '编辑参数后继续执行' },
    { key: 'reject', title: '拒绝', desc: '这次先拒绝' },
    { key: 'respond', title: '给出反馈后继续', desc: '不改放行决策，把指示反馈给模型' },
  ];
}

export interface DecisionFormProps {
  readonly approvalId: string;
  /** 原始 args（编辑器初值；提交 edit 时以编辑器当前文本为准） */
  readonly args: Readonly<Record<string, unknown>>;
  /** 审批卡变体（决定第 2 档文案；缺省 Bash 变体） */
  readonly variant?: ApprovalVariant;
}

export function DecisionForm({ approvalId, args, variant = 'bash' }: DecisionFormProps) {
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
      {/* .ap-opts / .ap-opt（approval-card.css，来源 approval-card.html 决策选项列表） */}
      <div role="group" aria-label="决策选项" className="ap-opts">
        {optionsOf(variant).map((option, index) => {
          const selected = choice === option.key;
          return (
            <button
              key={option.key}
              type="button"
              data-testid={`decision-option-${option.key}`}
              aria-pressed={selected}
              disabled={deciding}
              onClick={() => selectOption(option.key)}
              className={selected ? 'ap-opt sel' : 'ap-opt'}
            >
              <span className="no">{index + 1}.</span>
              <span className="tt">{option.title}</span>
              <span className="ds">{option.desc}</span>
              {/* .ck 选中勾：CSS visibility 控制显隐（与原型标记同构） */}
              <Icon name="check" size={13} className="ck" />
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

      {/* .ap-foot / .ap-reject / .ap-keys（approval-card.css，第 5 档反馈框 + 确认行） */}
      <div className="ap-foot">
        <div className="ap-reject">
          {/* 原型口径：第 5 档编号前缀（approval-card.html .ap-reject 内 <span class="no">5.</span>） */}
          <span className="no">5.</span>
          <textarea
            id={feedbackId}
            data-testid="decision-feedback"
            value={feedback}
            disabled={deciding}
            rows={2}
            aria-label="拒绝反馈 / 给模型的指示（选「给出反馈后继续」时必填）"
            placeholder="告诉模型接下来应该怎么做..."
            onChange={(event) => {
              setFeedback(event.target.value);
              setError(null);
            }}
          />
        </div>
        <div className="ap-keys">
          使用 Tab / 上下键选择，回车确认
          <span className="sp" aria-hidden="true" />
          <button
            type="submit"
            data-testid="decision-submit"
            disabled={deciding}
            className="btn btn-primary btn-sm"
            style={{ height: 28, padding: '0 16px' }}
          >
            {deciding ? '提交中…' : '确认'}
          </button>
        </div>
        {error !== null && (
          <p data-testid="decision-error" role="alert" className="mt-1.5 text-[length:var(--text-ui-xs)] text-destructive">
            {error}
          </p>
        )}
      </div>
    </form>
  );
}
