// A lazy module that failed to load, worded differently by Chromium, Firefox, and Safari, plus
// Vite's own message when a lazy chunk's CSS fails to preload. Retrying in place cannot recover
// it: React.lazy keeps the rejected import, and browsers keep a failed module fetch until the page
// reloads. The usual cause is a page left open across an Avibe update.
const CHUNK_LOAD_ERROR =
  /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS/i;

export function isChunkLoadError(error: unknown): boolean {
  return error instanceof Error && CHUNK_LOAD_ERROR.test(error.message);
}
