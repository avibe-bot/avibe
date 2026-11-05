// Where a motion demo reaches into the shipped pages it stages (docs/plans/web-ui-motion.md): selectors by
// the hooks the pages already render (test ids, data attributes, icons), so they hold in either language.
/** Inside one stacked state. */
export const inLayer = (layer: string, selector = '') => `[data-motion-layer="${layer}"] ${selector}`.trim();
/** The transcript's scrolling viewport, and the column of rows inside it. */
export const TRANSCRIPT = '[data-testid="chat-transcript"]';
export const TRANSCRIPT_ROWS = `${TRANSCRIPT} > div`;
/** One message row by id. */
export const row = (id: string) => `[data-message-id="${id}"]`;
/** The agent-working bubble: the transcript's tail while a turn runs. */
export const WORKING = `${TRANSCRIPT_ROWS} > :last-child`;
/** The chat header. */
export const HEADER = '[data-chat-header]';
/** The composer: the chat surface's last child, below the transcript. */
export const COMPOSER = `div:has(> div > ${TRANSCRIPT}) > :last-child`;
/** What is typed in the composer (the editor and its placeholder). */
export const TYPED = 'div:has(> [contenteditable])';
/** The composer's Send and Stop buttons, by their icons (so in either language). */
export const SEND = 'button:has(> .lucide-send)';
export const STOP = 'button:has(> .lucide-square)';


/** An Inbox card by its Session. */
export const card = (sessionId: string) => `[data-inbox-session-id="${sessionId}"]`;
