import { ChatPanel } from '@/features/chat/components/ChatPanel';
import { SessionsPanel } from '@/features/sessions/components/SessionsPanel';
import { WorkbenchPanel } from '@/features/workbench/components/WorkbenchPanel';

/**
 * 三栏 AppShell 占位布局（A4 §9.1，R3 app-shell）：
 *   [sidebar var(--sidebar-w,260px)] [main minmax(0,1fr)] [panel var(--panel-w,0)]
 *
 * 骨架期仅静态占位；M2 任务 2 接入：路由（/ 与 /s/:sessionId）、右面板拖拽/收起
 * （workbenchStore）、侧栏折叠（uiStore）与 <1024px 降级（A4 §9.2）。
 * approval / models 两个 feature 壳的挂载点在 M2 任务 7 / M3 审批任务接入。
 */
export default function App() {
  return (
    <div className="grid h-screen grid-cols-[var(--sidebar-w,260px)_minmax(0,1fr)_var(--panel-w,0px)] overflow-hidden bg-background text-foreground">
      <aside className="min-w-0 overflow-y-auto border-r border-border bg-sidebar">
        <SessionsPanel />
      </aside>
      <main className="min-w-0 overflow-hidden">
        <ChatPanel />
      </main>
      <section className="min-w-0 overflow-hidden border-l border-border bg-panel">
        <WorkbenchPanel />
      </section>
    </div>
  );
}
