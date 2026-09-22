import { response, runWorker, UUID_V4 } from "../lib/worker"

export async function GET(request: Request): Promise<Response> {
  const query = new URL(request.url).searchParams
  const id = query.get("request_id") || ""
  if ([...query.keys()].join() !== "request_id" || !UUID_V4.test(id)) return response({}, 404)
  const result = await runWorker(["status", id])
  if (result.status !== 200) return response({}, result.status)
  const receipt = result.receipt
  if (receipt?.schema_version !== 1 || receipt.request_id !== id || !["pending", "unknown", "failed", "created"].includes(receipt.state)) {
    return response({}, 503)
  }
  // Explicit public projection; the ledger and worker may hold private evidence.
  const publicReceipt: Record<string, unknown> = { schema_version: 1, request_id: id, state: receipt.state }
  if (receipt.state === "created") {
    if (!Number.isSafeInteger(receipt.issue_number) || receipt.issue_number <= 0 ||
        receipt.issue_url !== `https://github.com/avibe-bot/avibe/issues/${receipt.issue_number}`) return response({}, 503)
    publicReceipt.issue_number = receipt.issue_number
    publicReceipt.issue_url = receipt.issue_url
  }
  return response(publicReceipt, 200)
}
