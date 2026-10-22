import { useMemo, useState } from 'react';

import type { DiffHunk, DiffLine, ParsedDiffFile } from '@/lib/diffParse';
import { parseDiff } from '@/lib/diffParse';

import { EmptyState } from './EmptyState';

/**
 * DiffText（M3 Phase 4 diff 审查地基）：渲染单个文件 patch 的解析结果。
 *
 * - 输入对齐契约 DiffFileDetail（path/binary/patch/blob_ref）；内部 useMemo 调
 *   parseDiff，畸形输入降级为错误态，不抛异常不白屏；
 * - 行渲染：双列行号（old|new）+ 前缀符（+/-）+ 内容；加/删行用 design token
 *   底色（bg-diff-added-bg / bg-diff-removed-bg），hunk 头用 diff-hunk 族；
 * - noNewline 行尾附淡色 git 原文标记「\ No newline at end of file」；
 * - **长文件性能取舍**：单 hunk 超过 HUNK_COLLAPSE_LINE_THRESHOLD 行默认折叠、
 *   按钮展开——不做虚拟列表（与滚动容器强耦合、依赖测量，收益配不上复杂度；
 *   折叠后最坏 ~阈值×2 行 DOM，浏览器与测试环境都无压力）。阈值导出供测试。
 */

/** 单 hunk 折叠阈值（行数）：超过默认折叠，点按钮展开 */
export const HUNK_COLLAPSE_LINE_THRESHOLD = 400;

export interface DiffTextProps {
  /** 文件路径（占位/错误态展示用） */
  readonly path: string;
  /** 契约 DiffFileDetail.binary：true 时渲染占位（patch 恒为 null） */
  readonly binary: boolean;
  /** 契约 DiffFileDetail.patch：unified diff 文本；binary 文件为 null */
  readonly patch: string | null;
  /** 契约 DiffFileDetail.blob_ref：binary 时的内容引用，有则展示 */
  readonly blobRef?: string | null;
}

const LINE_GRID =
  'grid grid-cols-[3.5rem_3.5rem_1.25rem_1fr] items-start font-mono text-[length:var(--code-font-size)] leading-[1.6]';

const GUTTER_CLS = 'select-none pr-2 text-right text-foreground-subtlest';

/** 行号单元格：null 渲染空（add 无 old 号 / del 无 new 号） */
function LineNo({ testid, no }: { testid: string; no: number | null }) {
  return (
    <span data-testid={testid} className={GUTTER_CLS}>
      {no === null ? '' : no}
    </span>
  );
}

function DiffLineRow({ line }: { line: DiffLine }) {
  const isAdd = line.kind === 'add';
  const isDel = line.kind === 'del';
  const rowCls = isAdd ? 'bg-diff-added-bg' : isDel ? 'bg-diff-removed-bg' : '';
  const prefixCls = isAdd ? 'text-diff-added' : isDel ? 'text-diff-removed' : 'text-foreground-subtlest';
  const prefix = isAdd ? '+' : isDel ? '-' : ' ';
  return (
    <div data-testid="diff-line" data-kind={line.kind} className={`${LINE_GRID} ${rowCls}`}>
      <LineNo testid="diff-ln-old" no={line.oldLineNo} />
      <LineNo testid="diff-ln-new" no={line.newLineNo} />
      <span aria-hidden="true" className={`select-none ${prefixCls}`}>
        {prefix}
      </span>
      <span className="min-w-0 whitespace-pre-wrap break-all text-foreground">
        {line.text}
        {line.noNewline && (
          <span
            data-testid="diff-no-newline"
            className="ml-2 text-foreground-subtlest text-[length:var(--text-ui-2xs)] italic"
          >
            {'\\ No newline at end of file'}
          </span>
        )}
      </span>
    </div>
  );
}

