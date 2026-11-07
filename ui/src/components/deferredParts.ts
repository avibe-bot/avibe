import { DeferredMarkdown } from './ui/deferredMarkdown';
import { DeferredInboxPreview } from './workbench/InboxHoverPopover';
import { DeferredMentionEditor } from './workbench/deferredMentionEditor';

/**
 * The parts of shared components that load on demand (docs/plans/web-ui-first-load.md):
 * the chat composer's editor, the Inbox peek's preview, and the Markdown renderer. In the
 * app, the chat page and the first-screen prefetch (src/routePages.ts) load them before
 * they are drawn. Browser fixtures and the design canvas render those components outside
 * the app's routes, so they load these first.
 */
export function loadDeferredParts(): Promise<void> {
  return Promise.all([
    DeferredMentionEditor.preload(),
    DeferredInboxPreview.preload(),
    DeferredMarkdown.preload(),
  ]).then(() => undefined);
}
