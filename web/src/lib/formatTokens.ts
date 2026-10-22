/**
 * token 数量的人读格式化（AC-010-1 压缩提示；成本/配额展示可复用）。
 *
 * 规则：≥1M 用 M、≥1k 用 k（一位小数、去尾 0）；千位以下原样。
 * 例：9836 → "9.8k"、49970 → "50k"、1250000 → "1.3M"。
 */
export function formatTokens(count: number): string {
  if (!Number.isFinite(count)) return String(count);
  if (count >= 1_000_000) return `${trimZero(count / 1_000_000)}M`;
  if (count >= 1_000) return `${trimZero(count / 1_000)}k`;
  return String(count);
}

/** 一位小数 + 修剪 ".0" 尾巴（49.97 → "50"，9.836 → "9.8"）。 */
function trimZero(value: number): string {
  const fixed = value.toFixed(1);
  return fixed.endsWith('.0') ? fixed.slice(0, -2) : fixed;
}
