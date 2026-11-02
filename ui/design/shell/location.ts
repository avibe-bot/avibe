// Some components read a deep link straight from the document URL (`?mcp=github`) and then clear it.
// A frame's `search` fixture joins its document URL before anything renders, and the frame's own URL
// comes back once the frame has mounted, so a reload still draws the same frame.

let frameUrl: string | null = null;

export const designLocation = {
  enter(search?: string) {
    frameUrl = window.location.href;
    if (!search) return;
    const url = new URL(frameUrl);
    for (const [key, value] of new URLSearchParams(search)) url.searchParams.append(key, value);
    window.history.replaceState(window.history.state, '', url);
  },
  restore() {
    if (frameUrl !== null && window.location.href !== frameUrl) window.history.replaceState(window.history.state, '', frameUrl);
  },
};
