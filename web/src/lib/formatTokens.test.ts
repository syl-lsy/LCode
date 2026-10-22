/**
 * formatTokens 单测（AC-010-1：压缩提示的 token 数值必须 k/M 人读格式）。
 */
import { describe, expect, it } from 'vitest';

import { formatTokens } from './formatTokens';

describe('formatTokens：k/M 人读格式（AC-010-1）', () => {
  it('千位以下原样输出', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(999)).toBe('999');
  });

  it('千位以上一位小数 k，尾 0 修剪', () => {
    expect(formatTokens(1000)).toBe('1k');
    expect(formatTokens(9836)).toBe('9.8k');
    expect(formatTokens(12000)).toBe('12k');
  });

  it('百万位以上一位小数 M，尾 0 修剪', () => {
    expect(formatTokens(1_000_000)).toBe('1M');
    expect(formatTokens(1_250_000)).toBe('1.3M');
  });

  it('fixture 实测组合（run_status_compact：49970 → 9836）', () => {
    expect(formatTokens(49970)).toBe('50k');
    expect(formatTokens(9836)).toBe('9.8k');
  });
});
