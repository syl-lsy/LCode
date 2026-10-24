import { useState } from 'react';

import type { ChatDiffCardItem } from '../store';

import { useDiffStore, useWorkbenchStore } from '@/features/workbench/store';
import { Icon } from '@/components/Icon';

import { FilesDiffSection } from './FilesDiffSection';

/**
 * 改动文件卡（A4 §8.1 MessageList diffcard 行；changed-files.html .files-card）。
 *
 * 行结构（对照原型逐行）：主文件行（文件图标 + 主文件名 + 文件数徽标 + 打开 /
 * 选择打开方式）→ 展开开关行（chevron + 展开已更改文件 + N 个文件已更改 + +N -N
 * 徽标）→ 撤销行（撤销钮 + 说明）；展开后为 `.diff-view`：每文件一个 `.file-sec`
 * （FilesDiffSection 懒加载单文件 patch，.file-head 对齐原型）。
 *
 * - 「打开」→ 右栏审查标签（既有行为保留；data-testid=diff-card-open）；
 * - 「选择打开方式」→ 右栏文件预览标签（P0 只读占位，placeholder 标签
 *   `file-preview:<path>`；无已知文件时禁用）；
 * - 「撤销」P0 禁用占位（撤销接口未接入；后续轮次置灰语义见原型 note）；
 * - 展开明细数据来自 diff store（diff_ready 全量快照；store 缺该 review 时
 *   如实提示已归档，不伪造文件列表——F15 更改追踪诚实性）；
 * - 根节点保留 data-testid="diff-card" + data-verdict（MessageList 既有定位契约）。
 */

/** 路径 → { 文件名, 目录（含尾斜杠） }。 */
function splitPath(path: string): { readonly name: string; readonly dir: string | null } {
  const idx = path.lastIndexOf('/');
  if (idx < 0) return { name: path, dir: null };
  return { name: path.slice(idx + 1), dir: path.slice(0, idx + 1) };
}

const FILE_PREVIEW_PREFIX = 'file-preview:';

export function DiffCardRow({ item }: { item: ChatDiffCardItem }) {
  const openTab = useWorkbenchStore((s) => s.openTab);
  const review = useDiffStore((s) => s.reviews.get(item.id));
  const [expanded, setExpanded] = useState(false);

  const primary = review?.files[0] ?? null;
  const primaryName = primary === null ? null : splitPath(primary.path).name;
  const primaryPath = primary?.path ?? null;

  return (
    <div className="files-card mb-2.5" data-testid="diff-card" data-verdict={item.verdict}>
      {/* 主文件行：打开（审查标签）/ 选择打开方式（文件预览占位） */}
      <div className="fc-row">
        <Icon name="file-text" size={14} className="text-foreground-subtlest" />
        <span className="fc-name" title={primaryPath ?? undefined}>
          {primaryName ?? `改动文件（第 ${item.round} 轮）`}
        </span>
        <span className="badge flex-none">{item.fileCount} 个文件</span>
        {item.verdict !== 'pending' && (
          <span data-testid="diff-card-verdict" className="badge flex-none">
            {item.verdict === 'accepted' ? '已接受' : '已驳回'}
          </span>
        )}
        <span className="grow" aria-hidden="true" />
        <button
          type="button"
          data-testid="diff-card-open"
          className="btn btn-sm flex-none"
          onClick={() => openTab({ kind: 'review', id: item.id, title: `审查 · 第 ${item.round} 轮` })}
        >
          打开
        </button>
        <button
          type="button"
          data-testid="diff-card-open-with"
          className="btn btn-sm btn-ghost flex-none"
          disabled={primaryPath === null}
          title={primaryPath === null ? '改动明细加载后可用' : '以文件预览方式打开（P0 只读占位）'}
          onClick={() => {
            if (primaryPath !== null) {
              openTab({
                kind: 'placeholder',
                id: `${FILE_PREVIEW_PREFIX}${primaryPath}`,
                title: primaryName ?? primaryPath,
              });
            }
          }}
        >
          选择打开方式
        </button>
      </div>

      {/* 展开开关行：chevron + 文件计数 + +N -N */}
      <button
        type="button"
        data-testid="diff-card-expand"
        aria-expanded={expanded}
        className={`fc-row fc-exp ${expanded ? 'open' : ''}`}
        onClick={() => setExpanded((v) => !v)}
      >
        <span className="chev" aria-hidden="true">
          <Icon name="chevron-down" size={11} />
        </span>
        展开已更改文件
        <span className="grow" aria-hidden="true" />
        <span className="flex-none">{item.fileCount} 个文件已更改</span>
        <span className="badge badge-add flex-none">+{item.additionsTotal}</span>
        <span className="badge badge-del flex-none">-{item.deletionsTotal}</span>
      </button>

      {/* 撤销行：P0 禁用占位（撤销接口未接入） */}
      <div className="fc-row" style={{ paddingTop: 8, paddingBottom: 8 }}>
        <button
          type="button"
          data-testid="diff-card-undo"
          className="btn btn-sm btn-ghost flex-none"
          disabled
          title="P0 占位：撤销接口未接入（撤销本轮全部改动，后续轮次中置灰）"
          style={{ color: 'var(--color-destructive)' }}
        >
          <Icon name="undo" size={11} /> 撤销
        </button>
        <span className="text-[length:var(--text-ui-2xs)] text-foreground-subtlest">
          撤销本轮全部改动
        </span>
      </div>

      {/* 展开明细：每文件一个 file-sec（FilesDiffSection 懒加载 patch，样式对齐原型） */}
      {expanded &&
        (review !== undefined ? (
          <div className="diff-view" data-testid="diff-card-files">
            {review.files.map((file) => (
              <FilesDiffSection key={file.path} reviewId={item.id} file={file} />
            ))}
          </div>
        ) : (
          <div className="diff-view">
            <p className="px-3 py-2 text-[length:var(--text-ui-xs)] text-foreground-subtle">
              改动明细已归档，点击「打开」在审查标签中查看。
            </p>
          </div>
        ))}
    </div>
  );
}
