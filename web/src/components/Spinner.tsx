/** 加载指示（tokens.css 的 .spinner 全局类；role="status" 供读屏与测试定位）。 */
export function Spinner({ label = '加载中' }: { label?: string }) {
  return <span className="spinner" role="status" aria-label={label} />;
}
