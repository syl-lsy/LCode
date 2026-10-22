/**
 * diffParse 单测（M3 Phase 4 diff 审查地基）。
 *
 * 样本全部来自真实 `git diff` 输出（本机临时仓库 core.quotepath=false 下生成后
 * 原样拷贝，见各 const 命名），不凭想象编 diff 文本。重点钉死两处易错点：
 * 1. `\ No newline at end of file` 出现在 del/add/context 三种行后——只标记
 *    前一行、不吞行、不推进任何行号；
 * 2. 行号推进：context 双推进 / add 只推进 new / del 只推进 old。
 */
import { describe, expect, it } from 'vitest';

import { parseDiff } from './diffParse';

// ---------------------------------------------------------------- 真实样本

/** 纯修改两 hunk（真实 git diff；第二 hunk 带尾部 section「def beta():」） */
const MODIFY_TWO_HUNKS = `diff --git a/app.py b/app.py
index 64d908b..6912166 100644
--- a/app.py
+++ b/app.py
@@ -1,8 +1,9 @@
 import os
+import sys


 def alpha():
-    return 1
+    return 10


 def beta():
@@ -10,4 +11,4 @@ def beta():


 def gamma():
-    return 3
+    return 33
`;

/** 新增文件（git add -N 口径，AC-007-1） */
const ADDED_FILE = `diff --git a/newfile.ts b/newfile.ts
new file mode 100644
index 0000000..d5a09df
--- /dev/null
+++ b/newfile.ts
@@ -0,0 +1 @@
+brand new
`;

/** 删除文件 */
const DELETED_FILE = `diff --git a/old.txt b/old.txt
deleted file mode 100644
index 14916dc..0000000
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-old content line1
-old content line2
`;

/** 改名 + 小改（similarity index 57% + rename from/to，真实 -M 输出） */
const RENAMED_SIMILARITY = `diff --git a/orig.txt b/renamed-orig.txt
similarity index 57%
rename from orig.txt
rename to renamed-orig.txt
index a4e0ef3..c521d76 100644
--- a/orig.txt
+++ b/renamed-orig.txt
@@ -1,3 +1,3 @@
 same line one
-same line two
+same line two CHANGED
 same line three
`;

/** no-newline 在新增行后（真实 git diff：新侧末行无换行） */
const NO_NEWLINE_NEW_SIDE = `diff --git a/nl.txt b/nl.txt
index 1e9620a..e582a33 100644
--- a/nl.txt
+++ b/nl.txt
@@ -1,2 +1,2 @@
-keep a
-keep b with newline now
+x
+y no trailing newline
\\ No newline at end of file
`;

/** no-newline 在删除行后（真实 git diff：旧侧末行无换行） */
const NO_NEWLINE_OLD_SIDE = `diff --git a/onl.txt b/onl.txt
index 832f7c0..4198eb9 100644
--- a/onl.txt
+++ b/onl.txt
@@ -1,2 +1,2 @@
 p1
-p2 no trailing
\\ No newline at end of file
+p2 now has newline
`;

/** no-newline 在 context 行后（真实 git diff：末行两侧都无换行且未改动） */
const NO_NEWLINE_CONTEXT = `diff --git a/ctxnl.txt b/ctxnl.txt
index 33c47b5..2742ca3 100644
--- a/ctxnl.txt
+++ b/ctxnl.txt
@@ -1,3 +1,3 @@
-l1
+l1 CHANGED
 l2
 final ctx
\\ No newline at end of file
`;

/** 二进制文件（真实 git diff：无 ---/+++ 行，路径只能从 diff --git 头取） */
const BINARY_FILE = `diff --git a/blob.bin b/blob.bin
new file mode 100644
index 0000000..eaf36c1
Binary files /dev/null and b/blob.bin differ
`;

/** 仅 mode 变化（真实 git diff：有头无 hunk 的合法形态） */
const MODE_CHANGE_ONLY = `diff --git a/moved.md b/moved.md
old mode 100644
new mode 100755
`;

