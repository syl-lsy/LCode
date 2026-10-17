import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import App from './App';
import { ErrorBoundary } from './components/ErrorBoundary';

import './styles/tailwind.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('缺少 #root 挂载容器（index.html 损坏）');
}

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
