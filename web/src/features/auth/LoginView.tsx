import { useState } from 'react';

import { Icon, type IconName } from '@/components/Icon';

// 登录/欢迎页三状态页面级样式（来源 design/prototype/login.html）
import './login.css';

/**
 * 登录/欢迎页（B7，对照 design/prototype/login.html 三状态 1:1）：
 *   welcome 三入口 → apikey 表单 → wait OAuth 等待态，本地 state 切换演示。
 *
 * LCode 裁剪决策（ia-notes §4）：鉴权 P2 前不实现——本页纯静态占位，
 * 不接管导航逻辑、不发认证请求：入口/继续钮仅切换本地状态或空操作；
 * 「继续」按原型口径在密钥非空时解锁，点击为占位（title 轻提示）。
 * 独立全屏页，不入 AppShell 三栏；直接 URL（/login）可达。
 */

type LoginState = 'welcome' | 'apikey' | 'wait';

/** 三入口（login.html 原文文案；icon 为 Icon.tsx 名称，path 以原型内联 SVG 为事实源） */
const ENTRIES: readonly {
  readonly title: string;
  readonly sub: string;
  readonly icon: IconName;
  readonly target: LoginState;
}[] = [
  { title: '连接 Z.ai 继续使用', sub: '全球', icon: 'globe', target: 'wait' },
  { title: '连接 BigModel 继续使用', sub: '中国', icon: 'clock', target: 'wait' },
  { title: '使用 API key', sub: 'api_key_env · 密钥只进环境变量', icon: 'key', target: 'apikey' },
];

/** API key 提供方选项（login.html select 原文） */
const PROVIDERS: readonly string[] = ['BigModel', 'DeepSeek', 'Kimi', 'OpenAI 兼容'];

export function LoginView() {
  const [state, setState] = useState<LoginState>('welcome');
  /** API key 提供方（受控 select；原型默认第一项 BigModel） */
  const [provider, setProvider] = useState('BigModel');
  /** 密钥输入（仅驱动「继续」解锁，不外发——占位页无认证请求） */
  const [apiKey, setApiKey] = useState('');

  return (
    /* 原型 body 居中布局的页面根等价（padding 24px = p-6） */
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="stage" data-testid="login-page">
        <div className="brand">
          <div className="logo">L</div>
          <h1>欢迎来到 LCode</h1>
          <p>连接账号，开始使用 LCode</p>
        </div>

        {/* 状态 1：三入口 */}
        <div className="entries" data-testid="login-welcome" hidden={state !== 'welcome'}>
          {ENTRIES.map((entry, index) => (
            <button
              key={entry.title}
              type="button"
              className="entry"
              data-testid={`login-entry-${index}`}
              onClick={() => setState(entry.target)}
            >
              <Icon name={entry.icon} size={17} className="ic" />
              <span className="t">
                <b>{entry.title}</b>
                <span>{entry.sub}</span>
              </span>
              <Icon name="chevron-right" size={12} className="text-foreground-subtlest" />
            </button>
          ))}
        </div>

        {/* 状态 2：API key 表单 */}
        <div className="card" data-testid="login-apikey" hidden={state !== 'apikey'}>
          <h2>API Key</h2>
          <div className="field">
            <label htmlFor="login-provider">API key 提供方</label>
            <div className="ctl">
              <select
                id="login-provider"
                value={provider}
                onChange={(event) => setProvider(event.target.value)}
              >
                {PROVIDERS.map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label htmlFor="login-key-input">输入 API key</label>
            <div className="ctl">
              <input
                id="login-key-input"
                type="password"
                placeholder="sk-..."
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
              />
            </div>
            <div className="hint">LCode：密钥只进环境变量（api_key_env 引用），不入配置文件。</div>
          </div>
          <div className="row">
            <button type="button" className="btn" data-testid="login-apikey-cancel" onClick={() => setState('welcome')}>
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="login-continue"
              disabled={apiKey === ''}
              title="鉴权 P2 前不实现：本页为纯静态占位，不发起认证"
            >
              继续
            </button>
          </div>
          <div className="skip">
            <button type="button" data-testid="login-skip" onClick={() => setState('welcome')}>
              暂时跳过
            </button>
          </div>
        </div>

        {/* 状态 3：OAuth 等待态 */}
        <div className="card" data-testid="login-wait" hidden={state !== 'wait'}>
          <div className="wait">
            <div className="pulse-ring" aria-hidden="true" />
            <p>等待 BigModel 完成认证...</p>
            <div className="hint">已唤起系统浏览器完成 OAuth</div>
            <button
              type="button"
              className="btn"
              style={{ minWidth: 120 }}
              data-testid="login-wait-cancel"
              onClick={() => setState('welcome')}
            >
              取消
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
