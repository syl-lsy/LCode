/**
 * unified diff 解析器（M3 Phase 4 diff 审查地基，纯函数零依赖）。
 *
 * 输入 `DiffFileDetail.patch`（后端 git 输出的 unified diff 文本，通常单文件；
 * 也兼容整段多文件 `git diff` 输出，files 数组即为此保留）。
 *
 * 取舍说明：
 * - **行号推进规则（钉死）**：context 双推进 old/new；add 只推进 new；del 只推进
 *   old；`\ No newline at end of file` 是标记不是内容行——挂到紧邻前一行
 *   （old/new 双侧末行同时无换行的语义）、不吞行、不推进任何行号。
 * - **hunk 行数按声明计数消费**：hunk 头声明了 old/new 行数，消费满即止，然后
 *   回到外层扫描。这天然化解「删除行内容以 `-` 开头 / 上下文行以空格开头」与
 *   下一文件头（`--- `/`+++ `）的前缀歧义，不靠猜。
 * - **畸形输入不抛异常**：空 patch / 头后无 hunk / 畸形 @@ / hunk 截断一律降级
 *   为 unparseable 标记（文件级 + 整体级），交给 UI 渲染错误态。
 * - 行切分只按 `\n`，不剥行尾 `\r`——git 把 CRLF 源文件的 CR 当内容原样输出，
 *   剥掉会篡改内容。
 * - `DiffLineKind` 保留 `'meta'`：当前解析器不在 hunk 内产出 meta 行（文件头
 *   信息走 ParsedDiffFile 的字段承载），为 UI 侧后续混排头部行预留。
 */

export type DiffLineKind = 'context' | 'add' | 'del' | 'meta';

export interface DiffLine {
  kind: DiffLineKind;
  /** 行内容（不含 +/-/空格 前缀符；context 空行为空串） */
  text: string;
  oldLineNo: number | null;
  newLineNo: number | null;
  /** 紧随的 `\ No newline at end of file` 标记（挂前一行） */
  noNewline: boolean;
}

export interface DiffHunk {
  /** 原始 @@ 行全文（UI 降级展示用） */
  header: string;
  /** @@ ... @@ 之后的 hunk 上下文标题（如 `def beta():`），无则 null */
  section: string | null;
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  lines: DiffLine[];
}

export interface ParsedDiffFile {
  /** 新路径（b 侧）；rename 时为 rename to */
  path: string;
  /** rename 时的原路径，其余 null（对齐契约 DiffFileSummary.old_path） */
  oldPath: string | null;
  change: 'added' | 'modified' | 'deleted' | 'renamed';
  binary: boolean;
  /** 仅 mode 变化（old mode/new mode 头、无 hunk）的合法无内容形态 */
  isModeChangeOnly: boolean;
  hunks: DiffHunk[];
  /** 该文件段解析失败（畸形 @@ / hunk 截断 / 头后无内容），UI 渲染错误行 */
  unparseable: boolean;
}

export interface ParsedDiff {
  files: ParsedDiffFile[];
  /** 整体无法产出任何可用内容（空 patch / 全部文件段失败） */
  unparseable: boolean;
}

/** hunk 头解析产物（不含行列表） */
interface HunkHead {
  header: string;
  section: string | null;
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
}

/** hunk 头：@@ -a[,b] +c[,d] @@ [section]；count 省略时按 unified diff 规范取 1 */
const HUNK_HEADER_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

/** 从 `diff --git a/<p1> b/<p2>` 行提取双侧路径（binary 无 ---/+++ 时的降级来源）。
 * 取最后一个 ` b/` 作分隔——老路径自身含空格时仍能对齐（启发式，注释即契约）。 */
function splitGitHeaderLine(line: string): { oldPath: string; newPath: string } | null {
  const body = line.slice('diff --git '.length);
  if (!body.startsWith('a/')) return null;
  const sep = body.lastIndexOf(' b/');
  if (sep < 2) return null;
  return { oldPath: body.slice(2, sep), newPath: body.slice(sep + 3) };
}

