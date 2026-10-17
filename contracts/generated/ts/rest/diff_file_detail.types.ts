export type Additions = number;
export type Binary = boolean;
export type BlobRef = string | null;
export type Change = "added" | "modified" | "deleted" | "renamed";
export type Deletions = number;
export type Patch = string | null;
export type Path = string;

export interface DiffFileDetail {
  additions: Additions;
  binary?: Binary;
  blob_ref?: BlobRef;
  change: Change;
  deletions: Deletions;
  patch?: Patch;
  path: Path;
}