/** 单 hunk 块：头分隔行 + 行列表；超阈值 hunk 默认折叠（性能取舍见文件头注释） */
function DiffHunkBlock({ hunk }: { hunk: DiffHunk }) {
  const overThreshold = hunk.lines.length > HUNK_COLLAPSE_LINE_THRESHOLD;
  const [expanded, setExpanded] = useState(!overThreshold);
  return (
    <div data-testid="diff-hunk" data-collapsed={overThreshold && !expanded ? 'true' : 'false'}>
      <div
        data-testid="diff-hunk-header"
        className={`${LINE_GRID} bg-diff-hunk text-diff-hunk-fg`}
      >
        <span className="col-span-2 select-none pr-2 text-right opacity-80">@@</span>
        <span className="select-none">
          {hunk.header.slice(3, hunk.header.indexOf('@@', 2)).trimEnd()}
        </span>
        <span className="min-w-0 truncate" title={hunk.section ?? undefined}>
          {hunk.section !== null && <span data-testid="diff-section">{hunk.section}</span>}
        </span>
      </div>
      {overThreshold && !expanded ? (
        <button
          type="button"
          data-testid="diff-hunk-toggle"
          onClick={() => setExpanded(true)}
          className="w-full bg-surface px-3 py-1.5 text-left text-[length:var(--text-ui-xs)] text-foreground-subtle hover:bg-surface-hover hover:text-foreground"
        >
          展开 {hunk.lines.length} 行（已折叠以保持流畅）
        </button>
      ) : (
        hunk.lines.map((l, idx) => <DiffLineRow key={idx} line={l} />)
      )}
    </div>
  );
}

/** binary / patch 缺失占位；有 blob_ref 时一并展示引用 */
function BinaryPlaceholder({ path, blobRef }: { path: string; blobRef?: string | null }) {
  return (
    <div
      data-testid="diff-binary"
      className="rounded-md border border-dashed border-border-hover bg-surface px-3 py-4 text-center"
    >
      <p className="text-[length:var(--text-ui-sm)] text-foreground-subtle">
        二进制文件，无法展示文本 diff
      </p>
      <p className="mt-1 font-mono text-[length:var(--text-ui-xs)] text-foreground-subtlest">{path}</p>
      {blobRef !== null && blobRef !== undefined && blobRef !== '' && (
        <p className="mt-1 font-mono text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
          blob_ref: {blobRef}
        </p>
      )}
    </div>
  );
}

/** unparseable / 空 patch 的错误空态（不出行渲染，不白屏） */
function DiffErrorState({ description }: { description: string }) {
  return (
    <div data-testid="diff-error">
      <EmptyState
        icon={<span aria-hidden="true">⚠</span>}
        title="无法展示此 diff"
        description={description}
      />
    </div>
  );
}

export function DiffText({ path, binary, patch, blobRef }: DiffTextProps) {
  const parsed = useMemo(
    () => (patch === null ? null : parseDiff(patch)),
    [patch],
  );

  if (binary) {
    return <BinaryPlaceholder path={path} blobRef={blobRef} />;
  }
  if (parsed === null) {
    // 契约上 patch 为 null 仅当 binary；违反时按错误态降级而非白屏
    return <DiffErrorState description={`${path}：patch 缺失且未标记为二进制。`} />;
  }
  if (parsed.unparseable || parsed.files.length === 0) {
    return <DiffErrorState description={`${path}：diff 内容无法解析（格式异常或为空）。`} />;
  }

  // 契约 patch 为单文件段；files 多于一个仅出现在防御性场景，依序全部渲染
  return (
    <div data-testid="diff-text" className="overflow-x-auto">
      {parsed.files.map((file: ParsedDiffFile, fileIdx) => (
        <div key={`${file.path}-${fileIdx}`}>
          {file.unparseable ? (
            <DiffErrorState description={`${file.path || path}：该文件段解析失败（hunk 格式异常）。`} />
          ) : file.isModeChangeOnly ? (
            <p className="px-2 py-1.5 text-[length:var(--text-ui-xs)] text-foreground-subtle">
              仅文件模式（权限位）变更，无内容差异。
            </p>
          ) : (
            file.hunks.map((hunk, hunkIdx) => <DiffHunkBlock key={hunkIdx} hunk={hunk} />)
          )}
        </div>
      ))}
    </div>
  );
}
