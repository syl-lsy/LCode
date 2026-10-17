import { Component, type ErrorInfo, type ReactNode } from 'react';

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * 全局错误边界（A4 §2.1 main.tsx）：兜底渲染崩溃，避免整页白屏。
 * 骨架期仅提供重载；M2 起接入 toast 上报与"恢复会话"动作（NFR-006 错误给人读）。
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[ErrorBoundary] 渲染崩溃：', error, info.componentStack);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <div
          role="alert"
          className="flex h-screen flex-col items-center justify-center gap-3 bg-background p-6"
        >
          <h1 className="text-[length:var(--text-ui-lg)] font-semibold text-foreground-strong">
            界面渲染出错
          </h1>
          <pre className="max-w-prose overflow-auto rounded-md border border-border bg-surface p-3 font-mono text-[length:var(--code-font-size)] text-foreground-subtle">
            {error.message}
          </pre>
          <button
            type="button"
            className="rounded-md border border-border bg-card px-3 py-1.5 text-[length:var(--text-ui-sm)] text-foreground hover:bg-surface-hover"
            onClick={() => window.location.reload()}
          >
            重新加载
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
