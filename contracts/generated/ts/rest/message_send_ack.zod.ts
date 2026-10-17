import { z } from "zod";
export const MessageSendAckSchema = z.object({ "event_seq": z.number().int().gte(0), "message_id": z.string(), "run_id": z.string() }).strict()
