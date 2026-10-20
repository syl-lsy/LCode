/**
 * REST 请求唯一出口（A4 §7.1；基座铁律 1：组件与 store 禁止散写 URL、禁止裸 fetch）。
 *
 * 职责：信封解包（成功 `{code:"0",message,data}` / 错误 `{code,message}`，契约
 * contracts/src/lcode_contracts/rest.py 模块 docstring）、ApiError 归一、zod 校验注入、
 * baseURL 解析（VITE_API_BASE_URL，缺省回退 `/api/v1`）。
 *
 * 错误归一（§7.1）：
 * - 错误信封 → `ApiError(code = 服务端 RestErrorCode)`；
 * - 网络失败/超时 → `ApiError('NETWORK_ERROR')`；
 * - 响应 data 未过 zod → `ApiError('PARSE_ERROR')`；
 * - 调用方主动 abort（AbortSignal）**原样抛出 DOMException AbortError**：取消不是错误，
 *   调用方（切换会话/卸载）需要按名字识别并静默处理。
 *
 * 密钥永不进前端：本模块只读 `VITE_API_BASE_URL`（web/.env.example 唯一变量）。
 * 请求目标只允许同源相对路径（`path` 由本模块拼接 baseURL）——不接收绝对 URL，
 * 从源头避免把前端变成任意地址的请求代理。
 *
 * 注：`RestErrorCode` 未生成 TS 侧（contracts/generated/ts 无 errors 域），故 code
 * 按 string 透传；前端只做「原样上报 + 分类展示」，不复制错误码注册表（防第二事实源）。
 */

import { z } from 'zod';

/** 缺省 baseURL（A4 §7.1；dev 走 vite 同源代理） */
export const DEFAULT_API_BASE_URL = '/api/v1';

/** 成功信封的 code 恒为 "0"（contracts errors.SUCCESS_CODE） */
export const SUCCESS_CODE = '0';

/** 合成错误码：网络层失败（非服务端信封） */
export const NETWORK_ERROR_CODE = 'NETWORK_ERROR';
/** 合成错误码：响应体不是合法 JSON 或缺必填字段 */
export const PARSE_ERROR_CODE = 'PARSE_ERROR';

export interface ApiErrorInit {
  readonly httpStatus?: number;
  readonly cause?: unknown;
}

/** REST 层统一错误（code = 服务端 RestErrorCode 或合成码）。 */
export class ApiError extends Error {
  readonly code: string;
  readonly httpStatus?: number;

  constructor(code: string, message: string, init: ApiErrorInit = {}) {
    super(message, init.cause === undefined ? undefined : { cause: init.cause });
    this.name = 'ApiError';
    this.code = code;
    this.httpStatus = init.httpStatus;
  }
}

const successEnvelopeSchema = z.object({
  code: z.string(),
  message: z.string().nullish(),
  data: z.unknown(),
});

const errorEnvelopeSchema = z.object({
  code: z.string(),
  message: z.string(),
});

/** baseURL 解析：VITE_API_BASE_URL（非空字符串）→ 缺省 `/api/v1`；末尾斜杠归一。 */
export function resolveApiBaseUrl(env?: Record<string, unknown>): string {
  const source: Record<string, unknown> =
    env ?? (import.meta.env as unknown as Record<string, unknown>);
  const parsed = z.string().min(1).safeParse(source.VITE_API_BASE_URL);
  const raw = parsed.success ? parsed.data : DEFAULT_API_BASE_URL;
  return raw.endsWith('/') ? raw.slice(0, -1) : raw;
}

export type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE';

export type QueryValue = string | number | boolean | undefined | null;

export interface RequestOptions<T> {
  /** 相对路径（以 `/` 开头；不含 baseURL），如 `/sessions/${id}` */
  readonly path: string;
  readonly method?: HttpMethod;
  /** 请求 DTO（类型来自 @contracts；序列化为 JSON） */
  readonly body?: unknown;
  /** 响应 data 的 zod schema（来自 @contracts；缺省只解信封不校验 data） */
  readonly schema?: z.ZodType<T>;
  readonly query?: Record<string, QueryValue>;
  readonly signal?: AbortSignal;
  /** 注入 fetch（测试用；默认全局 fetch） */
  readonly fetchImpl?: typeof fetch;
}