/** 中文路径与内容（core.quotepath=false，UTF-8 直出非转义） */
const CHINESE_PATH = `diff --git a/中文文件.md b/中文文件.md
index 332168a..8542909 100644
--- a/中文文件.md
+++ b/中文文件.md
@@ -1,2 +1,2 @@
 内容一
-内容二
+内容二改
`;

// ---------------------------------------------------------------- 用例
// noUncheckedIndexedAccess 下测试内索引访问统一用 `!`（样本为常量，越界即用例自身 bug）

describe('parseDiff · 文件级 change 判定', () => {
  it('纯修改：change=modified，路径取 +++ b/ 侧', () => {
    const r = parseDiff(MODIFY_TWO_HUNKS);
    expect(r.unparseable).toBe(false);
    expect(r.files).toHaveLength(1);
    const f = r.files[0]!;
    expect(f.change).toBe('modified');
    expect(f.path).toBe('app.py');
    expect(f.oldPath).toBeNull();
    expect(f.binary).toBe(false);
  });

  it('新增文件：change=added，---/dev/null，hunk oldStart=0', () => {
    const f = parseDiff(ADDED_FILE).files[0]!;
    expect(f.change).toBe('added');
    expect(f.path).toBe('newfile.ts');
    const h = f.hunks[0]!;
    expect(h.oldStart).toBe(0);
    expect(h.oldLines).toBe(0);
    expect(h.newStart).toBe(1);
    expect(h.lines.map((l) => l.text)).toEqual(['brand new']);
  });

  it('删除文件：change=deleted，newLines=0，全部行 kind=del', () => {
    const f = parseDiff(DELETED_FILE).files[0]!;
    expect(f.change).toBe('deleted');
    const h = f.hunks[0]!;
    expect(h.newLines).toBe(0);
    expect(h.lines.every((l) => l.kind === 'del')).toBe(true);
    expect(h.lines.map((l) => l.oldLineNo)).toEqual([1, 2]);
    expect(h.lines.map((l) => l.newLineNo)).toEqual([null, null]);
  });

  it('改名：change=renamed，oldPath 取 rename from', () => {
    const f = parseDiff(RENAMED_SIMILARITY).files[0]!;
    expect(f.change).toBe('renamed');
    expect(f.path).toBe('renamed-orig.txt');
    expect(f.oldPath).toBe('orig.txt');
    expect(f.hunks).toHaveLength(1);
  });

  it('二进制：binary=true 无 hunk，路径从 diff --git 头降级解析', () => {
    const f = parseDiff(BINARY_FILE).files[0]!;
    expect(f.binary).toBe(true);
    expect(f.change).toBe('added'); // new file mode 头仍在
    expect(f.path).toBe('blob.bin');
    expect(f.hunks).toHaveLength(0);
  });

  it('仅 mode 变化：无 hunk 合法，isModeChangeOnly=true 不算 unparseable', () => {
    const r = parseDiff(MODE_CHANGE_ONLY);
    expect(r.unparseable).toBe(false);
    const f = r.files[0]!;
    expect(f.isModeChangeOnly).toBe(true);
    expect(f.unparseable).toBe(false);
    expect(f.hunks).toHaveLength(0);
  });
});

