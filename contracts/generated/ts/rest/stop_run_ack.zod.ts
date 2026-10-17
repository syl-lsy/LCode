import { z } from "zod";
export const StopRunAckSchema = z.object({ "run_id": z.string(), "status": z.literal("stopped") }).strict()
