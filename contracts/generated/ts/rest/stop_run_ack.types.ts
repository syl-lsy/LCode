export type RunId = string;
export type Status = "stopped";

export interface StopRunAck {
  run_id: RunId;
  status: Status;
}
