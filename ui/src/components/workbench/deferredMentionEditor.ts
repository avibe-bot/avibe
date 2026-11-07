import { lazyPage } from '../../lib/lazyPage';

/**
 * The chat composer's rich editor (Lexical and its mention typeahead), kept out
 * of the entry: only the chat page's composer draws it, and the chat page's own
 * loader (routePages.ts) fetches it, so it is ready before that composer mounts.
 * The home and the new-session sheet use the plain textarea.
 */
export const DeferredMentionEditor = lazyPage(() => import('./MentionEditor').then((m) => m.MentionEditor));
