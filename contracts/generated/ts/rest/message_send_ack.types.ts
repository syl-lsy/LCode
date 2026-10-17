export type EventSeq = number;
export type MessageId = string;
export type RunId = string;

export interface MessageSendAck {
  event_seq: EventSeq;
  message_id: MessageId;
  run_id: RunId;
}
