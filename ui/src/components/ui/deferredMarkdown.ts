import { lazyPage } from '../../lib/lazyPage';

/**
 * The Markdown renderer for surfaces off the first screen that only sometimes
 * draw it (an editor's preview tab, a file preview). The chat page imports
 * `Markdown` directly; this keeps the parser and its plugins out of the entry.
 */
export const DeferredMarkdown = lazyPage(() => import('./markdown').then((m) => m.Markdown));
