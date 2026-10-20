import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { ErrorBoundary } from './components/ErrorBoundary';
import { RouterRoot } from './router';
import { initUiDom, useUiStore } from './stores/uiStore';

import './styles/tailwind.css';

// 防 FOUC（A4 §9.4）：React 挂载前把持久化的主题/双字号落到 DOM（html.dark 类 +
// 根元素 CSS 变量）。初始值与 uiStore 模块加载时的读取是同一套逻辑（单一事实源）。
const { theme, uiFontSize, codeFontSize } = useUiStore.getState();
initUiDom(theme, uiFontSize, codeFontSize);

const container = document.getElementById('root');
if (!container) {
  throw new Error('缺少 #root 挂载容器（index.html 损坏）');
}

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <RouterRoot />
    </ErrorBoundary>
  </StrictMode>,
);