/** `--- a/x` / `+++ b/y` / `--- /dev/null` → 路径或 null；带时间戳后缀时截断 */
function stripSidePath(line: string): string | null {
  const rest = line.slice(4); // 跳过 `--- ` / `+++ `
  if (rest.startsWith('/dev/null')) return null;
  const tab = rest.indexOf('\t');
  const raw = tab >= 0 ? rest.slice(0, tab) : rest;
  if (raw.startsWith('a/') || raw.startsWith('b/')) return raw.slice(2);
  return raw; // 无前缀（个别工具输出）时原样保留
}

function parseHunkHeader(line: string): HunkHead | null {
  const m = HUNK_HEADER_RE.exec(line);
  if (m === null) return null;
  const at = line.indexOf('@@', 2); // 第二个 @@ 之后的剩余即 section
  const section = at >= 0 && at + 2 < line.length ? line.slice(at + 2).replace(/^ /, '') : null;
  return {
    header: line,
    section: section !== '' ? section : null,
    oldStart: Number(m[1]),
    oldLines: m[2] !== undefined ? Number(m[2]) : 1,
    newStart: Number(m[3]),
    newLines: m[4] !== undefined ? Number(m[4]) : 1,
  };
}

/** 消费一个 hunk 的 hunk 体：按声明行数计数（\ 标记不计数）。
 * 返回 null 表示 hunk 截断/出现非法行 → 文件降级。 */
function parseHunkBody(lines: readonly string[], start: number, head: HunkHead): { hunk: DiffHunk; next: number } | null {
  const hunk: DiffHunk = { ...head, lines: [] };
  let oldSeen = 0;
  let newSeen = 0;
  let i = start;
  while (oldSeen < hunk.oldLines || newSeen < hunk.newLines) {
    const raw = lines[i];
    if (raw === undefined) return null; // hunk 截断
    if (raw.startsWith('\\')) {
      // \ No newline at end of file：标记前一行，不吞行不推进（易错点 1）
      const prev = hunk.lines.at(-1);
      if (prev !== undefined) prev.noNewline = true;
      i += 1;
      continue;
    }
    let line: DiffLine;
    if (raw.startsWith('+')) {
      line = { kind: 'add', text: raw.slice(1), oldLineNo: null, newLineNo: hunk.newStart + newSeen, noNewline: false };
      newSeen += 1;
    } else if (raw.startsWith('-')) {
      line = { kind: 'del', text: raw.slice(1), oldLineNo: hunk.oldStart + oldSeen, newLineNo: null, noNewline: false };
      oldSeen += 1;
    } else if (raw.startsWith(' ') || raw === '') {
      // 空行容忍为 context（git 输出是前导空格，个别工具会丢）
      line = {
        kind: 'context',
        text: raw.startsWith(' ') ? raw.slice(1) : '',
        oldLineNo: hunk.oldStart + oldSeen,
        newLineNo: hunk.newStart + newSeen,
        noNewline: false,
      };
      oldSeen += 1;
      newSeen += 1;
    } else {
      return null; // 计数未满却出现非法行 → 畸形，降级
    }
    hunk.lines.push(line);
    i += 1;
  }
  // 计数已满后回看紧跟的 \ No newline 标记（易错点：标记可能出现在计数满的
  // 最后一行之后——不回看就会漏挂/错位，典型于 context 行后三态）
  const nextRaw = lines[i];
  if (nextRaw !== undefined && nextRaw.startsWith('\\')) {
    const prev = hunk.lines.at(-1);
    if (prev !== undefined) prev.noNewline = true;
    i += 1;
  }
  return { hunk, next: i };
}

