export type FilesReviewed = number;
export type FilesSkipped = string[];
export type Notes = string | null;

/**
 * 审查覆盖面（扇出场景下部分失败要如实呈现，AC-009-4）。
 */
export interface Coverage {
  files_reviewed?: FilesReviewed;
  files_skipped?: FilesSkipped;
  notes?: Notes;
}
