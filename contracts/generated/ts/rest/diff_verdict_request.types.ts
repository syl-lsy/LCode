export type Feedback = string | null;
export type Verdict = "accepted" | "rejected";

export interface DiffVerdictRequest {
  feedback?: Feedback;
  verdict: Verdict;
}