describe('parseDiff · hunk 解析与行号推进', () => {
  it('两 hunk 文件：hunk 边界、section 提取、行号连续性', () => {
    const hunks = parseDiff(MODIFY_TWO_HUNKS).files[0]!.hunks;
    expect(hunks).toHaveLength(2);

    const h0 = hunks[0]!;
    expect(h0.oldStart).toBe(1);
    expect(h0.oldLines).toBe(8);
    expect(h0.newStart).toBe(1);
    expect(h0.newLines).toBe(9);
    expect(h0.section).toBeNull();

    // 第二 hunk 的 section 标题（@@ ... @@ def beta():）
    const h1h = hunks[1]!;
    expect(h1h.header.startsWith('@@ -10,4 +11,4 @@')).toBe(true);
    expect(h1h.section).toBe('def beta():');

    // hunk1 行号推进：+import sys 只动 new；-    return 1 只动 old
    const l1 = h0.lines;
    expect(l1[0]).toMatchObject({ kind: 'context', text: 'import os', oldLineNo: 1, newLineNo: 1 });
    expect(l1[1]).toMatchObject({ kind: 'add', text: 'import sys', oldLineNo: null, newLineNo: 2 });
    // 空行是 context（text 为空串），双推进
    expect(l1[2]).toMatchObject({ kind: 'context', text: '', oldLineNo: 2, newLineNo: 3 });
    expect(l1[5]).toMatchObject({ kind: 'del', text: '    return 1', oldLineNo: 5, newLineNo: null });
    expect(l1[6]).toMatchObject({ kind: 'add', text: '    return 10', oldLineNo: null, newLineNo: 6 });

    // hunk2 起点接续：old=10 / new=11
    const l2 = h1h.lines;
    expect(l2[0]).toMatchObject({ kind: 'context', oldLineNo: 10, newLineNo: 11 });
    expect(l2[3]).toMatchObject({ kind: 'del', text: '    return 3', oldLineNo: 13, newLineNo: null });
    expect(l2[4]).toMatchObject({ kind: 'add', text: '    return 33', oldLineNo: null, newLineNo: 14 });
  });

  it('hunk 头省略 count（@@ -1 +1 @@ 形态）按 1 兜底', () => {
    const r = parseDiff(`diff --git a/one.txt b/one.txt
index 1111111..2222222 100644
--- a/one.txt
+++ b/one.txt
@@ -1 +1 @@
-only
+only changed
`);
    const h = r.files[0]!.hunks[0]!;
    expect(h.oldLines).toBe(1);
    expect(h.newLines).toBe(1);
    expect(h.lines).toHaveLength(2);
    expect(r.unparseable).toBe(false);
  });
});

describe('parseDiff · \\ No newline at end of file 三态', () => {
  it('(a) 删除行后：标记挂前一行，不吞行不错位，后继行照常解析', () => {
    const lines = parseDiff(NO_NEWLINE_OLD_SIDE).files[0]!.hunks[0]!.lines;
    expect(lines).toHaveLength(3); // p1 / -p2 no trailing / +p2 now has newline
    expect(lines[0]).toMatchObject({ kind: 'context', text: 'p1', oldLineNo: 1, newLineNo: 1, noNewline: false });
    expect(lines[1]).toMatchObject({ kind: 'del', text: 'p2 no trailing', oldLineNo: 2, noNewline: true });
    // 标记不吞行：del 行之后 add 行照常出现且行号正确
    expect(lines[2]).toMatchObject({ kind: 'add', text: 'p2 now has newline', newLineNo: 2, noNewline: false });
  });

  it('(b) 新增行后（hunk 末行）：标记挂最后一行，行数不少', () => {
    const lines = parseDiff(NO_NEWLINE_NEW_SIDE).files[0]!.hunks[0]!.lines;
    expect(lines).toHaveLength(4);
    expect(lines[0]).toMatchObject({ kind: 'del', text: 'keep a', oldLineNo: 1 });
    expect(lines[1]).toMatchObject({ kind: 'del', text: 'keep b with newline now', oldLineNo: 2 });
    expect(lines[2]).toMatchObject({ kind: 'add', text: 'x', newLineNo: 1 });
    expect(lines[3]).toMatchObject({ kind: 'add', text: 'y no trailing newline', newLineNo: 2, noNewline: true });
  });

  it('(c) context 行后：标记挂 context 行，且 old/new 双侧末行同时无换行', () => {
    const lines = parseDiff(NO_NEWLINE_CONTEXT).files[0]!.hunks[0]!.lines;
    expect(lines).toHaveLength(4);
    expect(lines[0]).toMatchObject({ kind: 'del', text: 'l1', oldLineNo: 1 });
    expect(lines[1]).toMatchObject({ kind: 'add', text: 'l1 CHANGED', newLineNo: 1 });
    expect(lines[2]).toMatchObject({ kind: 'context', text: 'l2', oldLineNo: 2, newLineNo: 2, noNewline: false });
    // 末行是 context 且带 noNewline；标记后无多余行
    expect(lines[3]).toMatchObject({ kind: 'context', text: 'final ctx', oldLineNo: 3, newLineNo: 3, noNewline: true });
  });
});

