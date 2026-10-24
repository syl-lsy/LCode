import { useEffect, useState } from 'react';

import type { DiffFileDetail, DiffReviewDetail } from '@contracts/rest';

import { getDiffFile } from '@/api/diffReviews';
import { ApiError } from '@/api/http';
import { GIT_MARK } from '@/components/DiffGitMark';
import { DiffText } from '@/components/DiffText';
import { Spinner } from '@/components/Spinner';

import { useDiffStore } from '@/features/workbench/store';

/**
 * 消息流改动文件卡的单文件 diff 段（changed-files.html .file-sec / .file-head）。
 *
 * 与审查面板的 DiffFileView 同一数据通道（useDiffStore.patch 懒加载缓存：挂载才
 * getDiffFile，缓存命中不再请求，abort 跟随卸载），但作为 chat feature 内的呈现
 * 组件独立实现——依赖方向门禁（A4 §2.2）只放行 chat 容器引 approval/todo 卡片，
 * workbench 组件不进流；store 例外（workbenchStore 同款 UI 例外口径）允许 chat
 * 读 diff store 与写 cachePatch。
 *
 * - 头部 .file-head：Git 状态方块字母（GIT_MARK）+ 路径 + 二进制标记 + +N/−N 徽标；
 * - 主体复用 DiffText；binary / >1MB 截断（patch=null）/ 空 patch 三态如实降级，
 *   加载失败就地错误 + 重试。
 */

/** 单文件摘要（契约 DiffFileSummary，取自 DiffReviewDetail.files 条目）。 */
type FileSummary = DiffReviewDetail['files'][number];

export interface FilesDiffSectionProps {
  readonly reviewId: string;
  readonly file: FileSummary;
}

export function FilesDiffSection({ reviewId, file }: FilesDiffSectionProps) {
  const cached = useDiffStore((s) => s.reviews.get(reviewId)?.patches.get(file.path));
  const cachePatch = useDiffStore((s) => s.cachePatch);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (cached !== undefined) return; // 缓存命中：不再请求
    const controller = new AbortController();
    let cancelled = false;
    setLoadError(null);
    getDiffFile(reviewId, file.path, { signal: controller.signal })
      .then((detail) => {
        if (cancelled) return;
        cachePatch(reviewId, detail);
      })
      .catch((error: unknown) => {
        if (cancelled || (error instanceof DOMException && error.name === 'AbortError')) return;
        setLoadError(error instanceof ApiError ? `${error.code}：${error.message}` : String(error));
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [reviewId, file.path, cached, cachePatch, attempt]);

  const loading = cached === undefined && loadError === null;

  return (
    <div className="file-sec" data-testid="chat-file-sec">
      <div className="file-head" data-testid="chat-file-head">
        <span className="git-mark flex-none" style={{ color: GIT_MARK[file.change].color }}>
          {GIT_MARK[file.change].letter}
        </span>
        <span className="min-w-0 flex-1 truncate" title={file.path}>
          {file.path}
        </span>
        {file.binary && <span className="badge flex-none">二进制</span>}
        <span className="badge badge-add flex-none">+{file.additions}</span>
        <span className="badge badge-del flex-none">-{file.deletions}</span>
      </div>
      <div className="px-2 py-1">
        {loading && (
          <div className="flex items-center gap-2 px-2 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle">
            <Spinner label="加载 diff" /> 加载 diff…
          </div>
        )}
        {!loading && loadError !== null && (
          <div data-testid="chat-file-error" className="flex flex-col items-start gap-1.5 px-2 py-2">
            <p role="alert" className="text-[length:var(--text-ui-xs)] text-destructive">
              diff 加载失败（{loadError}）
            </p>
            <button
              type="button"
              data-testid="chat-file-retry"
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
        {cached !== undefined && <DiffSectionBody detail={cached} />}
      </div>
    </div>
  );
}

/** 详情主体三态分发（与 DiffFileView 同口径：binary / 截断 / 空 patch / 正文）。 */
function DiffSectionBody({ detail }: { detail: DiffFileDetail }) {
  const binary = detail.binary === true;
  const patch = detail.patch ?? null;
  const blobRef = detail.blob_ref ?? null;
  if (binary) return <DiffText path={detail.path} binary patch={patch} blobRef={blobRef} />;
  if (patch === null) {
    return (
      <div
        data-testid="chat-file-oversize"
        className="rounded-md border border-dashed border-border-hover bg-surface px-3 py-3 text-center"
      >
        <p className="text-[length:var(--text-ui-xs)] text-foreground-subtle">
          文件过大，未内联展示（正文见审查标签）
        </p>
      </div>
    );
  }
  if (patch === '') {
    return (
      <p className="px-2 py-1.5 text-[length:var(--text-ui-xs)] text-foreground-subtle">
        无内容变更（仅元数据/文件模式变化）。
      </p>
    );
  }
  return <DiffText path={detail.path} binary={false} patch={patch} />;
}
