import { useEffect, useState } from 'react';

import type { DiffVerdictRequest } from '@contracts/rest';

import { getDiffReview, postVerdict } from '@/api/diffReviews';
import { ApiError } from '@/api/http';
import { DiffSummaryBar } from '@/components/DiffSummaryBar';
import { EmptyState } from '@/components/EmptyState';
import { Spinner } from '@/components/Spinner';

import { useDiffStore } from '../store';
import type { DiffReviewState } from '../store';

import { CHANGE_BADGE, CHANGE_LABEL, DiffFileView } from './DiffFileView';

/**
 * DiffReviewPanel（M3 Phase 4）：workbench「审查」标签的真面板。
 *
 * 数据来源：diff store（diff_ready 事件 / REST 回填双通道合流的快照）+ 逐文件
 * patch 懒加载缓存。reviewId 来自标签 id（`review:<reviewId>`，tabIdOf 规则）。
 *
 * - 顶部 DiffSummaryBar（round/baseline/totals/文件数/binary 数）；
 * - 文件列表可点选、可滚动；选中文件下方懒加载单文件详情（DiffFileView）；
 * - 裁决区（仅 pending）：接受 / 驳回（意见可选）；提交中禁用防重复；失败展示
 *   `错误（code）：message` 并保留输入；成功后**以响应 DTO 为准**回填（后端在
 *   会话有活动 run 时会把系统注记追加进 feedback——事件/POST 响应/详情携带同一
 *   字符串，store.applyVerdict 单调合并兜底乱序到达与事件重放）；
 * - 已裁决态：accepted/rejected/被取代（decided_by=system 的 rejected 是被新一轮
 *   审查取代，不是用户驳回）+ 意见 + decided_by；
 * - 刷新恢复：store 缺该 review 时 getDiffReview 一次性回填（upsertFromRest 内置
 *   终态不回退）；审查属其它会话 → 空态提示且不写入 store（切会话不串台）。
 */

/** 驳回空意见时的产品语义提示（后端只在有 feedback 时自动起返工 run）。 */
const EMPTY_FEEDBACK_HINT = '不填意见将不会自动返工（驳回附意见才会自动启动返工任务）。';

