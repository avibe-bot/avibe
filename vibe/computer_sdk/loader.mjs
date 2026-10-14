let sdkUrl;

export function initialize(data) {
  sdkUrl = data?.sdkUrl;
  if (typeof sdkUrl !== "string" || !sdkUrl.startsWith("file:")) {
    throw new Error("invalid @avibe/computer module URL");
  }
}

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "@avibe/computer") {
    return { url: sdkUrl, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
