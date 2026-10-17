export type FilesReviewed = number;
export type FilesSkipped = string[];
export type Notes = string | null;
export type Category = "security" | "correctness" | "performance" | "maintainability" | "style";
export type Confidence = "high" | "medium" | "low";
export type Description = string;
export type EndLine = number | null;
export type Evidence = string | null;
export type File = string;
export type Line = number | null;
export type Severity = "critical" | "high" | "medium" | "low" | "info";
export type Suggestion = string | null;
export type Title = string;
export type Findings = Finding[];
export type SchemaVersion = "1";
export type Summary = string;

/**
 * reviewer 子智能体的结构化返回（汇总端机器可拼，AC-011-1）。
 */
export interface ReviewerFindings {
  coverage?: Coverage | null;
  findings?: Findings;
  schema_version?: SchemaVersion;
  summary: Summary;
}
/**
 * 审查覆盖面（扇出场景下部分失败要如实呈现，AC-009-4）。
 */
export interface Coverage {
  files_reviewed?: FilesReviewed;
  files_skipped?: FilesSkipped;
  notes?: Notes;
}
/**
 * 单条发现。line/end_line 为 1 起始行号；文件级问题（无行号）两者皆 null。
 */
export interface Finding {
  category: Category;
  confidence?: Confidence;
  description: Description;
  end_line?: EndLine;
  evidence?: Evidence;
  file: File;
  line?: Line;
  severity: Severity;
  suggestion?: Suggestion;
  title: Title;
}