describe('parseDiff · 多文件与边界', () => {
  it('多文件 patch（add + del + modify 串接）拆出三个文件段', () => {
    const r = parseDiff(`${ADDED_FILE}${DELETED_FILE}${MODIFY_TWO_HUNKS}`);
    expect(r.files).toHaveLength(3);
    expect(r.files.map((f) => f.change)).toEqual(['added', 'deleted', 'modified']);
    expect(r.unparseable).toBe(false);
  });

  it('空 patch：files 空、unparseable=true，不抛异常', () => {
    const r = parseDiff('');
    expect(r.files).toEqual([]);
    expect(r.unparseable).toBe(true);
  });

  it('只有文件头没有 hunk（非 binary/mode）：该文件降级 unparseable', () => {
    const r = parseDiff(`diff --git a/empty.txt b/empty.txt
index 1111111..2222222 100644
--- a/empty.txt
+++ b/empty.txt
`);
    expect(r.files).toHaveLength(1);
    expect(r.files[0]!.unparseable).toBe(true);
    expect(r.unparseable).toBe(true);
  });

  it('畸形 @@ 头：不 throw，整体降级 unparseable', () => {
    const r = parseDiff(`diff --git a/bad.txt b/bad.txt
index 1111111..2222222 100644
--- a/bad.txt
+++ b/bad.txt
@@ 畸形内容 不是 hunk 头
 some line
`);
    expect(r.files).toHaveLength(1);
    expect(r.files[0]!.unparseable).toBe(true);
    expect(r.unparseable).toBe(true);
  });

  it('混合：一个好文件 + 一个坏文件（各有 diff --git 头）→ 好文件可用，整体不算全废', () => {
    const r = parseDiff(`${ADDED_FILE}diff --git a/broken.txt b/broken.txt
index 1111111..2222222 100644
--- a/broken.txt
+++ b/broken.txt
@@ 完全畸形 @@
垃圾内容
`);
    expect(r.files).toHaveLength(2);
    expect(r.files[0]!.unparseable).toBe(false);
    expect(r.files[1]!.unparseable).toBe(true);
    expect(r.unparseable).toBe(false); // 仍有可用内容
  });

  it('完整文件段之后的孤儿 @@ 杂音：不捏造文件条目、不拖累好文件', () => {
    const r = parseDiff(`${ADDED_FILE}@@ 完全畸形 @@\n垃圾内容\n`);
    expect(r.files).toHaveLength(1);
    expect(r.files[0]!.unparseable).toBe(false);
    expect(r.files[0]!.hunks).toHaveLength(1);
  });
});

describe('parseDiff · 非 ASCII 与鲁棒性', () => {
  it('中文路径与内容按 UTF-8 原样保留（core.quotepath=false 口径）', () => {
    const f = parseDiff(CHINESE_PATH).files[0]!;
    expect(f.path).toBe('中文文件.md');
    expect(f.hunks[0]!.lines.map((l) => `${l.kind}:${l.text}`)).toEqual([
      'context:内容一',
      'del:内容二',
      'add:内容二改',
    ]);
  });

  it('git 前导杂音行（非 diff --git 开头）被容忍跳过', () => {
    const f = parseDiff(`warning: some noise line\n${ADDED_FILE}`).files[0]!;
    expect(f.path).toBe('newfile.ts');
  });
});