/** 裁决表单（仅 pending 出现；本地 submitting 态防重复提交）。 */
function VerdictForm({ reviewId }: { reviewId: string }) {
  const applyVerdict = useDiffStore((s) => s.applyVerdict);
  const [feedback, setFeedback] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (verdict: 'accepted' | 'rejected'): Promise<void> => {
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    const text = feedback.trim();
    const body: DiffVerdictRequest =
      verdict === 'accepted'
        ? { verdict: 'accepted' }
        : { verdict: 'rejected', feedback: text === '' ? null : text };
    try {
      // 以响应 DTO 为准回填（不信本地乐观状态）：rejected 时后端可能已把系统
      // 注记追加进 feedback（活动 run 冲突分支），applyVerdict 单调合并保留它。
      // verdict 回填用请求值——响应 DTO 的 verdict 类型含 'pending'（生成物形状），
      // 契约保证回执 verdict === 请求 verdict。
      const detail = await postVerdict(reviewId, body);
      applyVerdict({
        reviewId,
        verdict: body.verdict,
        feedback: detail.feedback ?? null,
        decidedBy: detail.decided_by ?? null,
      });
    } catch (err) {
      if (err instanceof ApiError) {
        setError(`裁决提交失败（${err.code}）：${err.message}`);
      } else {
        setError(`裁决提交失败（UNKNOWN）：${err instanceof Error ? err.message : String(err)}`);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div data-testid="diff-verdict-form" className="flex-none border-t border-border px-3 py-2.5">
      <p className="mb-1.5 text-[length:var(--text-ui-xs)] font-medium text-foreground-strong">
        裁决
      </p>
      {feedback.trim() === '' && (
        <p
          data-testid="diff-verdict-empty-hint"
          className="mb-1.5 text-[length:var(--text-ui-2xs)] text-foreground-subtlest"
        >
          {EMPTY_FEEDBACK_HINT}
        </p>
      )}
      <textarea
        data-testid="diff-verdict-feedback"
        value={feedback}
        disabled={submitting}
        rows={2}
        placeholder="驳回意见（可选）：告诉模型上一轮改动需要怎么改…"
        onChange={(event) => {
          setFeedback(event.target.value);
          setError(null);
        }}
        className="w-full resize-none rounded-md border border-border bg-input px-2.5 py-2 text-[length:var(--text-ui-xs)] text-foreground outline-none placeholder:text-foreground-subtlest focus:border-accent-border"
      />
      <div className="mt-2 flex items-center gap-2">
        <button
          type="button"
          data-testid="diff-verdict-accept"
          disabled={submitting}
          onClick={() => {
            void submit('accepted');
          }}
          className="btn btn-primary btn-sm flex-1"
        >
          {submitting ? '提交中…' : '接受'}
        </button>
        <button
          type="button"
          data-testid="diff-verdict-reject"
          disabled={submitting}
          onClick={() => {
            void submit('rejected');
          }}
          className="btn btn-sm flex-1"
        >
          {submitting ? '提交中…' : '驳回'}
        </button>
      </div>
      {error !== null && (
        <p
          data-testid="diff-verdict-error"
          role="alert"
          className="mt-1.5 text-[length:var(--text-ui-xs)] text-destructive"
        >
          {error}
        </p>
      )}
    </div>
  );
}

/** 已裁决态：accepted / 用户驳回 / 被新一轮取代（decided_by=system）三分。 */
function ResolvedView({ review }: { review: DiffReviewState }) {
  const superseded = review.verdict === 'rejected' && review.decidedBy === 'system';
  return (
    <div
      data-testid="diff-review-resolved"
      className="flex-none border-t border-border px-3 py-2.5"
    >
      <div className="flex items-center gap-2">
        {review.verdict === 'accepted' ? (
          <span data-testid="diff-verdict-badge" className="badge badge-add">
            已接受
          </span>
        ) : superseded ? (
          <span data-testid="diff-verdict-badge" className="badge">
            已被新一轮审查取代
          </span>
        ) : (
          <span data-testid="diff-verdict-badge" className="badge badge-del">
            已驳回
          </span>
        )}
        {review.decidedBy !== null && !superseded && (
          <span className="text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
            裁决人：{review.decidedBy}
          </span>
        )}
        {superseded && (
          <span className="text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
            系统判定
          </span>
        )}
      </div>
      {review.feedback !== null && review.feedback !== '' && (
        <p
          data-testid="diff-review-feedback"
          className="mt-1.5 whitespace-pre-wrap break-words text-[length:var(--text-ui-xs)] leading-relaxed text-foreground-subtle"
        >
          {review.feedback}
        </p>
      )}
    </div>
  );
}

export interface DiffReviewPanelProps {
  /** 审查 id（来自标签 id `review:<reviewId>`） */
  readonly reviewId: string;
  /**
   * 当前会话 id（App 层自 sessionStore 下传；feature 隔离规则禁止 workbench 直接
   * import sessions store）。用于会话防串台：审查属其它会话 → 空态提示不展示。
   * null（无会话上下文）时跳过该校验。
   */
  readonly currentSessionId: string | null;
}

export function DiffReviewPanel({ reviewId, currentSessionId }: DiffReviewPanelProps) {
  const review = useDiffStore((s) => s.reviews.get(reviewId));
  const upsertFromRest = useDiffStore((s) => s.upsertFromRest);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [foreign, setForeign] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  const hasReview = review !== undefined;
  // 会话防串台（双闸）：store 里已有但属其它会话；或 REST 回填发现会话不符
  const crossSession =
    review !== undefined && currentSessionId !== null && review.sessionId !== currentSessionId;

  // 刷新恢复：store 缺该 review（页面刷新/切会话清空）→ REST 一次性回填。
  // upsertFromRest 内置「终态不回退」，回填不会把本地已裁决状态冲掉。
  useEffect(() => {
    if (hasReview) return;
    const controller = new AbortController();
    let cancelled = false;
    setLoadError(null);
    setForeign(false);
    getDiffReview(reviewId, { signal: controller.signal })
      .then((detail) => {
        if (cancelled) return;
        // 属其它会话：只提示，不写入当前会话的 diff store
        if (currentSessionId !== null && detail.session_id !== currentSessionId) {
          setForeign(true);
          return;
        }
        upsertFromRest(detail);
      })
      .catch((error: unknown) => {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
        setLoadError(error instanceof ApiError ? `${error.code}：${error.message}` : String(error));
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [reviewId, hasReview, currentSessionId, upsertFromRest, attempt]);

  if (loadError !== null) {
    return (
      <section data-testid="diff-review-panel" className="h-full">
        <EmptyState
          icon={<span aria-hidden="true">⚠</span>}
          title="审查加载失败"
          description={loadError}
          action={
            <button
              type="button"
              data-testid="diff-review-retry"
              className="btn btn-sm"
              onClick={() => {
                setLoadError(null);
                setAttempt((n) => n + 1);
              }}
            >
              重试
            </button>
          }
        />
      </section>
    );
  }

  if (foreign || crossSession) {
    return (
      <section data-testid="diff-review-panel" className="h-full">
        <EmptyState
          title="该审查属于其它会话"
          description="此标签指向的 diff 审查不属于当前会话，为避免串台不予展示。"
        />
      </section>
    );
  }

  if (review === undefined) {
    return (
      <section
        data-testid="diff-review-panel"
        className="flex h-full items-center justify-center gap-2 text-[length:var(--text-ui-xs)] text-foreground-subtle"
      >
        <Spinner label="加载审查" /> 加载审查…
      </section>
    );
  }

  // 默认选中首个文件（详情懒加载随之拉取）；用户点选后跟随用户
  const activePath = selected ?? review.files[0]?.path ?? null;
  const activeSummary =
    review.files.find((file) => file.path === activePath) ?? null;

  return (
    <section data-testid="diff-review-panel" className="flex h-full min-h-0 flex-col">
      <div className="flex-none border-b border-border px-3 py-2">
        <DiffSummaryBar
          round={review.round}
          baseline={review.baseline}
          files={review.files}
          additionsTotal={review.additionsTotal}
          deletionsTotal={review.deletionsTotal}
        />
      </div>
      <nav
        data-testid="diff-file-list"
        aria-label="改动文件列表"
        className="max-h-44 flex-none overflow-y-auto border-b border-border"
      >
        {review.files.map((file) => (
          <button
            key={file.path}
            type="button"
            data-testid="diff-file-item"
            data-active={file.path === activePath}
            title={
              file.old_path !== null ? `${file.old_path} → ${file.path}` : file.path
            }
            onClick={() => setSelected(file.path)}
            className={`flex w-full items-center gap-1.5 px-2 py-1 text-left text-[length:var(--text-ui-xs)] ${
              file.path === activePath
                ? 'bg-surface-hover text-foreground-strong'
                : 'text-foreground-subtle hover:bg-surface-hover'
            }`}
          >
            <span className={`flex-none ${CHANGE_BADGE[file.change]}`}>
              {CHANGE_LABEL[file.change]}
            </span>
            <span className="min-w-0 flex-1 truncate font-mono">
              {file.old_path !== null ? `${file.old_path} → ${file.path}` : file.path}
            </span>
            {file.binary && <span className="badge flex-none">二进制</span>}
            <span className="flex-none font-mono text-[length:var(--text-ui-2xs)]">
              <span className="text-diff-added">+{file.additions}</span>
              <span className="text-diff-removed"> -{file.deletions}</span>
            </span>
          </button>
        ))}
      </nav>
      {activeSummary !== null && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <DiffFileView reviewId={reviewId} summary={activeSummary} />
        </div>
      )}
      {review.verdict === 'pending' ? (
        <VerdictForm reviewId={reviewId} />
      ) : (
        <ResolvedView review={review} />
      )}
    </section>
  );
}