function buildUrl(baseUrl: string, path: string, query?: Record<string, QueryValue>): string {
  if (!path.startsWith('/')) {
    throw new ApiError('INVALID_ARGUMENT', `api 路径必须以 / 开头（收到 "${path}"）`);
  }
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null) continue;
    search.append(key, String(value));
  }
  const qs = search.toString();
  return `${baseUrl}${path}${qs === '' ? '' : `?${qs}`}`;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

async function readTextSafely(response: Response): Promise<string> {
  try {
    return await response.text();
  } catch (error) {
    throw new ApiError(PARSE_ERROR_CODE, '响应体读取失败', {
      httpStatus: response.status,
      cause: error,
    });
  }
}

function parseJsonBody(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new ApiError(PARSE_ERROR_CODE, '响应不是合法 JSON', { cause: error });
  }
}

/** 容错版：错误分支里非 JSON 错误体（网关裸文本）按原文处理，不抛 PARSE_ERROR。 */
function tryParseJsonBody(text: string): unknown | undefined {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/**
 * 发起一次 REST 请求（唯一 fetch 出口）。成功返回解包后的 data（经 schema 校验）。
 * 请求目标只允许相对路径，与 baseURL 拼接后交给 fetch。
 */
export async function request<T>(options: RequestOptions<T>): Promise<T> {
  const { path, method = 'GET', body, schema, query, signal, fetchImpl } = options;
  const url = buildUrl(resolveApiBaseUrl(), path, query);
  const doFetch = fetchImpl ?? ((input: string, init: RequestInit) => fetch(input, init));

  const init: RequestInit = { method, headers: { Accept: 'application/json' }, signal };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)['Content-Type'] = 'application/json';
  }

  let response: Response;
  try {
    response = await doFetch(url, init);
  } catch (error) {
    if (isAbortError(error)) throw error; // 取消不是错误：按名字透传
    throw new ApiError(NETWORK_ERROR_CODE, `网络请求失败（${method} ${path}）`, { cause: error });
  }

  const text = await readTextSafely(response);

  if (!response.ok) {
    // 错误侧信封 `{code,message}`；非信封体（网关裸文本/非 JSON）退化为 HTTP_<status>，
    // 原文进 message——错误路径上不再抛 PARSE_ERROR（裸文本是网络层事实，不是解析事故）
    let code = `HTTP_${response.status}`;
    let message = text === '' ? `请求失败（HTTP ${response.status}）` : text;
    if (text !== '') {
      const parsed = errorEnvelopeSchema.safeParse(tryParseJsonBody(text));
      if (parsed.success) {
        code = parsed.data.code;
        message = parsed.data.message;
      }
    }
    throw new ApiError(code, message, { httpStatus: response.status });
  }

  // 无 body 端点（DELETE / 204）直接返回 undefined
  if (response.status === 204 || text === '') {
    if (schema !== undefined) {
      throw new ApiError(PARSE_ERROR_CODE, '响应缺少响应体（期望按 schema 解包）', {
        httpStatus: response.status,
      });
    }
    return undefined as T;
  }

  const envelope = successEnvelopeSchema.safeParse(parseJsonBody(text));
  if (!envelope.success) {
    throw new ApiError(PARSE_ERROR_CODE, '成功响应信封格式不符（{code,message,data}）', {
      httpStatus: response.status,
    });
  }
  if (envelope.data.code !== SUCCESS_CODE) {
    // 2xx 但 code 非 "0"：按错误处理（防御服务端信封实现偏差）
    throw new ApiError(envelope.data.code, envelope.data.message ?? '请求失败', {
      httpStatus: response.status,
    });
  }

  if (schema === undefined) return envelope.data.data as T;
  const result = schema.safeParse(envelope.data.data);
  if (!result.success) {
    throw new ApiError(PARSE_ERROR_CODE, '响应 data 未通过契约 schema 校验', {
      httpStatus: response.status,
      cause: result.error,
    });
  }
  return result.data;
}

/** 端点模块共用的可选参数（abort 传播 + 测试注入 fetch）。 */
export interface CallOptions {
  readonly signal?: AbortSignal;
  readonly fetchImpl?: typeof fetch;
}
