import { useEffect, useState } from 'react';

import type { DiffVerdictRequest } from '@contracts/rest';

import { getDiffReview, postVerdict } from '@/api/diffReviews';
import { ApiError } from '@/api/http';
import { GIT_MARK } from '@/components/DiffGitMark';
import { DiffSummaryBar } from '@/components/DiffSummaryBar';
import { EmptyState } from '@/components/EmptyState';
import { Icon } from '@/components/Icon';
import { Spinner } from '@/components/Spinner';

import { useDiffStore } from '../store';
import type { DiffReviewState } from '../store';

import { DiffFileView, type DiffFileSummaryEntry } from './DiffFileView';

/**
 * DiffReviewPanel（M3 Phase 4）：workbench「审查」标签的真面板。
 *
 * 数据来源：diff store（diff_ready 事件 / REST 回填双通道合流的快照）+ 逐文件
 * patch 懒加载缓存。reviewId 来自标签 id（`review:<reviewId>`，tabIdOf 规则）。
 *
 * - 顶部 DiffSummaryBar（round/baseline/totals/文件数/binary 数）；
 * - 文件行结构（F15/R3§F，changed-files.html .rv-row）= Git 状态方块字母
 *   （GIT_MARK，--color-git-* 族）+ 文件名 + 路径 + 「+N -N」徽标 + 复制路径钮 +
 *   chevron；点击行展开/收起**行内 diff**（.rv-diff > DiffFileView 懒加载，
 *   chevron 随展开态旋转，可多行同开）；
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

/** 行内复制路径钮（真实剪贴板写入；stopPropagation 避免触发行展开）。 */
function CopyPathButton({ path }: { path: string }) {
  const [copied, setCopied] = useState(false);
  const copy = (): void => {
    const clip = typeof navigator !== 'undefined' ? navigator.clipboard : undefined;
    if (clip === undefined) return;
    void clip.writeText(path).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      },
      () => setCopied(false),
    );
  };
  return (
    <button
      type="button"
      data-testid="diff-file-copy"
      className="chip-btn flex-none"
      title={`复制路径：${path}`}
      onClick={(event) => {
        event.stopPropagation();
        copy();
      }}
    >
      <Icon name="copy" size={11} /> {copied ? '已复制' : '复制'}
    </button>
  );
}

/** 路径 → { 文件名, 目录（含尾斜杠） }（.rv-row 的 fn/pth 两段）。 */
function splitPath(path: string): { readonly name: string; readonly dir: string | null } {
  const idx = path.lastIndexOf('/');
  if (idx < 0) return { name: path, dir: null };
  return { name: path.slice(idx + 1), dir: path.slice(0, idx + 1) };
}

/** 审查面板单文件行 + 行内 diff 展开区（F15/R3§F 行结构）。 */
function FileRow({
  reviewId,
  file,
  expanded,
  onToggle,
}: {
  reviewId: string;
  file: DiffFileSummaryEntry;
  expanded: boolean;
  onToggle: () => void;
}) {
  const { name, dir } = splitPath(file.path);
  return (
    <div>
      <div
        data-testid="diff-file-item"
        data-expanded={expanded}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        title={expanded ? '收起行内 diff' : '展开行内 diff'}
        className="rv-row"
        onClick={onToggle}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onToggle();
          }
        }}
      >
        <span className="git-mark flex-none" style={{ color: GIT_MARK[file.change].color }}>
          {GIT_MARK[file.change].letter}
        </span>
        <span className="fn flex-none">{name}</span>
        {dir !== null && <span className="pth flex-none">{dir}</span>}
        <span className="grow" aria-hidden="true" />
        <span className="badge badge-add flex-none">+{file.additions}</span>
        <span className="badge badge-del flex-none">-{file.deletions}</span>
        <CopyPathButton path={file.path} />
        <span
          className="chev"
          aria-hidden="true"
          style={{
            transform: expanded ? 'rotate(180deg)' : undefined,
            transition: 'transform var(--dur) var(--ease)',
          }}
        >
          <Icon name="chevron-down" size={11} />
        </span>
      </div>
      {expanded && (
        <div className="rv-diff" data-testid="diff-file-inline">
          <DiffFileView reviewId={reviewId} summary={file} />
        </div>
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
  const [notFound, setNotFound] = useState(false);
  const [foreign, setForeign] = useState(false);
  const [attempt, setAttempt] = useState(0);
  /** 行内展开的文件路径集合（多行可同开；各行 DiffFileView 独立懒加载） */
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());

  const toggleExpanded = (path: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

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
        // 审查不存在（标签残留/数据被清理，B6 empty-states #1）：呈现「无审查」
        // 空态（原型文案照抄），刷新钮可重试；其余错误仍走错误态
        if (error instanceof ApiError && (error.code === 'DIFF_REVIEW_NOT_FOUND' || error.code === 'NOT_FOUND')) {
          setNotFound(true);
          return;
        }
        setLoadError(error instanceof ApiError ? `${error.code}：${error.message}` : String(error));
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [reviewId, hasReview, currentSessionId, upsertFromRest, attempt]);

  const retryFetch = (): void => {
    setNotFound(false);
    setLoadError(null);
    setAttempt((n) => n + 1);
  };

  // B6 无审查空态（empty-states #1「审查面板 · 非 Git 仓库」文案照抄官方快照）：
  // 头部 = 未暂存 badge + git 状态过滤 + 刷新钮（重试回填）；LCode 触发场景 =
  // 审查标签在场但 diffStore/REST 均无该审查数据
  if (notFound && review === undefined) {
    return (
      <section data-testid="diff-review-panel" className="h-full">
        <EmptyState
          testId="diff-review-empty"
          icon={<Icon name="review-check" size={16} />}
          title="当前 workspace 不在 Git 仓库中"
          description="打开一个 Git 仓库目录后，这里会展示当前 workspace 作用域内的改动。"
          head={
            <>
              <span className="badge">未暂存</span>
              <span className="t">git 状态过滤</span>
              <span style={{ flex: 1 }} />
              <button
                type="button"
                className="icon-btn"
                style={{ width: 22, height: 22 }}
                data-testid="diff-review-empty-refresh"
                title="刷新"
                onClick={retryFetch}
              >
                <Icon name="refresh" size={11} />
              </button>
            </>
          }
        />
      </section>
    );
  }

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

  // 行内展开区随行渲染（FileRow 内 DiffFileView 懒加载），无独立详情区
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
        className="min-h-0 flex-1 overflow-y-auto border-b border-border"
      >
        {review.files.map((file) => (
          <FileRow
            key={file.path}
            reviewId={reviewId}
            file={file}
            expanded={expanded.has(file.path)}
            onToggle={() => toggleExpanded(file.path)}
          />
        ))}
      </nav>
      {review.verdict === 'pending' ? (
        <VerdictForm reviewId={reviewId} />
      ) : (
        <ResolvedView review={review} />
      )}
    </section>
  );
}
