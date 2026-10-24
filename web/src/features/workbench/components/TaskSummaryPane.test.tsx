// @vitest-environment jsdom
/**
 * 任务名标签内容（F25/R3§G；side-panels.html tp-任务名 占位块）：
 * 任务概要卡（真实标题 + 骨架占位）+ 调用轨迹占位 + 待实测诚实注记。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import '../../../tests/test-utils';

import { TaskSummaryPane } from './TaskSummaryPane';

describe('TaskSummaryPane · 任务名标签占位块（F25）', () => {
  it('任务概要卡带真实标题 + 调用轨迹占位 + 待实测注记（不伪造数据）', () => {
    render(<TaskSummaryPane title="在 /root/obs-demo 目录创建 hello.py" />);

    expect(screen.getByTestId('task-summary-pane')).toBeInTheDocument();
    expect(screen.getByText('任务概要')).toBeInTheDocument();
    expect(screen.getByText('在 /root/obs-demo 目录创建 hello.py')).toHaveClass('ttitle');
    expect(screen.getByText('调用轨迹')).toBeInTheDocument();
    expect(screen.getByText(/时间线占位/)).toBeInTheDocument();
    // F15 诚实性：待实测注记明示占位性质；徽标为中性占位（无伪造计数）
    expect(screen.getByText(/本块为任务概要\/调用轨迹占位/)).toBeInTheDocument();
    expect(screen.getByText('任务名 · F25 占位')).toBeInTheDocument();
    expect(screen.queryByText('+58')).not.toBeInTheDocument();
  });
});
