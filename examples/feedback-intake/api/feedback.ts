import { boundedBody, response, runWorker } from "../lib/worker"

export async function POST(request: Request): Promise<Response> {
  if (request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
    return response({ error: "feedback_rejected" }, 415)
  }
  try {
    const body = await boundedBody(request)
    if (body === null) return response({ error: "feedback_rejected" }, 413)
    const result = await runWorker(["submit"], body)
    return response({ accepted: result.status === 202 }, result.status)
  } catch { return response({ error: "feedback_rejected" }, 400) }
}
