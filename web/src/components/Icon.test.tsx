// @vitest-environment jsdom
/**
 * Icon 统一描边图标组件（B0 基础渲染测试）：
 * - 全量图标名可渲染（不崩、svg 存在、aria-hidden）；
 * - 描边规范对齐原型 .ic（fill none / stroke currentColor / viewBox 24）；
 * - size 注入内联宽高；className 透传。
 */
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { GLYPHS, Icon, type IconName } from './Icon';
import '../tests/test-utils';

function renderIcon(name: IconName, props: Omit<{ name: IconName; size?: number; className?: string }, 'name'> = {}): SVGSVGElement {
  const { container } = render(<Icon name={name} {...props} />);
  const svg = container.querySelector('svg');
  if (svg === null) throw new Error(`图标 ${name} 未渲染出 svg`);
  return svg as unknown as SVGSVGElement; // 保持挂载，由 testing-library 自动清理
}

describe('Icon · 基础渲染', () => {
  it('全部图标名均可渲染：svg 存在且 aria-hidden', () => {
    const names = Object.keys(GLYPHS) as IconName[];
    expect(names.length).toBeGreaterThanOrEqual(20); // B0 首批清单完整性兜底
    for (const name of names) {
      const svg = renderIcon(name);
      expect(svg).toHaveAttribute('aria-hidden', 'true');
      expect(svg).toHaveAttribute('viewBox', '0 0 24 24');
    }
  });

  it('描边规范：fill=none、stroke=currentColor、stroke-width 1.7、round cap/join', () => {
    const svg = renderIcon('search');
    expect(svg).toHaveAttribute('fill', 'none');
    expect(svg).toHaveAttribute('stroke', 'currentColor');
    expect(svg).toHaveAttribute('stroke-width', '1.7');
    expect(svg).toHaveAttribute('stroke-linecap', 'round');
    expect(svg).toHaveAttribute('stroke-linejoin', 'round');
  });

  it('size 注入内联宽高（可覆盖类宽高，默认 14）', () => {
    expect(renderIcon('close').style.width).toBe('14px');
    expect(renderIcon('close').style.height).toBe('14px');
    expect(renderIcon('close', { size: 10 }).style.width).toBe('10px');
    expect(renderIcon('close', { size: 10 }).style.height).toBe('10px');
  });

  it('className 透传到 svg（如 .chev 着色钩子）', () => {
    expect(renderIcon('chevron-down', { className: 'chev' })).toHaveClass('chev');
  });
});
