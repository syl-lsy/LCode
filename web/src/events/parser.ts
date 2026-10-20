/**
 * SSE 行解析器（A4 §4.4；streaming-buffer 规则 2/4，frontend-pipeline §1）。
 *
 * 核心不变量：**不假设一个网络 chunk = 一个完整事件**。跨 chunk 的行缓冲 +
 * `TextDecoder(stream:true)` 保证「帧被任意字节边界切开（含多字节字符中间）」
 * 时解析结果与一次性喂入完全一致（parser.test.ts 的前缀一致性用例）。
 *
 * 信号模型（fetch 自研解析而非 EventSource 的理由，A4 §4.7）：
 * - `frame`：空行收口的完整帧（id/event 聚合 + 多行 data 以 \n 拼接）；
 * - `heartbeat`：注释行（契约 `: keep-alive`，15s 级）——不产帧，供上层回调/看门狗；
 * - `retry`：服务端建议的重连间隔（契约连接建立即发 `retry: 3000`）——上层作退避起点。
 *
 * `end()` 冲刷 decoder 残尾并显式报告残帧（流在帧中间被截断）——**不静默丢**，
 * 由调用方（sse.ts）记日志后走断线重连，缺口由服务端重放补齐（规则 3/24）。
 *
 * 与 WHATWG EventSource 规范的已知偏差（有意为之，契约帧形态保证安全）：
 * - 仅以 LF 切行并剥尾部 CR（契约服务端为 LF/CRLF；孤立 CR 行界不支持——A4 §4.4
 *   伪代码同口径）；
 * - id/event 按帧聚合、派发后整体重置（规范的「last event ID 跨帧持续」不适用于
 *   契约：每帧必带 id）；
 * - 不处理 id 值含 NUL 的规范边界（契约为十进制整数 seq）。
 */

/** 一个空行收口的完整 SSE 帧（data 已按多行语义以 \n 拼接）。 */
export interface SseFrame {
  /** `id:` 行原值（契约：十进制 seq 字符串）；帧未携带时为 undefined */
  readonly id?: string;
  /** `event:` 行原值（契约：事件 type，与 data.type 冗余） */
  readonly event?: string;
  /** `data:` 载荷（多行 data 以 \n 拼接；契约常态为单行 JSON） */
  readonly data: string;
}

/** push() 产出的信号：完整帧 / 注释心跳 / 服务端 retry 建议。 */
export type SseParserSignal =
  | { readonly kind: 'frame'; readonly frame: SseFrame }
  | { readonly kind: 'heartbeat' }
  | { readonly kind: 'retry'; readonly retryMs: number };

/** end() 的截断报告：truncated=true 时调用方必须显式记日志（不静默丢）。 */
export interface SseParserEndResult {
  /** 流结束时是否存在未收口内容（残行或未分派的半帧） */
  readonly truncated: boolean;
  /** 未被换行终结的残行内容（已含 decoder 冲刷出的替换符；无残行为空串） */
  readonly partialLine: string;
  /** 是否有帧已开始聚合字段（id/event/data 出现过但缺帧分界空行） */
  readonly frameStarted: boolean;
}

/** 聚合中的帧草稿（派发或 end() 后重置）。 */
interface FrameDraft {
  id: string | undefined;
  event: string | undefined;
  dataLines: string[];
}

/** SSE 规范：retry 值仅纯 ASCII 数字有效，其余忽略。 */
const RETRY_DIGITS = /^\d+$/;

const emptyDraft = (): FrameDraft => ({ id: undefined, event: undefined, dataLines: [] });

export class SseLineParser {
  private readonly decoder = new TextDecoder();
  /** 跨 chunk 行缓冲：只存放「尚未被换行终结」的尾段（frontend-pipeline §1）。 */
  private buffer = '';
  private draft: FrameDraft = emptyDraft();

  /**
   * 喂入一个网络 chunk，产出本 chunk 内完整收口的信号（可为空数组）。
   * chunk 边界可落在任意位置（帧中间/多字节字符中间）。
   */
  push(chunk: Uint8Array): SseParserSignal[] {
    // stream:true —— 不完整的多字节序列留在 decoder 内部等待下一 chunk（规则 4）
    this.buffer += this.decoder.decode(chunk, { stream: true });
    const lines = this.buffer.split('\n');
    this.buffer = lines.pop() ?? ''; // 最后一段可能是不完整行，留待下一 chunk
    const signals: SseParserSignal[] = [];
    for (const rawLine of lines) {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine; // 剥 CRLF 的 \r
      const signal = this.handleLine(line);
      if (signal) signals.push(signal);
    }
    return signals;
  }

  /**
   * 流结束时调用：冲刷 decoder 残尾并报告截断状态。残帧**不派发**（无空行收口
   * 即不完整事件），但必须显式返回给调用方记日志/走重连——不静默丢。
   * 调用后解析器归零（新连接应换新实例，本方法仅保证 end 幂等可重入）。
   */
  end(): SseParserEndResult {
    this.buffer += this.decoder.decode(); // 冲刷残尾：不完整多字节序列 → U+FFFD
    const frameStarted =
      this.draft.id !== undefined || this.draft.event !== undefined || this.draft.dataLines.length > 0;
    const result: SseParserEndResult = {
      truncated: this.buffer.length > 0 || frameStarted,
      partialLine: this.buffer,
      frameStarted,
    };
    this.buffer = '';
    this.draft = emptyDraft();
    return result;
  }

  /** 处理一个已终结的行，返回需要上抛的信号（字段聚合行返回 undefined）。 */
  private handleLine(line: string): SseParserSignal | undefined {
    if (line === '') return this.dispatch(); // 空行 = 帧分界
    if (line.startsWith(':')) return { kind: 'heartbeat' }; // 注释行（`: keep-alive`）
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon); // 无冒号：整行是字段名，值为空
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1); // 只剥一个前导空格（规范）
    switch (field) {
      case 'data':
        this.draft.dataLines.push(value);
        return undefined;
      case 'event':
        this.draft.event = value;
        return undefined;
      case 'id':
        this.draft.id = value;
        return undefined;
      case 'retry':
        return RETRY_DIGITS.test(value) ? { kind: 'retry', retryMs: Number(value) } : undefined;
      default:
        return undefined; // 未知字段名：按规范忽略（前向兼容）
    }
  }

  /** 空行触发：聚合非空则派发帧并重置草稿；无 data 的帧不派发（EventSource 语义）。 */
  private dispatch(): SseParserSignal | undefined {
    const draft = this.draft;
    this.draft = emptyDraft();
    if (draft.dataLines.length === 0) return undefined;
    const frame: SseFrame = { id: draft.id, event: draft.event, data: draft.dataLines.join('\n') };
    return { kind: 'frame', frame };
  }
}
