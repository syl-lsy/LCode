// @vitest-environment jsdom
/**
 * FilePreviewPane（changed-files.html 右栏文件预览标签；P0 只读占位，B3）：
 * - .crumb 面包屑分段（root / 目录 / 当前文件 cur）+ .v-body 占位正文；
 * - 「在编辑器中打开」「更多」disabled（LCode 无本地编辑器语义，P2 再启用）。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import '../../../tests/test-utils';

import { FilePreviewPane } from './FilePreviewPane';

describe('FilePreviewPane · 只读占位结构', () => {
  it('面包屑分段：root / 目录 / 当前文件（cur），正文标题为文件名', () => {
    render(<FilePreviewPane path="obs-demo/README.md" />);

    const pane = screen.getByTestId('file-preview-pane');
    expect(pane).toHaveTextContent('root');
    expect(pane).toHaveTextContent('obs-demo');
    expect(pane).toHaveTextContent('README.md');
    // 当前段 = 末段（crumb .cur 由 CSS 类承载，DOM 文案逐段在位）
    expect(pane.textContent).toContain('root/obs-demo/README.md');
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('README.md');
    // 占位正文如实声明（不伪造文件内容）
    expect(pane).toHaveTextContent('只读占位（P0）');
  });

  it('「在编辑器中打开」与「更多」均 disabled（P2 口径）', () => {
    render(<FilePreviewPane path="a/b.py" />);

    expect(screen.getByRole('button', { name: '在编辑器中打开' })).toBeDisabled();
    expect(screen.getByTitle('更多操作（P2）')).toBeDisabled();
  });

  it('无目录纯文件名路径：面包屑退化为 root / 文件名', () => {
    render(<FilePreviewPane path="hello.py" />);
    const pane = screen.getByTestId('file-preview-pane');
    expect(pane.textContent).toContain('root/hello.py');
  });
});
