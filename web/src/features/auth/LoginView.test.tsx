// @vitest-environment jsdom
/**
 * 登录/欢迎页三状态占位（B7，对照 design/prototype/login.html）：
 * - 默认 welcome 三入口，apikey/wait 两状态隐藏（hidden 互斥）；
 * - 「使用 API key」→ 表单态（提供方 select + 密钥非空解锁「继续」）；
 * - 连接入口 → OAuth 等待态（脉冲环 + 提示文案）；取消/跳过回 welcome；
 * - 纯静态占位：不发任何网络请求（MSW onUnhandled error 兜底即红）。
 */
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { renderAtRoute } from '@/tests/test-utils';

describe('LoginView · 登录/欢迎页三状态（B7）', () => {
  it('默认 welcome：品牌区 + 三入口，apikey/wait 隐藏', () => {
    renderAtRoute('/login');
    expect(screen.getByTestId('login-page')).toBeInTheDocument();
    expect(screen.getByText('欢迎来到 LCode')).toBeInTheDocument();
    expect(screen.getByText('连接 Z.ai 继续使用')).toBeInTheDocument();
    expect(screen.getByText('连接 BigModel 继续使用')).toBeInTheDocument();
    expect(screen.getByText('使用 API key')).toBeInTheDocument();
    expect(screen.getByTestId('login-welcome')).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('login-apikey')).toHaveAttribute('hidden');
    expect(screen.getByTestId('login-wait')).toHaveAttribute('hidden');
  });

  it('入口「使用 API key」→ 表单态：密钥非空解锁「继续」，取消/跳过回 welcome', async () => {
    const user = userEvent.setup();
    renderAtRoute('/login');
    await user.click(screen.getByTestId('login-entry-2'));
    expect(screen.getByTestId('login-apikey')).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('login-welcome')).toHaveAttribute('hidden');

    // 原型 keyInput 行为：密钥为空时「继续」disabled，非空解锁
    expect(screen.getByTestId('login-continue')).toBeDisabled();
    await user.type(screen.getByLabelText('输入 API key'), 'sk-demo');
    expect(screen.getByTestId('login-continue')).toBeEnabled();
    expect(screen.getByLabelText('API key 提供方')).toHaveValue('BigModel'); // 原型默认首项

    await user.click(screen.getByTestId('login-apikey-cancel'));
    expect(screen.getByTestId('login-welcome')).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('login-apikey')).toHaveAttribute('hidden');
  });

  it('表单态「暂时跳过」回 welcome', async () => {
    const user = userEvent.setup();
    renderAtRoute('/login');
    await user.click(screen.getByTestId('login-entry-2'));
    await user.click(screen.getByTestId('login-skip'));
    expect(screen.getByTestId('login-welcome')).not.toHaveAttribute('hidden');
  });

  it('连接入口 → OAuth 等待态：文案 + 脉冲环，取消回 welcome', async () => {
    const user = userEvent.setup();
    renderAtRoute('/login');
    await user.click(screen.getByTestId('login-entry-0'));
    expect(screen.getByTestId('login-wait')).not.toHaveAttribute('hidden');
    expect(screen.getByTestId('login-welcome')).toHaveAttribute('hidden');
    expect(screen.getByText('等待 BigModel 完成认证...')).toBeInTheDocument();
    expect(screen.getByText('已唤起系统浏览器完成 OAuth')).toBeInTheDocument();
    await user.click(screen.getByTestId('login-wait-cancel'));
    expect(screen.getByTestId('login-welcome')).not.toHaveAttribute('hidden');
  });
});
