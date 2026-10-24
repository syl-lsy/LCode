import { useEffect, useState } from 'react';

import type { DiffFileDetail } from '@contracts/rest';
import type { DiffReviewDetail } from '@contracts/rest';

import { getDiffFile } from '@/api/diffReviews';
import { ApiError } from '@/api/http';
import { DiffText } from '@/components/DiffText';
import { GIT_MARK } from '@/components/DiffGitMark';
import { Spinner } from '@/components/Spinner';

import { useDiffStore } from '../store';

/**
 * DiffFileView（M3 Phase 4）：审查面板的单文件视图。
 *
 * - 头部：.file-head（changed-files.html）= Git 状态方块字母（GIT_MARK，--color-git-*
 *   族）+ rename 展示 `old_path → path` + 二进制标记 + +N/−N 徽标；
 * - 主体：复用 DiffText 渲染 `DiffFileDetail.patch` 的解析结果；
 * - **patch 懒加载**：挂载（= 面板选中该文件）才 `getDiffFile`，结果写
 *   `useDiffStore.cachePatch`；已有缓存不再请求（懒加载幂等），abort 跟随卸载；
 * - **三种非文本形态区分**（后端零契约变更的 wire 产物，禁止混为一谈）：
 *   ① `binary === true` → DiffText 的二进制占位（patch 恒 null，正文走 blob_ref）；
 *   ② `binary === false && patch === null`（>1MB 截断，blob_ref 非空）→
 *      「文件过大，未内联展示」——不是空文件、不是错误；
 *   ③ `patch === ''`（真空 diff，如仅 mode 变化且无文本段）→「无内容变更」，
 *      不走解析错误态（parseDiff 对空串判 unparseable）；
 * - 加载失败就地错误态 + 重试按钮，不把面板整块打空。
 */

/** 单文件摘要（契约 DiffFileSummary，取自 DiffReviewDetail.files 条目）。 */
export type DiffFileSummaryEntry = DiffReviewDetail['files'][number];

/** 文件详情主体（按三种非文本形态分发，见文件头注释）。 */
function DiffFileBody({ detail }: { detail: DiffFileDetail }) {
  // 生成物类型把带 default 的字段标为可选（输入形状）；zod 解析后运行时恒有值，
  // 边界处 ?? null 收敛（同 approvals store 惯例）
  const binary = detail.binary === true;
  const patch = detail.patch ?? null;
  const blobRef = detail.blob_ref ?? null;
  if (binary) {
    // ① 二进制：DiffText 内建占位（含 blob_ref 展示）
    return <DiffText path={detail.path} binary patch={patch} blobRef={blobRef} />;
  }
  if (patch === null) {
    // ② >1MB 截断：正文未内联，blob_ref 指向沙箱工作区/基线 blob——如实标注，
    //    不渲染成「空文件」也不当解析错误
    return (
      <div
        data-testid="diff-file-oversize"
        className="rounded-md border border-dashed border-border-hover bg-surface px-3 py-4 text-center"
      >
        <p className="text-[length:var(--text-ui-sm)] text-foreground-subtle">
          文件过大，未内联展示
        </p>
        <p className="mt-1 font-mono text-[length:var(--text-ui-xs)] text-foreground-subtlest">
          {detail.path}
        </p>
        {blobRef !== null && blobRef !== '' && (
          <p className="mt-1 font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
            blob_ref: {blobRef}
          </p>
        )}
      </div>
    );
  }
  if (patch === '') {
    // ③ 真空 diff：明确「无内容变更」（parseDiff('') 判 unparseable，须在 DiffText 前拦截）
    return (
      <p
        data-testid="diff-file-nochange"
        className="px-2 py-1.5 text-[length:var(--text-ui-xs)] text-foreground-subtle"
      >
        无内容变更（仅元数据/文件模式变化）。
      </p>
    );
  }
  return <DiffText path={detail.path} binary={false} patch={patch} />;
}

export interface DiffFileViewProps {
  readonly reviewId: string;
  /** 该文件的摘要（审查详情 files 条目；懒加载决策与头部展示来源） */
  readonly summary: DiffFileSummaryEntry;
}

export function DiffFileView({ reviewId, summary }: DiffFileViewProps) {
  const cached = useDiffStore((s) => s.reviews.get(reviewId)?.patches.get(summary.path));
  const cachePatch = useDiffStore((s) => s.cachePatch);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (cached !== undefined) return; // 缓存命中：不再请求
    const controller = new AbortController();
    let cancelled = false;
    setLoadError(null);
    getDiffFile(reviewId, summary.path, { signal: controller.signal })
      .then((detail) => {
        if (cancelled) return;
        cachePatch(reviewId, detail);
      })
      .catch((error: unknown) => {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
        setLoadError(
          error instanceof ApiError ? `${error.code}：${error.message}` : String(error),
        );
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [reviewId, summary.path, cached, cachePatch, attempt]);

  const loading = cached === undefined && loadError === null;

  return (
    <section data-testid="diff-file-view" className="min-w-0">
      {/* .file-head（changed-files.css，来源 changed-files.html 文件头：Git 状态
          图标 + 路径 + 徽标） */}
      <header
        data-testid="diff-file-header"
        className="file-head"
      >
        <span className="git-mark flex-none" style={{ color: GIT_MARK[summary.change].color }}>
          {GIT_MARK[summary.change].letter}
        </span>
        <span
          className="min-w-0 flex-1 truncate"
          title={summary.old_path !== null ? `${summary.old_path} → ${summary.path}` : summary.path}
        >
          {summary.old_path !== null ? `${summary.old_path} → ${summary.path}` : summary.path}
        </span>
        {summary.binary && <span className="badge flex-none">二进制</span>}
        <span className="badge badge-add flex-none">+{summary.additions}</span>
        <span className="badge badge-del flex-none">-{summary.deletions}</span>
      </header>
      <div className="px-1 py-1">
        {loading && (
          <div className="flex items-center gap-2 px-2 py-3 text-[length:var(--text-ui-xs)] text-foreground-subtle">
            <Spinner label="加载 diff" /> 加载 diff…
          </div>
        )}
        {!loading && loadError !== null && (
          <div
            data-testid="diff-file-error"
            className="flex flex-col items-start gap-1.5 px-2 py-2"
          >
            <p role="alert" className="text-[length:var(--text-ui-xs)] text-destructive">
              diff 加载失败（{loadError}）
            </p>
            <button
              type="button"
              data-testid="diff-file-retry"
              className="btn btn-sm"
              onClick={() => {
                setLoadError(null);
                setAttempt((n) => n + 1);
              }}
            >
              重试
            </button>
          </div>
        )}
        {cached !== undefined && <DiffFileBody detail={cached} />}
      </div>
    </section>
  );
}
