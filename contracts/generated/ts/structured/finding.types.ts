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
