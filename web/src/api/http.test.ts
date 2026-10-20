/**
 * http.ts 单测（A4 §7.1）：信封双侧解包、ApiError 归一、zod 校验注入、baseURL 解析。
 *
 * 手法：注入 fetchImpl（fake），断言 URL 拼接/方法/头/体与错误归一路径。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';

import {
  ApiError,
  DEFAULT_API_BASE_URL,
  NETWORK_ERROR_CODE,
  PARSE_ERROR_CODE,
  request,
  resolveApiBaseUrl,
  SUCCESS_CODE,
} from './http';

type FakeFetch = (input: string, init: RequestInit) => Promise<Response>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function makeFetch(responder: () => Promise<Response>): { fetchImpl: FakeFetch; calls: { url: string; init: RequestInit }[] } {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchImpl: FakeFetch = async (url, init) => {
    calls.push({ url, init });
    return responder();
  };
  return { fetchImpl, calls };
}

beforeEach(() => {
  vi.stubEnv('VITE_API_BASE_URL', '');
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('resolveApiBaseUrl', () => {
  it('缺省回退 /api/v1', () => {
    expect(resolveApiBaseUrl({})).toBe(DEFAULT_API_BASE_URL);
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: undefined })).toBe('/api/v1');
  });

  it('非字符串/空串视为未配置（回退缺省）', () => {
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: 42 })).toBe('/api/v1');
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: '' })).toBe('/api/v1');
  });

  it('配置生效并归一末尾斜杠', () => {
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: 'https://api.example.com/v1/' })).toBe(
      'https://api.example.com/v1',
    );
    expect(resolveApiBaseUrl({ VITE_API_BASE_URL: '/api/test' })).toBe('/api/test');
  });
});

describe('request：成功信封', () => {
  it('解包 {code:"0",data} 并经 schema 校验；URL = baseURL + path + query', async () => {
    const { fetchImpl, calls } = makeFetch(async () =>
      jsonResponse({ code: SUCCESS_CODE, message: 'ok', data: { id: 's1', title: 't' } }),
    );
    const schema = z.object({ id: z.string(), title: z.string() });

    const data = await request({
      path: '/sessions/s1',
      schema,
      query: { title: '登录 超时', empty: null, skip: undefined, n: 3 },
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    expect(data).toEqual({ id: 's1', title: 't' });
    // URLSearchParams 的 application/x-www-form-urlencoded 口径：空格编码为 +
    expect(calls[0]?.url).toBe(
      `/api/v1/sessions/s1?title=${encodeURIComponent('登录 超时').replace(/%20/g, '+')}&n=3`,
    );
    expect(calls[0]?.init.method).toBe('GET');
    expect((calls[0]?.init.headers as Record<string, string>).Accept).toBe('application/json');
  });

  it('POST：请求体序列化为 JSON 并带 Content-Type', async () => {
    const { fetchImpl, calls } = makeFetch(async () =>
      jsonResponse({ code: SUCCESS_CODE, message: null, data: { ok: true } }),
    );

    await request({
      path: '/sessions/s1/messages',
      method: 'POST',
      body: { content: '你好' },
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    expect(calls[0]?.init.method).toBe('POST');
    expect(calls[0]?.init.body).toBe(JSON.stringify({ content: '你好' }));
    expect((calls[0]?.init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
  });

  it('204 / 空 body 且无 schema → 返回 undefined（DELETE 语义）', async () => {
    const { fetchImpl } = makeFetch(async () => new Response(null, { status: 204 }));

    const result = await request<void>({
      path: '/sessions/s1',
      method: 'DELETE',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(result).toBeUndefined();
  });

  it('未提供 schema 时原样返回 data（不做契约校验的端点）', async () => {
    const { fetchImpl } = makeFetch(async () =>
      jsonResponse({ code: SUCCESS_CODE, message: null, data: { anything: true } }),
    );
    const data = await request<{ anything: boolean }>({
      path: '/x',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    expect(data).toEqual({ anything: true });
  });
});

describe('request：错误归一', () => {
  it('错误信封 {code,message} → ApiError（code=服务端错误码，带 httpStatus）', async () => {
    const { fetchImpl } = makeFetch(async () =>
      jsonResponse({ code: 'RUN_ALREADY_ACTIVE', message: '已有运行中任务' }, 409),
    );

    const error = await request({
      path: '/sessions/s1/messages',
      method: 'POST',
      body: { content: 'x' },
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: 'RUN_ALREADY_ACTIVE', message: '已有运行中任务', httpStatus: 409 });
  });

  it('非信封错误体（网关裸文本）→ 退化 HTTP_<status> 码，原文进 message', async () => {
    const { fetchImpl } = makeFetch(async () => new Response('Bad Gateway', { status: 502 }));

    const error = await request({
      path: '/x',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }).catch((e: unknown) => e);

    expect(error).toMatchObject({ code: 'HTTP_502', message: 'Bad Gateway', httpStatus: 502 });
  });

  it('2xx 但 code 非 "0" → 按错误处理（信封实现偏差防御）', async () => {
    const { fetchImpl } = makeFetch(async () =>
      jsonResponse({ code: 'INTERNAL', message: 'boom', data: null }, 200),
    );

    const error = await request({ path: '/x', fetchImpl: fetchImpl as unknown as typeof fetch }).catch(
      (e: unknown) => e,
    );
    expect(error).toMatchObject({ code: 'INTERNAL', httpStatus: 200 });
  });

  it('网络失败 → NETWORK_ERROR（原始错误挂 cause）', async () => {
    const cause = new Error('ECONNREFUSED');
    const fetchImpl: FakeFetch = async () => {
      throw cause;
    };

    const error = await request({ path: '/x', fetchImpl: fetchImpl as unknown as typeof fetch }).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ code: NETWORK_ERROR_CODE });
    expect((error as ApiError).cause).toBe(cause);
  });

  it('调用方 abort 原样透传（取消不是错误，按名字识别）', async () => {
    const abort = new DOMException('aborted', 'AbortError');
    const fetchImpl: FakeFetch = async () => {
      throw abort;
    };

    await expect(
      request({ path: '/x', fetchImpl: fetchImpl as unknown as typeof fetch }),
    ).rejects.toBe(abort);
  });

  it('响应 data 未过 schema → PARSE_ERROR（边界防御）', async () => {
    const { fetchImpl } = makeFetch(async () =>
      jsonResponse({ code: SUCCESS_CODE, message: null, data: { id: 42 } }),
    );

    const error = await request({
      path: '/x',
      schema: z.object({ id: z.string() }),
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }).catch((e: unknown) => e);

    expect(error).toMatchObject({ code: PARSE_ERROR_CODE, httpStatus: 200 });
  });

  it('响应不是合法 JSON → PARSE_ERROR', async () => {
    const { fetchImpl } = makeFetch(async () => new Response('<html>', { status: 200 }));

    const error = await request({ path: '/x', fetchImpl: fetchImpl as unknown as typeof fetch }).catch(
      (e: unknown) => e,
    );
    expect(error).toMatchObject({ code: PARSE_ERROR_CODE });
  });

  it('空 body 但给了 schema → PARSE_ERROR（期望解包却无载荷）', async () => {
    const { fetchImpl } = makeFetch(async () => new Response(null, { status: 200 }));

    const error = await request({
      path: '/x',
      schema: z.object({ id: z.string() }),
      fetchImpl: fetchImpl as unknown as typeof fetch,
    }).catch((e: unknown) => e);
    expect(error).toMatchObject({ code: PARSE_ERROR_CODE });
  });

  it('path 不以 / 开头 → 直接拒绝（请求目标只允许相对路径）', async () => {
    await expect(
      request({ path: 'https://evil.example.com/x', fetchImpl: (async () => new Response(null)) as unknown as typeof fetch }),
    ).rejects.toMatchObject({ code: 'INVALID_ARGUMENT' });
  });
});