export function parseDiff(patch: string): ParsedDiff {
  if (patch.trim() === '') return { files: [], unparseable: true };

  const lines = patch.split('\n');
  // 去掉末尾因结尾换行产生的空串（split 伪影，非内容）
  if (lines.length > 0 && lines.at(-1) === '') lines.pop();

  const files: ParsedDiffFile[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (line === undefined) break;
    if (!line.startsWith('diff --git ')) {
      i += 1; // 容忍前导杂音
      continue;
    }

    // ---- 开新文件段 ----
    // oldPath 对齐契约 DiffFileSummary.old_path 语义：仅 rename 时非 null。
    // `--- a/` 与 diff --git 的 a 侧对 modified 恒等于新路径，零信息量，
    // 只作 path 提取的降级来源，不写入 oldPath。
    const headerPaths = splitGitHeaderLine(line);
    const file: ParsedDiffFile = {
      path: headerPaths?.newPath ?? '',
      oldPath: null,
      change: 'modified',
      binary: false,
      isModeChangeOnly: false,
      hunks: [],
      unparseable: false,
    };
    let isAdded = false;
    let isDeleted = false;
    let sawRename = false;
    let renameFrom: string | null = null;
    let sawModeChange = false;
    i += 1;

    // ---- 文件头区：消费直到遇到 @@ / 下一 diff --git / EOF ----
    while (i < lines.length) {
      const h = lines[i];
      if (h === undefined) break;
      if (h.startsWith('diff --git ')) break;
      if (h.startsWith('@@')) break;
      if (h.startsWith('new file mode ')) isAdded = true;
      else if (h.startsWith('deleted file mode ')) isDeleted = true;
      else if (h.startsWith('rename from ')) {
        renameFrom = h.slice('rename from '.length);
        sawRename = true;
      } else if (h.startsWith('rename to ')) {
        file.path = h.slice('rename to '.length);
        sawRename = true;
      } else if (h.startsWith('Binary files ') || h === 'GIT binary patch') {
        file.binary = true;
      } else if (h.startsWith('old mode ') || h.startsWith('new mode ')) {
        sawModeChange = true;
      } else if (h.startsWith('+++ ')) {
        const p = stripSidePath(h);
        if (p !== null && !sawRename) file.path = p;
      } else if (h.startsWith('--- ')) {
        // b 侧缺失时的兜底（真实 git 恒有 b 侧，防御个别工具输出）
        const p = stripSidePath(h);
        if (p !== null && !sawRename && file.path === '') file.path = p;
      }
      // index / similarity / dissimilarity / copy from/to 等头行：影响判定或忽略，均不消费行号
      i += 1;
    }

    file.change = isAdded ? 'added' : isDeleted ? 'deleted' : sawRename ? 'renamed' : 'modified';
    file.oldPath = sawRename ? renameFrom : null;

    // ---- hunk 区：按声明行数消费，直到下一文件头 ----
    while (i < lines.length) {
      const h = lines[i];
      if (h === undefined) break;
      if (h.startsWith('diff --git ')) break;
      if (!h.startsWith('@@')) {
        i += 1; // hunk 间杂音行容忍跳过
        continue;
      }
      const head = parseHunkHeader(h);
      if (head === null) {
        // 畸形 @@：文件尚无任何完整 hunk 时归属本文件（真损坏）；已有 hunk
        // 后出现视为孤儿杂音（完整文件段之后的尾巴），不拖累好文件
        if (file.hunks.length === 0) file.unparseable = true;
        break;
      }
      const body = parseHunkBody(lines, i + 1, head);
      if (body === null) {
        file.unparseable = true;
        i += 1;
        while (i < lines.length) {
          const l = lines[i];
          if (l === undefined || l.startsWith('diff --git ')) break;
          i += 1;
        }
        break;
      }
      file.hunks.push(body.hunk);
      i = body.next;
    }

    // ---- 文件段收尾判定 ----
    if (!file.binary && file.hunks.length === 0 && !sawModeChange) {
      // 有头无内容（非 binary 非 mode-only）：git 不会产出的可疑形态，降级
      file.unparseable = true;
    }
    if (sawModeChange && file.hunks.length === 0 && !file.binary) {
      file.isModeChangeOnly = true;
    }
    files.push(file);
  }

  const unparseable = files.length === 0 || files.every((f) => f.unparseable);
  return { files, unparseable };
}
