import { z } from "zod";
export const ApprovalDecisionAckSchema = z.object({ "approval": z.any(), "run_id": z.union([z.string(), z.null()]).default(null) }).strict()
