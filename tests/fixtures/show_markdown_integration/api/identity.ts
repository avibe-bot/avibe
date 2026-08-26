export function GET(request: Request) {
  return Response.json({
    authorization: request.headers.get("authorization"),
    cookie: request.headers.get("cookie"),
    csrf: request.headers.get("x-vibe-csrf-token"),
  })
}
